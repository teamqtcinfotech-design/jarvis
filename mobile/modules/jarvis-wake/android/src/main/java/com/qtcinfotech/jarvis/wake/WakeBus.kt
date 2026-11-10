package com.qtcinfotech.jarvis.wake

import android.content.Context
import android.content.SharedPreferences

/** Tiny in-process bridge between the service and the Expo module, plus the service's saved settings. */
object WakeBus {
    @Volatile var listener: (() -> Unit)? = null
    @Volatile var pendingWake = false
    @Volatile var appInForeground = false
    /**
     * The app asked the wake-word microphone to step aside (it is listening or speaking itself). The request
     * expires by itself: if the app is closed or killed while paused, "Hey JARVIS" comes back within a minute
     * instead of staying deaf until the app is opened again.
     */
    @Volatile private var pausedUntil = 0L
    private const val PAUSE_TTL_MS = 60_000L

    var paused: Boolean
        get() = android.os.SystemClock.elapsedRealtime() < pausedUntil
        set(value) {
            pausedUntil = if (value) android.os.SystemClock.elapsedRealtime() + PAUSE_TTL_MS else 0L
        }
    /** The background service is running (watching for reminders and messages, and maybe listening). */
    @Volatile var running = false
    /** The microphone part ("Hey JARVIS") is running too. */
    @Volatile var listening = false
    /** Android is feeding the listener silence (it was restarted in the background): the app must restart it. */
    @Volatile var micBlocked = false
    /** A background (voice-only) conversation is going on: the wake-word mic and the reminder watcher stand aside. */
    @Volatile var inConversation = false

    fun onWake() {
        val l = listener
        if (appInForeground && l != null) l() else pendingWake = true
    }

    /** A live push arrived while the app is open: the app refreshes its alerts at once. */
    @Volatile var pushListener: (() -> Unit)? = null

    fun onPush() {
        pushListener?.invoke()
    }

    fun prefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences("jarvis_wake", Context.MODE_PRIVATE)

    /** The service should run (someone is signed in on this phone). */
    const val KEY_ENABLED = "enabled"
    /** Listen for "Hey JARVIS" as well (needs the microphone). */
    const val KEY_WAKE_WORD = "wake_word"
    const val KEY_SERVER = "server"
    const val KEY_ACCESS = "access"
    const val KEY_REFRESH = "refresh"
    const val KEY_ADDRESS = "address"
    const val KEY_SPEAK_LOCKED = "speak_locked"
    const val KEY_SPOKEN = "spoken_keys"
    const val KEY_PUSH_TOKEN = "push_token"
    /** JARVIS speaks in the Grok voice (default); false = the phone's own voice only. */
    const val KEY_GROK_VOICE = "grok_voice"
}
