from rest_framework import status
from rest_framework.exceptions import APIException


class RuleDefinitionAPIError(APIException):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "INVALID_RULE_DEFINITION"
    default_detail = "The rule definition is invalid."


class VersionMismatchAPIError(APIException):
    status_code = status.HTTP_412_PRECONDITION_FAILED
    default_code = "VERSION_MISMATCH"
    default_detail = "The resource has changed; fetch its current version and retry."


class DuplicateNameAPIError(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "DUPLICATE_NAME"
    default_detail = "A resource with this name already exists."


class IdempotencyConflictAPIError(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "IDEMPOTENCY_CONFLICT"
    default_detail = "The idempotency key was already used with a different request."
