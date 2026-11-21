package com.qtcinfotech.jarvis.wake

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import android.provider.Settings
import android.util.Log

/**
 * Brings the background service back when the phone has killed it.
 *
 * Xiaomi (MIUI) kills the whole app, foreground service included, when JARVIS is swiped away from the recent
 * apps, and never restarts it; until now "Hey JARVIS" and the reminders were dead until the app was opened again.
 * While someone is signed in, an alarm fires every few minutes (and once a few seconds after a swipe) and starts
 * the service again if it is not running. With "display over other apps" allowed, Android lets the restarted
 * service use the microphone straight away, so "Hey JARVIS" comes back by itself.
 */
class WatchdogReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val prefs = WakeBus.prefs(context)
        if (!prefs.getBoolean(WakeBus.KEY_ENABLED, false)) return  // signed out: nothing to bring back
        if (!WakeBus.running) {
            Log.i(TAG, "watchdog: the service was not running; starting it again")
            try {
                WakeService.start(context, withMicrophone = overlayAllowed(context))
            } catch (e: Throwable) {
                Log.w(TAG, "watchdog: could not start the service: ${e.message}")
            }
        }
        schedule(context)
    }

    companion object {
        private const val TAG = "JarvisWake"
        private const val ACTION = "com.qtcinfotech.jarvis.wake.WATCHDOG"
        private const val EVERY_MS = 5 * 60_000L

        private fun pending(context: Context): PendingIntent =
            PendingIntent.getBroadcast(context, 7101, Intent(context, WatchdogReceiver::class.java).setAction(ACTION),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

        fun overlayAllowed(context: Context): Boolean =
            Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(context)

        /** The next check in [delayMs] (a few minutes by default; seconds right after the app was swiped away). */
        fun schedule(context: Context, delayMs: Long = EVERY_MS) {
            if (!WakeBus.prefs(context).getBoolean(WakeBus.KEY_ENABLED, false)) return
            val am = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
            val at = SystemClock.elapsedRealtime() + delayMs
            try {
                val exact = Build.VERSION.SDK_INT < Build.VERSION_CODES.S || am.canScheduleExactAlarms()
                if (exact) am.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pending(context))
                else am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pending(context))
            } catch (e: Throwable) {
                Log.w(TAG, "watchdog: could not set the alarm: ${e.message}")
            }
        }

        fun cancel(context: Context) {
            try {
                (context.getSystemService(Context.ALARM_SERVICE) as AlarmManager).cancel(pending(context))
            } catch (_: Throwable) {
            }
        }
    }
}
