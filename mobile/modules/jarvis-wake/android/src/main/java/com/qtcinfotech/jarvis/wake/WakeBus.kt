package com.qtcinfotech.jarvis.wake

import android.content.Context
import android.content.SharedPreferences

/** Tiny in-process bridge between the service and the Expo module, plus the service's saved settings. */
object WakeBus {
    @Volatile var listener: (() -> Unit)? = null
    @Volatile var pendingWake = false
    @Volatile var appInForeground = false
    @Volatile var paused = false
    /** The background service is running (watching for reminders and messages, and maybe listening). */
    @Volatile var running = false
    /** The microphone part ("Hey JARVIS") is running too. */
    @Volatile var listening = false
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
}
