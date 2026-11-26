package com.qtcinfotech.jarvis.wake

import android.app.KeyguardManager
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors

/**
 * The conversation that runs when "Hey JARVIS" is heard while the app is closed, on the live voice line
 * (LiveVoice: Grok's realtime voice is the ears and the mouth; the JARVIS server stays the brain), exactly as the
 * open app does it. The phone's own speech recogniser (ConversationLoop) is the fallback when the line cannot open.
 *
 * Why: on this Xiaomi the phone's recogniser heard nothing after "Ji?" every single time (the log says "no speech
 * detected" for each closed-app wake), while the live line worked whenever the app was open.
 *
 * What the app does for a form the model filled is done here: the form goes to /assistant/live/tool/ with the
 * conversation state (the card waiting for a yes, the question just asked), and the server's line is handed back
 * to the model to read. A question JARVIS asked after a reminder or an alert ("Done, or remind you later?",
 * "Shall I tell Sir when you'll do it?") is answered the same way.
 */
class LiveLoop(private val context: Context, private val notifier: WakeNotifier) {
    private val main = Handler(Looper.getMainLooper())
    private val net = Executors.newSingleThreadExecutor()
    private val prefs = WakeBus.prefs(context)
    private val overlay = OverlayOrb(context) { end() }
    @Volatile var active = false
        private set
    /** The line could not open (no live voice on this server, no token, no network): the caller falls back. */
    @Volatile var onFailed: (() -> Unit)? = null

    // The conversation state the app keeps between forms: what was said last, the card waiting for a yes, the
    // question JARVIS is waiting on, and the recent exchange.
    private var previous = ""
    private var awaiting = ""
    private var pending: JSONObject? = null
    /** "Which one?" was asked: the items offered and what was to be done with them, so "dono" / "pehla wala" lands. */
    private var choices: JSONObject? = null
    private var about: JSONObject? = null
    /** The rest of a count-only offer ("Sir, 2 reminders. Shall I tell you?") while the first is being read and answered. */
    private var pendingOffer: JSONArray? = null
    private val history = ArrayList<JSONObject>()
    private var endAfterSpeech = false
    private var privateOk = true
    private val address: String get() = prefs.getString(WakeBus.KEY_ADDRESS, "Sir") ?: "Sir"
    private val addressHi: String get() = prefs.getString(WakeBus.KEY_ADDRESS_HI, address) ?: address
    private val DEVANAGARI = Regex("[\\u0900-\\u097F]")

    fun start() = begin(greet = true, about = null)

    /** A reminder or an alert was just spoken with its question: no greeting, the next words answer it. */
    fun startAbout(about: JSONObject) = begin(greet = false, about = about)

    /** He talked over something JARVIS was saying: no greeting, just listen to what he has to say. */
    fun startListening() = begin(greet = false, about = null)

    private fun begin(greet: Boolean, about: JSONObject?) {
        if (active) return
        active = true
        WakeBus.inConversation = true
        this.about = about
        pendingOffer = null
        previous = ""
        pending = null
        choices = null
        history.clear()
        endAfterSpeech = false
        awaiting = about?.let { "JARVIS asked: \"${it.optString("question")}\" about \"${it.optString("title")}\"" } ?: ""
        val km = context.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        privateOk = !km.isKeyguardLocked || prefs.getBoolean(WakeBus.KEY_SPEAK_LOCKED, false)
        overlay.show(about?.optString("question")?.ifEmpty { "…" } ?: "…", "thinking")
        notifier.showConversation("Listening…")
        LiveVoice.nativeHandler = { event, data -> onEvent(event, data) }
        net.execute {
            val cfg = Api.get(context, "/assistant/live/session/" + (if (privateOk) "" else "?private_ok=0"), 15000)
            val session = cfg?.optJSONObject("session")
            if (cfg == null || session == null || cfg.optString("url").isEmpty() || cfg.optString("token").isEmpty()) {
                Log.w(TAG, "live line: no session from the server; falling back")
                main.post { fail() }
                return@execute
            }
            val config = JSONObject()
                .put("url", cfg.optString("url"))
                .put("token", cfg.optString("token"))
                .put("sessionJson", session.toString())
                .put("rate", cfg.optInt("rate", 24000))
                .put("bargeIn", prefs.getBoolean(WakeBus.KEY_BARGE_IN, true))
                .put("bargeLevel", 1200)
                .put("idleSeconds", cfg.optInt("idle_seconds", 45))
                .put("maxSeconds", cfg.optInt("max_seconds", 14 * 60))
                .put("greeting", if (greet) cfg.optString("greeting") else "")
            val ok = try {
                LiveVoice.start(context, config)
            } catch (e: Throwable) {
                Log.w(TAG, "live line: could not start: ${e.message}")
                false
            }
            if (!ok) main.post { fail() }
        }
    }

    private fun fail() {
        if (!active) return
        val next = onFailed
        end()
        next?.invoke()
    }

    fun end() {
        if (!active) return
        active = false
        if (LiveVoice.nativeHandler != null) LiveVoice.nativeHandler = null
        WakeBus.inConversation = false  // "Hey JARVIS" listens again from this moment
        overlay.hide()
        main.post {
            try {
                if (LiveVoice.open) LiveVoice.stop("loop")
            } catch (_: Throwable) {
            }
            notifier.restoreServiceNotification()
        }
    }

    fun destroy() = end()

    // ------------------------------------------------------------------ what the line reports
    private fun onEvent(event: String, data: JSONObject) {
        if (!active) return
        when (event) {
            "state" -> when (data.optString("state")) {
                "open" -> overlay.update(null, "listening")
                "closed" -> {
                    val reason = data.optString("reason")
                    Log.i(TAG, "live line closed ($reason)")
                    if (reason == "failed" && history.isEmpty()) main.post { fail() } else end()
                }
            }
            "heard" -> if (data.optBoolean("final")) {
                val text = data.optString("text").trim()
                if (text.isNotEmpty()) {
                    WakeBus.notePresent()  // he spoke to JARVIS: he is here
                    history.add(JSONObject().put("role", "user").put("text", text))
                    notifier.showConversation("You: $text")
                    overlay.update("“$text”", "thinking")
                }
            }
            "said" -> {
                val text = data.optString("text").trim()
                if (text.isNotEmpty()) {
                    history.add(JSONObject().put("role", "jarvis").put("text", text))
                    notifier.showConversation("JARVIS: $text")
                    overlay.update(text, "speaking")
                }
            }
            "listening" -> if (data.optBoolean("on")) overlay.update(null, "listening")
            "speaking" -> if (!data.optBoolean("on") && endAfterSpeech) {
                endAfterSpeech = false
                end()
            }
            "level" -> overlay.level((20.0 * Math.log10(maxOf(1.0, data.optDouble("level", 0.0)) / 300.0)).toFloat())
            "tool" -> {
                val callId = data.optString("callId")
                val name = data.optString("name")
                val args = try { JSONObject(data.optString("arguments", "{}")) } catch (_: Throwable) { JSONObject() }
                overlay.update(null, "thinking")
                net.execute {
                    val out = try {
                        runTool(name, args)
                    } catch (e: Throwable) {
                        Log.w(TAG, "live tool failed: ${e.message}")
                        JSONObject().put("error", "Something went wrong.").toString()
                    }
                    LiveVoice.toolResult(callId, out)
                }
            }
            "error" -> Log.w(TAG, "live line: ${data.optString("message")}")
        }
    }

    // ------------------------------------------------------------------ a form the model filled
    private fun runTool(name: String, args: JSONObject): String {
        val said = args.optString("said").trim()
        val ab = about
        if (ab != null && ab.optString("kind") == "offer" && said.isNotEmpty()) {
            // The count was asked ("Sir, ek reminder hai. Kya main bataun?"): a yes reads the first one with its
            // question, a no brings them all back at the usual "later" time; anything else is a sentence of its own.
            when (yesOrNo(said)) {
                true -> return readOffered(ab)
                false -> return leaveOffered(ab)
                null -> {
                    about = null
                    pendingOffer = null
                    awaiting = ""
                }
            }
        }
        if (ab != null && ab.optString("id").isNotEmpty() && ab.optString("qkind") in FREE_ANSWERS && said.isNotEmpty()) {
            // "Kal shaam tak" / "Sunil ko de do": free words for a question about work between people; the server
            // reads them as before and what it answers is what the voice says.
            val r = Api.post(context, "/alerts/${ab.optString("id")}/answer/", JSONObject().put("text", said))
            if (r != null && r.optBoolean("handled")) {
                val ask = r.optJSONObject("ask")
                if (ask != null) {
                    about = JSONObject(ab.toString()).put("question", ask.optString("en")).put("qkind", ask.optString("kind"))
                    awaiting = "JARVIS asked: \"${ask.optString("en")}\" about \"${ab.optString("title")}\""
                } else {
                    about = null
                    awaiting = ""
                }
                val say = (if (r.optString("lang") == "hi") r.optString("say") else "").ifBlank { r.optString("show") }.ifBlank { "Ji." }
                return JSONObject().put("say", say).toString()
            }
            about = null  // about something else: the sentence is understood as itself below
            awaiting = ""
        }
        val body = JSONObject()
            .put("name", name)
            .put("arguments", args)
            .put("previous", previous)
            .put("awaiting", awaiting)
            .put("history", JSONArray(history.takeLast(12)))
            .put("private_ok", privateOk)
        pending?.let { body.put("pending", it) }
        choices?.let { body.put("choices", it) }
        val draft = Api.post(context, "/assistant/live/tool/", body, 40000)
            ?: return JSONObject().put("error", "I couldn't reach the server.").toString()
        val action = draft.optString("action")
        // The same bookkeeping as the app: a question keeps what was said for the answer; a card that waits for a yes
        // stays pending until it is saved or dropped; anything settled clears the slate.
        choices = null
        if (draft.optBoolean("needs_clarification") || action == "choose_destination") {
            previous = draft.optString("transcript", previous)
            pending = if (action in CONFIRMABLE || action == "choose_destination") slim(draft) else null
            awaiting = ""
            // "Which one?": remember the options so the answer ("dono", "the franchise one") is matched against them.
            val intent = when (action) {
                "complete" -> "complete_task"
                "arrange_request" -> "change_task"
                "accept_request", "decline_request", "change_task", "delete_task" -> action
                else -> ""
            }
            val candidates = draft.optJSONArray("target_candidates")
            if (intent.isNotEmpty() && candidates != null && candidates.length() > 0) {
                val items = JSONArray()
                for (i in 0 until minOf(candidates.length(), 10)) {
                    val c = candidates.optJSONObject(i) ?: continue
                    items.put(JSONObject().put("id", c.optString("id")).put("title", c.optString("title")))
                }
                choices = JSONObject().put("intent", intent).put("items", items)
            }
        } else if (action == "confirm_pending" || action == "cancel_pending" || draft.optBoolean("card_done")
            || (draft.optBoolean("booked") && !draft.optBoolean("keep_card"))) {
            // keep_card: something was saved while a clash question is still open (several things in one breath):
            // the question's card stays, and the answer still goes to it.
            previous = ""
            pending = null
            awaiting = ""
        } else if (action in CONFIRMABLE) {
            previous = ""
            pending = slim(draft)
            awaiting = ""  // the server describes the waiting card from `pending`
        } else {
            previous = ""
            pending = null
            awaiting = ""
        }
        if (action != "answer" && action != "ask") about = null
        if (action == "end") endAfterSpeech = true
        var say = draft.optString("say").trim()
        // One of the offered read and answered: the next is counted again (never read unasked).
        val more = pendingOffer
        if (about == null && more != null && more.length() > 0 && action != "end") {
            val line = moreLine(more.length())
            about = JSONObject().put("kind", "offer").put("question", line).put("title", "").put("items", more)
            pendingOffer = null
            awaiting = "JARVIS asked: \"$line\""
            say = "$say $line".trim()
        }
        return (if (say.isNotEmpty()) JSONObject().put("say", say) else JSONObject().put("ok", true)).toString()
    }

    // ------------------------------------------------------------------ the count-only offer (his rule, 26 Nov 2026)
    private fun yesOrNo(said: String): Boolean? {
        val s = said.lowercase()
        if (NO.containsMatchIn(s)) return false
        if (YES.containsMatchIn(s)) return true
        return null
    }

    private fun hindiNow(): Boolean = prefs.getString(WakeBus.KEY_REPLY_LANG, "") == "hi"

    private fun moreLine(n: Int): String = if (hindiNow()) {
        if (n > 1) "$addressHi, $n और हैं। क्या मैं बताऊँ?" else "$addressHi, एक और है। क्या मैं बताऊँ?"
    } else {
        if (n > 1) "$address, $n more. Shall I tell you?" else "$address, one more. Shall I tell you?"
    }

    /** "Haan": the first offered item is read with its question (the next words answer it); the rest wait. */
    private fun readOffered(offer: JSONObject): String {
        val items = offer.optJSONArray("items") ?: JSONArray()
        val first = items.optJSONObject(0)
        if (first == null) {
            about = null
            pendingOffer = null
            awaiting = ""
            return JSONObject().put("say", if (hindiNow()) "$addressHi, कुछ भी बाकी नहीं है।" else "$address, nothing is waiting.").toString()
        }
        val rest = JSONArray()
        for (i in 1 until items.length()) items.optJSONObject(i)?.let { rest.put(it) }
        pendingOffer = if (rest.length() > 0) rest else null
        val q = first.optJSONObject("question")
        val spoken = first.optString("spoken").ifBlank { first.optString("title") }
        val hindi = DEVANAGARI.containsMatchIn(spoken)
        val question = if (q != null) (if (hindi) q.optString("hi") else q.optString("en")) else ""
        if (q != null) {
            about = JSONObject(first.toString()).put("question", q.optString("en")).put("qkind", q.optString("kind"))
            awaiting = "JARVIS asked: \"${q.optString("en")}\" about \"${first.optString("title")}\""
        } else {
            about = null
            awaiting = ""
            // Nothing to answer on this one: the next is counted straight after it.
            val more = pendingOffer
            if (more != null) {
                val line = moreLine(more.length())
                about = JSONObject().put("kind", "offer").put("question", line).put("title", "").put("items", more)
                pendingOffer = null
                awaiting = "JARVIS asked: \"$line\""
                return JSONObject().put("say", "$spoken $line".trim()).toString()
            }
        }
        return JSONObject().put("say", "$spoken $question".trim()).toString()
    }

    /** "Nahi": left alone now, and back at the usual "later" time, announced the same way. */
    private fun leaveOffered(offer: JSONObject): String {
        val items = offer.optJSONArray("items") ?: JSONArray()
        var until = ""
        var untilHi = ""
        for (i in 0 until items.length()) {
            val it = items.optJSONObject(i) ?: continue
            val body = JSONObject()
            if (it.optString("key").isNotEmpty()) body.put("key", it.optString("key").substringBefore(":s"))
            else if (it.optString("id").isNotEmpty()) body.put("alert_id", it.optString("id"))
            else continue
            val r = Api.post(context, "/reminders/snooze/", body, 15000) ?: continue
            if (until.isEmpty()) {
                until = r.optString("until_label")
                untilHi = r.optString("until_label_hi")
            }
        }
        about = null
        pendingOffer = null
        awaiting = ""
        val say = if (hindiNow()) (if (untilHi.isNotBlank()) "ठीक है, $untilHi फिर बताऊँगा।" else "ठीक है, बाद में फिर बताऊँगा।")
                  else (if (until.isNotBlank()) "Okay, I'll bring it up again $until." else "Okay, I'll bring it up again later.")
        return JSONObject().put("say", say).toString()
    }

    /** The waiting card without its long option lists (the server only needs what it says and when). */
    private fun slim(draft: JSONObject): JSONObject {
        val copy = JSONObject(draft.toString())
        for (k in listOf("categories", "staff_candidates", "target_candidates", "say", "say_lang", "tool", "view")) copy.remove(k)
        return copy
    }

    companion object {
        private const val TAG = "JarvisWake"
        /** Questions answered with free words (core/promises.py), not a yes or a no. */
        private val FREE_ANSWERS = setOf("promise", "unseen", "reassign")
        /** A yes or a no to the count-only offer (Hinglish, English, Hindi); anything else is a sentence of its own. */
        private val YES = Regex("^\\W*(haan|han|ha|haanji|hanji|ji|yes|yeah|ok|okay|sure|batao|bataiye|bata do|bolo|boliye|sunao|sunaiye|padho|padhiye|theek hai|thik hai|हाँ|हां|जी|बताओ|बताइए|बता दो|बोलो|सुनाओ|सुनाइए|पढ़ो|ठीक है)\\b")
        private val NO = Regex("^\\W*(nahi|nhi|nahin|no|nope|not now|baad mein|baad me|rehne do|rahne do|chhodo|chhod do|abhi nahi|नहीं|नही|बाद में|रहने दो|छोड़ो|अभी नहीं)\\b")
        /** Drafts that become a card waiting for a yes (src/lib/drafts.ts CONFIRMABLE). */
        private val CONFIRMABLE = setOf("private_task", "md_request", "delegation", "team_delegation", "pa_task", "pa_meeting",
            "pa_self_task", "mg_task", "person_task", "person_meeting", "own_task",
            "message", "accept_request", "decline_request", "complete", "remember", "change_task", "arrange_request", "delete_task", "organise")

        /** The live line is possible on this build (the server decides the rest when the session is asked for). */
        fun available(context: Context): Boolean =
            WakeBus.prefs(context).getBoolean(WakeBus.KEY_ENABLED, false) && !LiveVoice.open
    }
}
