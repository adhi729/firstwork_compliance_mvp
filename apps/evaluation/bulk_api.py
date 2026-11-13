import logging

from asgiref.sync import async_to_sync
from django.db import connection, transaction
from django.db.models import Count, Max
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.directory.models import Contract
from apps.evaluation.models import CheckVersion, ComplianceCheck, EvaluationJob
from workflows.client import start_bulk_evaluation

logger = logging.getLogger(__name__)


class EvaluationJobSerializer(serializers.Serializer):
    job_id = serializers.IntegerField(source="id")
    check_id = serializers.IntegerField(source="check_version.compliance_check_id")
    check_version = serializers.IntegerField(source="check_version.version")
    status = serializers.ChoiceField(choices=EvaluationJob.Status.choices)
    total_contracts = serializers.IntegerField()
    completed_contracts = serializers.IntegerField()
    progress_percent = serializers.SerializerMethodField()
    error = serializers.CharField(allow_blank=True)
    created_at = serializers.DateTimeField()
    started_at = serializers.DateTimeField(allow_null=True)
    completed_at = serializers.DateTimeField(allow_null=True)

    def get_progress_percent(self, job: EvaluationJob) -> int:
        if job.total_contracts == 0:
            return 100 if job.status == EvaluationJob.Status.COMPLETED else 0
        return job.completed_contracts * 100 // job.total_contracts


def _tenant_id(request: Request) -> str:
    tenant_id = getattr(request.user, "tenant_id", None)
    if not tenant_id:
        raise NotFound("Tenant not found.", code="TENANT_ACCESS_DENIED")
    return str(tenant_id)


def _mark_dispatch_failure(job_id: int, tenant_id: str) -> None:
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [tenant_id])
        EvaluationJob.objects.filter(
            id=job_id,
            tenant_id=tenant_id,
            status=EvaluationJob.Status.PENDING,
        ).update(
            status=EvaluationJob.Status.FAILED,
            completed_at=timezone.now(),
            error="Unable to start the bulk evaluation workflow.",
            updated_at=timezone.now(),
        )


def _dispatch_bulk_job(job_id: int, tenant_id: str) -> None:
    try:
        async_to_sync(start_bulk_evaluation)(job_id, tenant_id)
    except Exception:
        logger.exception(
            "Unable to start bulk evaluation workflow",
            extra={"tenant_id": tenant_id, "job_id": job_id},
        )
        _mark_dispatch_failure(job_id, tenant_id)


class StartBulkEvaluationView(APIView):
    @extend_schema(
        request=None,
        responses={202: EvaluationJobSerializer},
        description=(
            "Queue evaluation of the tenant's contracts using the check version pinned "
            "when the job is created. Contracts created later are excluded."
        ),
    )
    def post(self, request: Request, check_id: int) -> Response:
        tenant_id = _tenant_id(request)
        check = (
            ComplianceCheck.objects.filter(tenant_id=tenant_id, id=check_id)
            .only("id", "tenant_id", "current_version")
            .first()
        )
        if check is None or check.current_version is None:
            raise NotFound("Check not found.", code="CHECK_NOT_FOUND")

        try:
            check_version = CheckVersion.objects.get(
                tenant_id=tenant_id,
                compliance_check_id=check.id,
                version=check.current_version,
            )
        except CheckVersion.DoesNotExist as exc:
            raise NotFound("Check not found.", code="CHECK_NOT_FOUND") from exc

        contract_scope = Contract.objects.filter(tenant_id=tenant_id)
        snapshot = contract_scope.aggregate(total=Count("id"), max_id=Max("id"))
        job = EvaluationJob.objects.create(
            tenant_id=tenant_id,
            check_version=check_version,
            total_contracts=snapshot["total"] or 0,
            max_contract_id=snapshot["max_id"] or 0,
        )
        transaction.on_commit(lambda: _dispatch_bulk_job(job.id, tenant_id))

        response = Response(
            EvaluationJobSerializer(job).data,
            status=status.HTTP_202_ACCEPTED,
        )
        response["Location"] = f"/api/v1/evaluation-jobs/{job.id}"
        return response


class EvaluationJobProgressView(APIView):
    @extend_schema(
        responses={200: EvaluationJobSerializer},
        description="Return status and contract progress for a tenant-owned bulk evaluation.",
    )
    def get(self, request: Request, job_id: int) -> Response:
        tenant_id = _tenant_id(request)
        try:
            job = (
                EvaluationJob.objects.select_related("check_version")
                .get(tenant_id=tenant_id, id=job_id)
            )
        except EvaluationJob.DoesNotExist as exc:
            raise NotFound("Evaluation job not found.", code="EVALUATION_JOB_NOT_FOUND") from exc
        return Response(EvaluationJobSerializer(job).data)
