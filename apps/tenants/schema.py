from drf_spectacular.extensions import OpenApiAuthenticationExtension


class TenantAPIKeyScheme(OpenApiAuthenticationExtension):
    target_class = "apps.tenants.authentication.TenantAPIKeyAuthentication"
    name = "TenantAPIKey"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "tenant-id.secret",
        }
