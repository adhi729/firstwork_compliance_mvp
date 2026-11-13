from dataclasses import dataclass

from rest_framework.authentication import BaseAuthentication
from rest_framework.request import Request


@dataclass(frozen=True)
class TenantAPIKeyPrincipal:
    tenant_id: str
    api_key_id: str

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False


class TenantAPIKeyAuthentication(BaseAuthentication):
    def authenticate(self, request: Request):
        django_request = request._request
        tenant_id = getattr(django_request, "tenant_id", "")
        api_key_id = getattr(django_request, "api_key_id", None)
        if not tenant_id or not api_key_id:
            return None
        return TenantAPIKeyPrincipal(tenant_id, api_key_id), api_key_id

    def authenticate_header(self, request: Request) -> str:
        return "Bearer"
