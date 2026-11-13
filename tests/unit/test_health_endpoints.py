from unittest.mock import patch

import pytest
import redis
from django.test import Client

pytestmark = pytest.mark.django_db


def test_liveness_is_independent_of_dependencies():
    response = Client().get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_docs_renders_swagger_ui():
    response = Client().get("/api/docs/")
    assert response.status_code == 200
    assert b'id="swagger-ui"' in response.content


@patch("config.views.redis.Redis.from_url")
def test_readiness_checks_database_and_redis(redis_from_url):
    redis_from_url.return_value.ping.return_value = True
    response = Client().get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    redis_from_url.return_value.ping.assert_called_once_with()
    redis_from_url.return_value.close.assert_called_once_with()


@patch("config.views.redis.Redis.from_url")
def test_readiness_reports_redis_failure(redis_from_url):
    redis_from_url.return_value.ping.side_effect = redis.ConnectionError("unavailable")
    response = Client().get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
