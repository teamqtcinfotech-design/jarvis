package com.qtcinfotech.jarvis.wake

import android.app.KeyguardManager
import android.content.Context
import android.os.PowerManager
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Locale

/**
 * While the app is closed, checks the server every half minute for reminders that are due right now and
 * messages that just arrived, and speaks or shows them:
 *
 *  - screen on and unlocked  -> spoken aloud (it is him: the phone is unlocked)
 *  - locked                  -> notification; spoken only if "speak when locked" is on, and a private
 *                               reminder is then only "you have a private reminder"
 *  - app open on screen      -> nothing here; the app's own alert engine handles it
 *
 * Told once: whatever this speaks or shows is reported to the server ("told"), and whatever the server says was
 * told already (by the app, or by this part earlier) is skipped. So the same thing is never announced twice, by
 * either side. Several things due at the same moment are said as one sentence.
 *
 * It has its own login (device token) so its refreshes never clash with the app's.
 */
class ReminderWatcher(private val context: Context, private val notifier: WakeNotifier) {
    private val prefs = WakeBus.prefs(context)
    private val DEVANAGARI = Regex("[\\u0900-\\u097F]")

    /**
     * One thing to announce: its server key (for "told"), what to show and what to say, and the question JARVIS
     * asks right after it (a contract from the server: kind, en, hi), if any.
     */
    private class Item(val key: String, val reminderKey: String?, val alertId: String?, val title: String,
                       val spoken: String, val isPrivate: Boolean, val urgent: Boolean, val question: JSONObject? = null)

    /** Set by the service: after a spoken item with a question, listen for the answer (the voice-only conversation). */
    @Volatile var onAsk: ((JSONObject) -> Unit)? = null

    init {
        TtsEngine.init(context)
    }

    /** Returns false when the login has expired for good (the service should stop and let the app re-issue it). */
    fun check(): Boolean {
        if (WakeBus.appInForeground || WakeBus.inConversation) return true
        // The next hour's list is kept on the phone: with no connection, the reminders still fire from this copy.
        val fetched = Api.get(context, "/reminders/?hours=1")
        if (fetched != null) {
            prefs.edit().putString(WakeBus.KEY_REMINDERS_CACHE, fetched.toString()).putLong(WakeBus.KEY_REMINDERS_CACHED_AT, System.currentTimeMillis()).apply()
        } else if (Api.tokensDead) {
            return false
        }
        val reminders = fetched ?: cachedReminders() ?: return true
        val alerts = if (fetched != null) Api.get(context, "/alerts/") else null
        val nowSec = System.currentTimeMillis() / 1000
        val spoken = prefs.getStringSet(WakeBus.KEY_SPOKEN, emptySet())?.toMutableSet() ?: mutableSetOf()
        val due = ArrayList<Item>()
        val quiet = ArrayList<Item>()   // told to the server, never announced (small news, or told already)
        val missed = ArrayList<Item>()  // its time passed while the phone was off: shown, not spoken, kept for the app

        val list = reminders.optJSONArray("reminders")
        if (list != null) {
            val soon = ArrayList<String>()
            for (i in 0 until list.length()) {
                val r = list.getJSONObject(i)
                val key = r.optString("key")
                if (key.isEmpty() || key in spoken) continue
                val fireAtSec = parseIsoSeconds(r.optString("fire_at")) ?: continue
                val age = nowSec - fireAtSec
                // Due within the hour: fetch its voice now, so it speaks instantly (and even offline) at its time.
                if (age < 0 && age > -3600) soon.add(TtsEngine.forSpeech(r.optString("spoken")))  // as it will be spoken
                if (age < 0) continue             // not due yet
                if (!r.isNull("told_at")) {       // the app said it already: remember, do not repeat
                    spoken.add(key)
                    continue
                }
                val item = Item(key, key, null, r.optString("title"), r.optString("spoken"), r.optBoolean("is_private"),
                                r.optString("priority") == "URGENT", r.optJSONObject("question"))
                // Older than a few minutes: the phone was off (or asleep) at its time. A notification still goes up,
                // and it is NOT reported as told, so the app offers it among the held ones when it is next opened.
                if (age > 180) missed.add(item) else due.add(item)
            }
            if (soon.isNotEmpty()) GrokVoice.prefetch(context, soon)
        }
        val results = alerts?.optJSONArray("results")
        if (results != null) {
            for (i in 0 until results.length()) {
                val a = results.getJSONObject(i)
                val id = a.optString("id")
                val key = a.optString("announce_key").ifEmpty { "alert:$id" }  // a snoozed alert comes back as new
                if (id.isEmpty() || key in spoken) continue
                if (!a.optBoolean("fresh", true)) {  // told already, or old news: it stays in the lists only
                    spoken.add(key)
                    continue
                }
                val item = Item(key, null, id, a.optString("title"), a.optString("spoken_text"), false,
                                a.optString("priority") == "URGENT", a.optJSONObject("question"))
                if (a.optString("priority") == "LOW") quiet.add(item) else due.add(item)
            }
        }
        if (due.isNotEmpty()) announce(due)
        for (item in missed) {
            notifier.show(item.key, if (item.isPrivate) "JARVIS" else "JARVIS · Missed reminder",
                if (item.isPrivate) "You have a private reminder. Open JARVIS to see it." else item.title, item.urgent, silentSound())
            spoken.add(item.key)
        }
        for (item in due + quiet) spoken.add(item.key)
        if (due.isNotEmpty() || quiet.isNotEmpty()) tell(due + quiet)
        if (spoken.isNotEmpty()) {
            val trimmed = if (spoken.size > 300) spoken.toList().takeLast(300).toSet() else spoken
            prefs.edit().putStringSet(WakeBus.KEY_SPOKEN, trimmed).apply()
        }
        return true
    }

    /**
     * A live push (see PushService): show and speak it now, and remember it so the next check does not repeat it.
     * An alert comes with its id in the key ("alert:<id>"); a reminder comes with its own key (reminderKey).
     */
    fun deliverPush(key: String, title: String, spoken: String, urgent: Boolean, priority: String = "NORMAL",
                    question: JSONObject? = null, reminderKey: String? = null, isPrivate: Boolean = false) {
        val spokenKeys = prefs.getStringSet(WakeBus.KEY_SPOKEN, emptySet())?.toMutableSet() ?: mutableSetOf()
        if (key in spokenKeys) return
        val alertId = if (reminderKey == null) key.removePrefix("alert:").substringBefore(":s") else null
        val item = Item(key, reminderKey, alertId, title, spoken, isPrivate, urgent, question)
        if (!WakeBus.inConversation && priority != "LOW") announce(listOf(item))
        spokenKeys.add(key)
        prefs.edit().putStringSet(WakeBus.KEY_SPOKEN, spokenKeys).apply()
        tell(listOf(item))
    }

    private fun silentSound(): Boolean = prefs.getString(WakeBus.KEY_REMINDER_SOUND, "default") == "silent"

    /** The last list fetched, if it is less than an hour old (older than that, its items have all come and gone). */
    private fun cachedReminders(): JSONObject? {
        val at = prefs.getLong(WakeBus.KEY_REMINDERS_CACHED_AT, 0L)
        if (at == 0L || System.currentTimeMillis() - at > 60 * 60 * 1000L) return null
        val raw = prefs.getString(WakeBus.KEY_REMINDERS_CACHE, null) ?: return null
        return try { JSONObject(raw) } catch (_: Throwable) { null }
    }

    /** What the voice-only conversation needs to take the answer to this item's question. */
    private fun aboutOf(item: Item): JSONObject = JSONObject()
        .put("kind", if (item.reminderKey != null) "reminder" else "alert")
        .put("key", item.reminderKey ?: "")
        .put("id", item.alertId ?: "")
        .put("title", item.title)
        .put("question", item.question?.optString("en") ?: "")

    /** Tell the server these were spoken or shown, so the app (or this part later) never repeats them. */
    private fun tell(items: List<Item>) {
        val keys = JSONArray()
        val ids = JSONArray()
        for (item in items) {
            item.reminderKey?.let { keys.put(it) }
            item.alertId?.let { ids.put(it) }
        }
        if (keys.length() == 0 && ids.length() == 0) return
        Api.post(context, "/told/", JSONObject().put("keys", keys).put("ids", ids), 15000)
    }

    /** "2026-11-04T11:00:00+05:30" (with or without fractional seconds, or a trailing Z) -> epoch seconds. */
    private fun parseIsoSeconds(iso: String): Long? {
        if (iso.isBlank()) return null
        val cleaned = iso.replace(Regex("\\.\\d+"), "").replace("Z", "+00:00")
        return try {
            SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.US).parse(cleaned)?.time?.div(1000)
        } catch (_: Exception) {
            null
        }
    }

    /**
     * Show each as a notification; say them all as ONE sentence (the address once), by the unlocked / locked rules.
     * The lines come from the server already in the reader's language (Hindi or English); the wrapper follows them.
     */
    private fun announce(items: List<Item>) {
        val address = prefs.getString(WakeBus.KEY_ADDRESS, "Sir") ?: "Sir"
        val addressHi = prefs.getString(WakeBus.KEY_ADDRESS_HI, address) ?: address
        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        val km = context.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        val unlocked = pm.isInteractive && !km.isKeyguardLocked
        val speakLocked = prefs.getBoolean(WakeBus.KEY_SPEAK_LOCKED, false)
        val lines = ArrayList<String>()
        var ask: Item? = null  // the one item whose question JARVIS asks aloud, then listens for the answer
        for (item in items) {
            notifier.show(item.key, if (item.isPrivate) "JARVIS" else "JARVIS · Reminder",
                if (item.isPrivate) "You have a private reminder. Open JARVIS to see it." else item.title, item.urgent, silentSound())
            val generic = if (DEVANAGARI.containsMatchIn(item.spoken)) "$addressHi, आपका एक प्राइवेट रिमाइंडर है।"
                          else "$address, you have a private reminder."
            val line = when {
                unlocked -> item.spoken
                speakLocked -> if (item.isPrivate) generic else item.spoken
                else -> ""
            }
            if (line.isNotBlank()) lines.add(line)
            // A question is asked only when it is him for sure (unlocked), one at a time, and never for a
            // private item spoken as "a private reminder".
            if (unlocked && ask == null && item.question != null && line == item.spoken) ask = item
        }
        if (lines.isEmpty()) return
        var text = if (lines.size == 1) lines[0] else combined(lines, address, addressHi)
        val asked = ask
        if (asked != null && lines.size == 1) {
            val q = asked.question!!
            val hindi = DEVANAGARI.containsMatchIn(text)
            val question = if (hindi) q.optString("hi") else q.optString("en")
            if (question.isNotBlank()) text = "$text $question"
        }
        val listener = onAsk
        if (asked != null && listener != null && lines.size == 1 && ConversationLoop.available(context)) {
            speakThen(text) { listener(aboutOf(asked)) }
        } else {
            speak(text)
        }
    }

    /** Speak, then run [onDone] once the voice has finished (or failed), so listening never starts over the voice. */
    private fun speakThen(text: String, onDone: () -> Unit) {
        if (text.isBlank() || WakeBus.appInForeground) return
        val lang = if (DEVANAGARI.containsMatchIn(text)) "hi-IN" else (prefs.getString(WakeBus.KEY_VOICE_ACCENT, "hi-IN") ?: "hi-IN")
        TtsEngine.speakThen(TtsEngine.Request("bg-" + System.nanoTime(), text, null, lang, 0.95f)) { onDone() }
    }

    /** "Sir, 3 things. Your meeting starts in 15 minutes. Mansi has completed: Book flights. …" (or the Hindi twin). */
    private fun combined(lines: List<String>, address: String, addressHi: String): String {
        val hindi = lines.any { DEVANAGARI.containsMatchIn(it) }
        val parts = lines.map { raw ->
            var s = raw.trim()
            val low = s.lowercase(Locale.ROOT)
            for (a in listOf(address, addressHi)) {
                val al = a.lowercase(Locale.ROOT)
                if (al.isNotEmpty() && (low.startsWith("$al, ") || low.startsWith("$al "))) {
                    s = s.substring(a.length + 1).trim()
                    break
                }
            }
            if (!s.endsWith(".") && !s.endsWith("!") && !s.endsWith("?") && !s.endsWith("।")) s + (if (hindi) "।" else ".") else s
        }
        return if (hindi) "$addressHi, ${lines.size} बातें। " + parts.joinToString(" ")
               else "$address, ${lines.size} things. " + parts.joinToString(" ")
    }

    private fun speak(text: String) {
        if (text.isBlank() || WakeBus.appInForeground) return
        // The Hindi voice for a Hindi line; the chosen accent (the app's "JARVIS voice" setting) for an English one.
        val lang = if (DEVANAGARI.containsMatchIn(text)) "hi-IN" else (prefs.getString(WakeBus.KEY_VOICE_ACCENT, "hi-IN") ?: "hi-IN")
        TtsEngine.speak(TtsEngine.Request("bg-" + System.nanoTime(), text, null, lang, 0.95f))
    }

    fun close() {
        // The shared voice stays alive for the app.
    }
}
