package com.qtcinfotech.jarvis.wake

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition

/** JS-facing side of the "Hey JARVIS" listener. See src/lib/wake.ts for the wrapper the app uses. */
class JarvisWakeModule : Module() {
    private val context: Context
        get() = appContext.reactContext ?: throw IllegalStateException("No React context")

    override fun definition() = ModuleDefinition {
        Name("JarvisWake")
        Events("wake", "tts", "push")

        OnCreate {
            WakeBus.listener = { sendEvent("wake", emptyMap<String, Any>()) }
            WakeBus.pushListener = {
                try {
                    sendEvent("push", emptyMap<String, Any>())
                } catch (_: Throwable) {
                }
            }
            TtsEngine.init(context)
            TtsEngine.callback = { id, event ->
                try {
                    sendEvent("tts", mapOf("id" to id, "event" to event))
                } catch (_: Throwable) {
                }
            }
        }
        OnDestroy {
            WakeBus.listener = null
            WakeBus.pushListener = null
            TtsEngine.callback = null
        }

        // Live push: this phone's Firebase token (registered with the server by the app), and its removal at sign-out.
        AsyncFunction("getPushToken") {
            val token = PushService.currentToken()
            if (token != null) WakeBus.prefs(context).edit().putString(WakeBus.KEY_PUSH_TOKEN, token).apply()
            token
        }
        AsyncFunction("deletePushToken") {
            WakeBus.prefs(context).edit().remove(WakeBus.KEY_PUSH_TOKEN).apply()
            PushService.deleteToken()
        }

        // JARVIS's voice: reconnects by itself and always reports back (see TtsEngine).
        Function("speak") { id: String, text: String, lang: String, fallback: String?, rate: Double ->
            TtsEngine.speak(TtsEngine.Request(id, text, fallback, lang, rate.toFloat()))
        }
        Function("stopSpeaking") { TtsEngine.stop() }
        OnActivityEntersForeground {
            WakeBus.appInForeground = true
            appContext.currentActivity?.intent?.let { takeExtras(it) }
        }
        // The app was already running and a notification was tapped: the new intent carries what was tapped.
        OnNewIntent { intent -> takeExtras(intent) }
        OnActivityEntersBackground {
            WakeBus.appInForeground = false
            WakeBus.paused = false  // the app no longer needs the microphone: "Hey JARVIS" listens again at once
        }

        AsyncFunction("getStatus") {
            val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
            val (wakes, empty) = WakeBus.wakeCounts(context)
            mapOf(
                "supported" to true,
                "running" to WakeBus.running,
                "wakesToday" to wakes,
                "emptyWakesToday" to empty,
                // false while Android is silencing the microphone, so the app restarts the listening part
                "listening" to (WakeBus.listening && !WakeBus.micBlocked),
                "overlayAllowed" to (Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(context)),
                "micAllowed" to (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED),
                "notificationsAllowed" to NotificationManagerCompat.from(context).areNotificationsEnabled(),
                "batteryUnrestricted" to (Build.VERSION.SDK_INT < Build.VERSION_CODES.M || pm.isIgnoringBatteryOptimizations(context.packageName)),
                "manufacturer" to (Build.MANUFACTURER ?: ""),
            )
        }

        // Start (or re-apply) the background service. It always watches for reminders and messages; the microphone
        // part ("Hey JARVIS") runs only when wakeWord is true and the microphone is allowed. Returns whether it listens.
        AsyncFunction("start") { config: Map<String, Any?> ->
            val micAllowed = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
            val wakeWord = (config["wakeWord"] as? Boolean ?: true) && micAllowed
            WakeBus.prefs(context).edit()
                .putBoolean(WakeBus.KEY_ENABLED, true)
                .putBoolean(WakeBus.KEY_WAKE_WORD, wakeWord)
                .putString(WakeBus.KEY_SERVER, (config["serverUrl"] as? String ?: "").trimEnd('/'))
                .putString(WakeBus.KEY_ACCESS, config["accessToken"] as? String ?: "")
                .putString(WakeBus.KEY_REFRESH, config["refreshToken"] as? String ?: "")
                .putString(WakeBus.KEY_ADDRESS, config["address"] as? String ?: "Sir")
                .putString(WakeBus.KEY_ADDRESS_HI, (config["addressHi"] as? String)?.takeIf { it.isNotBlank() }
                    ?: (config["address"] as? String ?: "Sir"))
                .putBoolean(WakeBus.KEY_IS_MD, config["isMd"] as? Boolean ?: ((config["address"] as? String ?: "Sir") == "Sir"))
                .putBoolean(WakeBus.KEY_SPEAK_LOCKED, config["speakWhenLocked"] as? Boolean ?: false)
                .apply()
            (config["paName"] as? String)?.takeIf { it.isNotBlank() }?.let {
                WakeBus.prefs(context).edit().putString(WakeBus.KEY_PA_NAME, it).apply()
            }
            // The voice, the listening language, the reply language and the names to expect: kept when left out.
            val more = WakeBus.prefs(context).edit()
            (config["voiceAccent"] as? String)?.takeIf { it.isNotBlank() }?.let { more.putString(WakeBus.KEY_VOICE_ACCENT, it) }
            (config["listenLang"] as? String)?.takeIf { it.isNotBlank() }?.let { more.putString(WakeBus.KEY_LISTEN_LANG, it) }
            (config["replyLang"] as? String)?.takeIf { it == "hi" || it == "en" }?.let { more.putString(WakeBus.KEY_REPLY_LANG, it) }
            (config["reminderSound"] as? String)?.takeIf { it == "default" || it == "silent" }?.let { more.putString(WakeBus.KEY_REMINDER_SOUND, it) }
            (config["hints"] as? List<*>)?.let { list ->
                more.putString(WakeBus.KEY_HINTS, list.mapNotNull { (it as? String)?.trim() }.filter { it.isNotEmpty() }.take(60).joinToString("\n"))
            }
            more.apply()
            WakeBus.paused = false
            WakeService.start(context)
            wakeWord
        }

        // Switch only the microphone part on or off; the watcher keeps running.
        AsyncFunction("setListening") { on: Boolean ->
            val micAllowed = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
            val wakeWord = on && micAllowed
            WakeBus.prefs(context).edit().putBoolean(WakeBus.KEY_WAKE_WORD, wakeWord).apply()
            if (WakeBus.running || WakeBus.prefs(context).getBoolean(WakeBus.KEY_ENABLED, false)) {
                WakeBus.paused = false
                WakeService.start(context)
            }
            wakeWord
        }

        // Signing out: the service stops for good (its login is gone).
        AsyncFunction("stop") {
            WakeBus.prefs(context).edit().putBoolean(WakeBus.KEY_ENABLED, false).apply()
            WakeService.stop(context)
            WakeBus.running = false
            WakeBus.listening = false
        }

        Function("pause") { WakeBus.paused = true }
        Function("resume") { WakeBus.paused = false }

        Function("consumePendingWake") {
            val pending = WakeBus.pendingWake
            WakeBus.pendingWake = false
            pending
        }

        /** The reminder / alert key behind the notification that opened the app, once; null otherwise. */
        Function("consumePendingOpen") {
            val key = WakeBus.pendingOpenKey
            WakeBus.pendingOpenKey = null
            key
        }

        /** The app was woken by "Hey JARVIS" and nothing was said after the greeting (a false wake, most likely). */
        Function("noteEmptyWake") { WakeBus.countEmptyWake(context) }

        Function("openOverlaySettings") {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                open(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:${context.packageName}")))
            }
        }

        Function("openBatterySettings") {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                // Asks to ignore battery optimisations for JARVIS (Android shows its own confirmation).
                val ok = open(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${context.packageName}")))
                if (!ok) open(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            }
        }

        Function("openMiuiPermissions") {
            // Xiaomi / Redmi / POCO: the app's own permission page, where "Display pop-up windows while running
            // in the background" lives. Without it MIUI silently aborts JARVIS popping up on "Hey JARVIS".
            val intent = Intent("miui.intent.action.APP_PERM_EDITOR").apply {
                setClassName("com.miui.securitycenter", "com.miui.permcenter.permissions.PermissionsEditorActivity")
                putExtra("extra_pkgname", context.packageName)
            }
            open(intent) || open(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${context.packageName}")))
        }

        Function("openAutostartSettings") {
            // Xiaomi / Redmi / POCO: Security app -> Autostart. Other brands have no such page.
            val candidates = listOf(
                ComponentName("com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity"),
                ComponentName("com.miui.securitycenter", "com.miui.securitycenter.Main"),
            )
            candidates.any { open(Intent().setComponent(it)) }
        }
    }

    /** What the intent that brought the app up says: woken by "Hey JARVIS", or a tapped reminder / alert notification. */
    private fun takeExtras(intent: Intent) {
        if (intent.getBooleanExtra(WakeService.EXTRA_WAKE, false)) {
            intent.removeExtra(WakeService.EXTRA_WAKE)
            WakeBus.pendingWake = true
        }
        intent.getStringExtra(WakeService.EXTRA_OPEN_KEY)?.let {
            intent.removeExtra(WakeService.EXTRA_OPEN_KEY)
            if (it.isNotBlank()) WakeBus.pendingOpenKey = it
        }
    }

    private fun open(intent: Intent): Boolean = try {
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val activity = appContext.currentActivity
        if (activity != null) activity.startActivity(intent) else context.startActivity(intent)
        true
    } catch (_: Throwable) {
        false
    }
}
