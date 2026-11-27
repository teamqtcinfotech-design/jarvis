package com.qtcinfotech.jarvis.wake

import android.app.KeyguardManager
import android.content.Context
import android.os.PowerManager
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Locale

/**
 * While the app is closed, checks the server every few seconds for reminders that are due right now and
 * messages that just arrived, and speaks or shows them (his rules, 26 Nov 2026):
 *
 *  - a meeting, quiet hours  -> notification only (the server says so: "silence")
 *  - not with the phone      -> notification only, held; counted when he unlocks the phone next ("while you
 *                               were away: 2 reminders came. Shall I tell you?")
 *  - the room busy           -> held the same way, unless something is urgent (RoomSense)
 *  - here, room quiet        -> the count by kind only ("Sir, ek reminder hai. Kya main bataun?"); the voice-only
 *                               conversation reads them on a yes, one at a time, or brings them back later on a no
 *  - locked                  -> notification; spoken only if "speak when locked" is on, and a private
 *                               reminder is then only "you have a private reminder"
 *  - app open on screen      -> nothing here; the app's own alert engine handles it
 *
 * Told once: whatever this speaks or shows is reported to the server ("told"), and whatever the server says was
 * told already (by the app, or by this part earlier) is skipped. So the same thing is never announced twice, by
 * either side. Several things due at the same moment are said as one sentence.
 *
 * It has its own login (device token) so its refreshes never clash with the app's.
 */
class ReminderWatcher(private val context: Context, private val notifier: WakeNotifier) {
    private val prefs = WakeBus.prefs(context)
    private val DEVANAGARI = Regex("[\\u0900-\\u097F]")

    /**
     * One thing to announce: its server key (for "told"), what to show and what to say, and the question JARVIS
     * asks right after it (a contract from the server: kind, en, hi), if any.
     */
    private class Item(val key: String, val reminderKey: String?, val alertId: String?, val title: String,
                       val spoken: String, val isPrivate: Boolean, val urgent: Boolean, val question: JSONObject? = null,
                       /** Ring until seen: shown with the "ting ting" and the Seen button. */
                       val ring: Boolean = false, val quiet: Boolean = false, val seenLabel: String = "Seen",
                       val priority: String = "NORMAL",
                       /** What it is, for the count-only line: reminder, message, task, approval, request, update. */
                       val kind: String = "reminder") {
        fun toJson(): JSONObject = JSONObject().put("key", key).put("reminderKey", reminderKey ?: JSONObject.NULL)
            .put("alertId", alertId ?: JSONObject.NULL).put("title", title).put("spoken", spoken).put("isPrivate", isPrivate)
            .put("urgent", urgent).put("question", question ?: JSONObject.NULL).put("ring", ring).put("quiet", quiet)
            .put("seenLabel", seenLabel).put("priority", priority).put("kind", kind)

        companion object {
            fun fromJson(o: JSONObject): Item = Item(o.optString("key"), o.optString("reminderKey").takeIf { it.isNotEmpty() && !o.isNull("reminderKey") },
                o.optString("alertId").takeIf { it.isNotEmpty() && !o.isNull("alertId") }, o.optString("title"), o.optString("spoken"),
                o.optBoolean("isPrivate"), o.optBoolean("urgent"), o.optJSONObject("question"), o.optBoolean("ring"), o.optBoolean("quiet"),
                o.optString("seenLabel", "Seen"), o.optString("priority", "NORMAL"), o.optString("kind", "reminder"))
        }
    }

    /** The kind of an alert for the count-only line, the way the app sorts them (src/lib/alertEngine.tsx OFFER_KINDS). */
    private fun kindOf(alertKind: String, refType: String, question: JSONObject?): String = when {
        refType == "message" -> "message"
        question?.optString("kind") == "accept" || alertKind == "NEW_REQUEST" -> "request"
        refType == "approval" || question?.optString("kind") == "approve" -> "approval"
        alertKind == "TASK_ASSIGNED" -> "task"
        else -> "update"
    }

    /** Set by the service: after a spoken item with a question, listen for the answer (the voice-only conversation). */
    @Volatile var onAsk: ((JSONObject) -> Unit)? = null

    init {
        TtsEngine.init(context)
    }

    /** Returns false when the login has expired for good (the service should stop and let the app re-issue it). */
    fun check(): Boolean {
        if (WakeBus.appInForeground || WakeBus.inConversation) return true
        // The next hour's list is kept on the phone: with no connection, the reminders still fire from this copy.
        val fetched = Api.get(context, "/reminders/?hours=1")
        if (fetched != null) {
            prefs.edit().putString(WakeBus.KEY_REMINDERS_CACHE, fetched.toString()).putLong(WakeBus.KEY_REMINDERS_CACHED_AT, System.currentTimeMillis()).apply()
        } else if (Api.tokensDead) {
            return false
        }
        val reminders = fetched ?: cachedReminders() ?: return true
        val alerts = if (fetched != null) Api.get(context, "/alerts/") else null
        // A meeting, quiet hours (the server knows his diary and his settings): shown, never spoken (his rule, 26 Nov 2026).
        val silentNow = reminders.optJSONObject("silence")?.optBoolean("silent") == true
        val nowSec = System.currentTimeMillis() / 1000
        val spoken = prefs.getStringSet(WakeBus.KEY_SPOKEN, emptySet())?.toMutableSet() ?: mutableSetOf()
        val due = ArrayList<Item>()
        val quiet = ArrayList<Item>()   // told to the server, never announced (small news, or told already)
        val missed = ArrayList<Item>()  // its time passed while the phone was off: shown, not spoken, kept for the app

        val list = reminders.optJSONArray("reminders")
        if (list != null) {
            val soon = ArrayList<String>()
            for (i in 0 until list.length()) {
                val r = list.getJSONObject(i)
                val key = r.optString("key")
                if (key.isEmpty() || key in spoken) continue
                val fireAtSec = parseIsoSeconds(r.optString("fire_at")) ?: continue
                val age = nowSec - fireAtSec
                // Due within the hour: fetch its voice now, so it speaks instantly (and even offline) at its time.
                if (age < 0 && age > -3600) soon.add(TtsEngine.forSpeech(r.optString("spoken")))  // as it will be spoken
                if (age < 0) continue             // not due yet
                if (!r.isNull("told_at")) {       // the app said it already: remember, do not repeat
                    spoken.add(key)
                    continue
                }
                val item = Item(key, key, null, r.optString("title"), r.optString("spoken"), r.optBoolean("is_private"),
                                r.optString("priority") == "URGENT", r.optJSONObject("question"))
                // Older than a few minutes: the phone was off (or asleep) at its time. A notification still goes up,
                // and it is NOT reported as told, so the app offers it among the held ones when it is next opened.
                if (age > 180) missed.add(item) else due.add(item)
            }
            if (soon.isNotEmpty()) GrokVoice.prefetch(context, soon)
        }
        val results = alerts?.optJSONArray("results")
        if (results != null) {
            val stillRinging = HashSet<String>()  // what the server says waits for Seen right now
            var rungAgain = 0                      // items rung at a new number this pass (one sound for all)
            for (i in 0 until results.length()) {
                val a = results.getJSONObject(i)
                val id = a.optString("id")
                val key = a.optString("announce_key").ifEmpty { "alert:$id" }  // a snoozed alert comes back as new
                val ringing = a.optBoolean("ringing") && !RingReceiver.seenHere(context, id)
                if (ringing) stillRinging.add(id)
                val ringNo = a.optInt("rings").toString()
                if (id.isNotEmpty() && key in spoken) {
                    // Ring until seen, without live push: ring again once per ring number the server has counted.
                    if (ringing && RingReceiver.firstRing(context, id, ringNo)) {
                        notifier.showRingItem(id, a.optString("title"), a.optString("spoken_text").ifBlank { a.optString("body") },
                            a.optString("priority"), a.optString("seen_label", "Seen"))
                        rungAgain++
                    }
                    continue
                }
                if (id.isEmpty()) continue
                if (!a.optBoolean("fresh", true)) {  // told already, or old news: it stays in the lists only
                    spoken.add(key)
                    continue
                }
                if (ringing) RingReceiver.firstRing(context, id, ringNo)
                val item = Item(key, null, id, a.optString("title"), a.optString("spoken_text"), false,
                                a.optString("priority") == "URGENT", a.optJSONObject("question"),
                                ring = ringing, seenLabel = a.optString("seen_label", "Seen"), priority = a.optString("priority", "NORMAL"),
                                kind = kindOf(a.optString("kind"), a.optString("ref_type"), a.optJSONObject("question")))
                if (a.optString("priority") == "LOW" && !ringing) quiet.add(item) else due.add(item)
            }
            if (rungAgain > 0) {
                val n = notifier.active().size
                val hindi = prefs.getString(WakeBus.KEY_REPLY_LANG, "") == "hi"
                notifier.showRingSummary(if (hindi) "$n चीज़ें आपके देखने का इंतज़ार कर रही हैं" else "$n waiting for you",
                    if (hindi) "हर एक पर “देख लिया” दबाइए, या JARVIS खोलिए।" else "Tap Seen on each, or open JARVIS.",
                    quiet = false, alert = true)
            }
            // Seen, done or withdrawn somewhere else while this phone was off the network: its notification goes.
            for (id in notifier.active()) if (id !in stillRinging) notifier.cancelRing(id)
        }
        // Held (he is not with the phone, or the room is busy): shown, not told, counted when he is back.
        val held = if (due.isNotEmpty()) announce(due, silentNow) else emptyList()
        for (item in missed) {
            notifier.show(item.key, if (item.isPrivate) "JARVIS" else "JARVIS · Missed reminder",
                if (item.isPrivate) "You have a private reminder. Open JARVIS to see it." else item.title, item.urgent, silentSound())
            spoken.add(item.key)
        }
        for (item in due + quiet) spoken.add(item.key)
        val told = due.filter { d -> held.none { it.key == d.key } } + quiet
        if (told.isNotEmpty()) tell(told)
        if (spoken.isNotEmpty()) {
            val trimmed = if (spoken.size > 300) spoken.toList().takeLast(300).toSet() else spoken
            prefs.edit().putStringSet(WakeBus.KEY_SPOKEN, trimmed).apply()
        }
        return true
    }

    /**
     * A live push (see PushService): show and speak it now, and remember it so the next check does not repeat it.
     * An alert comes with its id in the key ("alert:<id>"); a reminder comes with its own key (reminderKey).
     */
    fun deliverPush(key: String, title: String, spoken: String, urgent: Boolean, priority: String = "NORMAL",
                    question: JSONObject? = null, reminderKey: String? = null, isPrivate: Boolean = false,
                    ring: Boolean = false, quiet: Boolean = false, seenLabel: String = "Seen", silent: Boolean = false,
                    kind: String = "reminder") {
        val spokenKeys = prefs.getStringSet(WakeBus.KEY_SPOKEN, emptySet())?.toMutableSet() ?: mutableSetOf()
        if (key in spokenKeys) return
        val alertId = if (reminderKey == null) key.removePrefix("alert:").substringBefore(":s") else null
        val item = Item(key, reminderKey, alertId, title, spoken, isPrivate, urgent, question, ring, quiet, seenLabel, priority, kind)
        if (ring && alertId != null && WakeBus.inConversation) {
            notifier.showRing(alertId, title, spoken, priority, quiet, seenLabel) // mid-conversation: rung and shown, not spoken
        }
        val held = if (!WakeBus.inConversation && (priority != "LOW" || ring)) announce(listOf(item), silent) else emptyList()
        spokenKeys.add(key)
        prefs.edit().putStringSet(WakeBus.KEY_SPOKEN, spokenKeys).apply()
        if (held.isEmpty()) tell(listOf(item))
    }

    // ------------------------------------------------------------------ held while he was away
    private fun loadHeld(): List<Item> {
        val raw = prefs.getString(WakeBus.KEY_HELD_ITEMS, null) ?: return emptyList()
        return try {
            val arr = JSONArray(raw)
            (0 until arr.length()).mapNotNull { arr.optJSONObject(it)?.let { o -> Item.fromJson(o) } }
        } catch (_: Throwable) {
            emptyList()
        }
    }

    /** Kept for when he is back: shown already, not told to the server (so the app offers them too if it is opened first). */
    private fun hold(items: List<Item>) {
        val all = loadHeld().toMutableList()
        for (item in items) if (all.none { it.key == item.key }) all.add(item)
        val arr = JSONArray()
        for (item in all.takeLast(30)) arr.put(item.toJson())
        prefs.edit().putString(WakeBus.KEY_HELD_ITEMS, arr.toString()).apply()
        android.util.Log.i("JarvisWake", "held ${items.size} for later (${all.size} waiting): not with the phone, or the room is busy")
    }

    /**
     * He is back (unlocked the phone; WakeService's receiver): what was held while he was away is offered once, as
     * a count ("Sir, while you were away: 2 reminders came. Shall I tell you?"). What the app told meanwhile (he
     * opened JARVIS first) is dropped; a meeting or quiet hours keeps it held.
     */
    fun onBack() {
        if (WakeBus.appInForeground || WakeBus.inConversation) return
        val held = loadHeld()
        if (held.isEmpty()) return
        val reminders = Api.get(context, "/reminders/?hours=1")
        if (reminders?.optJSONObject("silence")?.optBoolean("silent") == true) return  // still a meeting / quiet hours
        val alerts = if (reminders != null) Api.get(context, "/alerts/") else null
        val toldKeys = HashSet<String>()
        reminders?.optJSONArray("reminders")?.let { list ->
            for (i in 0 until list.length()) {
                val r = list.getJSONObject(i)
                if (!r.isNull("told_at")) toldKeys.add(r.optString("key"))
            }
        }
        val freshIds = HashSet<String>()
        alerts?.optJSONArray("results")?.let { list ->
            for (i in 0 until list.length()) {
                val a = list.getJSONObject(i)
                if (a.optBoolean("fresh", true)) freshIds.add(a.optString("id"))
            }
        }
        val still = held.filter { item ->
            when {
                item.reminderKey != null -> item.reminderKey !in toldKeys
                item.alertId != null -> alerts == null || item.alertId in freshIds
                else -> true
            }
        }
        prefs.edit().remove(WakeBus.KEY_HELD_ITEMS).apply()
        if (still.isEmpty()) return
        val heldAgain = announce(still, silent = false, back = true, shown = true)
        val told = still.filter { s -> heldAgain.none { it.key == s.key } }
        if (told.isNotEmpty()) tell(told)
    }

    private fun silentSound(): Boolean = prefs.getString(WakeBus.KEY_REMINDER_SOUND, "default") == "silent"

    /** The last list fetched, if it is less than an hour old (older than that, its items have all come and gone). */
    private fun cachedReminders(): JSONObject? {
        val at = prefs.getLong(WakeBus.KEY_REMINDERS_CACHED_AT, 0L)
        if (at == 0L || System.currentTimeMillis() - at > 60 * 60 * 1000L) return null
        val raw = prefs.getString(WakeBus.KEY_REMINDERS_CACHE, null) ?: return null
        return try { JSONObject(raw) } catch (_: Throwable) { null }
    }

    /** What the voice-only conversation needs to take the answer to this item's question. */
    private fun aboutOf(item: Item): JSONObject = JSONObject()
        .put("kind", if (item.reminderKey != null) "reminder" else "alert")
        .put("key", item.reminderKey ?: "")
        .put("id", item.alertId ?: "")
        .put("title", item.title)
        .put("question", item.question?.optString("en") ?: "")
        .put("qkind", item.question?.optString("kind") ?: "")

    /** Tell the server these were spoken or shown, so the app (or this part later) never repeats them. */
    private fun tell(items: List<Item>) {
        val keys = JSONArray()
        val ids = JSONArray()
        for (item in items) {
            item.reminderKey?.let { keys.put(it) }
            item.alertId?.let { ids.put(it) }
        }
        if (keys.length() == 0 && ids.length() == 0) return
        Api.post(context, "/told/", JSONObject().put("keys", keys).put("ids", ids), 15000)
    }

    /** "2026-11-04T11:00:00+05:30" (with or without fractional seconds, or a trailing Z) -> epoch seconds. */
    private fun parseIsoSeconds(iso: String): Long? {
        if (iso.isBlank()) return null
        val cleaned = iso.replace(Regex("\\.\\d+"), "").replace("Z", "+00:00")
        return try {
            SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.US).parse(cleaned)?.time?.div(1000)
        } catch (_: Exception) {
            null
        }
    }

    /**
     * Show each as a notification, then decide whether to speak (his rules, 26 Nov 2026):
     *  - a meeting or quiet hours (`silent`, from the server): shown only;
     *  - not with the phone (locked, or no unlock / word to JARVIS within the last few minutes): held, counted when
     *    he is back (returned, so the caller does not report them told);
     *  - the room busy (several voices, steady noise): held too, unless something is urgent;
     *  - otherwise nothing describes itself: only the count by kind is said ("Sir, ek reminder hai. Kya main
     *    bataun?") and the voice-only conversation takes the yes (read one at a time) or the no (back later).
     * Without a conversation possible on this phone, everything is said as one sentence, the old way.
     * The lines come from the server already in the reader's language (Hindi or English); the wrapper follows them.
     */
    private fun announce(items: List<Item>, silent: Boolean, back: Boolean = false, shown: Boolean = false): List<Item> {
        val address = prefs.getString(WakeBus.KEY_ADDRESS, "Sir") ?: "Sir"
        val addressHi = prefs.getString(WakeBus.KEY_ADDRESS_HI, address) ?: address
        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        val km = context.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        val unlocked = pm.isInteractive && !km.isKeyguardLocked
        val speakLocked = prefs.getBoolean(WakeBus.KEY_SPEAK_LOCKED, false)
        if (!shown) {  // (held items were shown when they were held: not posted a second time on his return)
            for (item in items) {
                if (item.ring && item.alertId != null) {
                    notifier.showRing(item.alertId, item.title, item.spoken, if (item.urgent) "URGENT" else "NORMAL", item.quiet, item.seenLabel)
                } else {
                    notifier.show(item.key, if (item.isPrivate) "JARVIS" else "JARVIS · Reminder",
                        if (item.isPrivate) "You have a private reminder. Open JARVIS to see it." else item.title, item.urgent, silentSound())
                }
            }
        }
        if (silent) {
            android.util.Log.i("JarvisWake", "announce: a meeting or quiet hours: ${items.size} shown, nothing said")
            return emptyList()
        }
        val hereOnly = prefs.getBoolean(WakeBus.KEY_HERE_ONLY, true)
        // Here = unlocked and (an unlock, a wake word or a word to JARVIS within the last minutes, or a screen that
        // has stayed on longer than its own timeout: it would have gone dark unless he were using it).
        val present = (unlocked && (!hereOnly || WakeBus.presentNow() || screenInUse(pm))) || speakLocked
        if (!present) {
            hold(items)
            return items
        }
        if (prefs.getBoolean(WakeBus.KEY_ROOM_CHECK, true) && items.none { it.urgent } && RoomSense.busy(context)) {
            hold(items)
            return items
        }
        val lines = ArrayList<String>()
        val speakable = ArrayList<Item>()
        var ask: Item? = null  // the one item whose question JARVIS asks aloud, then listens for the answer
        for (item in items) {
            val generic = if (DEVANAGARI.containsMatchIn(item.spoken)) "$addressHi, आपका एक प्राइवेट रिमाइंडर है।"
                          else "$address, you have a private reminder."
            val line = when {
                unlocked -> item.spoken
                speakLocked -> if (item.isPrivate) generic else item.spoken
                else -> ""
            }
            if (line.isNotBlank()) {
                lines.add(line)
                speakable.add(if (line == item.spoken) item else Item(item.key, item.reminderKey, item.alertId, item.title, line,
                    item.isPrivate, item.urgent, null, item.ring, item.quiet, item.seenLabel, item.priority, item.kind))
            }
            // A question is asked only when it is him for sure (unlocked), one at a time, and never for a
            // private item spoken as "a private reminder".
            if (unlocked && ask == null && item.question != null && line == item.spoken) ask = item
        }
        if (lines.isEmpty()) return emptyList()
        val listener = onAsk
        // The count and the conversation after it only when it is him for sure (unlocked): a locked phone that
        // "speaks when locked" says the old one sentence and asks nothing, so nobody else answers for him.
        if (unlocked && listener != null && (LiveLoop.available(context) || ConversationLoop.available(context))) {
            // The count only; the conversation that follows reads them on a yes, one at a time with their questions.
            val line = countLine(speakable, address, addressHi, back)
            speakThen(line) { listener(offerAbout(speakable, line)) }
            return emptyList()
        }
        var text = if (lines.size == 1) lines[0] else combined(lines, address, addressHi)
        val asked = ask
        if (asked != null && lines.size == 1) {
            val q = asked.question!!
            val hindi = DEVANAGARI.containsMatchIn(text)
            val question = if (hindi) q.optString("hi") else q.optString("en")
            if (question.isNotBlank()) text = "$text $question"
        }
        speak(text)
        return emptyList()
    }

    /** The screen has been on for longer than its own timeout (plus a little): it would have gone dark by itself
     *  unless he were using the phone, so he is here. */
    private fun screenInUse(pm: PowerManager): Boolean {
        val since = WakeBus.screenOnSince
        if (!pm.isInteractive || since == 0L) return false
        val timeout = try {
            android.provider.Settings.System.getInt(context.contentResolver, android.provider.Settings.System.SCREEN_OFF_TIMEOUT, 60_000)
        } catch (_: Throwable) {
            60_000
        }
        if (timeout <= 0 || timeout > 30 * 60_000) return false  // "never" / "stay awake": says nothing about him
        return System.currentTimeMillis() - since > timeout + 15_000
    }

    /** "Sir, ek reminder hai. Kya main bataun?" / "Sir, while you were away: 2 reminders and 1 message came. Shall I tell you?" */
    private fun countLine(items: List<Item>, address: String, addressHi: String, back: Boolean): String {
        val hindi = prefs.getString(WakeBus.KEY_REPLY_LANG, "") == "hi" || items.any { DEVANAGARI.containsMatchIn(it.spoken) }
        val order = listOf("reminder", "message", "approval", "request", "task", "update")
        val counts = items.groupingBy { if (it.kind in order) it.kind else "update" }.eachCount()
        val wordsEn = mapOf("reminder" to "reminder", "message" to "message", "approval" to "approval", "request" to "request",
            "task" to "new task", "update" to "update")
        val wordsHi = mapOf("reminder" to "रिमाइंडर", "message" to "संदेश", "approval" to "मंज़ूरी", "request" to "रिक्वेस्ट",
            "task" to "नया काम", "update" to "अपडेट")
        val urgent = items.any { it.urgent }
        val parts = order.filter { (counts[it] ?: 0) > 0 }.map { k ->
            val n = counts[k] ?: 0
            if (hindi) "${if (n == 1) (if (urgent && items.size == 1) "एक ज़रूरी" else "एक") else n} ${wordsHi[k]}"
            else "${if (n == 1) (if (urgent && items.size == 1) "one urgent" else "one") else n} ${wordsEn[k]}${if (n > 1) "s" else ""}"
        }
        val joined = if (hindi) parts.joinToString(" और ") else (if (parts.size > 1) parts.dropLast(1).joinToString(", ") + " and " + parts.last() else parts[0])
        val also = if (urgent && items.size > 1) (if (hindi) ", एक ज़रूरी भी" else ", one of them urgent") else ""
        return if (hindi) {
            if (back) "$addressHi, जब आप दूर थे: $joined$also आए हैं। क्या मैं बताऊँ?"
            else "$addressHi, $joined$also ${if (items.size == 1) "है" else "हैं"}। क्या मैं बताऊँ?"
        } else {
            if (back) "$address, while you were away: $joined$also came. Shall I tell you?"
            else "$address, $joined$also. Shall I tell you?"
        }
    }

    /** What the voice-only conversation needs to take the yes or the no to the count, and then the answers. */
    private fun offerAbout(items: List<Item>, line: String): JSONObject {
        val arr = JSONArray()
        for (item in items) {
            arr.put(JSONObject().put("kind", if (item.reminderKey != null) "reminder" else "alert").put("key", item.reminderKey ?: "")
                .put("id", item.alertId ?: "").put("title", item.title).put("spoken", item.spoken).put("question", item.question ?: JSONObject.NULL))
        }
        return JSONObject().put("kind", "offer").put("question", line).put("title", "").put("items", arr)
    }

    /** Speak, then run [onDone] once the voice has finished (or failed), so listening never starts over the voice. */
    private fun speakThen(text: String, onDone: () -> Unit) {
        if (text.isBlank() || WakeBus.appInForeground) return
        val lang = if (DEVANAGARI.containsMatchIn(text)) "hi-IN" else (prefs.getString(WakeBus.KEY_VOICE_ACCENT, "hi-IN") ?: "hi-IN")
        TtsEngine.speakThen(TtsEngine.Request("bg-" + System.nanoTime(), text, null, lang, 0.95f)) { onDone() }
    }

    /** "Sir, 3 things. Your meeting starts in 15 minutes. Mansi has completed: Book flights. …" (or the Hindi twin). */
    private fun combined(lines: List<String>, address: String, addressHi: String): String {
        val hindi = lines.any { DEVANAGARI.containsMatchIn(it) }
        val parts = lines.map { raw ->
            var s = raw.trim()
            val low = s.lowercase(Locale.ROOT)
            for (a in listOf(address, addressHi)) {
                val al = a.lowercase(Locale.ROOT)
                if (al.isNotEmpty() && (low.startsWith("$al, ") || low.startsWith("$al "))) {
                    s = s.substring(a.length + 1).trim()
                    break
                }
            }
            if (!s.endsWith(".") && !s.endsWith("!") && !s.endsWith("?") && !s.endsWith("।")) s + (if (hindi) "।" else ".") else s
        }
        return if (hindi) "$addressHi, ${lines.size} बातें। " + parts.joinToString(" ")
               else "$address, ${lines.size} things. " + parts.joinToString(" ")
    }

    private fun speak(text: String) {
        if (text.isBlank() || WakeBus.appInForeground) return
        // The Hindi voice for a Hindi line; the chosen accent (the app's "JARVIS voice" setting) for an English one.
        val lang = if (DEVANAGARI.containsMatchIn(text)) "hi-IN" else (prefs.getString(WakeBus.KEY_VOICE_ACCENT, "hi-IN") ?: "hi-IN")
        TtsEngine.speak(TtsEngine.Request("bg-" + System.nanoTime(), text, null, lang, 0.95f))
    }

    fun close() {
        // The shared voice stays alive for the app.
    }
}
