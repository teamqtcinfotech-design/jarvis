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
import android.os.PowerManager
import android.util.Log
import androidx.core.content.ContextCompat
import org.json.JSONObject
import kotlin.math.abs
import kotlin.math.sqrt

/**
 * Foreground service that runs whenever someone is signed in on this phone:
 *
 *  - always: checks the server every half minute for reminders due now and messages that just arrived, and
 *    speaks or shows them while the app is closed (ReminderWatcher);
 *  - when "Hey JARVIS" is on: also listens for the wake word on the phone (no audio leaves it).
 *
 * A permanent notification shows while it runs. The microphone part can be switched on and off without
 * stopping the watcher.
 *
 * Making "Hey JARVIS" catch on the first try:
 *  - the processor is kept awake while listening (otherwise Android dozes with the screen off and the first
 *    words are lost);
 *  - quiet or distant speech is brought up to a normal level before it is judged;
 *  - two slices in a row above a lower bar also count, not only one very confident slice;
 *  - the microphone is reopened if Android silences it or hands it to another app and does not give it back;
 *  - after the microphone was lent to JARVIS's own listening, the detector starts from a clean slate.
 */
class WakeService : Service() {
    private lateinit var notifier: WakeNotifier
    private var audioThread: Thread? = null
    @Volatile private var stopped = false
    @Volatile private var micStopped = false
    /** Ask the listening thread to close and reopen the microphone (the app came to the front, or it was silenced). */
    @Volatile private var reopenMic = false
    private var watcherThread: HandlerThread? = null
    private var watcher: ReminderWatcher? = null
    @Volatile private var loop: ConversationLoop? = null
    /** One beep player for the life of the service (a new one for every wake-up is never given back to Android). */
    private var beep: ToneGenerator? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        notifier = WakeNotifier(this)
        current = this
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            WakeBus.prefs(this).edit().putBoolean(WakeBus.KEY_ENABLED, false).apply()
            stopSelf()
            return START_NOT_STICKY
        }
        val prefs = WakeBus.prefs(this)
        val micAllowed = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        val noMic = intent?.getBooleanExtra(EXTRA_NO_MIC, false) == true
        val wantMic = prefs.getBoolean(WakeBus.KEY_WAKE_WORD, false) && micAllowed && !noMic

        // Android may refuse the microphone type when the service is (re)started from the background.
        // Never crash for that: fall back to watch-only, and the app upgrades it the next time it is opened.
        val listening = startInForeground(wantMic)
        if (listening) notifier.cancelSwitchOn()
        if (listening && audioThread == null) startListening()
        else if (listening) reopenMic = true          // started again from the app: take a fresh microphone
        if (!listening && audioThread != null) stopListening()
        if (watcherThread == null) startWatcher()
        WakeBus.running = true
        WakeBus.listening = listening
        WakeBus.micBlocked = false
        prefetchGreetings()
        return START_STICKY
    }

    /** Returns whether the service is now a microphone service. */
    private fun startInForeground(wantMic: Boolean): Boolean {
        fun go(mic: Boolean) {
            val notification = notifier.serviceNotification(listening = mic)
            when {
                Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE ->
                    startForeground(WakeNotifier.SERVICE_ID, notification,
                        if (mic) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
                Build.VERSION.SDK_INT >= Build.VERSION_CODES.R ->
                    startForeground(WakeNotifier.SERVICE_ID, notification,
                        if (mic) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else ServiceInfo.FOREGROUND_SERVICE_TYPE_MANIFEST)
                else -> startForeground(WakeNotifier.SERVICE_ID, notification)
            }
        }
        if (wantMic) {
            try {
                go(true)
                return true
            } catch (e: Exception) {
                Log.w(TAG, "Microphone service refused from the background (${e.javaClass.simpleName}); watching only for now")
            }
        }
        try {
            go(false)
        } catch (e: Exception) {
            Log.e(TAG, "Could not start in the foreground", e)
        }
        return false
    }

    private fun prefetchGreetings() {
        val prefs = WakeBus.prefs(this)
        val address = prefs.getString(WakeBus.KEY_ADDRESS, "Sir") ?: "Sir"
        val addressHi = prefs.getString(WakeBus.KEY_ADDRESS_HI, address) ?: address
        val isMd = prefs.getBoolean(WakeBus.KEY_IS_MD, address == "Sir")
        val lines = if (isMd) listOf("जी $addressHi?", "$addressHi?", "Done.") else listOf("जी?", "$address?", "Done.")
        GrokVoice.prefetch(this, lines)
    }

    private fun stopListening() {
        micStopped = true
        audioThread?.interrupt()
        audioThread = null
        WakeBus.listening = false
        Log.i(TAG, "Hey JARVIS switched off; still watching for reminders and messages")
    }

    private fun openRecorder(): AudioRecord? {
        val minBuf = AudioRecord.getMinBufferSize(WakeDetector.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        return try {
            val record = AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION, WakeDetector.SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, WakeDetector.CHUNK * 8),
            )
            if (record.state != AudioRecord.STATE_INITIALIZED) {
                record.release()
                null
            } else record
        } catch (e: Exception) {
            Log.w(TAG, "Microphone not available: ${e.message}")
            null
        }
    }

    private fun startListening() {
        stopped = false
        micStopped = false
        reopenMic = false
        audioThread = Thread({
            val detector = try {
                WakeDetector(this)
            } catch (e: Throwable) {
                Log.e(TAG, "Could not load the wake-word models", e)
                WakeBus.listening = false
                return@Thread
            }
            // Keep the processor awake while listening; without this Android dozes with the screen off and
            // the first "Hey JARVIS" (or two) is simply not heard.
            val wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "jarvis:wakeword").apply { setReferenceCounted(false) }
            var record: AudioRecord? = null
            val chunk = ShortArray(WakeDetector.CHUNK)
            val boosted = ShortArray(WakeDetector.CHUNK)
            var recording = false
            var failures = 0
            var silentChunks = 0
            var envelope = 0.0
            var prevScore = 0f
            var streak = 0        // slices in a row scoring at least STREAK_THRESHOLD
            var cooldownUntil = 0L
            var windowMax = 0f
            var windowLevel = 0.0
            var windowGain = 0.0
            var windowChunks = 0
            var lastReport = System.currentTimeMillis()
            // Barge-in: while JARVIS talks, the microphone stays open just to hear whether HE starts talking. The
            // first moments of each sentence set the baseline (the phone's own voice reaching its microphone); a
            // sound well above that, held for a few slices, is him, and JARVIS stops and listens.
            var bargeSince = 0L           // the stretch of talking being measured (TtsEngine.talkingSince)
            var bargeBase = 0.0           // loudest slice during the baseline window
            var bargeBaseSlices = 0
            var bargeRun = 0              // slices in a row above the bar

            fun closeRecorder() {
                try {
                    if (recording) record?.stop()
                } catch (_: Throwable) {
                }
                try {
                    record?.release()
                } catch (_: Throwable) {
                }
                record = null
                recording = false
            }

            Log.i(TAG, "Listening for Hey JARVIS (models loaded)")
            try {
                wakeLock.acquire()
                while (!stopped && !micStopped) {
                    // A voice conversation that is no longer running must never keep "Hey JARVIS" switched off.
                    if (WakeBus.inConversation && loop?.active != true) WakeBus.inConversation = false
                    // While JARVIS itself is talking the microphone is kept (for barge-in) unless the app asked for
                    // silence; while JARVIS or the app is LISTENING the microphone must be let go.
                    val talking = TtsEngine.speaking && WakeBus.prefs(this).getBoolean(WakeBus.KEY_BARGE_IN, true)
                    val aside = WakeBus.paused || (WakeBus.inConversation && !talking) || (WakeBus.appInForeground && WakeBus.listener == null)
                    if (aside) {
                        // Someone else needs the mic (JARVIS itself is listening or talking): let it go completely.
                        if (record != null) {
                            closeRecorder()
                            Log.i(TAG, "paused: microphone released")
                        }
                        Thread.sleep(80)
                        continue
                    }
                    if (reopenMic) {
                        reopenMic = false
                        closeRecorder()
                    }
                    if (record == null) {
                        Thread.sleep(120)  // give whoever had the microphone a moment to let go
                        record = openRecorder()
                        if (record == null) {
                            Thread.sleep(800)
                            continue
                        }
                    }
                    val rec = record ?: continue
                    if (!recording) {
                        try {
                            rec.startRecording()
                        } catch (e: Throwable) {
                            Log.w(TAG, "could not start recording: ${e.message}")
                        }
                        if (rec.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                            closeRecorder()
                            Thread.sleep(500)
                            continue
                        }
                        recording = true
                        failures = 0
                        silentChunks = 0
                        prevScore = 0f
                        streak = 0
                        envelope = 0.0
                        detector.reset()  // nothing from before the pause may colour the first second back
                        Log.i(TAG, "listening again")
                    }

                    var got = 0
                    while (got < chunk.size && !stopped && !micStopped) {
                        val n = rec.read(chunk, got, chunk.size - got)
                        if (n <= 0) break
                        got += n
                    }
                    if (got < chunk.size) {
                        failures += 1
                        if (failures >= 5) {
                            Log.w(TAG, "microphone stopped delivering sound; reopening it")
                            closeRecorder()
                            failures = 0
                        }
                        Thread.sleep(60)
                        continue
                    }
                    failures = 0

                    // Level of this slice, and whether Android is feeding pure digital silence (it does that when
                    // another app has taken the microphone, or when the service was started in the background).
                    var sum = 0.0
                    var peak = 0
                    for (s in chunk) {
                        val v = s.toInt()
                        sum += (v * v).toDouble()
                        val a = abs(v)
                        if (a > peak) peak = a
                    }
                    val rms = sqrt(sum / chunk.size)
                    if (peak == 0) silentChunks += 1 else silentChunks = 0
                    if (silentChunks >= SILENT_CHUNKS_LIMIT) {
                        silentChunks = 0
                        Log.w(TAG, "microphone is being silenced; reopening it")
                        if (!WakeBus.appInForeground && !WakeBus.micBlocked) {
                            WakeBus.micBlocked = true   // the app restarts the microphone part when it is next opened
                            notifier.showSwitchOn()
                        }
                        closeRecorder()
                        Thread.sleep(1500)
                        continue
                    }
                    if (peak > 0 && WakeBus.micBlocked) WakeBus.micBlocked = false

                    // Barge-in while JARVIS talks: no wake-word judging of its own voice, only "did he start talking?"
                    if (talking) {
                        val since = TtsEngine.talkingSince
                        if (since != bargeSince) {
                            bargeSince = since
                            bargeBase = 0.0
                            bargeBaseSlices = 0
                            bargeRun = 0
                        }
                        val age = System.currentTimeMillis() - since
                        if (age < BARGE_BASELINE_MS) {
                            if (rms > bargeBase) bargeBase = rms
                            bargeBaseSlices += 1
                        } else if (bargeBaseSlices > 0) {
                            val bar = maxOf(bargeBase * BARGE_OVER_BASE, BARGE_MIN_LEVEL)
                            bargeRun = if (rms > bar) bargeRun + 1 else 0
                            if (bargeRun >= BARGE_SLICES) {
                                Log.i(TAG, String.format("barge-in: level %.0f over baseline %.0f; stopping the voice", rms, bargeBase))
                                bargeRun = 0
                                bargeSince = 0L
                                TtsEngine.stop()
                                WakeBus.onBarge()
                            }
                        }
                        detector.reset()
                        prevScore = 0f
                        streak = 0
                        continue
                    }

                    // Bring quiet or distant speech up to a normal level before judging it (fast up, slow down).
                    envelope = if (rms > envelope) rms else envelope * 0.92 + rms * 0.08
                    val gain = if (envelope > NOISE_FLOOR) (TARGET_LEVEL / envelope).coerceIn(1.0, MAX_GAIN) else 1.0
                    val input = if (gain > 1.05) {
                        for (i in chunk.indices) {
                            val v = (chunk[i] * gain).toInt()
                            boosted[i] = (if (v > 32767) 32767 else if (v < -32768) -32768 else v).toShort()
                        }
                        boosted
                    } else chunk

                    val score = detector.process(input)

                    // Diagnostics: every 2 s, the loudest score, the sound level (0 = silence, ~3000 = speech) and the gain.
                    windowLevel += rms
                    windowGain += gain
                    windowChunks += 1
                    if (score > windowMax) windowMax = score
                    val now = System.currentTimeMillis()
                    if (now - lastReport >= 2000) {
                        Log.i(TAG, String.format("score max=%.3f level=%.0f gain=%.1f", windowMax, windowLevel / windowChunks, windowGain / windowChunks))
                        windowMax = 0f
                        windowLevel = 0.0
                        windowGain = 0.0
                        windowChunks = 0
                        lastReport = now
                    }

                    // A real "Hey JARVIS" lasts well over half a second, so it scores high on several 80 ms slices in a
                    // row: one very sure slice, two fairly sure, or three moderately sure ones. Never from amplified room
                    // noise: the sound itself (before the gain) must be at a speaking level.
                    streak = if (score >= STREAK_THRESHOLD) streak + 1 else 0
                    val speaking = envelope >= MIN_SPEECH_LEVEL
                    val heard = speaking && (score >= THRESHOLD || (score >= PAIR_THRESHOLD && prevScore >= PAIR_THRESHOLD)
                            || streak >= STREAK_SLICES)
                    val was = prevScore
                    val run = streak
                    prevScore = score
                    if (heard && now > cooldownUntil) {
                        cooldownUntil = now + 2500
                        prevScore = 0f
                        streak = 0
                        Log.i(TAG, String.format("Hey JARVIS detected (score %.3f, previous %.3f, streak %d, level %.0f, gain %.1f)",
                            score, was, run, envelope, gain))
                        WakeBus.countWake(this)
                        onWakeWord()
                    }
                }
            } catch (e: InterruptedException) {
                // switched off
            } catch (e: Throwable) {
                Log.e(TAG, "Listening stopped", e)
            } finally {
                closeRecorder()
                try {
                    if (wakeLock.isHeld) wakeLock.release()
                } catch (_: Throwable) {
                }
                detector.close()
            }
        }, "jarvis-wake").apply {
            priority = Thread.NORM_PRIORITY + 1
            start()
        }
    }

    private fun onWakeWord() {
        try {
            val tone = beep ?: ToneGenerator(AudioManager.STREAM_NOTIFICATION, 70).also { beep = it }
            tone.startTone(ToneGenerator.TONE_PROP_ACK, 180)
        } catch (_: Throwable) {
            beep = null  // the player died: make a new one next time
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
                val sir = WakeBus.prefs(this).getString(WakeBus.KEY_ADDRESS_HI, address) ?: address
                TtsEngine.speak(TtsEngine.Request("wake-" + System.nanoTime(),
                    "जी $sir? जार्विस खोलने के लिए नोटिफ़िकेशन दबाइए।", "Ji $address? Tap the notification to open JARVIS.", "hi-IN", 0.95f))
                notifier.showWakeFallback(address)
            }
        }, 1500)
    }

    /** A reminder or alert with a question was just spoken: open the voice-only conversation to take the answer. */
    private fun askAbout(about: JSONObject) {
        if (WakeBus.appInForeground || !ConversationLoop.available(this)) return
        val l = loop ?: ConversationLoop(this, notifier).also { loop = it }
        l.startAbout(about)
    }

    private fun startWatcher() {
        val thread = HandlerThread("jarvis-watch").apply { start() }
        watcherThread = thread
        val handler = Handler(thread.looper)
        val w = ReminderWatcher(this, notifier)
        w.onAsk = { about -> Handler(Looper.getMainLooper()).post { askAbout(about) } }
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
        if (current === this) current = null
        WakeBus.running = false
        WakeBus.listening = false
        loop?.destroy()
        loop = null
        try {
            beep?.release()
        } catch (_: Throwable) {
        }
        beep = null
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
        /** The running service, so a live push (another service) can hand it a question to listen for. */
        @Volatile private var current: WakeService? = null

        fun askAbout(context: Context, about: JSONObject) {
            val service = current ?: return
            Handler(Looper.getMainLooper()).post { service.askAbout(about) }
        }
        const val EXTRA_WAKE = "jarvis_wake"
        const val EXTRA_NO_MIC = "jarvis_no_mic"
        /** The reminder / alert key of a tapped notification: the app opens straight on it and asks its question. */
        const val EXTRA_OPEN_KEY = "jarvis_open_key"
        // Barge-in: measure the phone's own voice for this long, then a sound this much louder, for this many
        // 80 ms slices in a row (and at least a speaking level), means he is talking over JARVIS.
        private const val BARGE_BASELINE_MS = 600L
        private const val BARGE_OVER_BASE = 2.2
        private const val BARGE_MIN_LEVEL = 450.0
        private const val BARGE_SLICES = 4
        private const val THRESHOLD = 0.7f          // one slice this sure is enough
        private const val PAIR_THRESHOLD = 0.45f    // or two slices in a row this sure
        private const val STREAK_THRESHOLD = 0.35f  // or STREAK_SLICES in a row this sure
        private const val STREAK_SLICES = 3
        // The sound itself (before the gain) must be at least this, or nobody is speaking: hiss boosted to a "speech"
        // level never counts. Set low for now; the detection log prints the level, so it can be raised from evidence.
        private const val MIN_SPEECH_LEVEL = 300.0
        private const val TARGET_LEVEL = 3000.0     // the loudness normal speech near the phone has
        private const val NOISE_FLOOR = 110.0       // below this it is room noise: never amplified
        private const val MAX_GAIN = 3.0            // quiet speech is helped a little; hiss is never turned into "speech"
        private const val SILENT_CHUNKS_LIMIT = 75  // 6 s of pure zeros
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
