from pathlib import Path

import pytest
import yaml
from django.urls import resolve, reverse
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIRequestFactory


@pytest.fixture(scope="module")
def schema():
    return SchemaGenerator().get_schema(request=None, public=True)


def _parameters(operation, location):
    return {
        parameter["name"]: parameter
        for parameter in operation.get("parameters", [])
        if parameter["in"] == location
    }


def test_snapshot_matches_generated_schema(schema):
    snapshot = Path(__file__).resolve().parents[2] / "docs" / "FirstWork Compliance API.yaml"
    with snapshot.open(encoding="utf-8") as file:
        assert yaml.safe_load(file) == schema


@pytest.mark.parametrize(
    "path",
    ["/api/v1/rules/{rule_id}", "/api/v1/compliance-checks/{check_id}"],
)
def test_update_documents_required_if_match_and_precondition_failure(schema, path):
    operation = schema["paths"][path]["put"]
    assert _parameters(operation, "header")["If-Match"]["required"] is True
    assert "ETag" in operation["responses"]["200"]["headers"]
    failure = operation["responses"]["412"]
    assert "VERSION_MISMATCH" in failure["description"]
    assert "application/json" in failure["content"]


def test_etags_match_rule_and_check_read_behavior(schema):
    paths = schema["paths"]
    assert "ETag" in paths["/api/v1/rules"]["post"]["responses"]["201"]["headers"]
    assert "ETag" in paths["/api/v1/rules/{rule_id}"]["get"]["responses"]["200"]["headers"]
    check_read = paths["/api/v1/compliance-checks/{check_id}"]["get"]["responses"]["200"]
    assert "ETag" not in check_read.get("headers", {})
    assert "current_version" in schema["components"]["schemas"]["CheckOutput"]["properties"]


def test_evaluation_headers_and_bulk_job_schema(schema):
    paths = schema["paths"]
    evaluation = paths["/api/v1/compliance-checks/{check_id}/evaluate"]["post"]
    key = _parameters(evaluation, "header")["Idempotency-Key"]
    assert not key.get("required", False)
    assert "application/json" in evaluation["responses"]["409"]["content"]
    assert "IDEMPOTENCY_CONFLICT" in evaluation["responses"]["409"]["description"]
    bulk = paths["/api/v1/compliance-checks/{check_id}/evaluate-bulk"]["post"]
    assert "Location" in bulk["responses"]["202"]["headers"]
    assert "get" in paths["/api/v1/evaluation-jobs/{job_id}"]
    assert "compliant" in schema["components"]["schemas"]["EvaluationOutput"]["properties"]


@pytest.mark.parametrize("resource", ["workers", "companies", "contracts"])
def test_list_pagination_parameters(schema, resource):
    operation = schema["paths"][f"/api/v1/{resource}"]["get"]
    parameters = _parameters(operation, "query")
    assert "cursor" in parameters
    assert parameters["limit"]["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 1000,
        "default": 100,
    }


def test_eligibility_parameters_and_create_requirements(schema):
    path = "/api/v1/companies/{company_id}/eligible-contracts"
    parameters = _parameters(schema["paths"][path]["get"], "query")
    assert set(parameters) == {"check_id", "rule_id", "version", "limit", "cursor"}
    assert "exactly one" in parameters["rule_id"]["description"]
    assert "only valid with rule_id" in parameters["version"]["description"]
    components = schema["components"]["schemas"]
    for name in ("RuleCreateInput", "CheckCreateInput"):
        assert "name" in components[name]["required"]


def test_schema_and_swagger_views_use_the_generated_schema(schema):
    factory = APIRequestFactory()
    schema_view = resolve(reverse("schema")).func
    response = schema_view(factory.get("/api/schema/"))
    assert response.status_code == 200
    assert response.data == schema

    docs_view = resolve(reverse("api-docs")).func
    docs = docs_view(factory.get("/api/docs/"))
    assert docs.status_code == 200
    docs.render()
    assert b'id="swagger-ui"' in docs.content
    assert reverse("schema").encode() in docs.content
