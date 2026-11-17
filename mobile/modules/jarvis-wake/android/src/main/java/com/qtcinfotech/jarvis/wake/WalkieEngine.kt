package com.qtcinfotech.jarvis.wake

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import android.media.MediaRecorder
import android.os.Build
import android.os.SystemClock
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONArray
import org.json.JSONObject
import java.nio.ByteBuffer
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.TimeUnit
import kotlin.math.abs
import kotlin.math.sqrt

/**
 * The walkie-talkie on the phone (see backend/core/walkie.py for the relay it talks to).
 *
 *  - One line to the relay, kept open while the switch is on (reconnects by itself: 1, 2, 5, 10, 30 s).
 *  - Hold to talk: the microphone opens the moment the button goes down and the sound streams at once, in 20 ms
 *    pieces compressed by the phone's own AMR-WB encoder (the VoLTE codec, on every Android); the relay grants the
 *    floor while it is already flowing. Two pieces per message (40 ms).
 *  - Incoming: pieces are decoded and played straight away after a small cushion (120 ms, growing a little when
 *    the network jitters), on the speaker, even on a locked phone. Raw PCM is accepted too (the laptop test client).
 *  - A phone call takes the sound away (audio focus); the microphone is on only while the button is held.
 *
 * Events go to the app (JarvisWakeModule, "walkie") and, with the app closed, to the service for the incoming card.
 */
object WalkieEngine {
    private const val TAG = "JarvisWalkie"
    private const val SAMPLE_RATE = 16000
    private const val FRAME_SAMPLES = 320                 // 20 ms at 16 kHz
    private const val FRAME_BYTES = FRAME_SAMPLES * 2
    private const val PIECES_PER_MESSAGE = 2
    private const val CUSHION_MS = 120
    private const val MAX_CUSHION_MS = 400
    private val AMR_SIZES = intArrayOf(18, 24, 33, 37, 41, 47, 51, 59, 61, 6, 6, 6, 6, 6, 6, 1)  // AMR-WB frame sizes by TOC

    @Volatile var connected = false
    @Volatile var talking = false
    @Volatile var incomingFrom: String = ""
    @Volatile var incomingTalk: String = ""
    @Volatile var currentTalk: String = ""
    @Volatile var people: JSONArray = JSONArray()
    @Volatile var you: Int = 0
    @Volatile var listener: ((String, JSONObject) -> Unit)? = null   // the app, when open
    @Volatile var serviceListener: ((String, JSONObject) -> Unit)? = null  // the service: the incoming card

    private var context: Context? = null
    private var client: OkHttpClient? = null
    private var ws: WebSocket? = null
    @Volatile private var wanted = false
    @Volatile private var attempts = 0
    private var reconnectThread: Thread? = null

    // capture
    private var recorder: AudioRecord? = null
    private var encoder: MediaCodec? = null
    private var captureThread: Thread? = null
    @Volatile private var capturing = false

    // playback
    private val queue = ConcurrentLinkedQueue<ByteArray>()
    private var player: Thread? = null
    @Volatile private var playing = false
    @Volatile private var playCodec = "amrwb"
    private var cushionMs = CUSHION_MS
    // Timing marks (elapsedRealtime ms) so a test can read how fast a talk reaches this phone from logcat.
    @Volatile private var pressAt = 0L
    @Volatile private var incomingAt = 0L
    @Volatile private var firstAudioAt = 0L
    @Volatile private var pingAt = 0L
    @Volatile private var audioPieces = 0

    private fun emit(event: String, data: JSONObject = JSONObject()) {
        try { listener?.invoke(event, data) } catch (_: Throwable) {}
        try { serviceListener?.invoke(event, data) } catch (_: Throwable) {}
    }

    // ---------------------------------------------------------------- the line
    /** Open (or keep) the line. Safe to call often. */
    @Synchronized
    fun start(ctx: Context) {
        context = ctx.applicationContext
        val prefs = WakeBus.prefs(ctx)
        val url = prefs.getString(WakeBus.KEY_WALKIE_URL, "") ?: ""
        if (!prefs.getBoolean(WakeBus.KEY_WALKIE_ENABLED, false) || url.isEmpty() || !prefs.getBoolean(WakeBus.KEY_ENABLED, false)) return
        wanted = true
        if (ws != null || reconnectThread?.isAlive == true) return
        connectNow()
    }

    @Synchronized
    fun stop() {
        wanted = false
        attempts = 0
        try { ws?.close(1000, "off") } catch (_: Throwable) {}
        ws = null
        stopCapture()
        stopPlayback()
        connected = false
        emit("state", JSONObject().put("connected", false))
    }

    private fun connectNow() {
        val ctx = context ?: return
        val prefs = WakeBus.prefs(ctx)
        val base = prefs.getString(WakeBus.KEY_WALKIE_URL, "") ?: return
        val token = prefs.getString(WakeBus.KEY_ACCESS, "") ?: ""
        val url = base + (if (base.contains("?")) "&" else "?") + "token=" + token
        val c = client ?: OkHttpClient.Builder().pingInterval(20, TimeUnit.SECONDS).connectTimeout(8, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.SECONDS).build().also { client = it }
        Log.i(TAG, "connecting")
        ws = c.newWebSocket(Request.Builder().url(url).build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                connected = true
                attempts = 0
                Log.i(TAG, "line open")
                webSocket.send(JSONObject().put("t", "hello").toString())
                emit("state", JSONObject().put("connected", true))
            }

            override fun onMessage(webSocket: WebSocket, text: String) = onText(text)
            override fun onMessage(webSocket: WebSocket, bytes: ByteString) = onAudio(bytes.toByteArray())

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "line closed: $code $reason")
                dropped(code == 4401)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "line failed: ${t.message} (${response?.code})")
                dropped(response?.code == 401 || response?.code == 403)
            }
        })
    }

    private fun dropped(authProblem: Boolean) {
        ws = null
        connected = false
        if (talking) stopCapture()
        stopPlayback()
        emit("state", JSONObject().put("connected", false))
        if (!wanted) return
        val ctx = context ?: return
        reconnectThread = Thread({
            try {
                if (authProblem) Api.refreshTokens(ctx)
                val delays = longArrayOf(1000, 2000, 5000, 10000, 30000)
                Thread.sleep(delays[minOf(attempts, delays.size - 1)])
                attempts++
                synchronized(this) { if (wanted && ws == null) connectNow() }
            } catch (_: InterruptedException) {
            }
        }, "jarvis-walkie-reconnect").also { it.start() }
    }

    private fun send(obj: JSONObject) {
        ws?.send(obj.toString())
    }

    private fun onText(text: String) {
        val msg = try { JSONObject(text) } catch (_: Throwable) { return }
        when (msg.optString("t")) {
            "ready", "people", "presence" -> {
                if (msg.has("you")) you = msg.optInt("you")
                people = msg.optJSONArray("people") ?: JSONArray()
                emit("people", JSONObject().put("people", people).put("you", you))
            }
            "floor" -> {
                currentTalk = msg.optString("talk")
                Log.i(TAG, "timing: floor ${SystemClock.elapsedRealtime() - pressAt} ms after the press (listening ${msg.optJSONArray("listening")})")
                emit("floor", msg)
            }
            "busy", "refused", "cut" -> {
                if (talking) stopCapture()
                emit(msg.optString("t"), msg)
            }
            "incoming" -> {
                incomingFrom = msg.optJSONObject("from")?.optString("name") ?: ""
                incomingTalk = msg.optString("talk")
                currentTalk = incomingTalk
                playCodec = msg.optString("codec", "amrwb")
                incomingAt = SystemClock.elapsedRealtime()
                firstAudioAt = 0L
                pingAt = incomingAt
                send(JSONObject().put("t", "ping"))  // the line's round trip, logged with the pong
                Log.i(TAG, "timing: incoming from $incomingFrom ($playCodec)")
                startPlayback()
                emit("incoming", msg)
            }
            "done" -> {
                Log.i(TAG, "timing: they let go ${SystemClock.elapsedRealtime() - incomingAt} ms after incoming; $audioPieces messages of sound arrived, ${queue.size} still to play")
                // Let the cushion drain, then stop; a reply may follow in the same talk.
                Thread { Thread.sleep((cushionMs + 100).toLong()); if (queue.isEmpty()) stopPlayback() }.start()
                emit("done", msg)
            }
            "ended" -> {
                if (msg.optString("talk") == currentTalk) currentTalk = ""
                emit("ended", msg)
            }
            "quiet" -> emit("quiet", msg)
            "pong" -> if (pingAt != 0L) {
                Log.i(TAG, "timing: line round trip ${SystemClock.elapsedRealtime() - pingAt} ms")
                pingAt = 0L
            }
        }
    }

    // ---------------------------------------------------------------- talking (hold)
    /** The button went down: start sending at once; the relay answers "floor" or "busy" while the sound flows. */
    fun talkStart(to: List<Int>, talk: String?) {
        val socket = ws ?: run { emit("refused", JSONObject().put("why", "no line")); return }
        if (talking) return
        val msg = JSONObject().put("t", "talk").put("codec", "amrwb")
        if (!talk.isNullOrEmpty()) msg.put("talk", talk) else msg.put("to", JSONArray(to))
        talking = true
        pressAt = SystemClock.elapsedRealtime()
        stopPlayback()  // the speaker is theirs now
        socket.send(msg.toString())
        startCapture()
    }

    fun talkEnd() {
        if (!talking) return
        talking = false
        stopCapture()
        send(JSONObject().put("t", "end"))
    }

    fun quiet(minutes: Int) = send(JSONObject().put("t", "quiet").put("minutes", minutes))

    fun refreshPeople() = send(JSONObject().put("t", "status"))

    /** Each press is its own capture; a press that starts while the last one is still winding down never shares its mic. */
    @Volatile private var captureGen = 0

    private fun startCapture() {
        val ctx = context ?: return
        val gen = ++captureGen
        capturing = true
        captureThread = Thread({
            var rec: AudioRecord? = null
            var enc: MediaCodec? = null
            val startedAt = SystemClock.elapsedRealtime()
            var read = 0
            var dropped = 0
            var sent = 0
            try {
                val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
                am.requestAudioFocus(null, AudioManager.STREAM_VOICE_CALL, AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
                val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
                // Half a second of room in the microphone's own buffer: a slow moment delays a piece, never loses it.
                rec = AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, FRAME_BYTES * 25))
                recorder = rec
                enc = amrEncoder()
                encoder = enc
                Log.i(TAG, "capture: ${enc?.name ?: "raw pcm (no encoder)"}")
                rec.startRecording()
                val pcm = ByteArray(FRAME_BYTES)
                val out = java.io.ByteArrayOutputStream()
                var pieces = 0
                var lastLevel = 0L
                val info = MediaCodec.BufferInfo()
                val live = { capturing && gen == captureGen }

                // Take whatever the encoder has ready (waiting at most `waitUs`) and send it, two pieces at a time.
                fun drain(waitUs: Long) {
                    val e = enc ?: return
                    var outIndex = e.dequeueOutputBuffer(info, waitUs)
                    while (outIndex >= 0) {
                        val buf = e.getOutputBuffer(outIndex)!!
                        val frame = ByteArray(info.size)
                        buf.position(info.offset)
                        buf.get(frame)
                        e.releaseOutputBuffer(outIndex, false)
                        if ((info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) == 0 && frame.isNotEmpty()) {
                            out.write(frame)
                            pieces++
                            if (pieces >= PIECES_PER_MESSAGE) {
                                ws?.send(out.toByteArray().toByteString())
                                sent++
                                out.reset()
                                pieces = 0
                            }
                        }
                        outIndex = e.dequeueOutputBuffer(info, 0)
                    }
                }

                while (live()) {
                    var got = 0
                    while (got < FRAME_BYTES && live()) {
                        val n = rec.read(pcm, got, FRAME_BYTES - got)
                        if (n <= 0) break
                        got += n
                    }
                    if (got < FRAME_BYTES) continue
                    read++
                    val now = SystemClock.elapsedRealtime()
                    if (now - lastLevel > 100) {
                        lastLevel = now
                        emit("level", JSONObject().put("level", level(pcm)))
                    }
                    if (enc == null) {
                        ws?.send(pcm.toByteString())  // no encoder on this phone: raw PCM (the relay does not mind)
                        sent++
                        continue
                    }
                    // Feed the encoder. When its input is full, take its output first; a piece is dropped only if it
                    // stays full for 100 ms, which a working encoder never does.
                    var inIndex = enc.dequeueInputBuffer(0)
                    var tries = 0
                    while (inIndex < 0 && tries < 10 && live()) {
                        drain(5_000)
                        inIndex = enc.dequeueInputBuffer(5_000)
                        tries++
                    }
                    if (inIndex < 0) {
                        dropped++
                        continue
                    }
                    val buf = enc.getInputBuffer(inIndex)!!
                    buf.clear()
                    buf.put(pcm)
                    enc.queueInputBuffer(inIndex, 0, FRAME_BYTES, now * 1000, 0)
                    drain(0)
                }
                // The last words: give the encoder a moment to finish what it holds, then send the remainder.
                if (enc != null) {
                    val until = SystemClock.elapsedRealtime() + 120
                    while (SystemClock.elapsedRealtime() < until) drain(10_000)
                }
                if (out.size() > 0) {
                    ws?.send(out.toByteArray().toByteString())
                    sent++
                }
                Log.i(TAG, "timing: press ended after ${SystemClock.elapsedRealtime() - startedAt} ms: $read pieces read, $dropped dropped, $sent messages sent")
            } catch (e: Throwable) {
                Log.w(TAG, "capture failed: ${e.message}")
                emit("error", JSONObject().put("where", "mic").put("message", e.message ?: ""))
            } finally {
                try { rec?.stop() } catch (_: Throwable) {}
                try { rec?.release() } catch (_: Throwable) {}
                try { enc?.stop(); enc?.release() } catch (_: Throwable) {}
                if (gen == captureGen) {
                    recorder = null
                    encoder = null
                }
                try { (ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager).abandonAudioFocus(null) } catch (_: Throwable) {}
            }
        }, "jarvis-walkie-mic").also { it.start() }
    }

    private fun stopCapture() {
        capturing = false
        talking = false
        captureGen++
        captureThread = null
    }

    private fun amrEncoder(): MediaCodec? = try {
        val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_AMR_WB, SAMPLE_RATE, 1).apply {
            setInteger(MediaFormat.KEY_BIT_RATE, 23850)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, FRAME_BYTES)
        }
        val name = MediaCodecList(MediaCodecList.REGULAR_CODECS).findEncoderForFormat(format) ?: return null
        MediaCodec.createByCodecName(name).apply {
            configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            start()
        }
    } catch (e: Throwable) {
        Log.w(TAG, "no AMR-WB encoder: ${e.message}")
        null
    }

    private fun level(pcm: ByteArray): Double {
        var sum = 0.0
        var i = 0
        while (i + 1 < pcm.size) {
            val s = ((pcm[i + 1].toInt() shl 8) or (pcm[i].toInt() and 0xff)).toShort().toInt()
            sum += s.toDouble() * s
            i += 2
        }
        return minOf(1.0, sqrt(sum / (pcm.size / 2)) / 6000.0)
    }

    // ---------------------------------------------------------------- listening (play)
    private fun onAudio(data: ByteArray) {
        if (talking) return
        if (!playing) startPlayback()
        if (firstAudioAt == 0L) {
            firstAudioAt = SystemClock.elapsedRealtime()
            audioPieces = 0
            Log.i(TAG, "timing: first piece (${data.size} bytes) ${firstAudioAt - incomingAt} ms after incoming")
        }
        audioPieces++
        queue.add(data)
        if (queue.size > 60) queue.poll()  // a second behind: skip ahead rather than fall further back
    }

    private fun startPlayback() {
        if (playing) return
        playing = true
        queue.clear()
        player = Thread({
            var track: AudioTrack? = null
            var dec: MediaCodec? = null
            try {
                val attrs = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()
                val minBuf = AudioTrack.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
                track = AudioTrack.Builder().setAudioAttributes(attrs)
                    .setAudioFormat(AudioFormat.Builder().setSampleRate(SAMPLE_RATE).setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                    .setBufferSizeInBytes(maxOf(minBuf, FRAME_BYTES * 10))
                    .apply { if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY) }
                    .build()
                val ctx = context
                if (ctx != null) {
                    val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
                    am.requestAudioFocus(null, AudioManager.STREAM_VOICE_CALL, AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
                    if (!am.isWiredHeadsetOn && !am.isBluetoothA2dpOn) am.isSpeakerphoneOn = true
                }
                track.play()
                if (playCodec == "amrwb") dec = amrDecoder()
                // The cushion: wait for a few pieces (or a moment) before the first sound, so jitter never cuts words.
                val startedAt = SystemClock.elapsedRealtime()
                while (playing && queue.size < cushionMs / 20 && SystemClock.elapsedRealtime() - startedAt < cushionMs + 100) Thread.sleep(5)
                var idleSince = 0L
                var firstWrite = true
                val info = MediaCodec.BufferInfo()
                while (playing) {
                    val data = queue.poll()
                    if (data == null) {
                        if (idleSince == 0L) idleSince = SystemClock.elapsedRealtime()
                        else if (SystemClock.elapsedRealtime() - idleSince > 4000) break  // nothing for 4 s: the talk is over
                        Thread.sleep(5)
                        continue
                    }
                    if (idleSince != 0L && SystemClock.elapsedRealtime() - idleSince > 60) {
                        cushionMs = minOf(MAX_CUSHION_MS, cushionMs + 40)  // it ran dry: a little more cushion next time
                    }
                    idleSince = 0L
                    if (dec == null || playCodec != "amrwb") {
                        if (firstWrite) { firstWrite = false; logFirstSound() }
                        track.write(data, 0, data.size)
                        continue
                    }
                    var pos = 0
                    while (pos < data.size) {
                        val size = AMR_SIZES[(data[pos].toInt() shr 3) and 0x0f]
                        val end = minOf(data.size, pos + size)
                        val inIndex = dec.dequeueInputBuffer(20_000)
                        if (inIndex >= 0) {
                            val buf = dec.getInputBuffer(inIndex)!!
                            buf.clear()
                            buf.put(data, pos, end - pos)
                            dec.queueInputBuffer(inIndex, 0, end - pos, 0, 0)
                        }
                        pos = end
                        var outIndex = dec.dequeueOutputBuffer(info, 10_000)
                        while (outIndex >= 0) {
                            val buf = dec.getOutputBuffer(outIndex)!!
                            val pcm = ByteArray(info.size)
                            buf.position(info.offset)
                            buf.get(pcm)
                            dec.releaseOutputBuffer(outIndex, false)
                            if (pcm.isNotEmpty()) {
                                if (firstWrite) { firstWrite = false; logFirstSound() }
                                track.write(pcm, 0, pcm.size)
                            }
                            outIndex = dec.dequeueOutputBuffer(info, 0)
                        }
                    }
                }
            } catch (e: Throwable) {
                Log.w(TAG, "playback failed: ${e.message}")
                emit("error", JSONObject().put("where", "speaker").put("message", e.message ?: ""))
            } finally {
                try { track?.stop(); track?.release() } catch (_: Throwable) {}
                try { dec?.stop(); dec?.release() } catch (_: Throwable) {}
                context?.let {
                    try {
                        val am = it.getSystemService(Context.AUDIO_SERVICE) as AudioManager
                        am.isSpeakerphoneOn = false
                        am.abandonAudioFocus(null)
                    } catch (_: Throwable) {}
                }
                playing = false
            }
        }, "jarvis-walkie-play").also { it.start() }
    }

    private fun stopPlayback() {
        playing = false
        queue.clear()
        player = null
    }

    private fun logFirstSound() {
        val now = SystemClock.elapsedRealtime()
        Log.i(TAG, "timing: first sound out ${now - incomingAt} ms after incoming, ${if (firstAudioAt != 0L) now - firstAudioAt else -1} ms after the first piece arrived (cushion $cushionMs ms)")
    }

    private fun amrDecoder(): MediaCodec? = try {
        val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_AMR_WB, SAMPLE_RATE, 1)
        MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_AUDIO_AMR_WB).apply {
            configure(format, null, null, 0)
            start()
        }
    } catch (e: Throwable) {
        Log.w(TAG, "no AMR-WB decoder: ${e.message}")
        null
    }

    fun status(): JSONObject = JSONObject()
        .put("connected", connected).put("talking", talking).put("talk", currentTalk)
        .put("incomingFrom", incomingFrom).put("people", people).put("you", you)
        .put("encoder", context?.let { amrAvailable() } ?: false)

    private fun amrAvailable(): Boolean = try {
        MediaCodecList(MediaCodecList.REGULAR_CODECS).codecInfos.any { it.isEncoder && it.supportedTypes.any { t -> t.equals(MediaFormat.MIMETYPE_AUDIO_AMR_WB, true) } }
    } catch (_: Throwable) { false }

    @Suppress("unused")
    private fun pcmLevel(buf: ByteBuffer): Int = abs(buf.getShort(0).toInt())
}
