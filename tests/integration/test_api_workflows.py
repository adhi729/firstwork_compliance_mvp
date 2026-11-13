import hashlib
import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.test import override_settings
from rest_framework.test import APIClient

from apps.directory.models import Company, Contract, Worker
from apps.rules.models import Rule, RuleVersion
from apps.tenants.models import APIKey, Tenant


@pytest.mark.django_db(transaction=True)
class TestComplianceAPI:
    def setup_method(self):
        self.tenant = Tenant.objects.create(name=f"API tenant {secrets.token_hex(4)}")
        secret = secrets.token_urlsafe(32)
        self.token = f"{self.tenant.id}.{secret}"
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true)",
                    [str(self.tenant.id)],
                )
            APIKey.objects.create(
                tenant=self.tenant,
                key_hash=hashlib.sha256(self.token.encode()).hexdigest(),
                name="integration",
            )
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")

        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true)",
                    [str(self.tenant.id)],
                )
            self.company = Company.objects.create(
                tenant=self.tenant,
                name="Example GmbH",
                industry="technology",
                country="DE",
                bank_deposit=Decimal("1000.00"),
            )
            self.worker = Worker.objects.create(
                tenant=self.tenant,
                name="Alex Worker",
                date_of_birth=date(1990, 1, 1),
                nationality="DE",
            )
            self.contract = Contract.objects.create(
                tenant=self.tenant,
                company=self.company,
                worker=self.worker,
                start_date=datetime.now(UTC) - timedelta(days=10),
                end_date=datetime.now(UTC) + timedelta(days=365),
                country="DE",
            )

    def _create_rule(self, name="Adult"):
        response = self.client.post(
            "/api/v1/rules",
            {
                "name": name,
                "definition": {
                    "property": "worker.age",
                    "operator": ">=",
                    "value": 18,
                },
            },
            format="json",
        )
        assert response.status_code == 201, response.data
        return response

    def test_api_key_rule_version_check_evaluation_and_eligibility(self):
        rule_response = self._create_rule()
        rule = rule_response.data
        assert rule["current_version"] == 1
        assert rule_response["ETag"] == '"1"'

        check_response = self.client.post(
            "/api/v1/compliance-checks",
            {"name": "Adult check", "rule_ids": [rule["id"]]},
            format="json",
        )
        assert check_response.status_code == 201, check_response.data
        check = check_response.data
        assert check["versions"][0]["rules"][0]["version"] == 1

        evaluate_url = f"/api/v1/compliance-checks/{check['id']}/evaluate"
        evaluation = self.client.post(
            evaluate_url,
            {"contract_id": self.contract.id},
            format="json",
            HTTP_IDEMPOTENCY_KEY="eval-1",
        )
        assert evaluation.status_code == 200, evaluation.data
        assert evaluation.data["outcome"] == "COMPLIANT"
        assert evaluation.data["compliant"] is True
        assert evaluation.data["summary"] == {
            "rules": 1,
            "passed": 1,
            "failed": 0,
            "unknown": 0,
        }
        assert evaluation.data["results"][0]["conditions"][0]["actual"] == 36

        retry = self.client.post(
            evaluate_url,
            {"contract_id": self.contract.id},
            format="json",
            HTTP_IDEMPOTENCY_KEY="eval-1",
        )
        assert retry.status_code == 200
        assert retry.data["evaluation_id"] == evaluation.data["evaluation_id"]
        assert retry.data["compliant"] is True

        conflict = self.client.post(
            evaluate_url,
            {"contract_id": self.contract.id + 100},
            format="json",
            HTTP_IDEMPOTENCY_KEY="eval-1",
        )
        assert conflict.status_code == 409
        assert conflict.data["code"] == "IDEMPOTENCY_CONFLICT"

        updated = self.client.put(
            f"/api/v1/rules/{rule['id']}",
            {
                "name": "Adult",
                "definition": {
                    "property": "worker.age",
                    "operator": ">=",
                    "value": 100,
                },
            },
            format="json",
            HTTP_IF_MATCH='"1"',
        )
        assert updated.status_code == 200, updated.data
        assert updated.data["current_version"] == 2
        assert updated["ETag"] == '"2"'

        stale = self.client.put(
            f"/api/v1/rules/{rule['id']}",
            {
                "name": "Adult",
                "definition": {
                    "property": "worker.age",
                    "operator": ">=",
                    "value": 21,
                },
            },
            format="json",
            HTTP_IF_MATCH='"1"',
        )
        assert stale.status_code == 412
        assert stale.data["code"] == "VERSION_MISMATCH"

        pinned_evaluation = self.client.post(
            evaluate_url,
            {"contract_id": self.contract.id},
            format="json",
            HTTP_IDEMPOTENCY_KEY="eval-pinned",
        )
        assert pinned_evaluation.status_code == 200
        assert pinned_evaluation.data["outcome"] == "COMPLIANT"
        assert pinned_evaluation.data["results"][0]["version"] == 1

        eligible_v1 = self.client.get(
            f"/api/v1/companies/{self.company.id}/eligible-contracts",
            {"rule_id": rule["id"], "version": 1, "limit": 1},
        )
        assert eligible_v1.status_code == 200, eligible_v1.data
        assert [row["contract_id"] for row in eligible_v1.data["results"]] == [
            self.contract.id
        ]
        assert eligible_v1.data["next_cursor"] is None

        eligible_latest = self.client.get(
            f"/api/v1/companies/{self.company.id}/eligible-contracts",
            {"rule_id": rule["id"]},
        )
        assert eligible_latest.status_code == 200
        assert eligible_latest.data["rule_version"] == 2
        assert eligible_latest.data["results"] == []

    def test_authentication_and_rule_validation_errors(self):
        anonymous = APIClient().post(
            "/api/v1/rules",
            {"name": "No auth", "definition": {}},
            format="json",
        )
        assert anonymous.status_code == 401
        assert anonymous["Content-Type"] == "application/problem+json"

        invalid = self.client.post(
            "/api/v1/rules",
            {
                "name": "Invalid",
                "definition": {
                    "property": "worker.unknown",
                    "operator": "==",
                    "value": 1,
                },
            },
            format="json",
        )
        assert invalid.status_code == 422
        assert invalid.data["code"] == "INVALID_PROPERTY"
        assert "$.property" in invalid.data["detail"]

    def test_tenant_directory_crud_and_signed_pagination(self):
        worker_response = self.client.post(
            "/api/v1/workers",
            {
                "name": "Second Worker",
                "date_of_birth": "2000-06-15",
                "nationality": "FR",
            },
            format="json",
        )
        assert worker_response.status_code == 201, worker_response.data
        worker_id = worker_response.data["id"]
        assert self.client.get(f"/api/v1/workers/{worker_id}").status_code == 200

        company_response = self.client.post(
            "/api/v1/companies",
            {
                "name": "Second Company",
                "industry": "manufacturing",
                "country": "FR",
                "bank_deposit": "500.00",
            },
            format="json",
        )
        assert company_response.status_code == 201, company_response.data
        company_id = company_response.data["id"]

        contract_response = self.client.post(
            "/api/v1/contracts",
            {
                "worker_id": worker_id,
                "company_id": company_id,
                "start_date": "2026-01-01T00:00:00Z",
                "end_date": "2027-01-01T00:00:00Z",
                "country": "FR",
            },
            format="json",
        )
        assert contract_response.status_code == 201, contract_response.data
        contract_id = contract_response.data["id"]
        assert self.client.get(f"/api/v1/contracts/{contract_id}").status_code == 200

        first_page = self.client.get("/api/v1/workers", {"limit": 1})
        assert first_page.status_code == 200, first_page.data
        assert len(first_page.data["results"]) == 1
        assert first_page.data["next_cursor"]
        second_page = self.client.get(
            "/api/v1/workers",
            {"limit": 1, "cursor": first_page.data["next_cursor"]},
        )
        assert second_page.status_code == 200, second_page.data
        assert [row["id"] for row in second_page.data["results"]] == [worker_id]

        wrong_scope = self.client.get(
            "/api/v1/companies",
            {"limit": 1, "cursor": first_page.data["next_cursor"]},
        )
        assert wrong_scope.status_code == 400
        assert wrong_scope.data["code"] == "MALFORMED_REQUEST"

        missing_party = self.client.post(
            "/api/v1/contracts",
            {
                "worker_id": worker_id,
                "company_id": company_id + 1000,
                "start_date": "2026-01-01T00:00:00Z",
                "end_date": "2027-01-01T00:00:00Z",
                "country": "FR",
            },
            format="json",
        )
        assert missing_party.status_code == 404
        assert missing_party.data["code"] == "CONTRACT_NOT_FOUND"

    @override_settings(RULE_REFERENCES_ENABLED=True)
    def test_pinned_rule_references_evaluation_sql_and_dependents(self):
        child = self._create_rule("Reusable adult rule").data
        child_v2 = self.client.put(
            f"/api/v1/rules/{child['id']}",
            {
                "name": "Reusable adult rule",
                "definition": {
                    "property": "worker.age",
                    "operator": ">=",
                    "value": 21,
                },
            },
            format="json",
            HTTP_IF_MATCH='"1"',
        )
        assert child_v2.status_code == 200, child_v2.data
        composite_response = self.client.post(
            "/api/v1/rules",
            {
                "name": "German adult",
                "definition": {
                    "op": "AND",
                    "children": [
                        {"rule_ref": {"rule_id": child["id"]}},
                        {
                            "property": "User.nationality",
                            "operator": "==",
                            "value": "DE",
                        },
                    ],
                },
            },
            format="json",
        )
        assert composite_response.status_code == 201, composite_response.data
        composite = composite_response.data
        assert composite["definition"]["children"][0]["rule_ref"] == {
            "rule_id": child["id"],
            "version": 2,
        }
        assert composite["definition"]["children"][1]["property"] == "worker.nationality"

        versions = self.client.get(f"/api/v1/rules/{composite['id']}/versions")
        assert versions.status_code == 200
        assert versions.data[0]["schema_version"] == 2
        dependents = self.client.get(f"/api/v1/rules/{child['id']}/dependents")
        assert dependents.status_code == 200
        assert dependents.data == [
            {
                "rule_id": composite["id"],
                "version": 1,
                "name": "German adult",
                "referenced_version": 2,
            }
        ]
        explicit_pin = self.client.post(
            "/api/v1/rules",
            {
                "name": "Explicit old version",
                "definition": {
                    "op": "AND",
                    "children": [
                        {
                            "rule_ref": {
                                "rule_id": child["id"],
                                "version": 1,
                            }
                        }
                    ],
                },
            },
            format="json",
        )
        assert explicit_pin.status_code == 201, explicit_pin.data
        assert explicit_pin.data["definition"]["children"][0]["rule_ref"]["version"] == 1

        check = self.client.post(
            "/api/v1/compliance-checks",
            {"name": "Reusable-rule check", "rule_ids": [composite["id"]]},
            format="json",
        )
        assert check.status_code == 201, check.data
        evaluation_url = f"/api/v1/compliance-checks/{check.data['id']}/evaluate"
        evaluation = self.client.post(
            evaluation_url,
            {"contract_id": self.contract.id},
            format="json",
            HTTP_IDEMPOTENCY_KEY="reference-evaluation-v1",
        )
        assert evaluation.status_code == 200, evaluation.data
        assert evaluation.data["outcome"] == "COMPLIANT"
        assert evaluation.data["results"][0]["conditions"][0]["via"] == [
            f"{child['id']}@2"
        ]

        eligible = self.client.get(
            f"/api/v1/companies/{self.company.id}/eligible-contracts",
            {"rule_id": composite["id"]},
        )
        assert eligible.status_code == 200, eligible.data
        assert [row["contract_id"] for row in eligible.data["results"]] == [
            self.contract.id
        ]

        child_update = self.client.put(
            f"/api/v1/rules/{child['id']}",
            {
                "name": "Reusable adult rule",
                "definition": {
                    "property": "worker.age",
                    "operator": ">=",
                    "value": 100,
                },
            },
            format="json",
            HTTP_IF_MATCH='"2"',
        )
        assert child_update.status_code == 200, child_update.data
        replay = self.client.post(
            evaluation_url,
            {"contract_id": self.contract.id},
            format="json",
            HTTP_IDEMPOTENCY_KEY="reference-evaluation-v1",
        )
        assert replay.status_code == 200
        assert replay.data["compliant"] is True
        assert replay.data["results"][0]["conditions"][0]["via"] == [
            f"{child['id']}@2"
        ]

        self.client.post(f"/api/v1/rules/{child['id']}/archive")
        still_evaluates = self.client.post(
            f"/api/v1/compliance-checks/{check.data['id']}/evaluate",
            {"contract_id": self.contract.id},
            format="json",
            HTTP_IDEMPOTENCY_KEY="reference-after-archive",
        )
        assert still_evaluates.status_code == 200
        assert still_evaluates.data["outcome"] == "COMPLIANT"

        archived_reference = self.client.post(
            "/api/v1/rules",
            {
                "name": "Invalid archived reference",
                "definition": {
                    "op": "AND",
                    "children": [{"rule_ref": {"rule_id": child["id"]}}],
                },
            },
            format="json",
        )
        assert archived_reference.status_code == 422
        assert archived_reference.data["code"] == "INVALID_RULE_REFERENCE"

    @override_settings(RULE_REFERENCES_ENABLED=True)
    def test_invalid_and_cross_tenant_references_are_rejected(self):
        unknown = self.client.post(
            "/api/v1/rules",
            {
                "name": "Unknown reference",
                "definition": {
                    "op": "AND",
                    "children": [{"rule_ref": {"rule_id": 999999}}],
                },
            },
            format="json",
        )
        assert unknown.status_code == 422
        assert unknown.data["code"] == "INVALID_RULE_REFERENCE"
        assert "$.children[0]" in unknown.data["detail"]

        child_tenant = Tenant.objects.create(name=f"Other tenant {secrets.token_hex(4)}")
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true)",
                    [str(child_tenant.id)],
                )
            other_rule = Rule.objects.create(
                tenant=child_tenant,
                name="Other tenant rule",
                status=Rule.Status.ACTIVE,
                current_version=1,
            )
            RuleVersion.objects.create(
                tenant=child_tenant,
                rule=other_rule,
                version=1,
                definition={
                    "property": "worker.age",
                    "operator": ">=",
                    "value": 18,
                },
                schema_version=2,
                created_by="cross-tenant-test",
            )

        cross_tenant = self.client.post(
            "/api/v1/rules",
            {
                "name": "Cross-tenant reference",
                "definition": {
                    "op": "AND",
                    "children": [{"rule_ref": {"rule_id": other_rule.id}}],
                },
            },
            format="json",
        )
        assert cross_tenant.status_code == 422
        assert cross_tenant.data["code"] == "INVALID_RULE_REFERENCE"

        child = self._create_rule("Self reference target").data
        self_reference = self.client.put(
            f"/api/v1/rules/{child['id']}",
            {
                "name": "Self reference target",
                "definition": {
                    "op": "AND",
                    "children": [{"rule_ref": {"rule_id": child["id"]}}],
                },
            },
            format="json",
            HTTP_IF_MATCH='"1"',
        )
        assert self_reference.status_code == 422
        assert self_reference.data["code"] == "INVALID_RULE_REFERENCE"

    @override_settings(RULE_REFERENCES_ENABLED=False)
    def test_reference_creation_is_disabled_by_default(self):
        child = self._create_rule("Reference gate target").data
        disabled = self.client.post(
            "/api/v1/rules",
            {
                "name": "Disabled reference",
                "definition": {
                    "op": "AND",
                    "children": [{"rule_ref": {"rule_id": child["id"]}}],
                },
            },
            format="json",
        )
        assert disabled.status_code == 422
        assert disabled.data["code"] == "RULE_REFERENCES_DISABLED"

    @override_settings(RULE_REFERENCES_ENABLED=True)
    def test_expanded_reference_depth_limit_is_enforced(self):
        deep_definition = {
            "property": "worker.age",
            "operator": ">=",
            "value": 18,
        }
        for _ in range(19):
            deep_definition = {"op": "AND", "children": [deep_definition]}
        child = self.client.post(
            "/api/v1/rules",
            {"name": "Deep referenced rule", "definition": deep_definition},
            format="json",
        )
        assert child.status_code == 201, child.data

        parent = self.client.post(
            "/api/v1/rules",
            {
                "name": "Parent exceeds expanded depth",
                "definition": {
                    "op": "AND",
                    "children": [{"rule_ref": {"rule_id": child.data["id"]}}],
                },
            },
            format="json",
        )
        assert parent.status_code == 422
        assert parent.data["code"] == "INVALID_RULE_REFERENCE"

    def test_schema_v1_rule_versions_remain_evaluable(self):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true)",
                    [str(self.tenant.id)],
                )
            rule = Rule.objects.create(
                tenant=self.tenant,
                name="Legacy schema version one",
                status=Rule.Status.ACTIVE,
                current_version=1,
            )
            RuleVersion.objects.create(
                tenant=self.tenant,
                rule=rule,
                version=1,
                definition={
                    "property": "worker.age",
                    "operator": ">=",
                    "value": 18,
                },
                schema_version=1,
                created_by="legacy-test",
            )
        check = self.client.post(
            "/api/v1/compliance-checks",
            {"name": "Legacy check", "rule_ids": [rule.id]},
            format="json",
        )
        assert check.status_code == 201, check.data
        evaluation = self.client.post(
            f"/api/v1/compliance-checks/{check.data['id']}/evaluate",
            {"contract_id": self.contract.id},
            format="json",
            HTTP_IDEMPOTENCY_KEY="legacy-schema-version-one",
        )
        assert evaluation.status_code == 200, evaluation.data
        assert evaluation.data["outcome"] == "COMPLIANT"
