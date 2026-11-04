package com.qtcinfotech.jarvis.wake

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * After a reboot (or an app update) Android will not let the app open the microphone on its own,
 * so if "Hey JARVIS" was on, this posts "Tap to switch JARVIS on". Opening the app turns it back on.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED && intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        if (!WakeBus.prefs(context).getBoolean(WakeBus.KEY_ENABLED, false)) return
        WakeNotifier(context).showSwitchOn()
    }
}
