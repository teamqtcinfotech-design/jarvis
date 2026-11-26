package com.qtcinfotech.jarvis.wake

import android.util.Log
import com.google.android.gms.tasks.Tasks
import com.google.firebase.messaging.FirebaseMessaging
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Live push (Firebase Cloud Messaging). The server sends one small data message the moment an alert is
 * created; this shows and speaks it straight away, app closed or not, through the same rules as the
 * half-minute watcher (which stays as the safety net).
 *
 * The phone's token is sent to the server at sign-in (JarvisWakeModule.getPushToken) and again whenever
 * Firebase rotates it. Signing out deletes it (deletePushToken), so a phone no longer in use hears nothing.
 */
class PushService : FirebaseMessagingService() {
    override fun onNewToken(token: String) {
        val prefs = WakeBus.prefs(this)
        prefs.edit().putString(WakeBus.KEY_PUSH_TOKEN, token).apply()
        if (!prefs.getBoolean(WakeBus.KEY_ENABLED, false)) return // not signed in on this phone
        Executors.newSingleThreadExecutor().execute { register(this, token) }
    }

    override fun onMessageReceived(message: RemoteMessage) {
        val prefs = WakeBus.prefs(this)
        if (!prefs.getBoolean(WakeBus.KEY_ENABLED, false)) return // signed out: ignore anything still arriving
        if (!WakeBus.running) {
            // The phone had killed the background service (Xiaomi does, when the app is swiped away): a push is
            // allowed to start it again, so the reminders, the ringing and "Hey JARVIS" are back from this moment.
            try {
                WakeService.start(this, withMicrophone = WatchdogReceiver.overlayAllowed(this))
            } catch (e: Throwable) {
                Log.w(TAG, "push: could not start the service: ${e.message}")
            }
        }
        val data = message.data
        val type = data["type"]
        if (type == "walkie") {
            // Somebody is talking to this phone and its line was down: open the line now; the sound follows on it.
            Log.i(TAG, "push: walkie from ${data["from"]}")
            WalkieEngine.start(this)
            return
        }
        // Ring until seen: the stop (seen elsewhere, done, withdrawn) and every ring after the first.
        if (type == "ring_stop") {
            data["id"]?.let { WakeNotifier(this).cancelRing(it) }
            Log.i(TAG, "push: ring stopped")
            return
        }
        if (type == "ring") {
            // One ring for everything still waiting: each item its own (silent) notification with Seen, then the
            // summary that sounds. Items this phone has rung at this number already (or whose Seen was tapped
            // here) are left alone; if nothing is new, nothing sounds.
            Log.i(TAG, "push: ring ${data["count"]} waiting, ${data["priority"]}")
            val notifier = WakeNotifier(this)
            val items = try { JSONArray(data["items"] ?: "[]") } catch (_: Throwable) { JSONArray() }
            var fresh = 0
            for (i in 0 until items.length()) {
                val it = items.optJSONObject(i) ?: continue
                val id = it.optString("id")
                if (id.isEmpty() || RingReceiver.seenHere(this, id)) continue
                if (!RingReceiver.firstRing(this, id, it.optString("ring_no"))) continue
                notifier.showRingItem(id, it.optString("title").ifBlank { "JARVIS" }, it.optString("body"), it.optString("priority"),
                    data["seen_label"] ?: "Seen")
                fresh++
            }
            if (fresh > 0) notifier.showRingSummary(data["title"] ?: "JARVIS", data["body"] ?: "", data["quiet"] == "1", alert = true)
            if (WakeBus.appInForeground) WakeBus.onPush()
            return
        }
        if (type != "alert" && type != "reminder") return
        Log.i(TAG, "push: $type ${data["kind"]} ${data["priority"]}${if (data["ring"] == "1") " ring" else ""}")
        val ring = type == "alert" && data["ring"] == "1"
        if (WakeBus.appInForeground) {
            // The app is open: its own alert engine fetches and decides (speak / vibrate / hold). Work and messages
            // that ring until seen still ring, with the Seen button, like every later ring.
            val id = data["id"]
            if (ring && id != null && RingReceiver.firstRing(this, id, data["ring_no"] ?: "1")) {
                WakeNotifier(this).showRing(id, data["title"] ?: "JARVIS", data["body"] ?: "", data["priority"] ?: "NORMAL",
                    data["quiet"] == "1", data["seen_label"] ?: "Seen")
            }
            WakeBus.onPush()
            return
        }
        val question = data["question"]?.takeIf { it.isNotBlank() }?.let {
            try { JSONObject(it) } catch (_: Throwable) { null }
        }
        val watcher = ReminderWatcher(this, WakeNotifier(this))
        watcher.onAsk = { about -> WakeService.askAbout(this, about) }
        if (type == "reminder") {
            // A reminder at its exact moment (the half-minute poll stays as the safety net).
            val key = data["key"] ?: return
            watcher.deliverPush(
                key = key,
                title = data["title"] ?: "JARVIS",
                spoken = data["spoken"] ?: "",
                urgent = data["priority"] == "URGENT",
                priority = data["priority"] ?: "NORMAL",
                question = question,
                reminderKey = key,
                isPrivate = data["is_private"] == "1",
                silent = data["silent"] == "1",
            )
            return
        }
        val id = data["id"] ?: return
        if (ring) RingReceiver.firstRing(this, id, data["ring_no"] ?: "1")
        watcher.deliverPush(
            key = data["announce_key"]?.takeIf { it.isNotBlank() } ?: "alert:$id",
            title = data["title"] ?: "JARVIS",
            spoken = data["spoken"] ?: data["body"] ?: "",
            urgent = data["priority"] == "URGENT",
            priority = data["priority"] ?: "NORMAL",
            question = question,
            ring = ring,
            quiet = data["quiet"] == "1",
            seenLabel = data["seen_label"] ?: "Seen",
            silent = data["silent"] == "1",
            kind = when {
                data["ref_type"] == "message" -> "message"
                data["kind"] == "NEW_REQUEST" -> "request"
                data["ref_type"] == "approval" -> "approval"
                data["kind"] == "TASK_ASSIGNED" -> "task"
                else -> "update"
            },
        )
    }

    companion object {
        private const val TAG = "JarvisWake"

        /** The current token (blocking, call off the main thread), or null when Firebase is not set up on this build. */
        fun currentToken(): String? = try {
            Tasks.await(FirebaseMessaging.getInstance().token, 15, TimeUnit.SECONDS)
        } catch (e: Throwable) {
            Log.w(TAG, "push token unavailable: ${e.message}")
            null
        }

        fun deleteToken() {
            try {
                Tasks.await(FirebaseMessaging.getInstance().deleteToken(), 15, TimeUnit.SECONDS)
            } catch (e: Throwable) {
                Log.w(TAG, "push token delete failed: ${e.message}")
            }
        }

        /** Tell the server this phone's token (its own login). */
        fun register(context: android.content.Context, token: String): Boolean {
            val body = JSONObject().put("token", token).put("platform", "android")
            val ok = Api.post(context, "/me/push-token/", body, 15000) != null
            Log.i(TAG, if (ok) "push token registered" else "push token registration failed")
            return ok
        }
    }
}
