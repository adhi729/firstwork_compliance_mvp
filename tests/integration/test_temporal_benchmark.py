import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction

from apps.directory.models import Company, Contract, Worker
from apps.evaluation.management.commands.run_temporal_benchmark import _create_additional_contracts
from apps.evaluation.models import CheckVersionRule, EvaluationJob, EvaluationJobResult
from apps.rules.models import RuleVersion
from apps.tenants.models import Tenant
from workflows import activities
from workflows.bulk_evaluation import BulkEvaluationInput


def _run_activities(_start):
    def dispatch(job_id, tenant_id):
        args = BulkEvaluationInput(job_id=job_id, tenant_id=tenant_id)
        activities.mark_bulk_job_running(args)
        while not activities.process_bulk_evaluation_batch(args).completed:
            pass

    return dispatch


@pytest.mark.django_db(transaction=True)
def test_temporal_benchmark_builds_fixture_and_checks_results(monkeypatch):
    monkeypatch.setattr(
        "apps.evaluation.management.commands.run_temporal_benchmark.async_to_sync",
        _run_activities,
    )
    output = StringIO()
    call_command(
        "run_temporal_benchmark",
        rules=40,
        contracts=60,
        checks=2,
        rules_per_check=20,
        iterations=1,
        stdout=output,
        stderr=StringIO(),
    )
    report = json.loads(output.getvalue())
    fixture = report["fixture"]
    assert (fixture["rules"], fixture["workers"], fixture["companies"], fixture["contracts"]) == (
        40,
        60,
        20,
        60,
    )
    assert fixture["checks"] == 2
    assert report["latency"]["rule_versions_evaluated"] == 20
    assert all(job["result_count"] == 60 for job in report["bulk_evaluation"]["jobs"])
    scenario = report["additional_contract_scenario"]
    assert scenario["added_contracts"] == scenario["added_contract_results"] == 50
    assert scenario["job"]["result_count"] == scenario["job"]["completed_contracts"] == 110
    added_ids = scenario["added_contract_ids"]
    assert len(set(added_ids)) == 50
    tenant = Tenant.objects.get(pk=fixture["tenant_id"])
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.pk)])
        assert EvaluationJob.objects.filter(tenant=tenant, status="COMPLETED").count() == 3
        assert EvaluationJobResult.objects.filter(tenant=tenant).count() == 230
        assert not EvaluationJobResult.objects.filter(
            job_id__in=[job["job_id"] for job in report["bulk_evaluation"]["jobs"]],
            contract_id__in=added_ids,
        ).exists()
        assert (
            EvaluationJobResult.objects.filter(
                job_id=scenario["job"]["job_id"], contract_id__in=added_ids
            ).count()
            == 50
        )
        added = list(Contract.objects.filter(id__in=added_ids))
        assert len({contract.worker_id for contract in added}) == 50
        for contract in added:
            source = (
                Contract.objects.filter(worker_id=contract.worker_id, id__lt=contract.id)
                .order_by("id")
                .first()
            )
            assert source is not None
            assert (
                contract.company_id,
                contract.country,
                contract.start_date,
                contract.end_date,
            ) == (source.company_id, source.country, source.start_date, source.end_date)
        assert set(
            EvaluationJobResult.objects.filter(tenant=tenant).values_list("outcome", flat=True)
        ) == {"COMPLIANT", "NON_COMPLIANT"}
        assert CheckVersionRule.objects.filter(tenant=tenant).count() == 40
        assert {
            definition["property"]
            for definition in RuleVersion.objects.filter(tenant=tenant).values_list(
                "definition", flat=True
            )
        } == {"worker.age", "worker.nationality", "company.bank_deposit", "contract.country"}


@pytest.mark.django_db(transaction=True)
def test_temporal_benchmark_times_out_without_a_worker(monkeypatch):
    monkeypatch.setattr(
        "apps.evaluation.management.commands.run_temporal_benchmark.async_to_sync",
        lambda _start: lambda job_id, tenant_id: None,
    )
    with pytest.raises(CommandError, match="Timed out waiting for Temporal jobs"):
        call_command(
            "run_temporal_benchmark",
            rules=20,
            contracts=1,
            checks=1,
            iterations=1,
            timeout=0.01,
            poll_interval=0.005,
            skip_additional_contract_scenario=True,
            stdout=StringIO(),
            stderr=StringIO(),
        )


@pytest.mark.django_db(transaction=True)
def test_temporal_benchmark_marks_undispatched_jobs_failed(monkeypatch):
    def fail(_start):
        def dispatch(job_id, tenant_id):
            raise RuntimeError("Temporal unavailable")

        return dispatch

    monkeypatch.setattr(
        "apps.evaluation.management.commands.run_temporal_benchmark.async_to_sync", fail
    )
    with pytest.raises(CommandError, match="Could not start Temporal job"):
        call_command(
            "run_temporal_benchmark",
            rules=20,
            contracts=1,
            checks=2,
            iterations=1,
            skip_additional_contract_scenario=True,
            stdout=StringIO(),
            stderr=StringIO(),
        )
    tenant = Tenant.objects.get(name__startswith="temporal-benchmark-")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.pk)])
        assert EvaluationJob.objects.filter(status=EvaluationJob.Status.FAILED).count() == 2


@pytest.mark.django_db(transaction=True)
def test_temporal_benchmark_can_skip_scenario_for_small_fixtures(monkeypatch):
    monkeypatch.setattr(
        "apps.evaluation.management.commands.run_temporal_benchmark.async_to_sync",
        _run_activities,
    )
    output = StringIO()
    call_command(
        "run_temporal_benchmark",
        rules=20,
        contracts=1,
        checks=1,
        iterations=1,
        skip_additional_contract_scenario=True,
        stdout=output,
        stderr=StringIO(),
    )
    report = json.loads(output.getvalue())
    assert report["additional_contract_scenario"] is None
    assert report["bulk_evaluation"]["jobs"][0]["result_count"] == 1


def test_temporal_benchmark_rejects_invalid_counts():
    with pytest.raises(CommandError, match="--rules must be at least 20"):
        call_command("run_temporal_benchmark", rules=19)
    with pytest.raises(CommandError, match="--contracts must be at least 50"):
        call_command("run_temporal_benchmark", rules=20, contracts=49)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("dob", "start_offset", "end_offset", "eligible"),
    [
        (date(2009, 2, 28), 0, 1, False),  # Exactly 18.
        (date(2008, 2, 29), 0, 1, True),  # Leap birthday observed on February 28.
        (date(2008, 3, 1), 0, 1, False),  # Not yet 19.
        (date(2000, 1, 1), 1, 2, False),  # Future start.
        (date(2000, 1, 1), -1, 0, False),  # End is exclusive.
        (date(2000, 1, 1), 0, 1, True),  # Start is inclusive.
    ],
)
def test_additional_contract_selection_boundaries(dob, start_offset, end_offset, eligible):
    as_of = datetime(2027, 2, 28, 12, tzinfo=UTC)
    tenant = Tenant.objects.create(name="scenario-tenant")
    other = Tenant.objects.create(name="scenario-other")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.pk)])
        company = Company.objects.create(
            tenant=tenant,
            name="Company",
            industry="tech",
            country="DE",
            bank_deposit=Decimal("100"),
        )
        workers = Worker.objects.bulk_create(
            [
                Worker(
                    tenant=tenant,
                    name=f"Adult {i}",
                    date_of_birth=date(1990, 1, 1),
                    nationality="DE",
                )
                for i in range(49)
            ]
        )
        for worker in workers:
            Contract.objects.create(
                tenant=tenant,
                company=company,
                worker=worker,
                start_date=as_of - timedelta(days=1),
                end_date=as_of + timedelta(days=1),
                country="DE",
            )
        # A second active contract must not count as another eligible worker.
        Contract.objects.create(
            tenant=tenant,
            company=company,
            worker=workers[0],
            start_date=as_of - timedelta(days=2),
            end_date=as_of + timedelta(days=2),
            country="FR",
        )
        candidate = Worker.objects.create(
            tenant=tenant, name="Boundary", date_of_birth=dob, nationality="DE"
        )
        source = Contract.objects.create(
            tenant=tenant,
            company=company,
            worker=candidate,
            start_date=as_of + timedelta(days=start_offset),
            end_date=as_of + timedelta(days=end_offset),
            country="FR",
        )
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(other.pk)])
        other_company = Company.objects.create(
            tenant=other,
            name="Other",
            industry="tech",
            country="DE",
            bank_deposit=Decimal("100"),
        )
        other_worker = Worker.objects.create(
            tenant=other, name="Other", date_of_birth=date(1990, 1, 1), nationality="DE"
        )
        Contract.objects.create(
            tenant=other,
            company=other_company,
            worker=other_worker,
            start_date=as_of - timedelta(days=1),
            end_date=as_of + timedelta(days=1),
            country="DE",
        )
    if not eligible:
        with pytest.raises(CommandError, match="found 49"):
            _create_additional_contracts(tenant, as_of)
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.pk)])
            assert Contract.objects.filter(tenant=tenant).count() == 51
    else:
        ids = _create_additional_contracts(tenant, as_of)
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.pk)])
            added = list(Contract.objects.filter(id__in=ids))
            assert len(added) == len({item.worker_id for item in added}) == 50
            copied = next(item for item in added if item.worker_id == candidate.id)
            assert (copied.company_id, copied.country, copied.start_date, copied.end_date) == (
                source.company_id,
                source.country,
                source.start_date,
                source.end_date,
            )
            duplicate_worker_copy = next(item for item in added if item.worker_id == workers[0].id)
            assert duplicate_worker_copy.country == "DE"
