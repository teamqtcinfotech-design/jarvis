package com.qtcinfotech.jarvis.wake

import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import org.json.JSONObject
import kotlin.math.sqrt

/**
 * The room before JARVIS speaks (his ask, 26 Nov 2026): a moment of listening tells whether several people are
 * talking or there is steady noise (he is busy: everything but the urgent waits, and nothing is said), or the room
 * is quiet.
 *
 * The sound levels come from whichever microphone is already open (the wake-word listener feeds every 80 ms slice,
 * the live line every 100 ms frame, JARVIS's own voice left out); when none is, a short recording of its own is
 * made, unless someone else holds the microphone. Nothing is kept beyond a few seconds of levels; no sound leaves
 * the phone.
 */
object RoomSense {
    private const val TAG = "JarvisWake"
    private const val WINDOW_MS = 3_000L
    private const val FRESH_MS = 2_500L          // levels older than this say nothing about "now"
    private const val MIN_SLICES = 10            // about a second of sound before the buffer is trusted
    /** A slice louder than this is sound at a speaking level (speech at the phone ≈ 3000, room hiss ≈ 110). */
    const val ACTIVE_LEVEL = 350.0
    /** Sound at a speaking level for this share of the moment = several voices or steady noise. */
    const val BUSY_SHARE = 0.55

    private class Slice(val at: Long, val rms: Double, val source: String)
    private val slices = ArrayDeque<Slice>()

    /** One slice of sound from an open microphone. Slices while JARVIS itself talks are left out. */
    @Synchronized
    fun feed(rms: Double, ownVoice: Boolean, source: String) {
        if (ownVoice) return
        val now = System.currentTimeMillis()
        slices.addLast(Slice(now, rms, source))
        while (slices.isNotEmpty() && now - slices.first().at > WINDOW_MS) slices.removeFirst()
    }

    @Synchronized
    private fun fromBuffer(): JSONObject? {
        val now = System.currentTimeMillis()
        val recent = slices.filter { now - it.at <= FRESH_MS }
        if (recent.size < MIN_SLICES) return null
        return judge(recent.map { it.rms }, recent.last().source)
    }

    private fun judge(levels: List<Double>, source: String): JSONObject {
        val level = levels.average()
        val share = levels.count { it > ACTIVE_LEVEL }.toDouble() / levels.size
        return JSONObject().put("busy", share >= BUSY_SHARE).put("level", level.toInt()).put("share", (share * 100).toInt() / 100.0).put("source", source)
    }

    /** A short recording of its own (blocking: never on the main thread), or null when the microphone is taken. */
    private fun listen(ms: Long): JSONObject? {
        if (WakeBus.micHeld || LiveVoice.open) return null
        val rate = 16_000
        val minBuf = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val record = try {
            AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, 1280 * 8))
        } catch (e: Throwable) {
            Log.w(TAG, "room: microphone not available: ${e.message}")
            return null
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            record.release()
            return null
        }
        val chunk = ShortArray(1280)  // 80 ms
        val levels = ArrayList<Double>()
        try {
            record.startRecording()
            if (record.recordingState != AudioRecord.RECORDSTATE_RECORDING) return null
            val until = System.currentTimeMillis() + ms
            while (System.currentTimeMillis() < until) {
                var got = 0
                while (got < chunk.size) {
                    val n = record.read(chunk, got, chunk.size - got)
                    if (n <= 0) break
                    got += n
                }
                if (got < chunk.size) break
                var sum = 0.0
                var peak = 0
                for (s in chunk) {
                    val v = s.toInt()
                    sum += (v * v).toDouble()
                    if (v > peak) peak = v
                }
                if (peak == 0) continue  // Android feeding silence: another app has the microphone
                levels.add(sqrt(sum / chunk.size))
            }
        } catch (e: Throwable) {
            Log.w(TAG, "room: recording failed: ${e.message}")
        } finally {
            try { record.stop() } catch (_: Throwable) {}
            try { record.release() } catch (_: Throwable) {}
        }
        return if (levels.size >= MIN_SLICES) judge(levels, "probe") else null
    }

    /** What the room sounds like now: from an open microphone's last moment, else a recording of up to [ms];
     *  "none" (quiet) when it cannot be measured, so nothing is ever held for a measurement that failed. */
    fun check(context: Context, ms: Long): JSONObject {
        val out = fromBuffer() ?: listen(ms.coerceIn(300L, 4_000L))
        if (out != null) {
            Log.i(TAG, "room: ${if (out.optBoolean("busy")) "busy" else "quiet"} (level ${out.optInt("level")}, share ${out.optDouble("share")}, ${out.optString("source")})")
            return out
        }
        return JSONObject().put("busy", false).put("level", 0).put("share", 0.0).put("source", "none")
    }

    /** True when the room is busy right now; false when it is quiet or cannot be measured. */
    fun busy(context: Context, ms: Long = 1_500L): Boolean = check(context, ms).optBoolean("busy")
}
