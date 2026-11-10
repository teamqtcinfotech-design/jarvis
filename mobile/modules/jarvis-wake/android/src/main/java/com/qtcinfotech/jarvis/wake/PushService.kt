package com.qtcinfotech.jarvis.wake

import android.util.Log
import com.google.android.gms.tasks.Tasks
import com.google.firebase.messaging.FirebaseMessaging
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
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
        val data = message.data
        if (data["type"] != "alert") return
        Log.i(TAG, "push: ${data["kind"]} ${data["priority"]}")
        if (WakeBus.appInForeground) {
            WakeBus.onPush() // the app is open: its own alert engine fetches and decides (speak / vibrate / hold)
            return
        }
        val id = data["id"] ?: return
        ReminderWatcher(this, WakeNotifier(this)).deliverPush(
            key = "alert:$id",
            title = data["title"] ?: "JARVIS",
            spoken = data["spoken"] ?: data["body"] ?: "",
            urgent = data["priority"] == "URGENT",
            priority = data["priority"] ?: "NORMAL",
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
