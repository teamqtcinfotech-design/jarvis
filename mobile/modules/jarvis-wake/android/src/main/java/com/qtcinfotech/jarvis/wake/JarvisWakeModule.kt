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
import androidx.core.content.ContextCompat
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition

/** JS-facing side of the "Hey JARVIS" listener. See src/lib/wake.ts for the wrapper the app uses. */
class JarvisWakeModule : Module() {
    private val context: Context
        get() = appContext.reactContext ?: throw IllegalStateException("No React context")

    override fun definition() = ModuleDefinition {
        Name("JarvisWake")
        Events("wake")

        OnCreate {
            WakeBus.listener = { sendEvent("wake", emptyMap<String, Any>()) }
        }
        OnDestroy {
            WakeBus.listener = null
        }
        OnActivityEntersForeground {
            WakeBus.appInForeground = true
            val extras = appContext.currentActivity?.intent
            if (extras?.getBooleanExtra(WakeService.EXTRA_WAKE, false) == true) {
                extras.removeExtra(WakeService.EXTRA_WAKE)
                WakeBus.pendingWake = true
            }
        }
        OnActivityEntersBackground {
            WakeBus.appInForeground = false
        }

        AsyncFunction("getStatus") {
            val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
            mapOf(
                "supported" to true,
                "running" to WakeBus.running,
                "overlayAllowed" to (Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(context)),
                "micAllowed" to (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED),
                "batteryUnrestricted" to (Build.VERSION.SDK_INT < Build.VERSION_CODES.M || pm.isIgnoringBatteryOptimizations(context.packageName)),
                "manufacturer" to (Build.MANUFACTURER ?: ""),
            )
        }

        AsyncFunction("start") { config: Map<String, Any?> ->
            WakeBus.prefs(context).edit()
                .putBoolean(WakeBus.KEY_ENABLED, true)
                .putString(WakeBus.KEY_SERVER, (config["serverUrl"] as? String ?: "").trimEnd('/'))
                .putString(WakeBus.KEY_ACCESS, config["accessToken"] as? String ?: "")
                .putString(WakeBus.KEY_REFRESH, config["refreshToken"] as? String ?: "")
                .putString(WakeBus.KEY_ADDRESS, config["address"] as? String ?: "Sir")
                .putBoolean(WakeBus.KEY_SPEAK_LOCKED, config["speakWhenLocked"] as? Boolean ?: false)
                .apply()
            if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                return@AsyncFunction false
            }
            WakeBus.paused = false
            WakeService.start(context)
            true
        }

        AsyncFunction("stop") {
            WakeBus.prefs(context).edit().putBoolean(WakeBus.KEY_ENABLED, false).apply()
            WakeService.stop(context)
            WakeBus.running = false
        }

        Function("pause") { WakeBus.paused = true }
        Function("resume") { WakeBus.paused = false }

        Function("consumePendingWake") {
            val pending = WakeBus.pendingWake
            WakeBus.pendingWake = false
            pending
        }

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

        Function("openAutostartSettings") {
            // Xiaomi / Redmi / POCO: Security app -> Autostart. Other brands have no such page.
            val candidates = listOf(
                ComponentName("com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity"),
                ComponentName("com.miui.securitycenter", "com.miui.securitycenter.Main"),
            )
            candidates.any { open(Intent().setComponent(it)) }
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
