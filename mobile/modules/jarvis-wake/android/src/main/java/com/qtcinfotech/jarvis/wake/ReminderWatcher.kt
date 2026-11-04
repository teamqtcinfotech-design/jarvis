package com.qtcinfotech.jarvis.wake

import android.app.KeyguardManager
import android.content.Context
import android.os.PowerManager
import android.speech.tts.TextToSpeech
import android.util.Log
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
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
    private var tts: TextToSpeech? = null
    private var ttsReady = false

    init {
        tts = TextToSpeech(context.applicationContext) { status ->
            if (status == TextToSpeech.SUCCESS) {
                tts?.language = Locale("hi", "IN") // the app's default: an Indian voice
                ttsReady = true
            }
        }
    }

    /** Returns false when the login has expired for good (the service should stop and let the app re-issue it). */
    fun check(): Boolean {
        if (WakeBus.appInForeground) return true
        val server = prefs.getString(WakeBus.KEY_SERVER, "") ?: ""
        if (server.isEmpty()) return true
        val reminders = get("$server/api/v1/reminders/?hours=1") ?: return tokensAlive()
        val alerts = get("$server/api/v1/alerts/")
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
        if (!ttsReady || text.isBlank() || WakeBus.appInForeground) return
        tts?.speak(text, TextToSpeech.QUEUE_ADD, null, "jarvis-" + System.nanoTime())
    }

    // ------------------------------------------------------------------ HTTP with its own token chain
    private fun get(url: String): JSONObject? {
        val first = request("GET", url, null, prefs.getString(WakeBus.KEY_ACCESS, "") ?: "")
        if (first.first != 401) return first.second
        if (!refresh()) return null
        return request("GET", url, null, prefs.getString(WakeBus.KEY_ACCESS, "") ?: "").second
    }

    private var tokensDead = false
    private fun tokensAlive() = !tokensDead

    private fun refresh(): Boolean {
        val server = prefs.getString(WakeBus.KEY_SERVER, "") ?: ""
        val refreshToken = prefs.getString(WakeBus.KEY_REFRESH, "") ?: ""
        if (refreshToken.isEmpty()) {
            tokensDead = true
            return false
        }
        val body = JSONObject().put("refresh", refreshToken).toString()
        val (code, json) = request("POST", "$server/api/v1/auth/refresh/", body, null)
        if (code != 200 || json == null) {
            if (code == 401) tokensDead = true
            return false
        }
        prefs.edit()
            .putString(WakeBus.KEY_ACCESS, json.optString("access"))
            .putString(WakeBus.KEY_REFRESH, json.optString("refresh", refreshToken))
            .apply()
        return true
    }

    private fun request(method: String, url: String, body: String?, bearer: String?): Pair<Int, JSONObject?> {
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = method
                connectTimeout = 8000
                readTimeout = 12000
                setRequestProperty("Accept", "application/json")
                if (!bearer.isNullOrEmpty()) setRequestProperty("Authorization", "Bearer $bearer")
                if (body != null) {
                    doOutput = true
                    setRequestProperty("Content-Type", "application/json")
                    outputStream.use { it.write(body.toByteArray()) }
                }
            }
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() } ?: ""
            Pair(code, runCatching { JSONObject(text) }.getOrNull())
        } catch (e: Exception) {
            Log.w(TAG, "request failed: ${e.message}")
            Pair(0, null)
        } finally {
            conn?.disconnect()
        }
    }

    fun close() {
        tts?.stop()
        tts?.shutdown()
        tts = null
    }

    companion object {
        private const val TAG = "JarvisWake"
    }
}
