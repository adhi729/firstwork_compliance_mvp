import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection, transaction
from django.utils import timezone
from temporalio import activity
from temporalio.exceptions import ApplicationError

from apps.directory.models import Contract
from apps.evaluation.models import (
    CheckVersionRule,
    EvaluationJob,
    EvaluationJobResult,
)
from apps.evaluation.services import evaluate_check_version
from workflows.bulk_evaluation import (
    BULK_EVALUATION_BATCH_SIZE,
    BulkBatchOutcome,
    BulkEvaluationInput,
)

logger = logging.getLogger(__name__)


@contextmanager
def _tenant_transaction(tenant_id: str) -> Iterator[None]:
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])
        yield


def _load_job(args: BulkEvaluationInput) -> EvaluationJob:
    job = (
        EvaluationJob.objects.select_for_update()
        .select_related("check_version")
        .filter(id=args.job_id, tenant_id=args.tenant_id)
        .first()
    )
    if job is None:
        raise ApplicationError("Evaluation job not found.", non_retryable=True)
    return job


@activity.defn(name="mark_bulk_job_running")
def mark_bulk_job_running(args: BulkEvaluationInput) -> None:
    with _tenant_transaction(args.tenant_id):
        job = _load_job(args)
        if job.status in {EvaluationJob.Status.COMPLETED, EvaluationJob.Status.FAILED}:
            return
        job.status = EvaluationJob.Status.RUNNING
        job.started_at = job.started_at or timezone.now()
        job.save(update_fields=["status", "started_at", "updated_at"])


@activity.defn(name="process_bulk_evaluation_batch")
def process_bulk_evaluation_batch(args: BulkEvaluationInput) -> BulkBatchOutcome:
    with _tenant_transaction(args.tenant_id):
        job = _load_job(args)
        if job.status in {EvaluationJob.Status.COMPLETED, EvaluationJob.Status.FAILED}:
            return BulkBatchOutcome(completed=True, processed_contracts=0)

        contracts = list(
            Contract.objects.filter(
                tenant_id=args.tenant_id,
                id__gt=job.last_contract_id,
                id__lte=job.max_contract_id,
            )
            .select_related("worker", "company")
            .order_by("id")[:BULK_EVALUATION_BATCH_SIZE]
        )
        if not contracts:
            job.status = EvaluationJob.Status.COMPLETED
            job.completed_at = timezone.now()
            job.error = ""
            job.save(update_fields=["status", "completed_at", "error", "updated_at"])
            return BulkBatchOutcome(completed=True, processed_contracts=0)

        pins = list(
            CheckVersionRule.objects.filter(
                tenant_id=args.tenant_id,
                check_version_id=job.check_version_id,
            )
            .select_related("rule_version", "rule_version__rule")
            .order_by("position")
        )
        as_of = job.created_at
        results: list[EvaluationJobResult] = []
        for contract in contracts:
            started = time.perf_counter()
            evaluated = evaluate_check_version(
                job.check_version,
                contract,
                as_of,
                pins=pins,
            )
            results.append(
                EvaluationJobResult(
                    tenant_id=args.tenant_id,
                    job_id=job.id,
                    contract_id=contract.id,
                    outcome=evaluated["outcome"],
                    details={
                        "summary": evaluated["summary"],
                        "results": evaluated["results"],
                    },
                    as_of=as_of,
                    duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
                )
            )

        EvaluationJobResult.objects.bulk_create(
            results,
            update_conflicts=True,
            unique_fields=["job", "contract"],
            update_fields=["outcome", "details", "as_of", "duration_ms"],
        )
        job.last_contract_id = contracts[-1].id
        job.completed_contracts += len(contracts)
        job.status = EvaluationJob.Status.RUNNING
        job.save(
            update_fields=[
                "last_contract_id",
                "completed_contracts",
                "status",
                "updated_at",
            ]
        )
        return BulkBatchOutcome(completed=False, processed_contracts=len(contracts))


@activity.defn(name="mark_bulk_job_failed")
def mark_bulk_job_failed(args: BulkEvaluationInput, failure_type: str) -> None:
    with _tenant_transaction(args.tenant_id):
        job = _load_job(args)
        if job.status == EvaluationJob.Status.COMPLETED:
            return
        job.status = EvaluationJob.Status.FAILED
        job.completed_at = timezone.now()
        job.error = "Bulk evaluation failed."
        job.save(update_fields=["status", "completed_at", "error", "updated_at"])
    logger.error(
        "Bulk evaluation workflow failed",
        extra={
            "tenant_id": args.tenant_id,
            "job_id": args.job_id,
            "failure_type": failure_type,
        },
    )
