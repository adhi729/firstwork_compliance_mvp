import json
import math
import time
import uuid
from datetime import datetime
from io import StringIO

from asgiref.sync import async_to_sync
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Count, Max
from django.utils import timezone

from apps.directory.models import Company, Contract, Worker
from apps.evaluation.bulk_api import _mark_dispatch_failure
from apps.evaluation.models import (
    CheckVersion,
    CheckVersionRule,
    ComplianceCheck,
    EvaluationJob,
    EvaluationJobResult,
)
from apps.rules.models import Rule, RuleVersion
from apps.tenants.models import Tenant
from domain.engine import TruthValue, evaluate_definition
from workflows.client import start_bulk_evaluation

ADDITIONAL_CONTRACTS = 50
ADULT_RULE = {"property": "worker.age", "operator": ">", "value": 18}


def _create_additional_contracts(tenant: Tenant, as_of: datetime) -> list[int]:
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.pk)])
        sources = (
            Contract.objects.filter(tenant=tenant, start_date__lte=as_of, end_date__gt=as_of)
            .select_related("worker")
            .order_by("worker_id", "id")
        )
        selected = []
        seen_workers = set()
        for source in sources.iterator():
            if source.worker_id in seen_workers:
                continue
            seen_workers.add(source.worker_id)
            result = evaluate_definition(
                ADULT_RULE, {"worker": {"date_of_birth": source.worker.date_of_birth}}, as_of
            )
            if result.status == TruthValue.TRUE:
                selected.append(source)
                if len(selected) == ADDITIONAL_CONTRACTS:
                    break
        if len(selected) != ADDITIONAL_CONTRACTS:
            raise CommandError(
                f"Expected {ADDITIONAL_CONTRACTS} distinct eligible workers; "
                f"found {len(selected)}. No additional contracts were created."
            )
        created = Contract.objects.bulk_create(
            [
                Contract(
                    tenant=tenant,
                    worker_id=source.worker_id,
                    company_id=source.company_id,
                    country=source.country,
                    start_date=source.start_date,
                    end_date=source.end_date,
                )
                for source in selected
            ]
        )
        return [contract.id for contract in created]


class Command(BaseCommand):
    help = "Generate a 100k-rule tenant and benchmark Temporal bulk evaluation."

    def add_arguments(self, parser):
        parser.add_argument("--rules", type=int, default=100000)
        parser.add_argument("--contracts", type=int, default=2000)
        parser.add_argument("--checks", type=int, default=2)
        parser.add_argument("--rules-per-check", type=int, default=20)
        parser.add_argument("--iterations", type=int, default=50)
        parser.add_argument("--timeout", type=float, default=1800)
        parser.add_argument("--poll-interval", type=float, default=1)
        parser.add_argument("--skip-additional-contract-scenario", action="store_true")

    def handle(self, *args, **options):
        rules = options["rules"]
        contracts = options["contracts"]
        checks = options["checks"]
        rules_per_check = options["rules_per_check"]
        if (
            min(rules, contracts, checks, rules_per_check, options["iterations"]) < 1
            or not math.isfinite(options["timeout"])
            or not math.isfinite(options["poll_interval"])
            or options["timeout"] <= 0
            or options["poll_interval"] <= 0
        ):
            raise CommandError("All counts, timeout and poll interval must be positive.")
        if rules < max(20, rules_per_check):
            raise CommandError("--rules must be at least 20 and at least --rules-per-check.")
        if contracts < ADDITIONAL_CONTRACTS and not options["skip_additional_contract_scenario"]:
            raise CommandError(
                f"--contracts must be at least {ADDITIONAL_CONTRACTS} for the additional "
                "contract scenario; use --skip-additional-contract-scenario for smaller fixtures."
            )

        prefix = f"temporal-benchmark-{uuid.uuid4().hex}"
        tenant_name = f"{prefix}-0000"
        self.stderr.write(f"Generating benchmark tenant {tenant_name}")
        call_command(
            "generate_benchmark_data",
            tenants=1,
            rules_per_tenant=rules,
            contracts_per_tenant=contracts,
            prefix=prefix,
            diverse_rules=True,
            adult_workers=True,
            stdout=self.stderr,
        )
        tenant = Tenant.objects.get(name=tenant_name)
        tenant_id = str(tenant.id)
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])
            sample_size = min(rules, rules_per_check * checks)
            versions = list(RuleVersion.objects.filter(tenant=tenant).order_by("id")[:sample_size])
            snapshot = Contract.objects.filter(tenant=tenant).aggregate(
                total=Count("id"), max_id=Max("id")
            )
            jobs = []
            for index in range(checks):
                pins = [
                    versions[(index * rules_per_check + pos) % len(versions)]
                    for pos in range(rules_per_check)
                ]
                check = ComplianceCheck.objects.create(
                    tenant=tenant, name=f"benchmark-check-{index + 1}", current_version=1
                )
                version = CheckVersion.objects.create(
                    tenant=tenant,
                    compliance_check=check,
                    version=1,
                    rule_versions=[item.id for item in pins],
                )
                CheckVersionRule.objects.bulk_create(
                    [
                        CheckVersionRule(
                            tenant=tenant, check_version=version, rule_version=item, position=pos
                        )
                        for pos, item in enumerate(pins)
                    ]
                )
                jobs.append(
                    EvaluationJob.objects.create(
                        tenant=tenant,
                        check_version=version,
                        total_contracts=snapshot["total"],
                        max_contract_id=snapshot["max_id"],
                    )
                )
            fixture = {
                "tenant": tenant_name,
                "tenant_id": tenant_id,
                "rules": Rule.objects.filter(tenant=tenant).count(),
                "workers": Worker.objects.filter(tenant=tenant).count(),
                "companies": Company.objects.filter(tenant=tenant).count(),
                "contracts": snapshot["total"],
                "checks": checks,
                "rules_per_check": rules_per_check,
            }

        results, elapsed = self._run_jobs(
            jobs, tenant_id, tenant_name, contracts, options["timeout"], options["poll_interval"]
        )
        scenario = None
        if not options["skip_additional_contract_scenario"]:
            self.stderr.write("Creating 50 additional contracts for eligible workers")
            additional_ids = _create_additional_contracts(tenant, timezone.now())
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])
                if EvaluationJobResult.objects.filter(
                    job_id__in=[job.id for job in jobs], contract_id__in=additional_ids
                ).exists():
                    raise CommandError("An initial job unexpectedly contains a new contract.")
                snapshot = Contract.objects.filter(tenant=tenant).aggregate(
                    total=Count("id"), max_id=Max("id")
                )
                scenario_job = EvaluationJob.objects.create(
                    tenant=tenant,
                    check_version=jobs[0].check_version,
                    total_contracts=snapshot["total"],
                    max_contract_id=snapshot["max_id"],
                )
            scenario_results, scenario_elapsed = self._run_jobs(
                [scenario_job],
                tenant_id,
                tenant_name,
                contracts + ADDITIONAL_CONTRACTS,
                options["timeout"],
                options["poll_interval"],
            )
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])
                added_results = EvaluationJobResult.objects.filter(
                    tenant=tenant, job=scenario_job, contract_id__in=additional_ids
                ).count()
                if added_results != ADDITIONAL_CONTRACTS:
                    raise CommandError(
                        f"Additional-contract job has {added_results} results for "
                        f"{ADDITIONAL_CONTRACTS} new contracts."
                    )
            scenario = {
                "added_contract_ids": additional_ids,
                "added_contracts": len(additional_ids),
                "added_contract_results": added_results,
                "job": scenario_results[0],
                "elapsed_seconds": round(scenario_elapsed, 3),
            }

        self.stderr.write("Running the existing 20-rule latency and eligibility benchmark")
        latency_output = StringIO()
        call_command(
            "run_compliance_benchmark",
            tenant=tenant_name,
            iterations=options["iterations"],
            stdout=latency_output,
        )
        self.stdout.write(
            json.dumps(
                {
                    "fixture": fixture,
                    "bulk_evaluation": {
                        "jobs": results,
                        "elapsed_seconds": round(elapsed, 3),
                        "contract_evaluations_per_second": round(contracts * checks / elapsed, 2),
                    },
                    "additional_contract_scenario": scenario,
                    "latency": json.loads(latency_output.getvalue()),
                },
                indent=2,
            )
        )

    def _run_jobs(self, jobs, tenant_id, tenant_name, contracts, timeout, poll_interval):
        started = time.monotonic()
        for index, job in enumerate(jobs):
            self.stderr.write(f"Starting Temporal bulk evaluation job {job.id}")
            try:
                async_to_sync(start_bulk_evaluation)(job.id, tenant_id)
            except Exception as exc:
                for undispatched in jobs[index:]:
                    _mark_dispatch_failure(undispatched.id, tenant_id)
                raise CommandError(
                    f"Could not start Temporal job {job.id}: {exc}. Benchmark tenant: {tenant_name}"
                ) from exc

        deadline = started + timeout
        while True:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])
                current_jobs = list(EvaluationJob.objects.filter(id__in=[job.id for job in jobs]))
                if len(current_jobs) != len(jobs):
                    raise CommandError("A benchmark job is missing.")
                for job in current_jobs:
                    if job.status == EvaluationJob.Status.FAILED:
                        raise CommandError(f"Temporal job {job.id} failed: {job.error}")
                if all(job.status == EvaluationJob.Status.COMPLETED for job in current_jobs):
                    results = []
                    for job in current_jobs:
                        result_count = EvaluationJobResult.objects.filter(job=job).count()
                        if job.completed_contracts != contracts or result_count != contracts:
                            raise CommandError(
                                f"Job {job.id} completed with {job.completed_contracts} "
                                f"contracts and {result_count} results; expected {contracts}."
                            )
                        results.append(
                            {
                                "job_id": job.id,
                                "completed_contracts": job.completed_contracts,
                                "result_count": result_count,
                                "worker_duration_seconds": (
                                    (job.completed_at - job.started_at).total_seconds()
                                    if job.completed_at and job.started_at
                                    else None
                                ),
                            }
                        )
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CommandError(
                    f"Timed out waiting for Temporal jobs; tenant {tenant_name}, "
                    f"job IDs {[job.id for job in jobs]}."
                )
            time.sleep(min(poll_interval, remaining))

        elapsed = time.monotonic() - started
        return results, elapsed
