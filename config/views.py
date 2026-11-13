import logging

import redis
from django.conf import settings
from django.db import DatabaseError, connection
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


@require_GET
def healthz(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


@require_GET
def readyz(request: HttpRequest) -> JsonResponse:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        redis_url = settings.REDIS_URL
        if not redis_url:
            raise RuntimeError("REDIS_URL must be set")
        redis_client = redis.Redis.from_url(redis_url, socket_connect_timeout=1)
        try:
            redis_client.ping()
        finally:
            redis_client.close()
    except (DatabaseError, redis.RedisError):
        logger.exception("Readiness check failed")
        return JsonResponse({"status": "not_ready"}, status=503)
    return JsonResponse({"status": "ready"})
