import contextvars
import hashlib
import uuid
from collections.abc import Callable

from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse

current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


class RequestIDMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        supplied_id = request.headers.get("X-Request-ID", "")
        try:
            request_id = str(uuid.UUID(supplied_id)) if supplied_id else str(uuid.uuid4())
        except ValueError:
            request_id = str(uuid.uuid4())
        token = current_request_id.set(request_id)
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = request_id
            return response
        finally:
            current_request_id.reset(token)


class TenantContextMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        tenant_id = ""
        key_id = None
        if request.path.startswith("/api/v1/"):
            authorization = request.headers.get("Authorization", "")
            scheme, separator, token = authorization.partition(" ")
            tenant_part, dot, secret = token.partition(".")
            try:
                if scheme.lower() != "bearer" or not separator or not dot or not secret:
                    raise ValueError
                tenant_id = str(uuid.UUID(tenant_part))
            except ValueError:
                return JsonResponse(
                    {
                        "type": "about:blank",
                        "title": "Unauthenticated",
                        "status": 401,
                        "code": "UNAUTHENTICATED",
                    },
                    status=401,
                    content_type="application/problem+json",
                )

        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])
            if tenant_id:
                from apps.tenants.models import APIKey

                key = (
                    APIKey.objects.filter(
                        tenant_id=tenant_id,
                        key_hash=hashlib.sha256(f"{tenant_id}.{secret}".encode()).hexdigest(),
                        active=True,
                    )
                    .only("id")
                    .first()
                )
                if key is None:
                    return JsonResponse(
                        {
                            "type": "about:blank",
                            "title": "Unauthenticated",
                            "status": 401,
                            "code": "UNAUTHENTICATED",
                        },
                        status=401,
                        content_type="application/problem+json",
                    )
                key_id = str(key.id)
            setattr(request, "tenant_id", tenant_id)
            setattr(request, "api_key_id", key_id)
            return self.get_response(request)
