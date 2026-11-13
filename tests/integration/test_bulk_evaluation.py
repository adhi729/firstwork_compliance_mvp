import hashlib
import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from django.db import connection, transaction
from rest_framework.test import APIClient

from apps.directory.models import Company, Contract, Worker
from apps.evaluation.models import EvaluationJob, EvaluationJobResult
from apps.tenants.models import APIKey, Tenant
from workflows import activities
from workflows.bulk_evaluation import BulkEvaluationInput


def _set_tenant(tenant_id) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])


@pytest.mark.django_db(transaction=True)
class TestBulkEvaluation:
    def setup_method(self) -> None:
        self.tenant = Tenant.objects.create(name=f"Bulk tenant {secrets.token_hex(4)}")
        secret = secrets.token_urlsafe(32)
        token = f"{self.tenant.id}.{secret}"
        with transaction.atomic():
            _set_tenant(self.tenant.id)
            APIKey.objects.create(
                tenant=self.tenant,
                key_hash=hashlib.sha256(token.encode()).hexdigest(),
                name="bulk-integration",
            )
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        with transaction.atomic():
            _set_tenant(self.tenant.id)
            self.company = Company.objects.create(
                tenant=self.tenant,
                name="Bulk Example GmbH",
                industry="technology",
                country="DE",
                bank_deposit=Decimal("1000.00"),
            )
            self.worker = Worker.objects.create(
                tenant=self.tenant,
                name="Bulk Worker",
                date_of_birth=date(1990, 1, 1),
                nationality="DE",
            )
            self.contract = self._create_contract()

        rule_response = self.client.post(
            "/api/v1/rules",
            {
                "name": "Bulk adult worker",
                "definition": {
                    "property": "worker.age",
                    "operator": ">=",
                    "value": 18,
                },
            },
            format="json",
        )
        assert rule_response.status_code == 201, rule_response.data
        self.rule_id = rule_response.data["id"]
        check_response = self.client.post(
            "/api/v1/compliance-checks",
            {"name": "Bulk adult check", "rule_ids": [self.rule_id]},
            format="json",
        )
        assert check_response.status_code == 201, check_response.data
        self.check_id = check_response.data["id"]

    def _create_contract(self) -> Contract:
        return Contract.objects.create(
            tenant=self.tenant,
            company=self.company,
            worker=self.worker,
            start_date=datetime.now(UTC) - timedelta(days=10),
            end_date=datetime.now(UTC) + timedelta(days=365),
            country="DE",
        )

    def _create_additional_contracts(self, amount: int) -> list[int]:
        with transaction.atomic():
            _set_tenant(self.tenant.id)
            return [self._create_contract().id for _ in range(amount)]

    def _start_job(self) -> dict:
        with patch(
            "apps.evaluation.bulk_api.start_bulk_evaluation",
            new_callable=AsyncMock,
        ) as start_workflow:
            response = self.client.post(
                f"/api/v1/compliance-checks/{self.check_id}/evaluate-bulk",
                format="json",
            )
            assert response.status_code == 202, response.data
            assert response["Location"] == f"/api/v1/evaluation-jobs/{response.data['job_id']}"
            start_workflow.assert_awaited_once_with(response.data["job_id"], str(self.tenant.id))
        return response.data

    def test_submit_and_read_tenant_scoped_progress(self) -> None:
        job = self._start_job()

        assert job["check_id"] == self.check_id
        assert job["status"] == EvaluationJob.Status.PENDING
        assert job["total_contracts"] == 1
        assert job["completed_contracts"] == 0
        assert job["progress_percent"] == 0

        progress = self.client.get(f"/api/v1/evaluation-jobs/{job['job_id']}")
        assert progress.status_code == 200
        assert progress.data["check_version"] == 1
        assert progress.data["total_contracts"] == 1

        other_tenant = Tenant.objects.create(name=f"Other bulk tenant {secrets.token_hex(4)}")
        other_secret = secrets.token_urlsafe(32)
        other_token = f"{other_tenant.id}.{other_secret}"
        with transaction.atomic():
            _set_tenant(other_tenant.id)
            APIKey.objects.create(
                tenant=other_tenant,
                key_hash=hashlib.sha256(other_token.encode()).hexdigest(),
                name="other-bulk-integration",
            )
        other_client = APIClient()
        other_client.credentials(HTTP_AUTHORIZATION=f"Bearer {other_token}")
        hidden = other_client.get(f"/api/v1/evaluation-jobs/{job['job_id']}")
        assert hidden.status_code == 404

    def test_temporal_dispatch_failure_is_visible_in_job_progress(self) -> None:
        with patch(
            "apps.evaluation.bulk_api.start_bulk_evaluation",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Temporal unavailable"),
        ):
            response = self.client.post(
                f"/api/v1/compliance-checks/{self.check_id}/evaluate-bulk",
                format="json",
            )
        assert response.status_code == 202

        progress = self.client.get(f"/api/v1/evaluation-jobs/{response.data['job_id']}")
        assert progress.data["status"] == EvaluationJob.Status.FAILED
        assert progress.data["error"] == "Unable to start the bulk evaluation workflow."

    def test_batch_processing_is_keyset_paginated_and_replay_safe(self, monkeypatch) -> None:
        contract_ids = [self.contract.id, *self._create_additional_contracts(2)]
        job_data = self._start_job()
        job_id = job_data["job_id"]
        late_contract_id = self._create_additional_contracts(1)[0]
        monkeypatch.setattr(activities, "BULK_EVALUATION_BATCH_SIZE", 1)

        stricter_rule = self.client.post(
            "/api/v1/rules",
            {
                "name": "Bulk stricter worker age",
                "definition": {
                    "property": "worker.age",
                    "operator": ">=",
                    "value": 100,
                },
            },
            format="json",
        )
        assert stricter_rule.status_code == 201, stricter_rule.data
        updated_check = self.client.put(
            f"/api/v1/compliance-checks/{self.check_id}",
            {"rule_ids": [stricter_rule.data["id"]]},
            format="json",
            HTTP_IF_MATCH='"1"',
        )
        assert updated_check.status_code == 200, updated_check.data

        args = BulkEvaluationInput(job_id=job_id, tenant_id=str(self.tenant.id))
        activities.mark_bulk_job_running(args)

        bulk_create = EvaluationJobResult.objects.bulk_create
        failed_once = False

        def fail_after_upsert(*bulk_args, **bulk_kwargs):
            nonlocal failed_once
            bulk_create(*bulk_args, **bulk_kwargs)
            if not failed_once:
                failed_once = True
                raise RuntimeError("Simulated failure before activity commit")

        monkeypatch.setattr(EvaluationJobResult.objects, "bulk_create", fail_after_upsert)
        with pytest.raises(RuntimeError, match="Simulated failure"):
            activities.process_bulk_evaluation_batch(args)

        with transaction.atomic():
            _set_tenant(self.tenant.id)
            rolled_back_job = EvaluationJob.objects.get(id=job_id)
            rolled_back_result_count = EvaluationJobResult.objects.filter(job_id=job_id).count()
        assert rolled_back_job.last_contract_id == 0
        assert rolled_back_job.completed_contracts == 0
        assert rolled_back_result_count == 0
        monkeypatch.setattr(EvaluationJobResult.objects, "bulk_create", bulk_create)

        first = activities.process_bulk_evaluation_batch(args)
        assert first.processed_contracts == 1
        assert not first.completed

        second = activities.process_bulk_evaluation_batch(args)
        assert second.processed_contracts == 1
        third = activities.process_bulk_evaluation_batch(args)
        assert third.processed_contracts == 1
        done = activities.process_bulk_evaluation_batch(args)
        assert done.completed
        assert done.processed_contracts == 0

        progress = self.client.get(f"/api/v1/evaluation-jobs/{job_id}")
        assert progress.data["status"] == EvaluationJob.Status.COMPLETED
        assert progress.data["check_version"] == 1
        assert progress.data["completed_contracts"] == 3
        assert progress.data["progress_percent"] == 100

        with transaction.atomic():
            _set_tenant(self.tenant.id)
            result_rows = list(
                EvaluationJobResult.objects.filter(job_id=job_id)
                .order_by("contract_id")
                .values_list("contract_id", "outcome", "details")
            )
            result_count = EvaluationJobResult.objects.filter(job_id=job_id).count()
        assert [contract_id for contract_id, _, _ in result_rows] == sorted(contract_ids)
        assert late_contract_id not in {contract_id for contract_id, _, _ in result_rows}
        assert all(outcome == "COMPLIANT" for _, outcome, _ in result_rows)
        assert all(
            details["results"][0]["rule_id"] == self.rule_id
            for _, _, details in result_rows
        )
        assert result_count == 3
