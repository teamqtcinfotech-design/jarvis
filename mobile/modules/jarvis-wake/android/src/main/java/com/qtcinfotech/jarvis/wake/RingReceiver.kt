package com.qtcinfotech.jarvis.wake

import android.app.KeyguardManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.PowerManager
import android.util.Log
import org.json.JSONObject

/**
 * "Seen" tapped on a ringing notification (ring until seen). The ringing stops here at once; the server is told
 * (it stops the ringing on the person's other phones and tells whoever gave the work). For work, JARVIS then asks
 * "Shall I tell Sir when you'll do it?": aloud and listening when the phone is unlocked, otherwise as a notification
 * one tap away; with the app open, the app asks it itself.
 */
class RingReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_SEEN) return
        val id = intent.getStringExtra(EXTRA_ALERT) ?: return
        val notifier = WakeNotifier(context)
        notifier.cancelRing(id)
        rememberSeen(context, id)
        val pending = goAsync()
        Thread({
            try {
                val res = Api.post(context, "/alerts/$id/seen/", JSONObject().put("via", "notification"), 15000)
                Log.i(TAG, "seen from the notification: ${if (res != null) "told the server" else "server not reached"}")
                val q = res?.optJSONObject("question")
                if (q != null && q.optString("kind") == "promise") ask(context, notifier, id, res, q)
            } catch (e: Throwable) {
                Log.w(TAG, "seen failed: ${e.message}")
            } finally {
                pending.finish()
            }
        }, "jarvis-seen").start()
    }

    /** The question that follows Seen on work: by when will they do it. */
    private fun ask(context: Context, notifier: WakeNotifier, id: String, res: JSONObject, q: JSONObject) {
        val item = res.optJSONObject("item")
        val title = item?.optString("title")?.ifBlank { null } ?: "JARVIS"
        val hindi = DEVANAGARI.containsMatchIn(item?.optString("spoken_text") ?: "") || DEVANAGARI.containsMatchIn(title)
        val question = (if (hindi) q.optString("hi") else q.optString("en")).ifBlank { q.optString("en") }
        if (WakeBus.appInForeground) {
            // The app is open (the shade was pulled down over it): it asks, with its own screen.
            WakeBus.pendingOpenKey = "promise:$id"
            WakeBus.onPush()
            return
        }
        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        val km = context.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        val unlocked = pm.isInteractive && !km.isKeyguardLocked
        if (unlocked && WakeBus.running && !WakeBus.inConversation && ConversationLoop.available(context)) {
            val about = JSONObject().put("kind", "alert").put("key", "").put("id", id).put("title", title)
                .put("question", q.optString("en"))
            TtsEngine.init(context)
            TtsEngine.speakThen(TtsEngine.Request("seen-" + System.nanoTime(), question, null, if (hindi) "hi-IN" else
                (WakeBus.prefs(context).getString(WakeBus.KEY_VOICE_ACCENT, "hi-IN") ?: "hi-IN"), 0.95f)) {
                WakeService.askAbout(context, about)
            }
            return
        }
        notifier.showAsk(id, title, question)
    }

    companion object {
        private const val TAG = "JarvisWake"
        const val ACTION_SEEN = "com.qtcinfotech.jarvis.wake.SEEN"
        const val EXTRA_ALERT = "alert_id"
        private val DEVANAGARI = Regex("[\\u0900-\\u097F]")

        /** Seen on this phone: no ring for it may sound here again, even before the server hears of it. */
        fun rememberSeen(context: Context, id: String) {
            val prefs = WakeBus.prefs(context)
            val seen = prefs.getStringSet(WakeBus.KEY_SEEN_IDS, emptySet())?.toMutableSet() ?: mutableSetOf()
            seen.add(id)
            val trimmed = if (seen.size > 200) seen.toList().takeLast(200).toSet() else seen
            prefs.edit().putStringSet(WakeBus.KEY_SEEN_IDS, trimmed).apply()
        }

        fun seenHere(context: Context, id: String): Boolean =
            WakeBus.prefs(context).getStringSet(WakeBus.KEY_SEEN_IDS, emptySet())?.contains(id) == true

        /** True the first time a ring (alert id + ring number) is seen on this phone: push and watcher never ring twice. */
        @Synchronized
        fun firstRing(context: Context, id: String, ringNo: String): Boolean {
            val prefs = WakeBus.prefs(context)
            val key = "ring:$id:$ringNo"
            val spoken = prefs.getStringSet(WakeBus.KEY_SPOKEN, emptySet())?.toMutableSet() ?: mutableSetOf()
            if (key in spoken) return false
            spoken.add(key)
            val trimmed = if (spoken.size > 300) spoken.toList().takeLast(300).toSet() else spoken
            prefs.edit().putStringSet(WakeBus.KEY_SPOKEN, trimmed).apply()
            return true
        }
    }
}
