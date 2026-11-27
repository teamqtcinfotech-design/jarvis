package com.qtcinfotech.jarvis.wake

import android.content.Context
import android.content.SharedPreferences

/** Tiny in-process bridge between the service and the Expo module, plus the service's saved settings. */
object WakeBus {
    @Volatile var listener: (() -> Unit)? = null
    @Volatile var pendingWake = false
    /** Told whenever the app comes to the front or goes behind, or a voice-only conversation starts or ends (the
     *  floating button shows and hides on these). */
    @Volatile var stateListener: (() -> Unit)? = null
    @Volatile private var foreground = false
    var appInForeground: Boolean
        get() = foreground
        set(value) {
            foreground = value
            stateListener?.invoke()
        }
    /**
     * The app asked the wake-word microphone to step aside (it is listening or speaking itself). The request
     * expires by itself: if the app is closed or killed while paused, "Hey JARVIS" comes back within a minute
     * instead of staying deaf until the app is opened again.
     */
    @Volatile private var pausedUntil = 0L
    private const val PAUSE_TTL_MS = 60_000L

    var paused: Boolean
        get() = android.os.SystemClock.elapsedRealtime() < pausedUntil
        set(value) {
            pausedUntil = if (value) android.os.SystemClock.elapsedRealtime() + PAUSE_TTL_MS else 0L
        }
    /** The background service is running (watching for reminders and messages, and maybe listening). */
    @Volatile var running = false
    /** The microphone part ("Hey JARVIS") is running too. */
    @Volatile var listening = false
    /** Android is feeding the listener silence (it was restarted in the background): the app must restart it. */
    @Volatile var micBlocked = false
    /** A background (voice-only) conversation is going on: the wake-word mic and the reminder watcher stand aside. */
    @Volatile private var conversing = false
    var inConversation: Boolean
        get() = conversing
        set(value) {
            val changed = conversing != value
            conversing = value
            if (changed) stateListener?.invoke()
        }
    /** The wake-word service has the microphone open right now (whoever wants to listen next waits for this to clear). */
    @Volatile var micHeld = false

    /** When he was last known to be with the phone: unlocked it, said "Hey JARVIS", spoke to JARVIS (his rule, 26 Nov
     *  2026: JARVIS speaks only to someone who is really there; otherwise it shows, and counts when he is back). */
    @Volatile var lastPresentAt = 0L
    /** When the screen last came on (0 = off): a screen on for longer than its own timeout is being used. */
    @Volatile var screenOnSince = 0L
    const val HERE_MS = 3 * 60_000L
    const val KEY_PRESENT_AT = "present_at"

    /** He is here now; kept in the settings too (with a context), so a restarted service still knows. */
    fun notePresent(context: Context? = null) {
        lastPresentAt = System.currentTimeMillis()
        if (context != null) prefs(context).edit().putLong(KEY_PRESENT_AT, lastPresentAt).apply()
    }

    fun loadPresent(context: Context) {
        val saved = prefs(context).getLong(KEY_PRESENT_AT, 0L)
        if (saved > lastPresentAt) lastPresentAt = saved
    }

    fun presentNow(): Boolean = System.currentTimeMillis() - lastPresentAt < HERE_MS

    /** The app's code is loaded (open, or behind another app): it runs the conversation on the live line, whether or
     *  not its screen is showing. Otherwise the app is told when it next comes up. */
    fun onWake() {
        val l = listener
        if (l != null) l() else pendingWake = true
    }

    /** A live push arrived while the app is open: the app refreshes its alerts at once. */
    @Volatile var pushListener: (() -> Unit)? = null

    fun onPush() {
        pushListener?.invoke()
    }

    /** He started talking while JARVIS was speaking: the voice has been stopped; the app should listen now. */
    @Volatile var bargeListener: (() -> Unit)? = null

    fun onBarge() {
        bargeListener?.invoke()
    }

    fun prefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences("jarvis_wake", Context.MODE_PRIVATE)

    // ---- how often "Hey JARVIS" fired today, and how often nothing was said after it (a false wake, most likely).
    private fun today(): String = java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US).format(java.util.Date())

    @Synchronized
    private fun bump(context: Context, key: String) {
        val p = prefs(context)
        val day = today()
        val e = p.edit()
        if (p.getString(KEY_WAKE_DAY, "") != day) {
            e.putString(KEY_WAKE_DAY, day).putInt(KEY_WAKE_COUNT, 0).putInt(KEY_WAKE_EMPTY, 0).apply()
        }
        e.putInt(key, prefs(context).getInt(key, 0) + 1).apply()
    }

    fun countWake(context: Context) = bump(context, KEY_WAKE_COUNT)
    fun countEmptyWake(context: Context) = bump(context, KEY_WAKE_EMPTY)

    fun wakeCounts(context: Context): Pair<Int, Int> {
        val p = prefs(context)
        if (p.getString(KEY_WAKE_DAY, "") != today()) return 0 to 0
        return p.getInt(KEY_WAKE_COUNT, 0) to p.getInt(KEY_WAKE_EMPTY, 0)
    }

    /** The key of the reminder or alert whose notification was tapped to open the app (consumed once by the app). */
    @Volatile var pendingOpenKey: String? = null

    const val KEY_WAKE_DAY = "wake_day"
    const val KEY_WAKE_COUNT = "wake_count"
    const val KEY_WAKE_EMPTY = "wake_empty"

    /** The service should run (someone is signed in on this phone). */
    const val KEY_ENABLED = "enabled"
    /** Listen for "Hey JARVIS" as well (needs the microphone). */
    const val KEY_WAKE_WORD = "wake_word"
    const val KEY_SERVER = "server"
    const val KEY_ACCESS = "access"
    const val KEY_REFRESH = "refresh"
    const val KEY_ADDRESS = "address"
    /** The form of address in Devanagari ("सर", "मैम") for the Hindi lines, and whether this user is the MD. */
    const val KEY_ADDRESS_HI = "address_hi"
    const val KEY_IS_MD = "is_md"
    /** The PA's name as JARVIS says it (for the closed-app recogniser's word list). */
    const val KEY_PA_NAME = "pa_name"
    /** More names the closed-app recogniser should expect (staff, contacts, the MD), one per line. */
    const val KEY_HINTS = "hints"
    /** The phone voice for English lines ("hi-IN" Hindi accent, "en-IN" Indian English); Hindi lines always use hi-IN. */
    const val KEY_VOICE_ACCENT = "voice_accent"
    /** The language the closed-app recogniser starts in ("en-IN" understands Hinglish too; "hi-IN" for pure Hindi). */
    const val KEY_LISTEN_LANG = "listen_lang"
    /** The language JARVIS is speaking to this person right now ("hi" / "en"), for the few lines built here. */
    const val KEY_REPLY_LANG = "reply_lang"
    /** Notification sound for reminders that arrive while the app is closed: "default" (the phone's) or "silent". */
    const val KEY_REMINDER_SOUND = "reminder_sound"
    /** The last reminder list fetched (the next hour), so reminders still fire with no connection. */
    const val KEY_REMINDERS_CACHE = "reminders_cache"
    const val KEY_REMINDERS_CACHED_AT = "reminders_cached_at"
    /** Talking over JARVIS stops it and it listens (default on; off if the room echoes). */
    const val KEY_BARGE_IN = "barge_in"
    const val KEY_SPEAK_LOCKED = "speak_locked"
    const val KEY_SPOKEN = "spoken_keys"
    /** Alerts whose Seen was tapped on this phone (ring until seen): they never ring here again. */
    const val KEY_SEEN_IDS = "seen_ids"
    /** Alerts still waiting for Seen on this phone (their notifications are up), so the "N waiting" summary is right. */
    const val KEY_RING_ACTIVE = "ring_active"
    /** Walkie-talkie: the person's switch, and where the relay is (ws://…/ws/walkie/, from /me/). */
    const val KEY_WALKIE_ENABLED = "walkie_enabled"
    const val KEY_WALKIE_URL = "walkie_url"

    /** The talk behind the incoming card that opened the app (consumed once by the app). */
    @Volatile var pendingWalkieTalk: String? = null
    /** A photo or file shared into JARVIS from another app, as JSON {uri, mime, name, text} (consumed once by the app). */
    @Volatile var pendingShare: String? = null
    const val KEY_PUSH_TOKEN = "push_token"
    /** The wake word in use: the listener file bundled in the app, the phrase as said, and the assistant's name that
     *  goes with it ("Hey Billy" = Billy; the app itself stays JARVIS). */
    const val KEY_WAKE_MODEL = "wake_model"
    const val KEY_WAKE_PHRASE = "wake_phrase"
    const val KEY_ASSISTANT_NAME = "assistant_name"
    const val KEY_ASSISTANT_NAME_HI = "assistant_name_hi"
    const val DEFAULT_WAKE_MODEL = "hey_jarvis_v0.1.onnx"

    fun wakePhrase(context: Context): String = prefs(context).getString(KEY_WAKE_PHRASE, "Hey JARVIS") ?: "Hey JARVIS"
    fun assistantName(context: Context): String = prefs(context).getString(KEY_ASSISTANT_NAME, "JARVIS") ?: "JARVIS"
    fun assistantNameHi(context: Context): String = prefs(context).getString(KEY_ASSISTANT_NAME_HI, "जार्विस") ?: "जार्विस"
    /** Which Grok voice JARVIS speaks with (the MD's choice in Settings; male voices only). */
    const val KEY_VOICE_NAME = "voice_name"
    /** JARVIS speaks in the Grok voice (default); false = the phone's own voice only. */
    const val KEY_GROK_VOICE = "grok_voice"
    /** The floating JARVIS button over other apps (his ask, 26 Nov 2026): on or off, and where he last left it. */
    const val KEY_BUBBLE = "bubble"
    const val KEY_BUBBLE_X = "bubble_x"
    const val KEY_BUBBLE_Y = "bubble_y"
    /** With the app closed: speak only when he is here (HERE_MS), and listen to the room before speaking (RoomSense). */
    const val KEY_HERE_ONLY = "here_only"
    const val KEY_ROOM_CHECK = "room_check"
    /** What came while he was not with the phone (JSON array of items): offered as a count when he is back. */
    const val KEY_HELD_ITEMS = "held_items"
}
