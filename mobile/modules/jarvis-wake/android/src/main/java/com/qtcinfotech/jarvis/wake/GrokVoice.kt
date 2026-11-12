package com.qtcinfotech.jarvis.wake

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.util.Log
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.InputStream
import java.security.MessageDigest
import java.util.concurrent.Executors

/**
 * JARVIS's voice, spoken by Grok.
 *
 * The server turns a reply into sound and streams it; this plays it as it arrives, so the voice starts about half
 * a second after the words are known. Everything already heard is kept on the phone, so repeats ("Ji Sir?",
 * "Saved, Sir.") play instantly, cost nothing and work offline. Upcoming reminders are fetched ahead of time.
 *
 * If the voice cannot be reached before any sound has played, [speak] reports false and the caller uses the
 * phone's own voice for that sentence, so there is never silence.
 */
object GrokVoice {
    private const val TAG = "JarvisWake"
    private const val DEFAULT_RATE = 24000
    private const val MAX_CACHE_BYTES = 40L * 1024 * 1024
    private const val MAX_CACHE_FILES = 200
    private val DEVANAGARI = Regex("[\\u0900-\\u097F]")

    private val live = Executors.newSingleThreadExecutor()      // one sentence at a time, in order
    private val ahead = Executors.newSingleThreadExecutor()     // quiet fetching of sentences needed soon
    @Volatile private var track: AudioTrack? = null
    /** Goes up at every stop(). A sentence asked for before the last stop is never played, whenever its turn comes. */
    private val stops = java.util.concurrent.atomic.AtomicInteger(0)
    @Volatile private var playing = -1          // the stop count the sentence now playing was asked under
    private val cancelled: Boolean get() = stops.get() != playing
    @Volatile private var currentId: String? = null
    /** Told the moment the first sound of a sentence actually reaches the speaker (TtsEngine's "talking since"). */
    @Volatile var onSoundStart: (() -> Unit)? = null

    fun enabled(context: Context): Boolean {
        val prefs = WakeBus.prefs(context)
        return prefs.getBoolean(WakeBus.KEY_GROK_VOICE, true) && !(prefs.getString(WakeBus.KEY_SERVER, "") ?: "").isEmpty()
    }

    private fun dir(context: Context): File = File(context.cacheDir, "jarvis_voice").apply { mkdirs() }

    /** The voice JARVIS speaks with now ("" = the server's choice). Part of every cached sound's name, so a changed
     *  voice never plays an old recording in the old voice. */
    @Volatile private var voiceName: String = ""

    private fun voiceOf(context: Context): String =
        (WakeBus.prefs(context).getString(WakeBus.KEY_VOICE_NAME, "") ?: "").also { voiceName = it }

    private fun keyFor(text: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest((voiceName + "|" + text.trim()).toByteArray(Charsets.UTF_8))
        return digest.joinToString("") { "%02x".format(it) }.take(40)
    }

    private fun fileFor(context: Context, text: String): File = File(dir(context), keyFor(text) + ".pcm")

    private fun langOf(text: String): String = if (DEVANAGARI.containsMatchIn(text)) "hi" else "en"

    /**
     * Speak [text]. [onResult] gets "done", "stopped", or "fallback" (nothing was played: use the phone's voice).
     * Runs on a background thread; returns immediately.
     */
    fun speak(context: Context, id: String, text: String, onResult: (String) -> Unit) {
        val app = context.applicationContext
        val asked = stops.get()
        live.execute {
            if (stops.get() != asked) {
                onResult("stopped")  // it was cancelled while waiting its turn: say nothing at all
                return@execute
            }
            playing = asked
            currentId = id
            val result = try {
                play(app, text)
            } catch (e: Throwable) {
                Log.w(TAG, "Grok voice failed: ${e.message}")
                // A sentence that was cancelled must stay silent, not come back in the phone's own voice.
                if (cancelled) "stopped" else "fallback"
            }
            currentId = null
            onResult(result)
        }
    }

    private fun play(context: Context, text: String): String {
        val voice = voiceOf(context)
        val cached = fileFor(context, text)
        if (cached.exists() && cached.length() > 0) {
            cached.setLastModified(System.currentTimeMillis())
            return FileInputStream(cached).use { stream(it, DEFAULT_RATE, null) }
        }
        val body = JSONObject().put("text", text).put("lang", langOf(text)).put("format", "pcm")
        if (voice.isNotBlank()) body.put("voice", voice)
        val conn = Api.openStream(context, "/assistant/speak/", body) ?: return if (cancelled) "stopped" else "fallback"
        val tmp = File(dir(context), keyFor(text) + ".part")
        try {
            val rate = conn.getHeaderField("X-Sample-Rate")?.toIntOrNull() ?: DEFAULT_RATE
            val result = FileOutputStream(tmp).use { out -> stream(conn.inputStream, rate, out) }
            if (result == "done" && rate == DEFAULT_RATE && tmp.length() > 0) {
                tmp.renameTo(cached)
                prune(context)
            } else {
                tmp.delete()
            }
            return result
        } catch (e: Throwable) {
            tmp.delete()
            throw e
        } finally {
            try {
                conn.disconnect()
            } catch (_: Throwable) {
            }
        }
    }

    /** Plays 16-bit mono sound from [input] as it arrives. Returns "done", "stopped", or "fallback" if nothing came. */
    private fun stream(input: InputStream, rate: Int, copyTo: FileOutputStream?): String {
        val minBuf = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val player = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(rate)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
            .setBufferSizeInBytes(maxOf(minBuf, rate / 2))  // about a quarter second
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        track = player
        val buf = ByteArray(8192)
        var written = 0L
        var carry = -1  // a lone byte left over when a chunk ends mid-sample
        try {
            while (!cancelled) {
                val n = input.read(buf)
                if (n < 0) break
                if (n == 0) continue
                copyTo?.write(buf, 0, n)
                var offset = 0
                var length = n
                if (carry >= 0) {  // complete the split sample first
                    val pair = byteArrayOf(carry.toByte(), buf[0])
                    player.write(pair, 0, 2)
                    written += 2
                    offset = 1
                    length = n - 1
                    carry = -1
                }
                if (length % 2 == 1) {
                    carry = buf[offset + length - 1].toInt() and 0xFF
                    length -= 1
                }
                if (written == 0L) {
                    player.play()
                    onSoundStart?.invoke()
                }
                if (length > 0) {
                    player.write(buf, offset, length)
                    written += length
                }
            }
            if (cancelled) return "stopped"   // checked first: cancelled before any sound is still cancelled
            if (written == 0L) return "fallback"
            // Let the last of the sound play out before reporting done.
            val frames = (written / 2).toInt()
            var waited = 0
            while (!cancelled && player.playbackHeadPosition < frames && waited < 15000) {
                Thread.sleep(30)
                waited += 30
            }
            return if (cancelled) "stopped" else "done"
        } finally {
            track = null
            try {
                player.pause()
                player.flush()
            } catch (_: Throwable) {
            }
            player.release()
        }
    }

    fun stop() {
        stops.incrementAndGet()
        try {
            track?.pause()
        } catch (_: Throwable) {
        }
    }

    val speaking: Boolean get() = currentId != null

    /** Quietly fetch sentences that will be needed soon (greeting lines, reminders due in the next hour). */
    fun prefetch(context: Context, texts: List<String>) {
        val app = context.applicationContext
        if (!enabled(app)) return
        ahead.execute {
            for (text in texts.take(12)) {
                val clean = text.trim()
                if (clean.isEmpty() || clean.length > 400) continue
                val target = fileFor(app, clean)
                if (target.exists() && target.length() > 0) continue
                val voice = voiceOf(app)
                val body = JSONObject().put("text", clean).put("lang", langOf(clean)).put("format", "pcm")
                if (voice.isNotBlank()) body.put("voice", voice)
                val conn = Api.openStream(app, "/assistant/speak/", body, 5000, 15000) ?: return@execute
                val tmp = File(dir(app), keyFor(clean) + ".pre")
                try {
                    val rate = conn.getHeaderField("X-Sample-Rate")?.toIntOrNull() ?: DEFAULT_RATE
                    FileOutputStream(tmp).use { out -> conn.inputStream.copyTo(out) }
                    if (rate == DEFAULT_RATE && tmp.length() > 0) tmp.renameTo(target) else tmp.delete()
                } catch (e: Throwable) {
                    tmp.delete()
                } finally {
                    try {
                        conn.disconnect()
                    } catch (_: Throwable) {
                    }
                }
            }
            prune(app)
        }
    }

    private fun prune(context: Context) {
        val files = dir(context).listFiles { f -> f.name.endsWith(".pcm") }?.sortedBy { it.lastModified() } ?: return
        var total = files.sumOf { it.length() }
        var count = files.size
        for (f in files) {
            if (total <= MAX_CACHE_BYTES && count <= MAX_CACHE_FILES) break
            total -= f.length()
            count -= 1
            f.delete()
        }
    }

    /** Audio stream used for volume keys while JARVIS talks. */
    val streamType: Int = AudioManager.STREAM_MUSIC
}
