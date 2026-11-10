package com.qtcinfotech.jarvis.wake

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import java.nio.FloatBuffer

/**
 * On-device "Hey JARVIS" detection, a port of openWakeWord's streaming pipeline
 * (openwakeword/utils.py, AudioFeatures._streaming_features):
 *
 *   16 kHz PCM  -> every 1280 samples (80 ms): mel-spectrogram of the last 1760 samples (1280 + 3 frames lookback),
 *   each frame transformed as x/10 + 2 and appended to the mel buffer
 *   -> the last 76 mel frames -> speech embedding (96 numbers)
 *   -> the last 16 embeddings -> the "hey jarvis" classifier -> a score between 0 and 1.
 *
 * Audio never leaves the phone. The models are the free ones from the openWakeWord project.
 */
class WakeDetector(context: Context) : AutoCloseable {
    private val env: OrtEnvironment = OrtEnvironment.getEnvironment()
    private val opts = OrtSession.SessionOptions().apply {
        setIntraOpNumThreads(1)
        setInterOpNumThreads(1)
    }
    private val mel: OrtSession = env.createSession(context.assets.open("melspectrogram.onnx").readBytes(), opts)
    private val emb: OrtSession = env.createSession(context.assets.open("embedding_model.onnx").readBytes(), opts)
    private val cls: OrtSession = env.createSession(context.assets.open("hey_jarvis_v0.1.onnx").readBytes(), opts)
    private val melInput = mel.inputNames.first()
    private val embInput = emb.inputNames.first()
    private val clsInput = cls.inputNames.first()

    // Rolling buffers, same sizes as the Python reference.
    private val raw = FloatArray(RAW_LEN) // last 1760 raw samples (int16 values as floats, NOT normalised)
    private val melBuf = ArrayDeque<FloatArray>().apply { repeat(76) { add(FloatArray(32) { 1f }) } }
    private val featBuf = ArrayDeque<FloatArray>().apply { repeat(16) { add(FloatArray(96)) } }

    // What the buffers look like after two seconds of a quiet room. The classifier judges the last 1.3 s of
    // sound; starting it from blank buffers (all zeros) leaves it unable to recognise the phrase for the first
    // second and a half after the microphone comes back. Starting from "a quiet room" makes it ready at once.
    private val quietMel = ArrayList<FloatArray>()
    private val quietFeat = ArrayList<FloatArray>()

    init {
        val noise = java.util.Random(7)
        val chunk = ShortArray(CHUNK)
        repeat(WARM_CHUNKS) {
            for (i in chunk.indices) chunk[i] = (noise.nextInt(241) - 120).toShort()
            process(chunk)
        }
        melBuf.toList().takeLast(76).forEach { quietMel.add(it.copyOf()) }
        featBuf.toList().takeLast(16).forEach { quietFeat.add(it.copyOf()) }
    }

    /**
     * Forget what was heard before (after the microphone was handed to someone else and came back) and be ready
     * for "Hey JARVIS" straight away: the buffers go back to the quiet-room state, not to blank.
     */
    fun reset() {
        raw.fill(0f)
        melBuf.clear()
        quietMel.forEach { melBuf.add(it.copyOf()) }
        featBuf.clear()
        quietFeat.forEach { featBuf.add(it.copyOf()) }
    }

    /** Feed one 80 ms chunk (1280 samples of 16 kHz PCM16). Returns the wake-word score for this moment. */
    fun process(chunk: ShortArray): Float {
        require(chunk.size == CHUNK) { "chunk must be $CHUNK samples" }
        System.arraycopy(raw, CHUNK, raw, 0, RAW_LEN - CHUNK)
        for (i in 0 until CHUNK) raw[RAW_LEN - CHUNK + i] = chunk[i].toFloat()

        // 1) mel-spectrogram frames of the last 1760 samples
        val frames = run(mel, melInput, raw, longArrayOf(1, RAW_LEN.toLong()))
        val nFrames = frames.size / 32
        for (f in 0 until nFrames) {
            val row = FloatArray(32) { c -> frames[f * 32 + c] / 10f + 2f }
            melBuf.addLast(row)
        }
        while (melBuf.size > MEL_MAX) melBuf.removeFirst()

        // 2) one new embedding from the last 76 frames
        val window = FloatArray(76 * 32)
        var k = 0
        for (row in melBuf.toList().takeLast(76)) {
            System.arraycopy(row, 0, window, k, 32)
            k += 32
        }
        val embedding = run(emb, embInput, window, longArrayOf(1, 76, 32, 1))
        featBuf.addLast(embedding.copyOf(96))
        while (featBuf.size > FEAT_MAX) featBuf.removeFirst()

        // 3) classify the last 16 embeddings
        val feats = FloatArray(16 * 96)
        k = 0
        for (e in featBuf.toList().takeLast(16)) {
            System.arraycopy(e, 0, feats, k, 96)
            k += 96
        }
        val score = run(cls, clsInput, feats, longArrayOf(1, 16, 96))
        return if (score.isEmpty()) 0f else score[0]
    }

    private fun run(session: OrtSession, inputName: String, data: FloatArray, shape: LongArray): FloatArray {
        OnnxTensor.createTensor(env, FloatBuffer.wrap(data), shape).use { tensor ->
            session.run(mapOf(inputName to tensor)).use { result ->
                val out = result[0] as OnnxTensor
                val buf = out.floatBuffer
                val arr = FloatArray(buf.remaining())
                buf.get(arr)
                return arr
            }
        }
    }

    override fun close() {
        mel.close()
        emb.close()
        cls.close()
        opts.close()
    }

    companion object {
        const val SAMPLE_RATE = 16000
        const val CHUNK = 1280
        private const val RAW_LEN = CHUNK + 160 * 3
        private const val MEL_MAX = 970
        private const val FEAT_MAX = 120
        private const val WARM_CHUNKS = 24  // about 2 s of quiet-room noise, run once when the detector is loaded
    }
}
