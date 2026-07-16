"""
Django settings for the Turn Inspection Processing service.

Configuration is environment-driven so the same code runs locally (SQLite),
in CI, and on Railway (Postgres). See .env.example for the full list of vars.
"""
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).resolve().parent.parent

# Load a local .env if present (no-op in production where env is injected).
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


# --- Core -------------------------------------------------------------------

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "insecure-dev-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")
    if h.strip()
]

# Railway serves behind a proxy that terminates TLS.
CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if o.strip()
]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "turns",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.request",
            ]
        },
    }
]

# --- Database ---------------------------------------------------------------
# DATABASE_URL drives everything. Defaults to a local SQLite file so
# `manage.py check` / `makemigrations` work with zero setup.

DATABASES = {
    "default": dj_database_url.parse(
        os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'dev.sqlite3'}"),
        conn_max_age=600,
        conn_health_checks=True,
    )
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Static -----------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- Logging ----------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"}
    },
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}

# ---------------------------------------------------------------------------
# Application-specific configuration
# ---------------------------------------------------------------------------

# Local (or Railway volume) directory where downsampled photos + metadata are
# written, keyed by inspection run. In production point this at a mounted volume.
MEDIA_STORAGE_ROOT = Path(
    os.environ.get("MEDIA_STORAGE_ROOT", str(BASE_DIR / "storage"))
)

# Shared secret the contractor form / Salesforce Flow must present on the
# webhook (checked against the `X-Webhook-Secret` header). Required in prod.
WEBHOOK_SHARED_SECRET = os.environ.get("WEBHOOK_SHARED_SECRET", "")

# Image downsampling target (long edge, px) and JPEG quality for the ingest
# resize step. 1568px is Anthropic's per-image sweet spot for vision.
IMAGE_LONG_EDGE_PX = int(os.environ.get("IMAGE_LONG_EDGE_PX", "1568"))
IMAGE_JPEG_QUALITY = int(os.environ.get("IMAGE_JPEG_QUALITY", "80"))

# Worker polling cadence (seconds) when the job queue is empty.
WORKER_POLL_INTERVAL_SECONDS = float(
    os.environ.get("WORKER_POLL_INTERVAL_SECONDS", "3.0")
)
# Max attempts before a job is marked permanently failed.
JOB_MAX_ATTEMPTS = int(os.environ.get("JOB_MAX_ATTEMPTS", "3"))

# --- External integrations (read lazily by services) ------------------------
DROPBOX_ACCESS_TOKEN = os.environ.get("DROPBOX_ACCESS_TOKEN", "")
DROPBOX_APP_KEY = os.environ.get("DROPBOX_APP_KEY", "")
DROPBOX_APP_SECRET = os.environ.get("DROPBOX_APP_SECRET", "")
DROPBOX_REFRESH_TOKEN = os.environ.get("DROPBOX_REFRESH_TOKEN", "")

SALESFORCE_USERNAME = os.environ.get("SALESFORCE_USERNAME", "")
SALESFORCE_PASSWORD = os.environ.get("SALESFORCE_PASSWORD", "")
SALESFORCE_SECURITY_TOKEN = os.environ.get("SALESFORCE_SECURITY_TOKEN", "")
SALESFORCE_DOMAIN = os.environ.get("SALESFORCE_DOMAIN", "login")  # or "test"

# Work_Item__c field (URL) where the review-app link for a turn is written.
# TODO(confirm): create this custom field on Work_Item__c, or set to an existing
# URL field's API name.
SF_REVIEW_URL_FIELD = os.environ.get("SF_REVIEW_URL_FIELD", "Turn_Review_URL__c")

# --- Review app -------------------------------------------------------------
# Public base URL of this service (no trailing slash), used to build the review
# link written back to Salesforce.
REVIEW_BASE_URL = os.environ.get("REVIEW_BASE_URL", "http://localhost:8000").rstrip("/")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# --- Evaluation (LLM passes) ------------------------------------------------
# Pass 1 classification: cheap, high-volume -> Haiku. Pass 2/3 comparison &
# synthesis: vision-quality-sensitive -> Sonnet. All overridable per env.
EVAL_MODEL_CLASSIFY = os.environ.get("EVAL_MODEL_CLASSIFY", "claude-haiku-4-5")
EVAL_MODEL_COMPARE = os.environ.get("EVAL_MODEL_COMPARE", "claude-sonnet-5")
EVAL_MODEL_SYNTHESIZE = os.environ.get("EVAL_MODEL_SYNTHESIZE", "claude-sonnet-5")

# Max photos of each source (current/prior) sent in a single room-comparison
# call. Rooms with more are split into batches (never silently truncated).
EVAL_MAX_IMAGES_PER_ROOM = int(os.environ.get("EVAL_MAX_IMAGES_PER_ROOM", "30"))
# Concurrent classification calls (Pass 1). Keep modest to respect rate limits.
EVAL_CLASSIFY_CONCURRENCY = int(os.environ.get("EVAL_CLASSIFY_CONCURRENCY", "4"))
EVAL_MAX_TOKENS = int(os.environ.get("EVAL_MAX_TOKENS", "8000"))
