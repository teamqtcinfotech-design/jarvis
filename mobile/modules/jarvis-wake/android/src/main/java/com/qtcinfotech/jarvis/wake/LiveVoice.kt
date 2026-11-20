package com.qtcinfotech.jarvis.wake

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.os.SystemClock
import android.util.Base64
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.TimeUnit
import kotlin.math.sqrt

/**
 * The live voice line (see backend/core/livevoice.py): one open line from this phone to Grok's realtime voice model,
 * which is JARVIS's ears and mouth. The microphone streams up as the person talks; the model's voice streams down
 * and plays as it arrives. When the model fills a form (a tool call), the app is told and answers through
 * [toolResult] after the JARVIS server has done the work; the model then reads the server's line back.
 *
 *  - Opened with a short-lived token the server made (the API key never reaches the phone).
 *  - Barge-in: talking over JARVIS stops the voice at once (the model is told to cancel). To keep the phone's own
 *    voice from being heard as the person, the microphone is sent up while JARVIS speaks only when it is clearly
 *    louder than the echo.
 *  - Closes by itself after a quiet while (the line is billed per minute) and before the token runs out.
 *
 * Events go to the app through [listener] ("state", "heard", "said", "tool", "speaking", "usage", "error").
 */
object LiveVoice {
    private const val TAG = "JarvisLive"
    private const val FRAME_MS = 100
    private const val DEFAULT_RATE = 24000

    @Volatile var listener: ((String, JSONObject) -> Unit)? = null
    @Volatile var open = false
    @Volatile var speaking = false

    private var context: Context? = null
    private var client: OkHttpClient? = null
    @Volatile private var ws: WebSocket? = null
    @Volatile private var generation = 0
    private var rate = DEFAULT_RATE
    private var inRate = DEFAULT_RATE
    private var bargeIn = true
    private var bargeLevel = 1200
    private var idleMs = 45_000L
    private var maxMs = 14 * 60_000L
    private var greeting = ""

    // capture
    private var captureThread: Thread? = null
    @Volatile private var capturing = false
    @Volatile private var recorder: AudioRecord? = null
    // playback
    private val queue = ConcurrentLinkedQueue<ByteArray>()
    private var playThread: Thread? = null
    @Volatile private var track: AudioTrack? = null
    @Volatile private var playedUntil = 0L        // when the sound handed to the track will have been heard (a clock)
    @Volatile private var currentResponse = ""
    @Volatile private var cancelledResponse = ""
    @Volatile private var responseOpen = false    // the model is still producing the response now playing
    // the clock
    @Volatile private var startedAt = 0L
    @Volatile private var lastActivity = 0L
    @Volatile private var toolsPending = 0
    private var watchThread: Thread? = null
    private var heardText = ""

    private fun emit(event: String, data: JSONObject = JSONObject()) {
        try { listener?.invoke(event, data) } catch (_: Throwable) {}
    }

    private fun send(obj: JSONObject): Boolean = try { ws?.send(obj.toString()) ?: false } catch (_: Throwable) { false }

    // ---------------------------------------------------------------- the line
    /** Open the line. [config]: url, token, sessionJson (the session.update body as text), rate, bargeIn, bargeLevel,
     *  idleSeconds, maxSeconds, greeting. Returns false when it could not even start (no microphone permission…). */
    @Synchronized
    fun start(ctx: Context, config: JSONObject): Boolean {
        stop("restart", quiet = true)
        val app = ctx.applicationContext
        context = app
        val url = config.optString("url", "")
        val token = config.optString("token", "")
        val sessionJson = config.optString("sessionJson", "")
        if (url.isEmpty() || token.isEmpty() || sessionJson.isEmpty()) return false
        rate = config.optInt("rate", DEFAULT_RATE)
        bargeIn = config.optBoolean("bargeIn", true)
        bargeLevel = config.optInt("bargeLevel", 1200)
        idleMs = config.optLong("idleSeconds", 45) * 1000
        maxMs = config.optLong("maxSeconds", 14 * 60) * 1000
        greeting = config.optString("greeting", "")
        val gen = ++generation
        // The microphone first (its real rate goes into the session), before anything is sent.
        val rec = openRecorder() ?: return false
        recorder = rec
        // One voice at a time: whatever the phone was saying stops, and "Hey JARVIS" steps aside while the line is open.
        try { TtsEngine.stop() } catch (_: Throwable) {}
        try { GrokVoice.stop() } catch (_: Throwable) {}
        WakeBus.paused = true
        startedAt = SystemClock.elapsedRealtime()
        lastActivity = startedAt
        toolsPending = 0
        heardText = ""
        cancelledResponse = ""
        currentResponse = ""
        queue.clear()
        emit("state", JSONObject().put("state", "connecting"))
        val c = client ?: OkHttpClient.Builder().pingInterval(20, TimeUnit.SECONDS).connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.SECONDS).build().also { client = it }
        val session = try { JSONObject(sessionJson) } catch (_: Throwable) { return false }
        val request = Request.Builder().url(url).addHeader("Authorization", "Bearer $token").build()
        ws = c.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (gen != generation) return
                open = true
                Log.i(TAG, "line open")
                // The session: JARVIS's persona, the facts, the form. The microphone's real rate goes in (the server
                // reads whatever the phone can record).
                try {
                    val audio = session.optJSONObject("audio")
                    val input = audio?.optJSONObject("input")
                    input?.optJSONObject("format")?.put("rate", inRate)
                } catch (_: Throwable) {}
                webSocket.send(JSONObject().put("type", "session.update").put("session", session).toString())
                startPlayback(gen)
                startCapture(app, gen)
                startWatch(gen)
                emit("state", JSONObject().put("state", "open"))
                if (greeting.isNotBlank()) say(greeting)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                if (gen != generation) return
                try { handle(JSONObject(text)) } catch (e: Throwable) { Log.w(TAG, "bad message: ${e.message}") }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (gen != generation) return
                Log.w(TAG, "line failed: ${t.message}")
                emit("error", JSONObject().put("message", t.message ?: "connection failed").put("http", response?.code ?: 0))
                stop("failed")
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (gen != generation) return
                Log.i(TAG, "line closed: $code $reason")
                stop("closed")
            }
        })
        return true
    }

    @Synchronized
    fun stop(reason: String = "app", quiet: Boolean = false) {
        val wasOpen = open || ws != null
        generation++
        open = false
        capturing = false
        try { ws?.close(1000, reason) } catch (_: Throwable) {}
        ws = null
        queue.clear()
        stopTrack()
        val rec = recorder
        recorder = null
        if (rec != null && captureThread == null) {
            // Opened for a line that never got going: let the microphone go here (a running capture does it itself).
            try { rec.release() } catch (_: Throwable) {}
        }
        speaking = false
        WakeBus.paused = false
        captureThread = null
        playThread = null
        watchThread = null
        if (wasOpen && !quiet) {
            val seconds = ((SystemClock.elapsedRealtime() - startedAt) / 1000).toInt()
            emit("state", JSONObject().put("state", "closed").put("reason", reason).put("seconds", seconds))
        }
    }

    /** The JARVIS server's answer to a form: the model reads its "say" line back (or says what went wrong). */
    fun toolResult(callId: String, output: String) {
        toolsPending = maxOf(0, toolsPending - 1)
        lastActivity = SystemClock.elapsedRealtime()
        send(JSONObject().put("type", "conversation.item.create").put("item", JSONObject()
            .put("type", "function_call_output").put("call_id", callId).put("output", output)))
        send(JSONObject().put("type", "response.create"))
    }

    /** Make the line say [text] now, word for word (a greeting, a reminder that fired, a line the app built). */
    fun say(text: String) {
        if (text.isBlank()) return
        lastActivity = SystemClock.elapsedRealtime()
        send(JSONObject().put("type", "response.create").put("response", JSONObject()
            .put("instructions", "Say this to the user now, exactly as written and nothing else, in the script it is written in: $text")))
    }

    /** Stop the voice mid-sentence (the person tapped): the model drops the rest of that reply. */
    fun interrupt() {
        if (!speaking) return
        val stillProducing = responseOpen
        cutPlayback()
        if (stillProducing) send(JSONObject().put("type", "response.cancel"))
    }

    fun status(): JSONObject = JSONObject().put("open", open).put("speaking", speaking)
        .put("seconds", if (startedAt > 0) (SystemClock.elapsedRealtime() - startedAt) / 1000 else 0)

    // ---------------------------------------------------------------- what the server says
    private fun handle(msg: JSONObject) {
        when (val t = msg.optString("type")) {
            "error" -> {
                val err = msg.optJSONObject("error")
                val text = err?.optString("message")?.takeIf { it.isNotBlank() } ?: msg.toString().take(300)
                Log.w(TAG, "server error: $text")
                emit("error", JSONObject().put("message", text))
            }
            "input_audio_buffer.speech_started" -> {
                lastActivity = SystemClock.elapsedRealtime()
                if (speaking && bargeIn) {
                    // He talked over JARVIS: the voice stops at once and the model drops the rest of that reply
                    // (asked only while the model is still producing it; sound already here is simply dropped).
                    val stillProducing = responseOpen
                    cutPlayback()
                    if (stillProducing) send(JSONObject().put("type", "response.cancel"))
                    emit("barge", JSONObject())
                }
                emit("listening", JSONObject().put("on", true))
            }
            "input_audio_buffer.speech_stopped" -> {
                lastActivity = SystemClock.elapsedRealtime()
                emit("listening", JSONObject().put("on", false))
            }
            "conversation.item.input_audio_transcription.updated", "conversation.item.input_audio_transcription.completed" -> {
                val text = msg.optString("transcript", "")
                if (text.isNotBlank()) {
                    heardText = text
                    emit("heard", JSONObject().put("text", text).put("final", false))
                }
            }
            "response.created" -> {
                lastActivity = SystemClock.elapsedRealtime()
                responseOpen = true
                if (heardText.isNotBlank()) {
                    emit("heard", JSONObject().put("text", heardText).put("final", true))
                    heardText = ""
                }
            }
            "response.output_audio.delta" -> {
                val rid = msg.optString("response_id", "")
                if (rid.isNotEmpty() && rid == cancelledResponse) return
                if (rid != currentResponse) currentResponse = rid
                val delta = msg.optString("delta", "")
                if (delta.isEmpty()) return
                val bytes = try { Base64.decode(delta, Base64.DEFAULT) } catch (_: Throwable) { return }
                if (bytes.isNotEmpty()) queue.add(bytes)
            }
            "response.output_audio_transcript.done", "response.audio_transcript.done" -> {
                val rid = msg.optString("response_id", "")
                if (rid.isNotEmpty() && rid == cancelledResponse) return
                val text = msg.optString("transcript", "")
                if (text.isNotBlank()) emit("said", JSONObject().put("text", text))
            }
            "response.function_call_arguments.done" -> {
                toolsPending++
                lastActivity = SystemClock.elapsedRealtime()
                emit("tool", JSONObject().put("callId", msg.optString("call_id", "")).put("name", msg.optString("name", ""))
                    .put("arguments", msg.optString("arguments", "{}")))
            }
            "response.done" -> {
                responseOpen = false
                lastActivity = SystemClock.elapsedRealtime()
                val usage = msg.optJSONObject("response")?.optJSONObject("usage") ?: msg.optJSONObject("usage")
                val billed = usage?.optDouble("billable_audio_seconds", 0.0) ?: 0.0
                if (billed > 0) emit("usage", JSONObject().put("seconds", billed))
            }
            else -> if (t.isEmpty()) Log.d(TAG, "message without a type")
        }
    }

    // ---------------------------------------------------------------- the microphone
    private fun startCapture(ctx: Context, gen: Int) {
        capturing = true
        val rec0 = recorder ?: openRecorder()
        if (rec0 == null) {
            emit("error", JSONObject().put("message", "The microphone could not be opened."))
            stop("mic")
            return
        }
        recorder = rec0
        captureThread = Thread({
            val rec: AudioRecord = rec0
            val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            try {
                am.requestAudioFocus(null, AudioManager.STREAM_MUSIC, AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
                try { AutomaticGainControl.create(rec.audioSessionId)?.enabled = false } catch (_: Throwable) {}
                try { NoiseSuppressor.create(rec.audioSessionId)?.enabled = true } catch (_: Throwable) {}
                try { AcousticEchoCanceler.create(rec.audioSessionId)?.enabled = true } catch (_: Throwable) {}
                rec.startRecording()
                val frameBytes = inRate * 2 * FRAME_MS / 1000
                val pcm = ByteArray(frameBytes)
                val silence = ByteArray(frameBytes)
                var lastLevelAt = 0L
                while (capturing && gen == generation) {
                    var got = 0
                    while (got < frameBytes && capturing && gen == generation) {
                        val n = rec.read(pcm, got, frameBytes - got)
                        if (n <= 0) break
                        got += n
                    }
                    if (got < frameBytes) continue
                    val level = rms(pcm)
                    val now = SystemClock.elapsedRealtime()
                    if (now - lastLevelAt > 150) {
                        lastLevelAt = now
                        emit("level", JSONObject().put("level", level))
                    }
                    // While JARVIS speaks, only a voice clearly above the echo goes up (and only when barge-in is on);
                    // otherwise the server hears silence and never answers the phone's own voice.
                    val bytes = if (speaking && (!bargeIn || level < bargeLevel)) silence else pcm
                    val b64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
                    send(JSONObject().put("type", "input_audio_buffer.append").put("audio", b64))
                }
            } catch (e: Throwable) {
                Log.w(TAG, "capture failed: ${e.message}")
                emit("error", JSONObject().put("message", "Microphone: ${e.message ?: "failed"}"))
            } finally {
                try { rec.stop() } catch (_: Throwable) {}
                try { rec.release() } catch (_: Throwable) {}
                if (recorder === rec) recorder = null
                try { am.abandonAudioFocus(null) } catch (_: Throwable) {}
            }
        }, "jarvis-live-mic").also { it.start() }
    }

    /** The microphone at the line's rate, or at 16 kHz when the phone cannot record at 24 kHz (the session is told). */
    private fun openRecorder(): AudioRecord? {
        for (candidate in intArrayOf(rate, 16000)) {
            val minBuf = AudioRecord.getMinBufferSize(candidate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            if (minBuf <= 0) continue
            try {
                val rec = AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, candidate, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, candidate * 2))  // a second of room
                if (rec.state == AudioRecord.STATE_INITIALIZED) {
                    inRate = candidate
                    return rec
                }
                rec.release()
            } catch (_: Throwable) {
            }
        }
        return null
    }

    private fun rms(pcm: ByteArray): Int {
        var sum = 0.0
        var n = 0
        var i = 0
        while (i + 1 < pcm.size) {
            val s = ((pcm[i + 1].toInt() shl 8) or (pcm[i].toInt() and 0xFF)).toShort().toInt()
            sum += (s * s).toDouble()
            n++
            i += 2
        }
        return if (n == 0) 0 else sqrt(sum / n).toInt()
    }

    // ---------------------------------------------------------------- the voice
    private fun startPlayback(gen: Int) {
        playThread = Thread({
            var player: AudioTrack? = null
            try {
                val minBuf = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
                player = AudioTrack.Builder()
                    .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                    .setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(rate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                    .setBufferSizeInBytes(maxOf(minBuf, rate))  // half a second
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .apply { if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY) }
                    .build()
                track = player
                var started = false
                while (gen == generation) {
                    val chunk = queue.poll()
                    if (chunk != null) {
                        if (!speaking) {
                            speaking = true
                            emit("speaking", JSONObject().put("on", true))
                        }
                        if (!started) {
                            player.play()
                            started = true
                        }
                        val even = chunk.size - (chunk.size % 2)
                        if (even > 0) {
                            player.write(chunk, 0, even)
                            // The sound just handed over plays after whatever is still in the track: a clock of when it
                            // will have been heard (the head position is unreliable across a flush).
                            val ms = even * 1000L / (rate * 2)
                            playedUntil = maxOf(playedUntil, SystemClock.elapsedRealtime()) + ms
                        }
                        continue
                    }
                    if (speaking && !responseOpen && SystemClock.elapsedRealtime() >= playedUntil + 120) {
                        speaking = false
                        emit("speaking", JSONObject().put("on", false))
                    }
                    Thread.sleep(15)
                }
            } catch (e: Throwable) {
                Log.w(TAG, "playback failed: ${e.message}")
            } finally {
                track = null
                try { player?.pause(); player?.flush() } catch (_: Throwable) {}
                try { player?.release() } catch (_: Throwable) {}
            }
        }, "jarvis-live-voice").also { it.start() }
    }

    /** The rest of what JARVIS was saying is dropped (he talked over it, or tapped). */
    private fun cutPlayback() {
        cancelledResponse = currentResponse
        queue.clear()
        try {
            track?.pause()
            track?.flush()
            track?.play()
        } catch (_: Throwable) {}
        playedUntil = 0
        responseOpen = false
        if (speaking) {
            speaking = false
            emit("speaking", JSONObject().put("on", false))
        }
    }

    private fun stopTrack() {
        try { track?.pause(); track?.flush() } catch (_: Throwable) {}
    }

    // ---------------------------------------------------------------- the clock
    private fun startWatch(gen: Int) {
        watchThread = Thread({
            var lastPause = 0L
            while (gen == generation) {
                val now = SystemClock.elapsedRealtime()
                if (now - lastPause > 20_000) {
                    WakeBus.paused = true  // the pause expires by itself after a minute: keep it while the line is open
                    lastPause = now
                }
                if (now - startedAt > maxMs) {
                    stop("expired")
                    break
                }
                if (!speaking && toolsPending == 0 && now - lastActivity > idleMs) {
                    stop("idle")
                    break
                }
                Thread.sleep(500)
            }
        }, "jarvis-live-watch").also { it.start() }
    }
}
