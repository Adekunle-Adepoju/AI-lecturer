import os
from pathlib import Path
from dotenv import load_dotenv
import dj_database_url

# Load environment variables from .env file
load_dotenv()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# No hardcoded fallback in production: missing key -> Django refuses to start
DEBUG = os.environ.get("DEBUG", "0") == "1"
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-key" if DEBUG else None)

# SECURITY WARNING: don't run with debug turned on in production!


ALLOWED_HOSTS = ["10.251.19.165", "localhost", "127.0.0.1", "planner-cornball-casino.ngrok-free.dev"]
CSRF_TRUSTED_ORIGINS = ["https://planner-cornball-casino.ngrok-free.dev"]

ADMIN_URL = os.environ.get("ADMIN_URL", "admin/")


RENDER_HOST = os.environ.get("RENDER_EXTERNAL_HOSTNAME")  # set automatically by Render
if RENDER_HOST:
    ALLOWED_HOSTS.append(RENDER_HOST)
    CSRF_TRUSTED_ORIGINS.append(f"https://{RENDER_HOST}")

# Application definition

INSTALLED_APPS = [
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'channels',
    'core',
    'battle',
    'django_cleanup.apps.CleanupConfig',
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    'engagement',
    'focus_sprint',
    "django_q",
]

# Staff/superuser traffic is ignored by default so sponsor numbers are clean.
# Set ANALYTICS_TRACK_STAFF=1 in .env while you're testing as staff.
ANALYTICS_TRACK_STAFF = os.environ.get("ANALYTICS_TRACK_STAFF") == "1"

SITE_ID = 1

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

LOGIN_REDIRECT_URL = "dashboard"
SOCIALACCOUNT_LOGIN_ON_GET = True

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
    }
}

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'core.middleware.NoCacheForAuthenticatedPagesMiddleware',
    "allauth.account.middleware.AccountMiddleware",
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'petrolearn.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [ BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.active_focus_sprint',
            ],
        },
    },
]

WSGI_APPLICATION = 'petrolearn.wsgi.application'

ASGI_APPLICATION = 'petrolearn.asgi.application'

if os.environ.get("REDIS_URL"):
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [os.environ["REDIS_URL"]]},
        },
    }
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    "default": dj_database_url.config(
        default=os.environ.get("DATABASE_URL"),
        conn_max_age=0,
    )
}
DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = True

# --- Old SQLite config, kept for quick rollback if needed ---
# DATABASES = {
#     "default": {
#         "ENGINE": "django.db.backends.sqlite3",
#         "NAME": BASE_DIR / "db.sqlite3",
#         "OPTIONS": {
#             "transaction_mode": "IMMEDIATE",
#             "timeout": 20,
#             "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
#         },
#     }
# }

# --- Django-Q2: ORM broker, tuned for 512 MB ---
Q_CLUSTER = {
    "name": "rovea",
    "orm": "default",            # use the default Django DB as the broker (no Redis)
    "workers": 1,                # one process = one Gemini job at a time. Don't raise on 512 MB.
    "recycle": 5,                # restart the worker after 5 tasks to release leaked memory
    "max_rss": 180_000,          # KB (~175 MB): recycle the worker if it grows past this
    "timeout": 1800,              # hard-kill a task after 10 min (Gemini takes 30-60s)
    "retry": 2000,                # MUST be greater than timeout, or tasks run twice
    "max_attempts": 1,           # never auto-rerun: avoids double Gemini quota burn
    "ack_failures": True,        # failed tasks leave the queue instead of looping
    "queue_limit": 10,           # cap tasks held in memory by the cluster
    "bulk": 1,                   # take one task at a time
    "poll": 2,                   # check the DB every 2s instead of 0.2s (lighter on Supabase)
    "save_limit": 50,            # keep only the last 50 successful task records
    "catch_up": False,           # don't replay missed schedules after downtime
    "cpu_affinity": 1,
    "label": "Rovea Tasks",
    "sync": os.getenv("Q_SYNC") == "1",   # set Q_SYNC=1 locally/in tests to run tasks inline
}

MAX_UPLOAD_MB = 25

# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = '/static/'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

STATICFILES_DIRS = []
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True


GEMINI_API_KEY_CHAT = os.environ.get("GEMINI_API_KEY_CHAT")
GEMINI_API_KEY_EXTRACTION = os.environ.get("GEMINI_API_KEY_EXTRACTION")
GEMINI_API_KEY_IMAGES = os.environ.get("GEMINI_API_KEY_IMAGES")
GEMINI_API_KEY_SIMULATOR = os.environ.get('GEMINI_API_KEY_SIMULATOR')
GEMINI_API_KEY_GENERATION = os.environ.get('GEMINI_API_KEY_GENERATION')
GEMINI_API_KEY_BATTLE = os.environ.get('GEMINI_API_KEY_BATTLE')
GEMINI_API_KEY_CLEANUP = os.environ.get("GEMINI_API_KEY_CLEANUP")

LOGIN_URL = '/'

