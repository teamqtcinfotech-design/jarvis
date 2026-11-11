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
    /** He has said something real in this conversation. Until then a quiet room ends it silently (a false wake). */
    private var heardAnything = false
    /** Changes every time listening starts or ends, so a late safety timer knows it is out of date. */
    private var listenRound = 0
    private val prefs = WakeBus.prefs(context)
    private val address: String get() = prefs.getString(WakeBus.KEY_ADDRESS, "Sir") ?: "Sir"
    /** The same word in Devanagari ("सर", "मैम") for the Hindi lines. */
    private val addressHi: String get() = prefs.getString(WakeBus.KEY_ADDRESS_HI, address) ?: address
    private val isMd: Boolean get() = prefs.getBoolean(WakeBus.KEY_IS_MD, address == "Sir")
    /** The phone voice for English lines (the app's "JARVIS voice" setting); Hindi lines always use the Hindi voice. */
    private val accent: String get() = prefs.getString(WakeBus.KEY_VOICE_ACCENT, "hi-IN") ?: "hi-IN"
    /** The language JARVIS is speaking to him: the server says so with every reply; before the first, the app's last word. */
    private var replyLang: String = prefs.getString(WakeBus.KEY_REPLY_LANG, "en") ?: "en"
    private val overlay = OverlayOrb(context) { end() }

    fun start() {
        if (active) return
        active = true
        WakeBus.inConversation = true
        silenceRounds = 0
        retries = 0
        patient = true
        heardAnything = false
        overlay.show("…", "thinking")
        main.post {
            ensureRecognizer()
            send("", "wake")
        }
    }

    /**
     * A reminder or an alert was just spoken with its question ("Done, or remind you later?"): no greeting, just
     * listen for the answer. The server is told what was asked, so "ho gaya" / "baad mein" / "haan" land on it.
     */
    fun startAbout(about: JSONObject) {
        if (active) return
        active = true
        WakeBus.inConversation = true
        silenceRounds = 0
        retries = 0
        patient = true
        heardAnything = true  // a quiet room after a question is "no answer", not a false wake
        overlay.show(about.optString("question").ifEmpty { "…" }, "listening")
        main.post {
            ensureRecognizer()
            send("", "ask", about)
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
    private fun send(text: String, event: String = "", about: JSONObject? = null) {
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
            if (about != null) body.put("about", about)
            val resp = Api.post(context, "/assistant/turn/", body)
            main.post { handle(resp) }
        }
    }

    private fun handle(resp: JSONObject?) {
        if (!active) return
        if (resp == null) {
            sayOwn("Sorry $address, I couldn't reach the server.", "माफ़ कीजिए $addressHi, सर्वर से संपर्क नहीं हो पाया।") { end() }
            return
        }
        val show = resp.optString("show")
        val say = resp.optString("say", show)
        val hindi = resp.optString("say_lang") == "hi"
        replyLang = if (hindi) "hi" else "en"
        val lang = if (hindi) "hi-IN" else accent  // the Hindi voice for Hindi lines; the chosen accent for English ones
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

    /** One of the few lines this loop builds itself, in the language JARVIS is speaking to him right now. */
    private fun sayOwn(en: String, hi: String, onDone: () -> Unit) {
        if (replyLang == "hi") speakThen(hi, en, "hi-IN", onDone) else speakThen(en, null, accent, onDone)
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
        // The app's listening setting: Indian English (understands Hinglish too), or Hindi for pure-Hindi speakers.
        putExtra(RecognizerIntent.EXTRA_LANGUAGE, prefs.getString(WakeBus.KEY_LISTEN_LANG, "en-IN") ?: "en-IN")
        putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
        putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
        putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false)
        putExtra("android.speech.extras.SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS", 1200L)
        putExtra("android.speech.extras.SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS", 1200L)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            // Names it should expect: JARVIS, the two of them, the staff and the contacts (so "Mansi" is not "Mercy").
            val names = linkedSetOf("JARVIS", "Jarvis", address)
            prefs.getString(WakeBus.KEY_PA_NAME, null)?.takeIf { it.isNotBlank() }?.let { names.add(it) }
            prefs.getString(WakeBus.KEY_HINTS, null)?.split("\n")?.map { it.trim() }?.filter { it.isNotEmpty() }?.let { names.addAll(it) }
            putStringArrayListExtra(RecognizerIntent.EXTRA_BIASING_STRINGS, ArrayList(names.take(60)))
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
                speakThen(if (isMd) "जी $addressHi?" else "जी?", "Ji $address?", "hi-IN") { listen() }
                return
            }
            // A stray word from the room right after a wake ("okay", "haan", "hmm") is not an instruction: the
            // wake was most likely false. Close quietly rather than answer it.
            if (!heardAnything && isFiller(text)) {
                Log.i(TAG, "only a stray word after the wake; closing quietly")
                WakeBus.countEmptyWake(context)
                end()
                return
            }
            heardAnything = true
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
                    else sayOwn("Sorry $address, I can't hear right now. Please try again in a moment.",
                        "माफ़ कीजिए $addressHi, अभी सुन नहीं पा रहा। थोड़ी देर में फिर कोशिश कीजिए।") { end() }
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
            // Woken, greeted, and nothing said: most likely a false wake. Two quiet rounds, then close without a word.
            !heardAnything -> if (silenceRounds < 2) listen() else {
                WakeBus.countEmptyWake(context)
                end()
            }
            silenceRounds < 2 -> listen()
            silenceRounds == 2 -> speakThen(if (isMd) "$addressHi?" else "$address?", "$address?", "hi-IN") { listen() }
            else -> end()
        }
    }

    /** One or two words that carry no instruction ("okay", "haan ji", "hmm"). */
    private fun isFiller(text: String): Boolean {
        val words = text.lowercase().replace(Regex("[^\\p{L}\\p{N} ]"), " ").split(Regex("\\s+")).filter { it.isNotBlank() }
        return words.isNotEmpty() && words.size <= 2 && words.all { it in FILLER }
    }

    companion object {
        private const val TAG = "JarvisWake"
        private const val LISTEN_TIMEOUT_MS = 15_000L   // nothing heard and no word from the speech service
        private const val SPEECH_TIMEOUT_MS = 30_000L   // he started talking: the longest one sentence may take
        /** Replies after which nothing is being asked of him. */
        private val RESTFUL = setOf("answer", "saved", "setting_applied", "dismiss", "briefing", "read_missed", "snoozed")
        /** Words the room says on its own; never an instruction when they are all that was heard after a wake. */
        private val FILLER = setOf("ok", "okay", "k", "haan", "ha", "han", "hmm", "hm", "mm", "yes", "no", "nah", "hello", "hi", "hey",
            "achha", "accha", "acha", "theek", "thik", "hai", "ji", "kya", "what", "oh", "ah", "um", "uh", "yeah", "yep", "huh", "so",
            "ok ok", "ठीक", "हाँ", "हां", "जी", "अच्छा", "क्या", "हैं", "है", "नहीं")
        /** "Hey JARVIS" as the phone's speech recogniser tends to write it, at the start of what was heard. */
        private val WAKE_PHRASE = Regex(
            "^\\s*(?:(?:hey|hi|hello|ok|okay|he|hay|hai|a|oh|हे|हाय|हेलो)[\\s,]+)?(?:jarvis|jarwis|jarvish|jervis|jarves|javis|jarbis|जार्विस|जारविस)[\\s,.!?]*",
            RegexOption.IGNORE_CASE,
        )
        fun available(context: Context): Boolean = SpeechRecognizer.isRecognitionAvailable(context)
    }
}
