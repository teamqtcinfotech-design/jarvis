"""JARVIS backend settings.

All secrets come from backend/.env (never committed). See .env.example.
"""
import hashlib
import base64
import os
from datetime import timedelta
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DEBUG = env_bool("DJANGO_DEBUG", True)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DEBUG is off.")
    SECRET_KEY = "dev-only-insecure-key-change-me"

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*" if DEBUG else "").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "jarvis.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "jarvis.wsgi.application"

# Database: DATABASE_URL > DB_PASSWORD (local PostgreSQL) > SQLite (dev fallback only)
if os.environ.get("DATABASE_URL"):
    DATABASES = {"default": dj_database_url.parse(os.environ["DATABASE_URL"], conn_max_age=60)}
elif os.environ.get("DB_PASSWORD"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DB_NAME", "jarvis"),
            "USER": os.environ.get("DB_USER", "postgres"),
            "PASSWORD": os.environ["DB_PASSWORD"],
            "HOST": os.environ.get("DB_HOST", "localhost"),
            "PORT": os.environ.get("DB_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "dev.sqlite3"}}

AUTH_USER_MODEL = "core.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

import sys  # noqa: E402

if "test" in sys.argv:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # tests only

LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework_simplejwt.authentication.JWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "EXCEPTION_HANDLER": "core.views.exception_handler",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.AnonRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {"user": "300/min", "anon": "30/min", "login": "10/min", "assistant": "40/min",
                                "ask": "12/min", "speak": "90/min"},
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}

# Security headers
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True

# Production behind nginx + HTTPS (set on the VPS)
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]
if env_bool("DJANGO_BEHIND_HTTPS_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# ---------------------------------------------------------------- JARVIS
JARVIS_TIMEZONE = "Asia/Kolkata"

# Field-level encryption for MD private task content.
_fek = os.environ.get("FIELD_ENCRYPTION_KEY", "")
if not _fek:
    if not DEBUG:
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY must be set when DEBUG is off.")
    # Dev fallback derived from SECRET_KEY so local runs work; production must set a real key.
    _fek = base64.urlsafe_b64encode(hashlib.sha256(("fek:" + SECRET_KEY).encode()).digest()).decode()
FIELD_ENCRYPTION_KEY = _fek

# AI brain (Claude). Key stays on the server only.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
JARVIS_AI_ENABLED = env_bool("JARVIS_AI_ENABLED", bool(ANTHROPIC_API_KEY))
JARVIS_AI_COMMAND_MODEL = os.environ.get("JARVIS_AI_COMMAND_MODEL", "claude-haiku-5-5")
JARVIS_AI_BRIEFING_MODEL = os.environ.get("JARVIS_AI_BRIEFING_MODEL", "claude-sonnet-5-5")
JARVIS_AI_TIMEOUT_SECONDS = float(os.environ.get("JARVIS_AI_TIMEOUT_SECONDS", "20"))
# General questions ("aaj ka temperature?", "flight price?") use Sonnet with web search.
JARVIS_AI_ANSWER_MODEL = os.environ.get("JARVIS_AI_ANSWER_MODEL", "claude-sonnet-5-5")
JARVIS_AI_ANSWER_TIMEOUT_SECONDS = float(os.environ.get("JARVIS_AI_ANSWER_TIMEOUT_SECONDS", "50"))
JARVIS_AI_MAX_SEARCHES = int(os.environ.get("JARVIS_AI_MAX_SEARCHES", "3"))  # web searches per question
JARVIS_AI_DAILY_ANSWER_LIMIT = int(os.environ.get("JARVIS_AI_DAILY_ANSWER_LIMIT", "150"))  # per user per day

# xAI (Grok): the next brain and voice. Key stays on the server only. Models can be pinned per job.
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_BASE_URL = os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1").rstrip("/")
XAI_COMMAND_MODEL = os.environ.get("XAI_COMMAND_MODEL", "grok-4.3")      # fast: commands, agent tools
XAI_SMART_MODEL = os.environ.get("XAI_SMART_MODEL", "grok-4.7")          # briefings, hard questions, web search
XAI_VOICE_MODEL = os.environ.get("XAI_VOICE_MODEL", "grok-voice-3.0")    # speech-to-speech
XAI_STT_MODEL = os.environ.get("XAI_STT_MODEL", "grok-voice-transcribe-2.0")
XAI_TTS_VOICE = os.environ.get("XAI_TTS_VOICE", "naksh")  # a male voice (the MD's choice in Settings wins)
XAI_TIMEOUT_SECONDS = float(os.environ.get("XAI_TIMEOUT_SECONDS", "40"))
# JARVIS's spoken replies are voiced by Grok. A per-person daily character cap keeps the bill predictable
# (60,000 characters is roughly 500 short replies, under one dollar); past it the phone uses its own voice.
JARVIS_GROK_VOICE = env_bool("JARVIS_GROK_VOICE", True)
JARVIS_TTS_DAILY_CHAR_LIMIT = int(os.environ.get("JARVIS_TTS_DAILY_CHAR_LIMIT", "60000"))
JARVIS_TTS_MAX_CHARS = int(os.environ.get("JARVIS_TTS_MAX_CHARS", "700"))  # one spoken reply, cut at a sentence end

# Live push to the phones (Firebase Cloud Messaging). Either a path to the service-account key or its JSON.
# Without one, push is off and the phones fall back to their half-minute check.
FIREBASE_SERVICE_ACCOUNT_FILE = os.environ.get("FIREBASE_SERVICE_ACCOUNT_FILE", "")
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
JARVIS_PUSH_ENABLED = env_bool("JARVIS_PUSH_ENABLED", True)

# TaskPanel connector (public_html/taskPanel integration API, signed server-to-server).
# "mock" = built-in contract double with the same API shape; "live" = real TaskPanel.
TASKPANEL_MODE = os.environ.get("TASKPANEL_MODE", os.environ.get("LARAVEL_MODE", "mock")).lower()
TASKPANEL_BASE_URL = os.environ.get("TASKPANEL_BASE_URL", "").rstrip("/")  # https://host/taskPanel/integration/v1
TASKPANEL_KEY_ID = os.environ.get("TASKPANEL_KEY_ID", "")
TASKPANEL_SIGNING_SECRET = os.environ.get("TASKPANEL_SIGNING_SECRET", "")
TASKPANEL_ENV = os.environ.get("TASKPANEL_ENV", "local")
TASKPANEL_EXPECTED_INSTANCE = os.environ.get("TASKPANEL_EXPECTED_INSTANCE", "")
TASKPANEL_VERIFY_TLS = env_bool("TASKPANEL_VERIFY_TLS", True)
TASKPANEL_WRITES_ENABLED = env_bool("TASKPANEL_WRITES_ENABLED", TASKPANEL_MODE != "live")
TASKPANEL_DEFAULT_CATEGORY = os.environ.get("TASKPANEL_DEFAULT_CATEGORY", "general")
TASKPANEL_CONNECT_TIMEOUT = float(os.environ.get("TASKPANEL_CONNECT_TIMEOUT", "3"))
TASKPANEL_READ_TIMEOUT = float(os.environ.get("TASKPANEL_READ_TIMEOUT", "15"))
TASKPANEL_POLL_ACTIVE_SECONDS = int(os.environ.get("TASKPANEL_POLL_ACTIVE_SECONDS", "5"))
TASKPANEL_POLL_IDLE_SECONDS = int(os.environ.get("TASKPANEL_POLL_IDLE_SECONDS", "30"))
TASKPANEL_DIRECTORY_TTL_SECONDS = int(os.environ.get("TASKPANEL_DIRECTORY_TTL_SECONDS", "60"))
TASKPANEL_MOCK_PROGRESS = env_bool("TASKPANEL_MOCK_PROGRESS", True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {
        "jarvis": {"handlers": ["console"], "level": "INFO"},
        "django.request": {"handlers": ["console"], "level": "WARNING"},
    },
}
