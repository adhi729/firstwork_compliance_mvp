import hashlib
import json
import secrets
import uuid

from django.db import connection, transaction
from django.http import HttpRequest, JsonResponse
from django.test import TestCase

from apps.tenants.middleware import TenantContextMiddleware
from apps.tenants.models import APIKey, Tenant


class APIKeyAuthenticationTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="API key tenant")
        self.secret = secrets.token_urlsafe(32)
        self.token = f"{self.tenant.id}.{self.secret}"
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true)",
                    [str(self.tenant.id)],
                )
            APIKey.objects.create(
                tenant=self.tenant,
                key_hash=hashlib.sha256(self.token.encode()).hexdigest(),
                name="test",
            )

    def _invoke(self, authorization: str, supplied_tenant: str = ""):
        request = HttpRequest()
        request.path = "/api/v1/private"
        request.META["HTTP_AUTHORIZATION"] = authorization
        if supplied_tenant:
            request.META["HTTP_X_TENANT_ID"] = supplied_tenant

        middleware = TenantContextMiddleware(
            lambda current_request: JsonResponse(
                {
                    "tenant_id": getattr(current_request, "tenant_id"),
                    "api_key_id": getattr(current_request, "api_key_id"),
                }
            )
        )
        return middleware(request)

    def test_valid_bearer_key_resolves_tenant_from_credential(self):
        response = self._invoke(f"Bearer {self.token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["tenant_id"], str(self.tenant.id))

    def test_tenant_header_cannot_authorize_or_override_credential(self):
        other_tenant = Tenant.objects.create(name="Other tenant")
        response = self._invoke("Bearer invalid", str(other_tenant.id))
        self.assertEqual(response.status_code, 401)

        response = self._invoke(f"Bearer {self.token}", str(other_tenant.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["tenant_id"], str(self.tenant.id))

    def test_missing_or_malformed_bearer_key_is_unauthenticated(self):
        for authorization in ("", "Basic abc", f"Bearer {uuid.uuid4()}.bad"):
            with self.subTest(authorization=authorization):
                response = self._invoke(authorization)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response["Content-Type"], "application/problem+json")
