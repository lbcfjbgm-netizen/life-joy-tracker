"""Django settings for Yamtrack project."""

import json
import sys
import warnings
import zoneinfo
from pathlib import Path
from urllib.parse import urljoin, urlparse

from celery.schedules import crontab
from decouple import (
    Config,
    Csv,
    RepositorySecret,
    Undefined,
    UndefinedValueError,
    config,
    undefined,
)
from django.core.cache import CacheKeyWarning
from django.core.exceptions import ImproperlyConfigured

BASE_URL = config("BASE_URL", default=None)
if BASE_URL:
    FORCE_SCRIPT_NAME = BASE_URL

REDIS_PREFIX = config("REDIS_PREFIX", default=None)

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def secret(key, default=undefined, **kwargs):
    """Try to read a config value from a secret file.

    If only the filename is given, try to read from /run/secrets/<key>.
    If an absolute path is specified, try to read from this path.
    """
    if isinstance(default, Undefined):
        default = None

    file = config(key, default, **kwargs)

    if file is None:
        return undefined
    if file == default:
        return default

    path = Path(file)
    try:
        if path.is_absolute():
            secret_value = Config(RepositorySecret(path.parent))(
                path.stem,
                default,
                **kwargs,
            )
        else:
            secret_value = Config(RepositorySecret())(file, default, **kwargs)
    except (
        FileNotFoundError,
        IsADirectoryError,
        UndefinedValueError,
    ) as err:
        msg = f"File from {key} not found. Please check the path and filename."
        raise UndefinedValueError(msg) from err
    else:
        if isinstance(secret_value, str):
            return secret_value.strip()
        return secret_value


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/stable/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config(
    "SECRET",
    default=secret("SECRET_FILE", default="ifx7bdUWo5EwC2NQNihjRjOrW00Cdv5Y"),
)


# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config("DEBUG", default=False, cast=bool)

# 本地自用：留空即可让 debug_toolbar 不显示（它只对 INTERNAL_IPS 里的地址生效）
INTERNAL_IPS = []

ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="*", cast=Csv())

if ALLOWED_HOSTS != ["*"]:
    if "localhost" not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append("localhost")
    if "127.0.0.1" not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append("127.0.0.1")


CSRF_TRUSTED_ORIGINS = config("CSRF", default="", cast=Csv())

URLS = config("URLS", default="", cast=Csv())

for url in URLS:
    CSRF_TRUSTED_ORIGINS.append(url)
    ALLOWED_HOSTS.append(urlparse(url).hostname)

if BASE_URL:
    CSRF_COOKIE_PATH = BASE_URL + "/"

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Application definition

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "app",
    "events",
    "integrations",
    "lists",
    "users",
    "debug_toolbar",
    "django_celery_beat",
    "django_celery_results",
    "django_select2",
    "simple_history",
    "widget_tweaks",
    "health_check",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "django.contrib.humanize",
]

MIDDLEWARE = [
    "debug_toolbar.middleware.DebugToolbarMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Needs to be near the top (right after SecurityMiddleware): it compresses
    # the finished response of everything below it. It skips bodies under 200
    # bytes and anything that already carries a Content-Encoding (images, SSE),
    # so binary and streamed responses are left alone.
    "django.middleware.gzip.GZipMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "app.middleware.ProviderAPIErrorMiddleware",
]

YAMTRACK_AUTO_LOGIN_USERNAME = config("YAMTRACK_AUTO_LOGIN_USERNAME", default=None)
if YAMTRACK_AUTO_LOGIN_USERNAME:
    _index = MIDDLEWARE.index("django.contrib.auth.middleware.AuthenticationMiddleware")
    # This allows auto-login if the user is not already authenticated.
    MIDDLEWARE.insert(_index + 1, "app.middleware.AutoLoginMiddleware")

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.media",
                "app.context_processors.export_vars",
                "app.context_processors.media_enums",
                "app.context_processors.persistent_messages",
                "app.context_processors.sidebar_items",
                "django.template.context_processors.request",
            ],
        },
    },
]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

WSGI_APPLICATION = "config.wsgi.application"

# Database
# https://docs.djangoproject.com/en/stable/ref/settings/#databases

# create db folder if it doesn't exist
Path(BASE_DIR / "db").mkdir(parents=True, exist_ok=True)

if config("DB_HOST", default=None):
    DB_POOL_MIN_SIZE = config("DB_POOL_MIN_SIZE", default=1, cast=int)
    DB_POOL_MAX_SIZE = config("DB_POOL_MAX_SIZE", default=4, cast=int)

    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": config("DB_HOST"),
            "NAME": config("DB_NAME", default=secret("DB_NAME_FILE")),
            "USER": config("DB_USER", default=secret("DB_USER_FILE")),
            "PASSWORD": config("DB_PASSWORD", default=secret("DB_PASSWORD_FILE")),
            "PORT": config("DB_PORT"),
            "OPTIONS": {
                "pool": {
                    "min_size": DB_POOL_MIN_SIZE,
                    "max_size": DB_POOL_MAX_SIZE,
                },
            },
        },
    }

    sslmode = config("DB_SSL_MODE", default=None)
    if sslmode:
        DATABASES["default"]["OPTIONS"]["sslmode"] = sslmode

    sslcertmode = config("DB_SSL_CERT_MODE", default=None)
    if sslcertmode:
        DATABASES["default"]["OPTIONS"]["sslcertmode"] = sslcertmode

else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db" / "db.sqlite3",
            "OPTIONS": {
                "timeout": 10,
                "transaction_mode": "IMMEDIATE",
                "init_command": (
                    "PRAGMA journal_mode=WAL;"
                    "PRAGMA synchronous=NORMAL;"
                    "PRAGMA mmap_size=134217728;"
                    "PRAGMA journal_size_limit=27103364;"
                    "PRAGMA temp_store=MEMORY;"
                    "PRAGMA cache_size=2000;"
                ),
            },
        },
    }

# Cache
# https://docs.djangoproject.com/en/stable/topics/cache/
CACHE_TIMEOUT = 86400  # 24 hours
REDIS_URL = config("REDIS_URL", default="redis://localhost:6379")
KEY_PREFIX = f"{REDIS_PREFIX}" if REDIS_PREFIX else ""
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "TIMEOUT": CACHE_TIMEOUT,
        "VERSION": 19,
        "KEY_PREFIX": KEY_PREFIX,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    },
}

# not using Memcached, ignore CacheKeyWarning
# https://docs.djangoproject.com/en/stable/topics/cache/#cache-key-warnings
warnings.simplefilter("ignore", CacheKeyWarning)


# Password validation
# https://docs.djangoproject.com/en/stable/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
]

# Logging
# https://docs.djangoproject.com/en/stable/topics/logging/
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "loggers": {
        "requests_ratelimiter.requests_ratelimiter": {
            "level": "WARNING",
        },
        "psycopg": {
            "level": "DEBUG" if DEBUG else "WARNING",
        },
        "urllib3": {
            "level": "WARNING",
        },
        "celery.utils.functional": {
            "level": "WARNING",
        },
        "fakeredis": {
            "level": "WARNING",
        },
    },
    "formatters": {
        "verbose": {
            # format consistent with gunicorn's
            "format": "[{asctime}] [{process}] [{levelname}] {message}",
            "datefmt": "%Y-%m-%d %H:%M:%S %z",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "level": "DEBUG" if DEBUG else "INFO",
        },
    },
    "root": {"handlers": ["console"], "level": "DEBUG" if DEBUG else "INFO"},
}

# Internationalization
# https://docs.djangoproject.com/en/stable/topics/i18n/

LANGUAGE_CODE = "zh-hans"

LANGUAGES = [
    ("zh-hans", "简体中文"),
    ("en", "English"),
]

LOCALE_PATHS = [BASE_DIR / "locale"]

TIME_ZONE = config("TZ", default="UTC")

USE_I18N = True

USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/stable/howto/static-files/

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

STATICFILES_DIRS = [BASE_DIR / "static"]

# Steam 客户端本地截图缓存，详情页直接从这里读图，不入库。
# 目录结构是 <根>\<appid>\screenshots\<文件名>.jpg 加同名的 thumbnails\ 副本，
# 读取逻辑在 app/steam_screenshots.py。这里默认留空，也就是「没配就自动探测」
# （扫 Steam 常见安装位置下的 userdata\<账号>\760\remote）；换机器一般不用改。
STEAM_SCREENSHOT_ROOT = config(
    "STEAM_SCREENSHOT_ROOT",
    default="",
)

if BASE_URL:
    STATIC_URL = f"{BASE_URL}/static/"

# Default primary key field type
# https://docs.djangoproject.com/en/stable/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Auth settings

LOGIN_URL = "account_login"

LOGIN_REDIRECT_URL = "home"

AUTH_USER_MODEL = "users.User"

# Yamtrack settings

# For CSV imports and the Trakt export archive
FILE_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024  # 50 MB

VERSION = config("VERSION", default="dev")

# Single-user local site: the admin is part of the daily workflow, so it is on
# by default. Setting ADMIN_ENABLED=False in .env still turns it off.
ADMIN_ENABLED = config("ADMIN_ENABLED", default=True, cast=bool)

TRACK_TIME = config("TRACK_TIME", default=True, cast=bool)

TZ = zoneinfo.ZoneInfo(TIME_ZONE)

# Placeholder shown when an item has no cover. It used to point at an SVG on
# themoviedb.org, which meant every list page waited ~1.1 s on a TLS handshake
# to a third party before it could paint a grey rectangle (12~45 of them per
# page). It is now an inline data URI: same grey tile with a picture glyph, no
# network request at all. Templates keep using ``{{ IMG_NONE }}`` unchanged,
# and ``image_relay.localize`` still recognises it by string comparison.
IMG_NONE = (
    "data:image/svg+xml;charset=utf-8,"
    "%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2064%2064%27"
    "%20preserveAspectRatio%3D%27xMidYMid%20slice%27%3E%3Crect%20width%3D%2764%27%20height%3D%2764%27"
    "%20fill%3D%27%233e454d%27%2F%3E%3Cg%20fill%3D%27none%27%20stroke%3D%27%238b949f%27"
    "%20stroke-width%3D%272.2%27%20stroke-linejoin%3D%27round%27%20stroke-linecap%3D%27round%27%3E"
    "%3Crect%20x%3D%2716%27%20y%3D%2718%27%20width%3D%2732%27%20height%3D%2728%27%20rx%3D%273%27%2F%3E"
    "%3Cpath%20d%3D%27M19%2042l8-10%206%207%205-5%207%208%27%2F%3E"
    "%3Ccircle%20cx%3D%2741%27%20cy%3D%2726%27%20r%3D%272.6%27%2F%3E%3C%2Fg%3E%3C%2Fsvg%3E"
)

REQUEST_TIMEOUT = 120  # seconds
# Only the connect phase is capped tightly: a host that never answers the TCP
# handshake fails fast, while a slow but reachable API keeps the generous read
# timeout above (some payloads are megabytes).
API_CONNECT_TIMEOUT = 5  # seconds

# A person waiting for the search box gets its own, much tighter read timeout.
# The wide one above stays untouched: imports, syncs and achievement fetching
# keep it, because losing an item to a short timeout is worse than being slow.
# The interactive path also drops the retries of urllib3 (see
# ``app.providers.services``), so a host that accepts the connection and then
# says nothing costs one read timeout instead of four: 5 s can go to the TCP
# handshake and 6 s to the first byte, which keeps a search under 10 s.
SEARCH_READ_TIMEOUT = 6  # seconds

# ...and the whole search, not just one hop, is capped. A search is several
# requests in a row (the store call, then one cover lookup per game, run in
# rounds of a thread pool), so a per request timeout still adds up: 10 games
# through 8 workers with a 6 s read timeout each could reach 18 s. Every
# request inside the interactive block is clamped to what is left of this
# budget, and a cover lookup that no longer fits is answered from the App ID
# alone. Measured: with every Steam host silent the search returns in 8 s, which
# leaves room for the rest of the view under the 10 s target.
SEARCH_TOTAL_BUDGET = 8  # seconds
PER_PAGE = 24

TMDB_API = config(
    "TMDB_API",
    default=secret(
        "TMDB_API_FILE",
        "61572be02f0a068658828f6396aacf60",
    ),
)
TMDB_NSFW = config("TMDB_NSFW", default=False, cast=bool)
TMDB_LANG = config("TMDB_LANG", default="en")

TVDB_API = config(
    "TVDB_API",
    default=secret(
        "TVDB_API_FILE",
        "91b5c503-23f1-4181-be23-64ad8b8e8bc1",
    ),
)

MAL_API = config(
    "MAL_API",
    default=secret(
        "MAL_API_FILE",
        "25b5581dafd15b3e7d583bb79e9a1691",
    ),
)
MAL_NSFW = config("MAL_NSFW", default=False, cast=bool)

MU_NSFW = config("MU_NSFW", default=False, cast=bool)

IGDB_ID = config(
    "IGDB_ID",
    default=secret(
        "IGDB_ID_FILE",
        "8wqmm7x1n2xxtnz94lb8mthadhtgrt",
    ),
)
IGDB_SECRET = config(
    "IGDB_SECRET",
    default=secret(
        "IGDB_SECRET_FILE",
        "ovbq0hwscv58hu46yxn50hovt4j8kj",
    ),
)
IGDB_NSFW = config("IGDB_NSFW", default=False, cast=bool)

# BoardGameGeek API Token - Register at https://boardgamegeek.com/using_the_xml_api
BGG_API_TOKEN = config(
    "BGG_API_TOKEN",
    default=secret(
        "BGG_API_TOKEN_FILE",
        "92f43ab1-d1d5-4e18-8b82-d1f56dc12927",
    ),
)

STEAM_API_KEY = config(
    "STEAM_API_KEY",
    default=secret(
        "STEAM_API_KEY_FILE",
        "",
    ),  # Generate default key https://steamcommunity.com/dev/apikey
)

# 本地自用：默认的 Steam 账号（省得每次导入都手填 SteamID64）
STEAM_ID = config("STEAM_ID", default="")

# 家庭共享需要的网页登录令牌（access_token），从
# https://store.steampowered.com/pointssummary/ajaxgetasyncconfig 的 data.webapi_token 取
STEAM_ACCESS_TOKEN = config("STEAM_ACCESS_TOKEN", default="")

HARDCOVER_API = config(
    "HARDCOVER_API",
    default=secret(
        "HARDCOVER_API_FILE",
        "Bearer "
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJpc3MiOiJIYXJkY292ZXIiLCJ2ZXJzaW9uIjoiOCIsImp0aSI6IjMzNDhiNGE1"
        "LWIzYTUtNDAxMy1hODU3LWQ4NGI1OTdmYmI3ZCIsImFwcGxpY2F0aW9uSWQi"
        "OjIsInN1YiI6IjM0OTUxIiwiYXVkIjoiMSIsImlkIjoiMzQ5NTEiLCJsb2dnZWRJ"
        "biI6dHJ1ZSwiaWF0IjoxNzc4ODQzMTE1LCJleHAiOjE4MTAzNzkxMTUsImh0dHBz"
        "Oi8vaGFzdXJhLmlvL2p3dC9jbGFpbXMiOnsieC1oYXN1cmEtYWxsb3dlZC1yb2xl"
        "cyI6WyJ1c2VyIl0sIngtaGFzdXJhLWRlZmF1bHQtcm9sZSI6InVzZXIiLCJ4"
        "LWhhc3VyYS1yb2xlIjoidXNlciIsIlgtaGFzdXJhLXVzZXItaWQiOiIzNDk1MSJ9"
        "LCJ1c2VyIjp7ImlkIjozNDk1MX19."
        "j4MVAEi_-w2N7DuiMgAxkfVc6RuKd88AHrOyzF5xLyU",
    ),
)
HARDCOVER_API = HARDCOVER_API.strip()
if not HARDCOVER_API.startswith("Bearer "):
    msg = "HARDCOVER_API must start with 'Bearer '."
    raise ImproperlyConfigured(msg)

COMICVINE_API = config(
    "COMICVINE_API",
    default=secret(
        "COMICVINE_API_FILE",
        "cdab0706269e4bca03a096fbc39920dadf7e4992",
    ),
)

TRAKT_API = config(
    "TRAKT_API",
    default=secret(
        "TRAKT_API_FILE",
        "b4d9702b11cfaddf5e863001f68ce9d4394b678926e8a3f64d47bf69a55dd0fe",
    ),
)

TRAKT_API_SECRET = config(
    "TRAKT_API_SECRET",
    default=secret(
        "TRAKT_API_SECRET_FILE",
        "",
    ),
)

ANILIST_ID = config(
    "ANILIST_ID",
    default=secret(
        "ANILIST_ID_FILE",
        "",
    ),
)

ANILIST_SECRET = config(
    "ANILIST_SECRET",
    default=secret(
        "ANILIST_SECRET_FILE",
        "",
    ),
)

SIMKL_ID = config(
    "SIMKL_ID",
    default=secret(
        "SIMKL_ID_FILE",
        "a973e57e85d94068315d5ac29669d85da8abc0fb7aff1d22e00e04bdf1882578",
    ),
)
SIMKL_SECRET = config(
    "SIMKL_SECRET",
    default=secret(
        "SIMKL_SECRET_FILE",
        "1b548a88ac7884a757cc58a552842913a9337f3cab3a4905836c6dc305dda316",
    ),
)


TESTING = False

HEALTHCHECK_CELERY_PING_TIMEOUT = config(
    "HEALTHCHECK_CELERY_PING_TIMEOUT",
    default=1,
    cast=int,
)

# Third party settings

DEBUG_TOOLBAR_CONFIG = {
    "SKIP_TEMPLATE_PREFIXES": (
        "django/forms/widgets/",
        "admin/widgets/",
    ),
    "ROOT_TAG_EXTRA_ATTRS": "hx-preserve",
}

SELECT2_CACHE_BACKEND = "default"
SELECT2_JS = [
    "js/libraries/jquery-3.7.1.min.js",
    "js/libraries/select2-4.1.0.min.js",
]
SELECT2_I18N_PATH = "js/i18n"
SELECT2_CSS = [
    "css/libraries/select2-4.1.0.min.css",
]
SELECT2_THEME = "tailwindcss-4"

# Celery settings

CELERY_BROKER_URL = config("CELERY_REDIS_URL", default=REDIS_URL)
CELERY_TIMEZONE = TIME_ZONE

if REDIS_PREFIX:
    CELERY_BROKER_TRANSPORT_OPTIONS = {
        "global_keyprefix": f"{REDIS_PREFIX}",
        "queue_prefix": f"{REDIS_PREFIX}",
    }

CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_WORKER_CONCURRENCY = 1
# 2026-10-26 改：原来是 1，意思是「每个任务跑完就把干活的执行体换掉」。
# 真正的多进程池（prefork）下这等于每跑一个任务就重启一次进程，导入 Steam
# 这种长任务会被反复打断；本机现在用 --pool solo（单进程、没有子进程），
# 这个值其实压根不生效，属于「暂时不炸、哪天换池就炸」的定时炸弹。
# 所以改成 200（跑满 200 个任务才换一次，足够兜住内存慢慢变大的情况）。
CELERY_WORKER_MAX_TASKS_PER_CHILD = 200
CELERY_BEAT_SYNC_EVERY = 1

CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 60 * 60 * 6  # 6 hours

CELERY_RESULT_EXTENDED = True
CELERY_RESULT_BACKEND = "django-db"
CELERY_CACHE_BACKEND = "default"
CELERY_RESULT_EXPIRES = 60 * 60 * 24 * 7  # 7 days
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# https://docs.celeryq.dev/en/stable/userguide/configuration.html#task-serializer
CELERY_TASK_SERIALIZER = "pickle"
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std-setting-accept_content
CELERY_ACCEPT_CONTENT = ["application/json", "application/x-python-serialize"]


DAILY_DIGEST_HOUR = config(
    "DAILY_DIGEST_HOUR",
    default=8,
    cast=int,
)
USER_MESSAGE_RETENTION_DAYS = config(
    "USER_MESSAGE_RETENTION_DAYS",
    default=30,
    cast=int,
)
CELERY_BEAT_SCHEDULE = {
    "reload_calendar": {
        "task": "Reload calendar",
        "schedule": 60 * 60 * 24,  # every 24 hours
    },
    "send_release_notifications": {
        "task": "Send release notifications",
        # 每 6 小时一次（原为 10 分钟）：用户不需要发行提醒，且数据库里该周期任务
        # 已置为 enabled=False。这里保留任务只为不改变数据库状态，避免 beat 重启
        # 时把周期频率与数据库登记值打架。
        "schedule": 60 * 60 * 6,
    },
    "send_daily_digest": {
        "task": "Send daily digest",
        "schedule": crontab(hour=DAILY_DIGEST_HOUR, minute=0),
    },
    "cleanup_user_messages": {
        "task": "Cleanup user messages",
        "schedule": 60 * 60 * 24,  # every 24 hours
    },
}

IS_PROD = not any(cmd in sys.argv for cmd in ("runserver", "test"))
if IS_PROD:
    ALLAUTH_TRUSTED_CLIENT_IP_HEADER = "X-Real-IP"
if CSRF_TRUSTED_ORIGINS:
    # Check if all origins start with http:// or https://
    all_http = all(
        origin.startswith("http://") for origin in CSRF_TRUSTED_ORIGINS if origin
    )
    all_https = all(
        origin.startswith("https://") for origin in CSRF_TRUSTED_ORIGINS if origin
    )

    if all_http:
        ACCOUNT_DEFAULT_HTTP_PROTOCOL = "http"
    elif all_https:
        ACCOUNT_DEFAULT_HTTP_PROTOCOL = "https"
    else:
        # Mixed protocols or invalid formats, use config value
        ACCOUNT_DEFAULT_HTTP_PROTOCOL = config(
            "ACCOUNT_DEFAULT_HTTP_PROTOCOL",
            default="https",
        )
else:
    # Empty CSRF_TRUSTED_ORIGINS, default to http
    ACCOUNT_DEFAULT_HTTP_PROTOCOL = "http"

ACCOUNT_LOGOUT_REDIRECT_URL = config(
    "ACCOUNT_LOGOUT_REDIRECT_URL",
    default="/accounts/login/?loggedout=1",
)
ACCOUNT_SESSION_REMEMBER = True
ACCOUNT_USER_MODEL_EMAIL_FIELD = None
ACCOUNT_FORMS = {
    "login": "users.forms.CustomLoginForm",
    "signup": "users.forms.CustomSignupForm",
}

if BASE_URL:
    # Join base only if relative URL
    if not urlparse(ACCOUNT_LOGOUT_REDIRECT_URL).netloc:
        ACCOUNT_LOGOUT_REDIRECT_URL = urljoin(BASE_URL, ACCOUNT_LOGOUT_REDIRECT_URL)
    SESSION_COOKIE_PATH = BASE_URL + "/"

SOCIALACCOUNT_LOGIN_ON_GET = True

SOCIAL_PROVIDERS = config("SOCIAL_PROVIDERS", default="", cast=Csv())
INSTALLED_APPS += SOCIAL_PROVIDERS

SOCIALACCOUNT_PROVIDERS = config(
    "SOCIALACCOUNT_PROVIDERS",
    default=secret(
        "SOCIALACCOUNT_PROVIDERS_FILE",
        default="{}",
    ),
    cast=json.loads,
)

SOCIALACCOUNT_ONLY = config("SOCIALACCOUNT_ONLY", default=False, cast=bool)
if SOCIALACCOUNT_ONLY:
    ACCOUNT_EMAIL_VERIFICATION = "none"

REGISTRATION = config("REGISTRATION", default=True, cast=bool)
if not REGISTRATION:
    ACCOUNT_ADAPTER = "users.account_adapter.NoNewUsersAccountAdapter"

REDIRECT_LOGIN_TO_SSO = config("REDIRECT_LOGIN_TO_SSO", default=False, cast=bool)

SESSION_COOKIE_AGE = config("SESSION_COOKIE_AGE", default=60 * 60 * 24 * 14, cast=int)
