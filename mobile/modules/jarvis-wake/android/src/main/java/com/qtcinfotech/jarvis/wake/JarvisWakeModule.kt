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
import expo.modules.kotlin.Promise
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import org.json.JSONObject

/** JS-facing side of the "Hey JARVIS" listener. See src/lib/wake.ts for the wrapper the app uses. */
class JarvisWakeModule : Module() {
    private val context: Context
        get() = appContext.reactContext ?: throw IllegalStateException("No React context")

    override fun definition() = ModuleDefinition {
        Name("JarvisWake")
        Events("wake", "tts", "push", "barge", "walkie", "live")

        OnCreate {
            WakeBus.listener = { sendEvent("wake", emptyMap<String, Any>()) }
            // The walkie-talkie engine tells the app what happens on the line (people, floor, incoming, levels).
            WalkieEngine.listener = { event, data ->
                try {
                    sendEvent("walkie", mapOf("event" to event, "data" to data.toString()))
                } catch (_: Throwable) {
                }
            }
            WakeBus.pushListener = {
                try {
                    sendEvent("push", emptyMap<String, Any>())
                } catch (_: Throwable) {
                }
            }
            // The live voice line tells the app what happens on it (heard, said, a form to run, speaking, closed).
            LiveVoice.listener = { event, data ->
                try {
                    sendEvent("live", mapOf("event" to event, "data" to data.toString()))
                } catch (_: Throwable) {
                }
            }
            WakeBus.bargeListener = {
                try {
                    sendEvent("barge", emptyMap<String, Any>())
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
            WakeBus.bargeListener = null
            TtsEngine.callback = null
            WalkieEngine.listener = null
            LiveVoice.listener = null
            LiveVoice.stop("destroyed")
        }

        // ---------------------------------------------------------------- the live voice line (Grok realtime)
        Function("liveSupported") { true }
        /** Open the line with what the server handed the app (url, token, the session as JSON text, the rules). */
        AsyncFunction("liveStart") { config: Map<String, Any?> ->
            val json = JSONObject()
            for ((k, v) in config) if (v != null) json.put(k, v)
            LiveVoice.start(context, json)
        }
        Function("liveStop") { LiveVoice.stop("app") }
        Function("liveToolResult") { callId: String, output: String -> LiveVoice.toolResult(callId, output) }
        Function("liveSay") { text: String -> LiveVoice.say(text) }
        Function("liveInterrupt") { LiveVoice.interrupt() }
        Function("liveStatus") { LiveVoice.status().toString() }

        // ---------------------------------------------------------------- sign in by phone (an SMS code)
        Function("otpSupported") { true }
        AsyncFunction("otpStart") { phone: String, promise: Promise -> PhoneOtp.start(appContext.currentActivity, phone, promise) }
        AsyncFunction("otpConfirm") { code: String, promise: Promise -> PhoneOtp.confirm(code, promise) }
        AsyncFunction("otpAwaitAuto") { promise: Promise -> PhoneOtp.awaitAuto(promise) }
        Function("otpCancel") { PhoneOtp.cancel() }

        // ---------------------------------------------------------------- walkie-talkie
        /** The person's switch and the relay's address (from /me/): the line opens when both are set. */
        AsyncFunction("walkieStart") { url: String, enabled: Boolean ->
            WakeBus.prefs(context).edit().putBoolean(WakeBus.KEY_WALKIE_ENABLED, enabled).putString(WakeBus.KEY_WALKIE_URL, url.trim()).apply()
            if (enabled) WalkieEngine.start(context) else WalkieEngine.stop()
            enabled
        }
        /** Hold to talk: to these people (a new talk), or a reply inside a talk. The sound streams at once. */
        Function("walkieTalk") { to: List<Int>, talk: String? -> WalkieEngine.talkStart(to, talk) }
        Function("walkieEnd") { WalkieEngine.talkEnd() }
        Function("walkieQuiet") { minutes: Int -> WalkieEngine.quiet(minutes) }
        Function("walkieRefresh") { WalkieEngine.refreshPeople() }
        Function("walkieStatus") { WalkieEngine.status().toString() }
        /** The talk behind the incoming card that opened the app, once; null otherwise. */
        Function("consumePendingWalkie") {
            val talk = WakeBus.pendingWalkieTalk
            WakeBus.pendingWalkieTalk = null
            talk
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

        // JARVIS's voice changed in Settings: used from the next sentence on (no restart needed).
        Function("setVoiceName") { name: String ->
            if (name.isNotBlank()) WakeBus.prefs(context).edit().putString(WakeBus.KEY_VOICE_NAME, name).apply()
        }
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
                .putBoolean(WakeBus.KEY_WALKIE_ENABLED, config["walkie"] as? Boolean ?: false)
                .putString(WakeBus.KEY_WALKIE_URL, (config["walkieUrl"] as? String ?: "").trim())
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
            (config["bargeIn"] as? Boolean)?.let { more.putBoolean(WakeBus.KEY_BARGE_IN, it) }
            (config["floatingButton"] as? Boolean)?.let { more.putBoolean(WakeBus.KEY_BUBBLE, it) }
            (config["voiceName"] as? String)?.takeIf { it.isNotBlank() }?.let { more.putString(WakeBus.KEY_VOICE_NAME, it) }
            // The wake word chosen (its listener file, the phrase, the assistant's name that goes with it).
            (config["wakeModel"] as? String)?.takeIf { it.isNotBlank() }?.let { more.putString(WakeBus.KEY_WAKE_MODEL, it) }
            (config["wakePhrase"] as? String)?.takeIf { it.isNotBlank() }?.let { more.putString(WakeBus.KEY_WAKE_PHRASE, it) }
            (config["assistantName"] as? String)?.takeIf { it.isNotBlank() }?.let { more.putString(WakeBus.KEY_ASSISTANT_NAME, it) }
            (config["assistantNameHi"] as? String)?.takeIf { it.isNotBlank() }?.let { more.putString(WakeBus.KEY_ASSISTANT_NAME_HI, it) }
            (config["hints"] as? List<*>)?.let { list ->
                more.putString(WakeBus.KEY_HINTS, list.mapNotNull { (it as? String)?.trim() }.filter { it.isNotEmpty() }.take(60).joinToString("\n"))
            }
            more.apply()
            WakeBus.paused = false
            WakeService.start(context)
            if (config["walkie"] as? Boolean == true) WalkieEngine.start(context) else WalkieEngine.stop()
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
            WatchdogReceiver.cancel(context)
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

        /** A photo or file shared into JARVIS (Android's Share sheet), as JSON, once; null otherwise. */
        Function("consumePendingShare") {
            val share = WakeBus.pendingShare
            WakeBus.pendingShare = null
            share
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

    /** What the intent that brought the app up says: woken by "Hey JARVIS", a tapped reminder / alert notification,
     *  or a photo / file shared into JARVIS from another app. */
    private fun takeExtras(intent: Intent) {
        if (intent.getBooleanExtra(WakeService.EXTRA_WAKE, false)) {
            intent.removeExtra(WakeService.EXTRA_WAKE)
            WakeBus.pendingWake = true
        }
        if (intent.action == Intent.ACTION_SEND && intent.type != null) {
            // "Share to JARVIS": the file's content URI (kept readable for this app by the sharing app's grant) and
            // any words sent with it. Read once; the intent is then plain so a later return does not repeat it.
            @Suppress("DEPRECATION")
            val uri: Uri? = intent.getParcelableExtra(Intent.EXTRA_STREAM)
            val text = intent.getStringExtra(Intent.EXTRA_TEXT) ?: ""
            if (uri != null) {
                var name = uri.lastPathSegment ?: "shared"
                try {
                    appContext.reactContext?.contentResolver?.query(uri, arrayOf(android.provider.OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                        if (c.moveToFirst()) c.getString(0)?.let { n -> if (n.isNotBlank()) name = n }
                    }
                } catch (_: Exception) {
                    // the name is only for the label
                }
                WakeBus.pendingShare = JSONObject().put("uri", uri.toString()).put("mime", intent.type ?: "").put("name", name).put("text", text).toString()
            }
            intent.action = Intent.ACTION_MAIN
            intent.removeExtra(Intent.EXTRA_STREAM)
            intent.removeExtra(Intent.EXTRA_TEXT)
        }
        intent.getStringExtra(WakeService.EXTRA_OPEN_KEY)?.let {
            intent.removeExtra(WakeService.EXTRA_OPEN_KEY)
            if (it.startsWith("walkie:")) {
                // The incoming-talk card: the Talk screen comes up over the lock screen, ready for Hold to reply.
                WakeBus.pendingWalkieTalk = it.removePrefix("walkie:")
                appContext.currentActivity?.let { a ->
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
                        a.setShowWhenLocked(true)
                        a.setTurnScreenOn(true)
                    }
                }
            } else if (it.isNotBlank()) {
                WakeBus.pendingOpenKey = it
            }
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
