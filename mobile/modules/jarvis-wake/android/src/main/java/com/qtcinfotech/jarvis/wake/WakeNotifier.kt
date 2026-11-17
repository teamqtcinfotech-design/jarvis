package com.qtcinfotech.jarvis.wake

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.drawable.Icon
import android.media.AudioAttributes
import android.net.Uri
import android.os.Build

/**
 * Notifications for the background part: the permanent "listening" one, reminder pop-ups, and ring-until-seen.
 *
 * Ring until seen is a GROUP: one notification per waiting item (silent, with its own Seen button) under one
 * summary that carries the "ting ting" and says "3 waiting". So however many items wait, every ring is ONE sound,
 * and Seen on one item leaves the others where they are. The ids still waiting are kept (KEY_RING_ACTIVE), so a
 * stop that reaches this phone late, or a Seen tapped here, keeps the summary honest.
 */
class WakeNotifier(private val context: Context) {
    private val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
    private val prefs = WakeBus.prefs(context)

    /** JARVIS's own "ting ting" (res/raw/jarvis_ting.wav). */
    private val ting: Uri = Uri.parse("android.resource://${context.packageName}/raw/jarvis_ting")

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
            // Android fixes a channel's sound when it is made, so "silent" is its own channel (chosen in Settings).
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_REMINDERS_SILENT, "Reminders (app closed, silent)", NotificationManager.IMPORTANCE_HIGH).apply {
                    description = "The same reminders, with a vibration and no sound."
                    lockscreenVisibility = Notification.VISIBILITY_PRIVATE
                    setSound(null, null)
                    enableVibration(true)
                }
            )
            // Ring until seen: the sound lives on the summary's channel (ting, or vibration only in meeting mode);
            // the items themselves are silent, so one ring is one sound.
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_RING, "Work and messages (ring until seen)", NotificationManager.IMPORTANCE_HIGH).apply {
                    description = "Work given to you and messages from the team ring every few minutes until you tap Seen."
                    setSound(ting, AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_NOTIFICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build())
                    enableVibration(true)
                    vibrationPattern = longArrayOf(0, 300, 180, 300)
                    lockscreenVisibility = Notification.VISIBILITY_PRIVATE
                }
            )
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_RING_QUIET, "Work and messages (meeting mode)", NotificationManager.IMPORTANCE_HIGH).apply {
                    description = "The same, with a vibration and no sound while you are in a meeting."
                    setSound(null, null)
                    enableVibration(true)
                    vibrationPattern = longArrayOf(0, 300, 180, 300)
                    lockscreenVisibility = Notification.VISIBILITY_PRIVATE
                }
            )
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_RING_ITEM, "Work and messages (each item)", NotificationManager.IMPORTANCE_HIGH).apply {
                    description = "Each item waiting for your Seen. The sound comes from the ring itself, not from these."
                    setSound(null, null)
                    enableVibration(false)
                    lockscreenVisibility = Notification.VISIBILITY_PRIVATE
                }
            )
        }
    }

    /**
     * Opens the app. ``extra`` = a flag to set (woken by "Hey JARVIS"); ``openKey`` = the reminder / alert this
     * notification is about, so the app comes up on it and asks its question. Each key gets its own pending
     * intent (the request code), otherwise Android would hand every notification the last key.
     */
    private fun openApp(extra: String? = null, openKey: String? = null): PendingIntent {
        val launch = context.packageManager.getLaunchIntentForPackage(context.packageName)!!.apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            if (extra != null) putExtra(extra, true)
            if (openKey != null) putExtra(WakeService.EXTRA_OPEN_KEY, openKey)
        }
        val code = openKey?.hashCode() ?: extra?.hashCode() ?: 0
        return PendingIntent.getActivity(context, code, launch, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }

    private fun icon(): Int = context.applicationInfo.icon

    private fun builder(channel: String): Notification.Builder =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) Notification.Builder(context, channel)
        else @Suppress("DEPRECATION") Notification.Builder(context)

    /** The permanent notification. It stays while someone is signed in; the switch in Settings controls the microphone part. */
    fun serviceNotification(listening: Boolean = WakeBus.listening): Notification {
        return builder(CHANNEL_SERVICE)
            .setSmallIcon(icon())
            .setContentTitle(if (listening) "JARVIS is listening for “Hey JARVIS”" else "JARVIS is on")
            .setContentText(if (listening) "Nothing is sent until you say the words. Reminders and messages reach you here too."
                            else "Reminders and messages reach you even when the app is closed.")
            .setContentIntent(openApp())
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .build()
    }

    /** A reminder or message pop-up. silent = the person chose no sound for closed-app reminders (vibration only). */
    fun show(key: String, title: String, text: String, urgent: Boolean, silent: Boolean = false) {
        val channel = if (silent) CHANNEL_REMINDERS_SILENT else CHANNEL_REMINDERS
        val n = builder(channel)
            .setSmallIcon(icon())
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text)) // a morning briefing is a whole paragraph
            .setContentIntent(openApp(openKey = key))
            .setAutoCancel(true)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .apply {
                if (silent) setDefaults(Notification.DEFAULT_VIBRATE)
                else if (urgent) setDefaults(Notification.DEFAULT_ALL)
                else setDefaults(Notification.DEFAULT_VIBRATE)
            }
            .build()
        manager.notify(key.hashCode(), n)
    }

    // ---------------------------------------------------------------- ring until seen
    /** One item waiting for Seen: its own (silent) notification, under the ring group. Posting it again changes nothing audible. */
    fun showRingItem(alertId: String, title: String, text: String, priority: String, seenLabel: String) {
        val seen = PendingIntent.getBroadcast(
            context, ("seen:$alertId").hashCode(),
            Intent(context, RingReceiver::class.java).setAction(RingReceiver.ACTION_SEEN).putExtra(RingReceiver.EXTRA_ALERT, alertId),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val label = seenLabel.ifBlank { "Seen" }
        fun action() = Notification.Action.Builder(Icon.createWithResource(context, icon()), label, seen).build()
        // On a locked phone: who it is from and the Seen button, never the words themselves.
        val locked = builder(CHANNEL_RING_ITEM)
            .setSmallIcon(icon())
            .setContentTitle(title)
            .setContentText(if (label == "Seen") "Tap Seen, or unlock to read it." else "“$label” दबाइए, या पढ़ने के लिए अनलॉक कीजिए।")
            .addAction(action())
            .build()
        val n = builder(CHANNEL_RING_ITEM)
            .setSmallIcon(icon())
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setContentIntent(openApp(openKey = "alert:$alertId"))
            .addAction(action())
            .setAutoCancel(true)
            .setOnlyAlertOnce(true)
            .setCategory(Notification.CATEGORY_REMINDER)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setPublicVersion(locked)
            .setGroup(RING_GROUP)
            .setSortKey(when (priority) { "URGENT" -> "0"; "HIGH" -> "1"; else -> "2" }) // the most urgent on top
            .apply { if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) setGroupAlertBehavior(Notification.GROUP_ALERT_SUMMARY) }
            .build()
        manager.notify(ringId(alertId), n)
        addActive(alertId)
    }

    /**
     * The ring itself: one summary over the items, with the "ting ting" (or vibration only in meeting mode). Posted
     * at every ring, so every ring is heard once. With `alert = false` it only updates the count, silently.
     */
    fun showRingSummary(title: String, body: String, quiet: Boolean, alert: Boolean = true) {
        val channel = if (!alert) CHANNEL_RING_ITEM else if (quiet) CHANNEL_RING_QUIET else CHANNEL_RING
        val n = builder(channel)
            .setSmallIcon(icon())
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(Notification.BigTextStyle().bigText(body))
            .setContentIntent(openApp())
            .setAutoCancel(false)
            .setOnlyAlertOnce(!alert)
            .setCategory(Notification.CATEGORY_REMINDER)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setGroup(RING_GROUP)
            .setGroupSummary(true)
            .apply {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) setGroupAlertBehavior(Notification.GROUP_ALERT_SUMMARY)
                else if (alert) legacySound(this, quiet)
            }
            .build()
        manager.notify(RING_SUMMARY_ID, n)
    }

    /** Before Android 8 a notification carries its own sound (there are no channels). */
    @Suppress("DEPRECATION")
    private fun legacySound(b: Notification.Builder, quiet: Boolean) {
        if (quiet) b.setDefaults(Notification.DEFAULT_VIBRATE) else b.setSound(ting).setVibrate(longArrayOf(0, 300, 180, 300))
    }

    /** One item: shown (silently) and rung (the summary sounds). The first delivery of work or a message. */
    fun showRing(alertId: String, title: String, text: String, priority: String, quiet: Boolean, seenLabel: String) {
        showRingItem(alertId, title, text, priority, seenLabel)
        val n = active().size
        showRingSummary(if (n <= 1) title else waitingTitle(n), if (n <= 1) text else waitingBody(n), quiet, alert = true)
    }

    /** Seen (here or anywhere else), done or withdrawn: the item goes; the summary follows the rest, silently. */
    fun cancelRing(alertId: String) {
        manager.cancel(ringId(alertId))
        val left = removeActive(alertId)
        if (left.isEmpty()) manager.cancel(RING_SUMMARY_ID)
        else showRingSummary(waitingTitle(left.size), waitingBody(left.size), quiet = true, alert = false)
    }

    /** Nothing waits any more (the list says so): everything of the ring group goes. */
    fun clearRings() {
        for (id in active()) manager.cancel(ringId(id))
        manager.cancel(RING_SUMMARY_ID)
        prefs.edit().putStringSet(WakeBus.KEY_RING_ACTIVE, emptySet()).apply()
    }

    fun active(): Set<String> = prefs.getStringSet(WakeBus.KEY_RING_ACTIVE, emptySet()) ?: emptySet()

    private fun addActive(id: String) {
        val set = active().toMutableSet()
        if (set.add(id)) prefs.edit().putStringSet(WakeBus.KEY_RING_ACTIVE, set).apply()
    }

    private fun removeActive(id: String): Set<String> {
        val set = active().toMutableSet()
        if (set.remove(id)) prefs.edit().putStringSet(WakeBus.KEY_RING_ACTIVE, set).apply()
        return set
    }

    private fun hindi(): Boolean = prefs.getString(WakeBus.KEY_REPLY_LANG, "") == "hi"
    private fun waitingTitle(n: Int) = if (hindi()) "$n चीज़ें आपके देखने का इंतज़ार कर रही हैं" else "$n waiting for you"
    private fun waitingBody(n: Int) = if (hindi()) "हर एक पर “देख लिया” दबाइए, या JARVIS खोलिए।" else "Tap Seen on each, or open JARVIS."

    /** After Seen on a locked phone: the question that follows ("Shall I tell Sir when you'll do it?"), one tap away. */
    fun showAsk(alertId: String, title: String, question: String) {
        manager.notify(
            ("ask:$alertId").hashCode(),
            builder(CHANNEL_REMINDERS_SILENT).setSmallIcon(icon())
                .setContentTitle(title)
                .setContentText(question)
                .setStyle(Notification.BigTextStyle().bigText(question))
                .setContentIntent(openApp(openKey = "promise:$alertId"))
                .setAutoCancel(true)
                .setTimeoutAfter(30 * 60_000L)
                .setDefaults(Notification.DEFAULT_VIBRATE)
                .build(),
        )
    }

    private fun ringId(alertId: String) = ("alert:$alertId").hashCode()

    // ---------------------------------------------------------------- walkie-talkie
    /** Somebody is talking on the walkie-talkie while the app is closed or the phone locked: the sound already
     *  plays; this card brings the Talk screen up (full screen on a locked phone) with Hold to reply. */
    fun showIncomingTalk(from: String, talkId: String) {
        val open = openApp(openKey = "walkie:$talkId")
        manager.notify(
            WALKIE_ID,
            builder(CHANNEL_REMINDERS).setSmallIcon(icon())
                .setContentTitle("$from is talking…")
                .setContentText("Walkie-talkie · tap to reply")
                .setContentIntent(open)
                .setFullScreenIntent(open, true)
                .setCategory(Notification.CATEGORY_CALL)
                .setPriority(Notification.PRIORITY_MAX)
                .setAutoCancel(true)
                .setTimeoutAfter(90_000)
                .setOnlyAlertOnce(true)
                .build(),
        )
    }

    fun cancelIncomingTalk() = manager.cancel(WALKIE_ID)

    fun showSwitchOn() {
        manager.notify(
            SWITCH_ON_ID,
            builder(CHANNEL_REMINDERS).setSmallIcon(icon())
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
        manager.notify(
            SERVICE_ID,
            builder(CHANNEL_SERVICE).setSmallIcon(icon())
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
        manager.notify(
            WAKE_ID,
            builder(CHANNEL_REMINDERS).setSmallIcon(icon())
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
        const val CHANNEL_REMINDERS_SILENT = "jarvis-background-silent"
        const val CHANNEL_RING = "jarvis-ring"
        const val CHANNEL_RING_QUIET = "jarvis-ring-quiet"
        const val CHANNEL_RING_ITEM = "jarvis-ring-item"
        const val RING_GROUP = "jarvis-ring"
        const val SERVICE_ID = 7001
        private const val SWITCH_ON_ID = 7002
        private const val WAKE_ID = 7003
        private const val RING_SUMMARY_ID = 7004
        private const val WALKIE_ID = 7005
    }
}
