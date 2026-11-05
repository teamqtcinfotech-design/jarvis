package com.qtcinfotech.jarvis.wake

import android.Manifest
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.ToneGenerator
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * Foreground service that runs whenever someone is signed in on this phone:
 *
 *  - always: checks the server every half minute for reminders due now and messages that just arrived, and
 *    speaks or shows them while the app is closed (ReminderWatcher);
 *  - when "Hey JARVIS" is on: also listens for the wake word on the phone (no audio leaves it).
 *
 * A permanent notification shows while it runs. The microphone part can be switched on and off without
 * stopping the watcher; after a reboot the watcher comes back by itself and the microphone waits for one tap.
 */
class WakeService : Service() {
    private lateinit var notifier: WakeNotifier
    private var audioThread: Thread? = null
    @Volatile private var stopped = false
    @Volatile private var micStopped = false
    private var watcherThread: HandlerThread? = null
    private var watcher: ReminderWatcher? = null
    private var loop: ConversationLoop? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        notifier = WakeNotifier(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            WakeBus.prefs(this).edit().putBoolean(WakeBus.KEY_ENABLED, false).apply()
            stopSelf()
            return START_NOT_STICKY
        }
        val prefs = WakeBus.prefs(this)
        val micAllowed = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        // Started from the boot receiver (app in the background): Android does not allow the microphone then.
        val noMic = intent?.getBooleanExtra(EXTRA_NO_MIC, false) == true
        val wantMic = prefs.getBoolean(WakeBus.KEY_WAKE_WORD, false) && micAllowed && !noMic

        val notification = notifier.serviceNotification(listening = wantMic)
        when {
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE ->
                startForeground(WakeNotifier.SERVICE_ID, notification,
                    if (wantMic) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.R ->
                startForeground(WakeNotifier.SERVICE_ID, notification,
                    if (wantMic) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else ServiceInfo.FOREGROUND_SERVICE_TYPE_MANIFEST)
            else -> startForeground(WakeNotifier.SERVICE_ID, notification)
        }
        if (wantMic) notifier.cancelSwitchOn()
        if (wantMic && audioThread == null) startListening()
        if (!wantMic && audioThread != null) stopListening()
        if (watcherThread == null) startWatcher()
        WakeBus.running = true
        WakeBus.listening = wantMic
        return START_STICKY
    }

    private fun stopListening() {
        micStopped = true
        audioThread?.interrupt()
        audioThread = null
        WakeBus.listening = false
        Log.i(TAG, "Hey JARVIS switched off; still watching for reminders and messages")
    }

    private fun startListening() {
        stopped = false
        micStopped = false
        audioThread = Thread({
            val detector = try {
                WakeDetector(this)
            } catch (e: Throwable) {
                Log.e(TAG, "Could not load the wake-word models", e)
                stopSelf()
                return@Thread
            }
            val minBuf = AudioRecord.getMinBufferSize(WakeDetector.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val record = try {
                AudioRecord(
                    MediaRecorder.AudioSource.VOICE_RECOGNITION, WakeDetector.SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, WakeDetector.CHUNK * 4),
                )
            } catch (e: SecurityException) {
                Log.e(TAG, "Microphone not available", e)
                detector.close()
                stopSelf()
                return@Thread
            }
            if (record.state != AudioRecord.STATE_INITIALIZED) {
                Log.e(TAG, "AudioRecord failed to initialise")
                record.release()
                detector.close()
                stopSelf()
                return@Thread
            }
            val chunk = ShortArray(WakeDetector.CHUNK)
            var cooldownUntil = 0L
            var recording = false
            var windowMax = 0f
            var windowLevel = 0.0
            var windowChunks = 0
            var lastReport = System.currentTimeMillis()
            Log.i(TAG, "Listening for Hey JARVIS (models loaded)")
            try {
                while (!stopped && !micStopped) {
                    if (WakeBus.paused || WakeBus.inConversation || WakeBus.appInForeground && WakeBus.listener == null) {
                        // Someone else needs the mic (JARVIS itself is listening or talking): let it go.
                        if (recording) {
                            record.stop()
                            recording = false
                            Log.i(TAG, "paused: microphone released")
                        }
                        Thread.sleep(100)
                        continue
                    }
                    if (!recording) {
                        record.startRecording()
                        recording = true
                        Log.i(TAG, "listening again (state=${record.recordingState})")
                    }
                    var got = 0
                    while (got < chunk.size && !stopped && !micStopped) {
                        val n = record.read(chunk, got, chunk.size - got)
                        if (n <= 0) break
                        got += n
                    }
                    if (got < chunk.size) continue
                    val score = detector.process(chunk)
                    // Diagnostics: every 2 s, the loudest score and the sound level (0 = silence, ~3000 = speech).
                    var sum = 0.0
                    for (s in chunk) sum += (s * s).toDouble()
                    windowLevel += Math.sqrt(sum / chunk.size)
                    windowChunks += 1
                    if (score > windowMax) windowMax = score
                    val now = System.currentTimeMillis()
                    if (now - lastReport >= 2000) {
                        Log.i(TAG, String.format("score max=%.3f level=%.0f", windowMax, windowLevel / windowChunks))
                        windowMax = 0f
                        windowLevel = 0.0
                        windowChunks = 0
                        lastReport = now
                    }
                    if (score >= THRESHOLD && now > cooldownUntil) {
                        cooldownUntil = now + 2500
                        Log.i(TAG, String.format("Hey JARVIS detected (score %.3f)", score))
                        onWakeWord()
                    }
                }
            } catch (e: Throwable) {
                Log.e(TAG, "Listening stopped", e)
            } finally {
                try {
                    if (recording) record.stop()
                } catch (_: Throwable) {
                }
                record.release()
                detector.close()
            }
        }, "jarvis-wake").apply {
            priority = Thread.NORM_PRIORITY
            start()
        }
    }

    private fun onWakeWord() {
        try {
            ToneGenerator(AudioManager.STREAM_NOTIFICATION, 70).apply {
                startTone(ToneGenerator.TONE_PROP_ACK, 180)
            }
        } catch (_: Throwable) {
        }
        if (WakeBus.appInForeground && WakeBus.listener != null) {
            WakeBus.onWake() // the app is open: it runs the conversation on screen
            return
        }
        // App closed (or behind another app, or the phone locked): talk right here, voice only. The screen stays as it is.
        if (ConversationLoop.available(this)) {
            val l = loop ?: ConversationLoop(this, notifier).also { loop = it }
            l.start()
            return
        }
        // No speech recognition service on this phone: fall back to bringing the app up.
        WakeBus.pendingWake = true
        val launch = packageManager.getLaunchIntentForPackage(packageName) ?: return
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
        launch.putExtra(EXTRA_WAKE, true)
        try {
            startActivity(launch)
        } catch (e: Throwable) {
            Log.w(TAG, "Could not open the app from the background: ${e.message}")
        }
        // Xiaomi (MIUI) silently aborts pop-ups from the background unless its own switch is on.
        // If the app is still not in front after a moment: answer by voice and offer one tap to continue.
        Handler(Looper.getMainLooper()).postDelayed({
            if (!WakeBus.appInForeground && WakeBus.pendingWake) {
                Log.w(TAG, "App did not come to the front (MIUI background pop-up blocked?)")
                val address = WakeBus.prefs(this).getString(WakeBus.KEY_ADDRESS, "Sir") ?: "Sir"
                val sir = if (address == "Sir") "सर" else address
                TtsEngine.speak(TtsEngine.Request("wake-" + System.nanoTime(),
                    "जी $sir? जार्विस खोलने के लिए नोटिफ़िकेशन दबाइए।", "Ji $address? Tap the notification to open JARVIS.", "hi-IN", 0.95f))
                notifier.showWakeFallback(address)
            }
        }, 1500)
    }

    private fun startWatcher() {
        val thread = HandlerThread("jarvis-watch").apply { start() }
        watcherThread = thread
        val handler = Handler(thread.looper)
        val w = ReminderWatcher(this, notifier)
        watcher = w
        val task = object : Runnable {
            override fun run() {
                if (stopped) return
                val alive = try {
                    w.check()
                } catch (e: Throwable) {
                    Log.w(TAG, "reminder check failed: ${e.message}")
                    true
                }
                if (!alive) {
                    Log.w(TAG, "Background login expired; the app will set it up again when opened.")
                    handler.post { stopSelf() }
                    return
                }
                handler.postDelayed(this, WATCH_EVERY_MS)
            }
        }
        handler.postDelayed(task, 5_000)
    }

    override fun onDestroy() {
        stopped = true
        WakeBus.running = false
        WakeBus.listening = false
        loop?.destroy()
        loop = null
        audioThread?.interrupt()
        audioThread = null
        watcher?.close()
        watcher = null
        watcherThread?.quitSafely()
        watcherThread = null
        super.onDestroy()
    }

    companion object {
        private const val TAG = "JarvisWake"
        const val ACTION_STOP = "com.qtcinfotech.jarvis.wake.STOP"
        const val EXTRA_WAKE = "jarvis_wake"
        const val EXTRA_NO_MIC = "jarvis_no_mic"
        private const val THRESHOLD = 0.5f
        private const val WATCH_EVERY_MS = 30_000L  // a message from Sir or Mansi reaches a closed app within half a minute

        /** (Re)start the service; it reads the saved settings and switches the microphone part on or off to match. */
        fun start(context: Context, withMicrophone: Boolean = true) {
            val intent = Intent(context, WakeService::class.java)
            if (!withMicrophone) intent.putExtra(EXTRA_NO_MIC, true)
            ContextCompat.startForegroundService(context, intent)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, WakeService::class.java))
        }
    }
}
