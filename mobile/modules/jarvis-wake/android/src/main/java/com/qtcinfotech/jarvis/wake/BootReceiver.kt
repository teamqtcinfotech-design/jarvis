package com.qtcinfotech.jarvis.wake

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.provider.Settings
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
        // With "display over other apps" allowed, Android lets the service use the microphone even when it is
        // started from the background, so "Hey JARVIS" comes back by itself. Otherwise: watch only, and ask
        // for one tap. If the microphone turns out to be silenced anyway, the service posts that tap itself.
        val overlay = Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(context)
        val wakeWord = prefs.getBoolean(WakeBus.KEY_WAKE_WORD, false)
        try {
            WakeService.start(context, withMicrophone = overlay)
        } catch (e: Throwable) {
            Log.w("JarvisWake", "Could not start the watcher after boot: ${e.message}")
        }
        if (wakeWord && !overlay) WakeNotifier(context).showSwitchOn()
    }
}
