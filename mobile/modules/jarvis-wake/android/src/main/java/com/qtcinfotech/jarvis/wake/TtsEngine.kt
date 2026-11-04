package com.qtcinfotech.jarvis.wake

import android.content.Context
import android.os.Bundle
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.util.Log
import java.util.Locale

/**
 * JARVIS's voice. One shared text-to-speech connection that reconnects by itself when the engine is lost
 * ("not bound to TTS engine") and ALWAYS reports done / error / stopped for every utterance, so the app
 * can never get stuck in "speaking".
 */
object TtsEngine {
    private const val TAG = "JarvisWake"
    private val DEVANAGARI = Regex("[\\u0900-\\u097F]")

    data class Request(val id: String, val text: String, val fallback: String?, val lang: String, val rate: Float, val retries: Int = 0)

    private val lock = Any()
    private var appContext: Context? = null
    private var tts: TextToSpeech? = null
    private var ready = false
    private val pending = ArrayDeque<Request>()
    @Volatile var callback: ((id: String, event: String) -> Unit)? = null

    fun init(context: Context) {
        synchronized(lock) {
            appContext = context.applicationContext
            if (tts == null) create()
        }
    }

    private fun create() {
        val ctx = appContext ?: return
        ready = false
        val engine = TextToSpeech(ctx) { status ->
            synchronized(lock) {
                if (status == TextToSpeech.SUCCESS) {
                    ready = true
                    Log.i(TAG, "TTS engine connected")
                    flush()
                } else {
                    Log.w(TAG, "TTS init failed: $status")
                    failAll()
                }
            }
        }
        engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) {}
            override fun onDone(utteranceId: String?) {
                utteranceId?.let { callback?.invoke(it, "done") }
            }

            @Deprecated("Deprecated in Java")
            override fun onError(utteranceId: String?) {
                utteranceId?.let { callback?.invoke(it, "error") }
            }

            override fun onError(utteranceId: String?, errorCode: Int) {
                utteranceId?.let { callback?.invoke(it, "error") }
            }

            override fun onStop(utteranceId: String?, interrupted: Boolean) {
                utteranceId?.let { callback?.invoke(it, "stopped") }
            }
        })
        tts = engine
    }

    /** Queues the sentence. The result arrives through [callback] as done / error / stopped. */
    fun speak(req: Request) {
        synchronized(lock) {
            if (tts == null) create()
            if (!ready) {
                pending.addLast(req)
                return
            }
            speakNow(req)
        }
    }

    private fun speakNow(req: Request) {
        val engine = tts ?: return failOne(req)
        val wantHindi = req.lang.startsWith("hi")
        var text = req.text
        var locale = if (wantHindi) Locale("hi", "IN") else Locale("en", "IN")
        var res = engine.setLanguage(locale)
        if (res == TextToSpeech.LANG_MISSING_DATA || res == TextToSpeech.LANG_NOT_SUPPORTED) {
            // No Hindi voice installed: use Indian English, and plain-letter text instead of Devanagari.
            locale = Locale("en", "IN")
            res = engine.setLanguage(locale)
            if (DEVANAGARI.containsMatchIn(text) && !req.fallback.isNullOrBlank()) text = req.fallback
        }
        engine.setSpeechRate(req.rate)
        val r = engine.speak(text, TextToSpeech.QUEUE_ADD, Bundle(), req.id)
        if (r != TextToSpeech.SUCCESS) {
            Log.w(TAG, "speak failed ($r), reconnecting the TTS engine")
            try {
                engine.shutdown()
            } catch (_: Throwable) {
            }
            tts = null
            ready = false
            if (req.retries >= 1) return failOne(req)
            pending.addLast(req.copy(retries = req.retries + 1))
            create()
        }
    }

    private fun flush() {
        while (pending.isNotEmpty() && ready) speakNow(pending.removeFirst())
    }

    private fun failAll() {
        while (pending.isNotEmpty()) failOne(pending.removeFirst())
    }

    private fun failOne(req: Request) {
        callback?.invoke(req.id, "error")
    }

    fun stop() {
        synchronized(lock) {
            pending.clear()
            try {
                tts?.stop()
            } catch (_: Throwable) {
            }
        }
    }
}
