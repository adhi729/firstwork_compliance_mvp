from rest_framework.throttling import SimpleRateThrottle


class TenantRateThrottle(SimpleRateThrottle):
    scope = "tenant"

    def get_cache_key(self, request, view):
        tenant_id = getattr(request.user, "tenant_id", None)
        if not tenant_id:
            return None
        return self.cache_format % {"scope": self.scope, "ident": tenant_id}
