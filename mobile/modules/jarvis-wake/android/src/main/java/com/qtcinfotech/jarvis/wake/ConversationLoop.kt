package com.qtcinfotech.jarvis.wake

import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Log
import org.json.JSONObject
import java.util.concurrent.Executors

/**
 * The voice-only conversation that runs when "Hey JARVIS" is heard while the app is closed (phone locked or not):
 *
 *   greet -> listen -> send what he said to the server (which thinks AND does the work) -> speak the reply
 *   -> listen again ... until he says "bas" / "stop", or stays quiet.
 *
 * The screen never has to come up. The server keeps the conversation memory; this class only hears and speaks.
 */
class ConversationLoop(private val context: Context, private val notifier: WakeNotifier) {
    private val main = Handler(Looper.getMainLooper())
    private val net = Executors.newSingleThreadExecutor()
    private var recognizer: SpeechRecognizer? = null
    @Volatile var active = false
        private set
    private var silenceRounds = 0
    private var retries = 0
    private val prefs = WakeBus.prefs(context)
    private val address: String get() = prefs.getString(WakeBus.KEY_ADDRESS, "Sir") ?: "Sir"
    private val isMd: Boolean get() = address == "Sir"
    private val overlay = OverlayOrb(context) { end() }

    fun start() {
        if (active) return
        active = true
        WakeBus.inConversation = true
        silenceRounds = 0
        retries = 0
        overlay.show("…", "thinking")
        main.post {
            ensureRecognizer()
            send("", "wake")
        }
    }

    fun end() {
        if (!active) return
        active = false
        WakeBus.inConversation = false
        overlay.hide()
        main.post {
            try {
                recognizer?.cancel()
            } catch (_: Throwable) {
            }
            notifier.restoreServiceNotification()
        }
    }

    fun destroy() {
        end()
        main.post {
            try {
                recognizer?.destroy()
            } catch (_: Throwable) {
            }
            recognizer = null
        }
    }

    // ------------------------------------------------------------------ server
    private fun send(text: String, event: String = "") {
        if (!active) return
        notifier.showConversation(if (text.isEmpty()) "Listening…" else "You: $text")
        overlay.update(if (text.isEmpty()) null else "“$text”", "thinking")
        net.execute {
            val km = context.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
            val locked = km.isKeyguardLocked
            val body = JSONObject()
                .put("text", text)
                .put("event", event)
                .put("locked", locked)
                .put("private_ok", !locked || prefs.getBoolean(WakeBus.KEY_SPEAK_LOCKED, false))
            val resp = Api.post(context, "/assistant/turn/", body)
            main.post { handle(resp) }
        }
    }

    private fun handle(resp: JSONObject?) {
        if (!active) return
        if (resp == null) {
            speakThen("Sorry $address, I couldn't reach the server.", null, "en-IN") { end() }
            return
        }
        val show = resp.optString("show")
        val say = resp.optString("say", show)
        val lang = if (resp.optString("say_lang") == "hi") "hi-IN" else "hi-IN" // Indian voice for both, like the app
        val listen = resp.optBoolean("listen", true)
        if (show.isNotBlank()) {
            notifier.showConversation("JARVIS: $show")
            overlay.update(show, "speaking")
        }
        speakThen(say, resp.optString("fallback", show), lang) {
            if (active && listen) listen() else end()
        }
    }

    private fun speakThen(text: String, fallback: String?, lang: String, onDone: () -> Unit) {
        if (text.isBlank()) {
            onDone()
            return
        }
        TtsEngine.speakThen(TtsEngine.Request("conv-" + System.nanoTime(), text, fallback, lang, 0.95f)) {
            main.post(onDone)
        }
    }

    // ------------------------------------------------------------------ hearing
    private fun ensureRecognizer() {
        if (recognizer != null) return
        recognizer = SpeechRecognizer.createSpeechRecognizer(context).apply { setRecognitionListener(listener) }
    }

    private fun listen() {
        if (!active) return
        main.post {
            ensureRecognizer()
            notifier.showConversation("Listening…")
            overlay.update(null, "listening")
            try {
                recognizer?.startListening(intent())
            } catch (e: Throwable) {
                Log.w(TAG, "startListening failed: ${e.message}")
                end()
            }
        }
    }

    private fun intent(): Intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
        putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-IN") // Indian English understands Hinglish too
        putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
        putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
        putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false)
        putExtra("android.speech.extras.SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS", 1200L)
        putExtra("android.speech.extras.SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS", 1200L)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            putStringArrayListExtra(RecognizerIntent.EXTRA_BIASING_STRINGS, arrayListOf("JARVIS", "Jarvis", "Mansi", "Sir"))
        }
    }

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onPartialResults(partialResults: Bundle?) {}
        override fun onEvent(eventType: Int, params: Bundle?) {}

        override fun onResults(results: Bundle?) {
            val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()?.trim().orEmpty()
            if (text.isEmpty()) {
                silence()
                return
            }
            silenceRounds = 0
            retries = 0
            Log.i(TAG, "heard: $text")
            send(text)
        }

        override fun onError(error: Int) {
            if (!active) return
            when (error) {
                SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> silence()
                SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> end()
                SpeechRecognizer.ERROR_RECOGNIZER_BUSY, SpeechRecognizer.ERROR_CLIENT -> {
                    if (retries++ < 3) main.postDelayed({ listen() }, 400) else end()
                }
                else -> {
                    Log.w(TAG, "recognizer error $error")
                    if (retries++ < 2) main.postDelayed({ listen() }, 600)
                    else speakThen("Sorry $address, I can't hear right now. Please try again in a moment.", null, "en-IN") { end() }
                }
            }
        }
    }

    /** Quiet rounds: re-listen twice, nudge once, re-listen twice more, then rest (the wake word brings JARVIS back). */
    private fun silence() {
        if (!active) return
        silenceRounds += 1
        when {
            silenceRounds < 2 -> listen()
            silenceRounds == 2 -> speakThen(if (isMd) "सर?" else "$address?", "$address?", "hi-IN") { listen() }
            silenceRounds < 5 -> listen()
            else -> end()
        }
    }

    companion object {
        private const val TAG = "JarvisWake"
        fun available(context: Context): Boolean = SpeechRecognizer.isRecognitionAvailable(context)
    }
}
