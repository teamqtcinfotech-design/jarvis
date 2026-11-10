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
    /** JARVIS asked something (or was just woken) and is waiting; false after a plain answer or "saved". */
    private var patient = true
    /** Changes every time listening starts or ends, so a late safety timer knows it is out of date. */
    private var listenRound = 0
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
        patient = true
        overlay.show("…", "thinking")
        main.post {
            ensureRecognizer()
            send("", "wake")
        }
    }

    fun end() {
        if (!active) return
        active = false
        WakeBus.inConversation = false  // "Hey JARVIS" listens again from this moment
        overlay.hide()
        main.post {
            listenRound += 1
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
        // After a plain answer or a "saved", one short listen for a follow-up is enough; then the microphone goes
        // straight back to "Hey JARVIS". Only a question that is waiting for him gets the longer wait.
        patient = resp.optString("action") !in RESTFUL
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
            if (!active) return@post
            ensureRecognizer()
            notifier.showConversation("Listening…")
            overlay.update(null, "listening")
            try {
                recognizer?.startListening(intent())
            } catch (e: Throwable) {
                Log.w(TAG, "startListening failed: ${e.message}")
                end()
                return@post
            }
            armSafetyTimer(LISTEN_TIMEOUT_MS)
        }
    }

    /**
     * Android's speech service sometimes never answers (it was killed, or never got the microphone). Without a
     * timer the conversation would wait for ever and "Hey JARVIS" would stay deaf. Each call replaces the last.
     */
    private fun armSafetyTimer(ms: Long) {
        val round = ++listenRound
        main.postDelayed({
            if (active && round == listenRound) {
                Log.w(TAG, "speech service did not answer; giving the microphone back")
                try {
                    recognizer?.destroy()
                } catch (_: Throwable) {
                }
                recognizer = null  // a fresh one next time
                end()
            }
        }, ms)
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
        override fun onBeginningOfSpeech() {
            if (active) armSafetyTimer(SPEECH_TIMEOUT_MS)  // he is talking: give a long sentence its time
        }
        override fun onRmsChanged(rmsdB: Float) {
            overlay.level(rmsdB)  // the waves on screen move with his voice
        }
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onPartialResults(partialResults: Bundle?) {}
        override fun onEvent(eventType: Int, params: Bundle?) {}

        override fun onResults(results: Bundle?) {
            listenRound += 1  // answered: the safety timer for this round is void
            if (!active) return
            val heard = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()?.trim().orEmpty()
            if (heard.isEmpty()) {
                silence()
                return
            }
            silenceRounds = 0
            retries = 0
            Log.i(TAG, "heard: $heard")
            // He said "Hey JARVIS" while JARVIS was already listening: that is a call, not an instruction.
            val text = WAKE_PHRASE.replaceFirst(heard, "").trim()
            if (text.isEmpty()) {
                patient = true
                speakThen(if (isMd) "जी सर?" else "जी?", "Ji $address?", "hi-IN") { listen() }
                return
            }
            send(text)
        }

        override fun onError(error: Int) {
            listenRound += 1
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

    /**
     * He said nothing. After a plain answer: rest at once, so "Hey JARVIS" is listening again within a second.
     * While a question is waiting for him: listen once more, ask "Sir?" once, listen, then rest.
     */
    private fun silence() {
        if (!active) return
        silenceRounds += 1
        when {
            !patient -> end()
            silenceRounds < 2 -> listen()
            silenceRounds == 2 -> speakThen(if (isMd) "सर?" else "$address?", "$address?", "hi-IN") { listen() }
            else -> end()
        }
    }

    companion object {
        private const val TAG = "JarvisWake"
        private const val LISTEN_TIMEOUT_MS = 15_000L   // nothing heard and no word from the speech service
        private const val SPEECH_TIMEOUT_MS = 30_000L   // he started talking: the longest one sentence may take
        /** Replies after which nothing is being asked of him. */
        private val RESTFUL = setOf("answer", "saved", "setting_applied", "dismiss", "briefing", "read_missed")
        /** "Hey JARVIS" as the phone's speech recogniser tends to write it, at the start of what was heard. */
        private val WAKE_PHRASE = Regex(
            "^\\s*(?:(?:hey|hi|hello|ok|okay|he|hay|hai|a|oh|हे|हाय|हेलो)[\\s,]+)?(?:jarvis|jarwis|jarvish|jervis|jarves|javis|jarbis|जार्विस|जारविस)[\\s,.!?]*",
            RegexOption.IGNORE_CASE,
        )
        fun available(context: Context): Boolean = SpeechRecognizer.isRecognitionAvailable(context)
    }
}
