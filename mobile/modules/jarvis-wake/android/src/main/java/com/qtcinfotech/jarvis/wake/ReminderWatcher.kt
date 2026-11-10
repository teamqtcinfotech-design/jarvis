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

    /** One thing to announce: its server key (for "told"), what to show and what to say. */
    private class Item(val key: String, val reminderKey: String?, val alertId: String?, val title: String,
                       val spoken: String, val isPrivate: Boolean, val urgent: Boolean)

    init {
        TtsEngine.init(context)
    }

    /** Returns false when the login has expired for good (the service should stop and let the app re-issue it). */
    fun check(): Boolean {
        if (WakeBus.appInForeground || WakeBus.inConversation) return true
        val reminders = Api.get(context, "/reminders/?hours=1") ?: return !Api.tokensDead
        val alerts = Api.get(context, "/alerts/")
        val nowSec = System.currentTimeMillis() / 1000
        val spoken = prefs.getStringSet(WakeBus.KEY_SPOKEN, emptySet())?.toMutableSet() ?: mutableSetOf()
        val due = ArrayList<Item>()
        val quiet = ArrayList<Item>()   // told to the server, never announced (small news, or told already)

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
                if (age < 0 && age > -3600) soon.add(r.optString("spoken"))
                if (age < 0 || age > 180) continue // not due yet, or too old (the app will show it as held)
                if (!r.isNull("told_at")) {       // the app said it already: remember, do not repeat
                    spoken.add(key)
                    continue
                }
                due.add(Item(key, key, null, r.optString("title"), r.optString("spoken"), r.optBoolean("is_private"),
                             r.optString("priority") == "URGENT"))
            }
            if (soon.isNotEmpty()) GrokVoice.prefetch(context, soon)
        }
        val results = alerts?.optJSONArray("results")
        if (results != null) {
            for (i in 0 until results.length()) {
                val a = results.getJSONObject(i)
                val id = a.optString("id")
                val key = "alert:$id"
                if (id.isEmpty() || key in spoken) continue
                if (!a.optBoolean("fresh", true)) {  // told already, or old news: it stays in the lists only
                    spoken.add(key)
                    continue
                }
                val item = Item(key, null, id, a.optString("title"), a.optString("spoken_text"), false, a.optString("priority") == "URGENT")
                if (a.optString("priority") == "LOW") quiet.add(item) else due.add(item)
            }
        }
        if (due.isNotEmpty()) announce(due)
        for (item in due + quiet) spoken.add(item.key)
        if (due.isNotEmpty() || quiet.isNotEmpty()) tell(due + quiet)
        if (spoken.isNotEmpty()) {
            val trimmed = if (spoken.size > 300) spoken.toList().takeLast(300).toSet() else spoken
            prefs.edit().putStringSet(WakeBus.KEY_SPOKEN, trimmed).apply()
        }
        return true
    }

    /** A live push (see PushService): show and speak it now, and remember it so the next check does not repeat it. */
    fun deliverPush(key: String, title: String, spoken: String, urgent: Boolean, priority: String = "NORMAL") {
        val spokenKeys = prefs.getStringSet(WakeBus.KEY_SPOKEN, emptySet())?.toMutableSet() ?: mutableSetOf()
        if (key in spokenKeys) return
        val item = Item(key, null, key.removePrefix("alert:"), title, spoken, false, urgent)
        if (!WakeBus.inConversation && priority != "LOW") announce(listOf(item))
        spokenKeys.add(key)
        prefs.edit().putStringSet(WakeBus.KEY_SPOKEN, spokenKeys).apply()
        tell(listOf(item))
    }

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

    /** Show each as a notification; say them all as ONE sentence (the address once), by the unlocked / locked rules. */
    private fun announce(items: List<Item>) {
        val address = prefs.getString(WakeBus.KEY_ADDRESS, "Sir") ?: "Sir"
        val generic = "$address, you have a private reminder."
        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        val km = context.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        val unlocked = pm.isInteractive && !km.isKeyguardLocked
        val speakLocked = prefs.getBoolean(WakeBus.KEY_SPEAK_LOCKED, false)
        val lines = ArrayList<String>()
        for (item in items) {
            notifier.show(item.key, if (item.isPrivate) "JARVIS" else "JARVIS · Reminder",
                if (item.isPrivate) "You have a private reminder. Open JARVIS to see it." else item.title, item.urgent)
            val line = when {
                unlocked -> item.spoken
                speakLocked -> if (item.isPrivate) generic else item.spoken
                else -> ""
            }
            if (line.isNotBlank()) lines.add(line)
        }
        if (lines.isEmpty()) return
        speak(if (lines.size == 1) lines[0] else combined(lines, address))
    }

    /** "Sir, 3 things. Your meeting starts in 15 minutes. Mansi has completed: Book flights. …" */
    private fun combined(lines: List<String>, address: String): String {
        val parts = lines.map { raw ->
            var s = raw.trim()
            val low = s.lowercase(Locale.ROOT)
            val a = address.lowercase(Locale.ROOT)
            if (low.startsWith("$a, ") || low.startsWith("$a ")) s = s.substring(address.length + 1).trim()
            if (!s.endsWith(".") && !s.endsWith("!") && !s.endsWith("?")) "$s." else s
        }
        return "$address, ${lines.size} things. " + parts.joinToString(" ")
    }

    private fun speak(text: String) {
        if (text.isBlank() || WakeBus.appInForeground) return
        TtsEngine.speak(TtsEngine.Request("bg-" + System.nanoTime(), text, null, "hi-IN", 0.95f))
    }

    fun close() {
        // The shared voice stays alive for the app.
    }
}
