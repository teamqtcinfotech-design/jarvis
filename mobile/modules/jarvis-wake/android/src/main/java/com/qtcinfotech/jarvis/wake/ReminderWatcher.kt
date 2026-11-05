package com.qtcinfotech.jarvis.wake

import android.app.KeyguardManager
import android.content.Context
import android.os.PowerManager
import java.text.SimpleDateFormat
import java.util.Locale

/**
 * While the app is closed, checks the server once a minute for reminders that are due right now and
 * requests that just arrived, and speaks or shows them:
 *
 *  - screen on and unlocked  -> spoken aloud (it is him: the phone is unlocked)
 *  - locked                  -> notification; spoken only if "speak when locked" is on, and a private
 *                               reminder is then only "you have a private reminder"
 *  - app open on screen      -> nothing here; the app's own alert engine handles it
 *
 * It has its own login (device token) so its refreshes never clash with the app's.
 */
class ReminderWatcher(private val context: Context, private val notifier: WakeNotifier) {
    private val prefs = WakeBus.prefs(context)

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
        var changed = false

        val list = reminders.optJSONArray("reminders")
        if (list != null) {
            for (i in 0 until list.length()) {
                val r = list.getJSONObject(i)
                val key = r.optString("key")
                if (key.isEmpty() || key in spoken) continue
                val fireAtSec = parseIsoSeconds(r.optString("fire_at")) ?: continue
                val age = nowSec - fireAtSec
                if (age < 0 || age > 180) continue // not due yet, or too old (the app will show it as held)
                deliver(key, r.optString("title"), r.optString("spoken"), r.optBoolean("is_private"), r.optString("priority") == "URGENT")
                spoken.add(key)
                changed = true
            }
        }
        val results = alerts?.optJSONArray("results")
        if (results != null) {
            for (i in 0 until results.length()) {
                val a = results.getJSONObject(i)
                val key = "alert:" + a.optString("id")
                if (key in spoken) continue
                deliver(key, a.optString("title"), a.optString("spoken_text"), false, a.optString("priority") == "URGENT")
                spoken.add(key)
                changed = true
            }
        }
        if (changed) {
            val trimmed = if (spoken.size > 300) spoken.toList().takeLast(300).toSet() else spoken
            prefs.edit().putStringSet(WakeBus.KEY_SPOKEN, trimmed).apply()
        }
        return true
    }

    /** A live push (see PushService): show and speak it now, and remember it so the next check does not repeat it. */
    fun deliverPush(key: String, title: String, spoken: String, urgent: Boolean) {
        val spokenKeys = prefs.getStringSet(WakeBus.KEY_SPOKEN, emptySet())?.toMutableSet() ?: mutableSetOf()
        if (key in spokenKeys) return
        if (!WakeBus.inConversation) deliver(key, title, spoken, false, urgent)
        spokenKeys.add(key)
        prefs.edit().putStringSet(WakeBus.KEY_SPOKEN, spokenKeys).apply()
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

    private fun deliver(key: String, title: String, spokenText: String, isPrivate: Boolean, urgent: Boolean) {
        val address = prefs.getString(WakeBus.KEY_ADDRESS, "Sir") ?: "Sir"
        val generic = "$address, you have a private reminder."
        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        val km = context.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        val unlocked = pm.isInteractive && !km.isKeyguardLocked
        notifier.show(key, if (isPrivate) "JARVIS" else "JARVIS · Reminder",
            if (isPrivate) "You have a private reminder. Open JARVIS to see it." else title, urgent)
        when {
            unlocked -> speak(spokenText)
            prefs.getBoolean(WakeBus.KEY_SPEAK_LOCKED, false) -> speak(if (isPrivate) generic else spokenText)
            else -> Unit
        }
    }

    private fun speak(text: String) {
        if (text.isBlank() || WakeBus.appInForeground) return
        TtsEngine.speak(TtsEngine.Request("bg-" + System.nanoTime(), text, null, "hi-IN", 0.95f))
    }

    fun close() {
        // The shared voice stays alive for the app.
    }

}
