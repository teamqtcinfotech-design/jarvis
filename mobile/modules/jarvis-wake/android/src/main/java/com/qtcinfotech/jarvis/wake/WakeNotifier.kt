package com.qtcinfotech.jarvis.wake

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build

/** Notifications for the background part: the permanent "listening" one and reminder pop-ups. */
class WakeNotifier(private val context: Context) {
    private val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    init {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_SERVICE, "JARVIS is on", NotificationManager.IMPORTANCE_MIN).apply {
                    description = "Shows while JARVIS watches for reminders and messages (and listens for “Hey JARVIS”)."
                    setShowBadge(false)
                }
            )
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_REMINDERS, "Reminders (app closed)", NotificationManager.IMPORTANCE_HIGH).apply {
                    description = "Reminders and requests that arrive while JARVIS is closed."
                    lockscreenVisibility = Notification.VISIBILITY_PRIVATE
                }
            )
        }
    }

    private fun openApp(extra: String? = null): PendingIntent {
        val launch = context.packageManager.getLaunchIntentForPackage(context.packageName)!!.apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            if (extra != null) putExtra(extra, true)
        }
        return PendingIntent.getActivity(context, extra?.hashCode() ?: 0, launch,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }

    private fun icon(): Int = context.applicationInfo.icon

    /** The permanent notification. It stays while someone is signed in; the switch in Settings controls the microphone part. */
    fun serviceNotification(listening: Boolean = WakeBus.listening): Notification {
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) Notification.Builder(context, CHANNEL_SERVICE)
        else @Suppress("DEPRECATION") Notification.Builder(context)
        return builder
            .setSmallIcon(icon())
            .setContentTitle(if (listening) "JARVIS is listening for “Hey JARVIS”" else "JARVIS is on")
            .setContentText(if (listening) "Nothing is sent until you say the words. Reminders and messages reach you here too."
                            else "Reminders and messages reach you even when the app is closed.")
            .setContentIntent(openApp())
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .build()
    }

    fun show(key: String, title: String, text: String, urgent: Boolean) {
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) Notification.Builder(context, CHANNEL_REMINDERS)
        else @Suppress("DEPRECATION") Notification.Builder(context)
        val n = builder
            .setSmallIcon(icon())
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text)) // a morning briefing is a whole paragraph
            .setContentIntent(openApp())
            .setAutoCancel(true)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .apply { if (urgent) setDefaults(Notification.DEFAULT_ALL) else setDefaults(Notification.DEFAULT_VIBRATE) }
            .build()
        manager.notify(key.hashCode(), n)
    }

    fun showSwitchOn() {
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) Notification.Builder(context, CHANNEL_REMINDERS)
        else @Suppress("DEPRECATION") Notification.Builder(context)
        manager.notify(
            SWITCH_ON_ID,
            builder.setSmallIcon(icon())
                .setContentTitle("Tap to switch JARVIS on")
                .setContentText("After a restart, Android needs one tap before JARVIS can listen again.")
                .setContentIntent(openApp())
                .setAutoCancel(true)
                .build(),
        )
    }

    fun cancelSwitchOn() = manager.cancel(SWITCH_ON_ID)

    /** During a voice-only conversation the permanent notification shows the last line, so he can glance at it. */
    fun showConversation(text: String) {
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) Notification.Builder(context, CHANNEL_SERVICE)
        else @Suppress("DEPRECATION") Notification.Builder(context)
        manager.notify(
            SERVICE_ID,
            builder.setSmallIcon(icon())
                .setContentTitle("JARVIS")
                .setContentText(text)
                .setStyle(Notification.BigTextStyle().bigText(text))
                .setContentIntent(openApp())
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .build(),
        )
    }

    fun restoreServiceNotification() = manager.notify(SERVICE_ID, serviceNotification())

    /** "Ji Sir? Tap to talk" when the phone would not let JARVIS pop up by itself. */
    fun showWakeFallback(address: String) {
        val open = openApp(WakeService.EXTRA_WAKE)
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) Notification.Builder(context, CHANNEL_REMINDERS)
        else @Suppress("DEPRECATION") Notification.Builder(context)
        manager.notify(
            WAKE_ID,
            builder.setSmallIcon(icon())
                .setContentTitle("Ji $address?")
                .setContentText("Tap to talk to JARVIS.")
                .setContentIntent(open)
                .setFullScreenIntent(open, true)
                .setCategory(Notification.CATEGORY_CALL)
                .setPriority(Notification.PRIORITY_MAX)
                .setAutoCancel(true)
                .setTimeoutAfter(30_000)
                .setDefaults(Notification.DEFAULT_VIBRATE)
                .build(),
        )
    }

    companion object {
        const val CHANNEL_SERVICE = "jarvis-wake-service"
        const val CHANNEL_REMINDERS = "jarvis-background"
        const val SERVICE_ID = 7001
        private const val SWITCH_ON_ID = 7002
        private const val WAKE_ID = 7003
    }
}
