import os

import dj_database_url

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("DJANGO_SECRET_KEY must be set")

DEBUG = False
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "drf_spectacular",
    "apps.tenants.apps.TenantsConfig",
    "apps.directory.apps.DirectoryConfig",
    "apps.rules.apps.RulesConfig",
    "apps.evaluation.apps.EvaluationConfig",
    "apps.webhooks.apps.WebhooksConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "apps.tenants.middleware.RequestIDMiddleware",
    "apps.tenants.middleware.TenantContextMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
    },
]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL must be set")
DATABASES = {
    "default": dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=60,
        conn_health_checks=True,
    ),
}
DATABASES["default"]["OPTIONS"] = {"connect_timeout": 3}
DATA_UPLOAD_MAX_MEMORY_SIZE = 1_048_576
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000

REDIS_URL = os.environ.get("REDIS_URL")
if not REDIS_URL:
    raise RuntimeError("REDIS_URL must be set")
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS")
if not TEMPORAL_ADDRESS:
    raise RuntimeError("TEMPORAL_ADDRESS must be set")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
WEBHOOK_SIGNING_SECRET = os.environ.get("WEBHOOK_SIGNING_SECRET")
if not WEBHOOK_SIGNING_SECRET:
    raise RuntimeError("WEBHOOK_SIGNING_SECRET must be set")
_rule_references_enabled = os.environ.get("RULE_REFERENCES_ENABLED", "false").lower()
if _rule_references_enabled not in {"true", "false"}:
    raise RuntimeError("RULE_REFERENCES_ENABLED must be true or false")
RULE_REFERENCES_ENABLED = _rule_references_enabled == "true"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.tenants.authentication.TenantAPIKeyAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_THROTTLE_CLASSES": ["apps.tenants.throttling.TenantRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        "tenant": os.environ.get("API_TENANT_RATE", "1000/min"),
    },
    "EXCEPTION_HANDLER": "apps.tenants.exception_handlers.problem_detail_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}
SPECTACULAR_SETTINGS = {
    "TITLE": "FirstWork Compliance API",
    "DESCRIPTION": (
        "Tenant-scoped compliance rules and evaluation API. Rule property entity prefixes "
        "are case-insensitive; user. is an alias for worker. and is stored canonically."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "ENUM_NAME_OVERRIDES": {
        "CountryCode": "apps.directory.countries.COUNTRY_CHOICES",
    },
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "config.logging.RequestJSONFormatter"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "json"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
