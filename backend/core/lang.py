"""Which language JARVIS speaks to a person: Hindi (Devanagari for the voice, Hinglish on screen) or English.

One rule for every spoken line, server-built ones included:
  1. a pinned setting wins ("always Hindi" / "always English");
  2. otherwise the language of what the person just said, from its script and words;
  3. otherwise the model's own reading of the sentence;
  4. otherwise the language the person has mostly used lately (remembered turn by turn);
  5. otherwise English.
The phone's recogniser often writes Hindi speech in Roman letters, so the word test matters as much as the script.
"""
import re

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_TOKEN = re.compile(r"[a-zA-Z']+")

# Everyday Hindi / Hinglish words as the recogniser writes them. Ambiguous tokens ("me", "do", "is", "to", "us")
# are left out of both lists on purpose.
HINDI = {
    "hai", "hain", "ho", "hoon", "hu", "tha", "thi", "the", "ka", "ki", "ke", "ko", "se", "mein", "par", "pe", "aur", "ya",
    "nahi", "nahin", "nai", "haan", "han", "ha", "kya", "kyun", "kyu", "kab", "kahan", "kaise", "kaun", "kaunsa", "konsa",
    "kaunsi", "karo", "kar", "karna", "karni", "karne", "kijiye", "karein", "karun", "dena", "de", "dedo", "lena", "lelo",
    "bolo", "bol", "batao", "bata", "dikhao", "dikha", "bhejo", "bhej", "rakho", "rakh", "lagao", "daalo", "daal", "yaad",
    "dilana", "dila", "baje", "bajay", "kal", "aaj", "parso", "subah", "shaam", "sham", "raat", "dopahar", "abhi", "phir",
    "baad", "pehle", "wala", "wale", "wali", "mera", "meri", "mere", "mujhe", "hum", "hamara", "aap", "aapka", "aapki",
    "aapke", "tum", "uska", "uski", "unka", "sab", "sabhi", "thoda", "bahut", "zyada", "kam", "theek", "thik", "accha",
    "achha", "acha", "bas", "chahiye", "chahta", "chahti", "milna", "mil", "jao", "jana", "aana", "aao", "bulao", "puchho",
    "pucho", "samjha", "samjhe", "suno", "dekho", "likho", "banao", "kyunki", "lekin", "magar", "matlab", "wahan", "yahan",
    "kitna", "kitne", "kitni", "koi", "kuch", "karwa", "karwao", "ji", "bilkul", "zaroor", "jaldi", "turant", "isko",
    "usko", "inko", "unko", "saath", "liye", "baat", "kaam", "din", "hafte", "hafta", "mahina", "mahine", "saal", "waala",
    "nhi", "krdo", "krna", "kro", "hogaya", "hogya", "gaya", "gayi", "diya", "diye", "liya", "raha", "rahi", "rahe", "bhi",
    "toh", "to", "sirf", "wapas", "band", "chalu", "shuru", "khatam", "pura", "poora", "baaki", "baki", "pending", "sunao",
}
# English words that carry a sentence. Loanwords Hinglish uses as its own ("meeting", "task", "accept", "cancel",
# "urgent", "time") are not here: "request accept karo" is Hindi, and only the Hindi words should say so.
ENGLISH = {
    "the", "is", "are", "am", "was", "were", "what", "when", "where", "who", "how", "which", "why", "please", "can",
    "could", "would", "should", "show", "give", "send", "remind", "tell", "today", "tomorrow", "yesterday", "this",
    "that", "these", "those", "with", "for", "and", "have", "has", "need", "want", "my", "your", "of", "at", "on",
    "in", "it", "them", "all", "any", "something", "anything", "nothing", "later", "yes", "no", "not", "there",
    "here", "about", "from", "will", "shall", "did", "does",
}

Lang = str  # "hi" | "en"


def detect(text: str) -> str:
    """'hi', 'en', or '' when the sentence gives no clear sign (a bare name, a number)."""
    text = (text or "").strip()
    if not text:
        return ""
    if DEVANAGARI.search(text):
        return "hi"
    tokens = [t.lower() for t in _TOKEN.findall(text)]
    if not tokens:
        return ""
    hi = sum(1 for t in tokens if t in HINDI)
    en = sum(1 for t in tokens if t in ENGLISH)
    short = len(tokens) <= 4
    if hi and not en:
        return "hi" if (hi >= 2 or short) else ""
    if en and not hi:
        return "en" if (en >= 2 or short) else ""
    if hi >= 2 and hi > en:
        return "hi"
    if en >= 2 and en > hi:
        return "en"
    return ""


def clean(value) -> str:
    v = str(value or "").strip().lower()
    return v if v in ("hi", "en") else ""


def lang_for(user, text: str = "", vote: str = "") -> Lang:
    """The language to reply in, by the rule above."""
    from . import services  # local import: services imports this module

    pinned = clean(services.get_prefs(user).get("replyLanguage"))
    if pinned:
        return pinned
    return detect(text) or clean(vote) or current(user)


def current(user) -> Lang:
    """The language the person has mostly used lately (for lines spoken with nothing just said: alerts, reminders)."""
    from . import services

    prefs = services.get_prefs(user)
    pinned = clean(prefs.get("replyLanguage"))
    if pinned:
        return pinned
    recent = [clean(x) for x in str(prefs.get("langRecent") or "").split(",") if clean(x)]
    if not recent:
        return "en"
    return "hi" if recent.count("hi") * 2 >= len(recent) else "en"


def remember(user, lang: str) -> None:
    """Note the language of one more sentence (the last six are kept)."""
    from . import services

    lang = clean(lang)
    if not lang:
        return
    prefs = services.get_prefs(user)
    recent = [clean(x) for x in str(prefs.get("langRecent") or "").split(",") if clean(x)]
    recent = (recent + [lang])[-6:]
    if ",".join(recent) != str(prefs.get("langRecent") or ""):
        services.set_pref_quiet(user, "langRecent", ",".join(recent))


def pick(lang: str, en: str, hi: str) -> str:
    """The Hindi twin when the language is Hindi and a twin exists; otherwise the English line."""
    return hi if lang == "hi" and hi else en
