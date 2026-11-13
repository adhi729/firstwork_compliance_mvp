import logging
from http import HTTPStatus

from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


def problem_detail_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        logger.error(
            "Unhandled API exception",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        response = Response(
            {
                "type": "about:blank",
                "title": "Internal Server Error",
                "status": status.HTTP_500_INTERNAL_SERVER_ERROR,
                "code": "INTERNAL_SERVER_ERROR",
                "detail": "An unexpected error occurred.",
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        response["Content-Type"] = "application/problem+json"
        return response

    code = "MALFORMED_REQUEST"
    if response.status_code == status.HTTP_401_UNAUTHORIZED:
        code = "UNAUTHENTICATED"
    elif response.status_code == status.HTTP_403_FORBIDDEN:
        code = "TENANT_ACCESS_DENIED"
    elif response.status_code == status.HTTP_404_NOT_FOUND:
        code = str(getattr(exc, "default_code", "NOT_FOUND")).upper()
    elif response.status_code == status.HTTP_409_CONFLICT:
        code = str(getattr(exc, "default_code", "CONFLICT")).upper()
    elif response.status_code == status.HTTP_412_PRECONDITION_FAILED:
        code = str(getattr(exc, "default_code", "VERSION_MISMATCH")).upper()
    elif response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY:
        code = str(getattr(exc, "default_code", "INVALID_REQUEST")).upper()
    elif response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        code = "RATE_LIMITED"
    elif response.status_code == status.HTTP_504_GATEWAY_TIMEOUT:
        code = "EVALUATION_TIMEOUT"

    get_codes = getattr(exc, "get_codes", None)
    if callable(get_codes) and response.status_code in {
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_ENTITY,
    }:
        exception_code = get_codes()
        if isinstance(exception_code, str) and exception_code not in {
            "not_found",
            "conflict",
            "invalid",
        }:
            code = exception_code.upper()

    detail = response.data
    title = HTTPStatus(response.status_code).phrase
    problem = {
        "type": "about:blank",
        "title": title,
        "status": response.status_code,
        "code": code,
    }
    if isinstance(exc, ValidationError):
        problem["errors"] = detail
    elif isinstance(detail, dict) and "detail" in detail:
        problem["detail"] = str(detail["detail"])
    else:
        problem["detail"] = str(detail)
    response.data = problem
    response["Content-Type"] = "application/problem+json"
    return response
