package com.qtcinfotech.jarvis.wake

import android.content.Context
import android.content.SharedPreferences

/** Tiny in-process bridge between the service and the Expo module, plus the service's saved settings. */
object WakeBus {
    @Volatile var listener: (() -> Unit)? = null
    @Volatile var pendingWake = false
    @Volatile var appInForeground = false
    @Volatile var paused = false
    @Volatile var running = false
    /** A background (voice-only) conversation is going on: the wake-word mic and the reminder watcher stand aside. */
    @Volatile var inConversation = false

    fun onWake() {
        val l = listener
        if (appInForeground && l != null) l() else pendingWake = true
    }

    fun prefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences("jarvis_wake", Context.MODE_PRIVATE)

    const val KEY_ENABLED = "enabled"
    const val KEY_SERVER = "server"
    const val KEY_ACCESS = "access"
    const val KEY_REFRESH = "refresh"
    const val KEY_ADDRESS = "address"
    const val KEY_SPEAK_LOCKED = "speak_locked"
    const val KEY_SPOKEN = "spoken_keys"
}
