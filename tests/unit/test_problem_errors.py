from django.http import HttpRequest
from rest_framework.exceptions import NotFound, ValidationError

from apps.tenants.errors import RuleDefinitionAPIError, VersionMismatchAPIError
from apps.tenants.exception_handlers import problem_detail_exception_handler


def test_rule_definition_errors_use_problem_details_and_422():
    response = problem_detail_exception_handler(
        RuleDefinitionAPIError("Invalid property at conditions[0]"),
        {"request": HttpRequest()},
    )
    assert response is not None
    assert response.status_code == 422
    assert response["Content-Type"] == "application/problem+json"
    assert response.data["code"] == "INVALID_RULE_DEFINITION"


def test_stale_version_uses_412_problem_code():
    response = problem_detail_exception_handler(
        VersionMismatchAPIError(),
        {"request": HttpRequest()},
    )
    assert response is not None
    assert response.status_code == 412
    assert response.data["code"] == "VERSION_MISMATCH"


def test_validation_errors_include_paths_and_never_raise_500():
    response = problem_detail_exception_handler(
        ValidationError({"definition": ["Invalid node at children[0]"]}),
        {"request": HttpRequest()},
    )
    assert response is not None
    assert response.status_code == 400
    assert "children[0]" in str(response.data["errors"])


def test_custom_not_found_code_is_preserved():
    response = problem_detail_exception_handler(
        NotFound("Rule not found", code="RULE_NOT_FOUND"),
        {"request": HttpRequest()},
    )
    assert response is not None
    assert response.status_code == 404
    assert response.data["code"] == "RULE_NOT_FOUND"
