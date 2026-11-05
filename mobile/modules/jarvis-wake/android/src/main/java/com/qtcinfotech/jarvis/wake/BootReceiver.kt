package com.qtcinfotech.jarvis.wake

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * After a reboot (or an app update): start the background service again straight away in watch-only mode, so
 * reminders and messages keep arriving. Android will not let an app open the microphone on its own after a
 * reboot, so if "Hey JARVIS" was on this also posts "Tap to switch JARVIS on"; opening the app turns it back on.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED && intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        val prefs = WakeBus.prefs(context)
        if (!prefs.getBoolean(WakeBus.KEY_ENABLED, false)) return
        try {
            WakeService.start(context, withMicrophone = false)
        } catch (e: Throwable) {
            Log.w("JarvisWake", "Could not start the watcher after boot: ${e.message}")
        }
        if (prefs.getBoolean(WakeBus.KEY_WAKE_WORD, false)) WakeNotifier(context).showSwitchOn()
    }
}
