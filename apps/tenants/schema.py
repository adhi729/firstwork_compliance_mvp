from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, OpenApiTypes
from rest_framework import serializers


class ProblemDetailSerializer(serializers.Serializer):
    type = serializers.CharField()
    title = serializers.CharField()
    status = serializers.IntegerField()
    code = serializers.CharField()
    detail = serializers.CharField(required=False)
    errors = serializers.JSONField(required=False)


IF_MATCH = OpenApiParameter(
    "If-Match",
    OpenApiTypes.STR,
    OpenApiParameter.HEADER,
    required=True,
    description=(
        'Current resource version, for example "1". A missing, invalid, or stale '
        "version returns 412 VERSION_MISMATCH."
    ),
)
ETAG = OpenApiParameter(
    "ETag",
    OpenApiTypes.STR,
    OpenApiParameter.HEADER,
    response=[200, 201],
    description="The current version as a quoted entity tag, for example \"1\".",
)
PAGINATION_PARAMETERS = [
    OpenApiParameter(
        "limit",
        {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
        OpenApiParameter.QUERY,
        description="Maximum number of results to return.",
    ),
    OpenApiParameter(
        "cursor",
        OpenApiTypes.STR,
        OpenApiParameter.QUERY,
        description="Signed next_cursor from a previous page of the same query.",
    ),
]


def problem_response(description: str) -> OpenApiResponse:
    return OpenApiResponse(response=ProblemDetailSerializer, description=description)


class TenantAPIKeyScheme(OpenApiAuthenticationExtension):
    target_class = "apps.tenants.authentication.TenantAPIKeyAuthentication"
    name = "TenantAPIKey"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "tenant-id.secret",
        }
