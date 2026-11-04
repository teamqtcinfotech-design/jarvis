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
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * Foreground service: listens for "Hey JARVIS" on the phone (no audio leaves it) and, while the app is closed,
 * speaks reminders that come due. A permanent notification shows while it runs; "Stop" ends it.
 */
class WakeService : Service() {
    private lateinit var notifier: WakeNotifier
    private var audioThread: Thread? = null
    @Volatile private var stopped = false
    private var watcherThread: HandlerThread? = null
    private var watcher: ReminderWatcher? = null

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
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "No microphone permission; not starting.")
            stopSelf()
            return START_NOT_STICKY
        }
        val notification = notifier.serviceNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            startForeground(WakeNotifier.SERVICE_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(WakeNotifier.SERVICE_ID, notification)
        }
        notifier.cancelSwitchOn()
        if (audioThread == null) startListening()
        if (watcherThread == null) startWatcher()
        WakeBus.running = true
        return START_STICKY
    }

    private fun startListening() {
        stopped = false
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
            try {
                while (!stopped) {
                    if (WakeBus.paused || WakeBus.appInForeground && WakeBus.listener == null) {
                        // Someone else needs the mic (JARVIS itself is listening or talking): let it go.
                        if (recording) {
                            record.stop()
                            recording = false
                        }
                        Thread.sleep(150)
                        continue
                    }
                    if (!recording) {
                        record.startRecording()
                        recording = true
                    }
                    var got = 0
                    while (got < chunk.size && !stopped) {
                        val n = record.read(chunk, got, chunk.size - got)
                        if (n <= 0) break
                        got += n
                    }
                    if (got < chunk.size) continue
                    val score = detector.process(chunk)
                    val now = System.currentTimeMillis()
                    if (score >= THRESHOLD && now > cooldownUntil) {
                        cooldownUntil = now + 2500
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
            WakeBus.onWake()
            return
        }
        // App closed or behind another app: bring it to the front; the app greets ("Ji Sir?") once it is up.
        WakeBus.pendingWake = true
        val launch = packageManager.getLaunchIntentForPackage(packageName) ?: return
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
        launch.putExtra(EXTRA_WAKE, true)
        try {
            startActivity(launch)
        } catch (e: Throwable) {
            Log.w(TAG, "Could not open the app from the background: ${e.message}")
        }
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
        private const val THRESHOLD = 0.5f
        private const val WATCH_EVERY_MS = 60_000L

        fun start(context: Context) {
            val intent = Intent(context, WakeService::class.java)
            ContextCompat.startForegroundService(context, intent)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, WakeService::class.java))
        }
    }
}
