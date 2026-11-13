import hashlib
import json
import time
import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.directory.models import Contract
from apps.evaluation.models import ComplianceCheck, EvaluationResult
from apps.evaluation.services import evaluate_check_version
from apps.tenants.errors import IdempotencyConflictAPIError


class EvaluateInputSerializer(serializers.Serializer):
    contract_id = serializers.IntegerField(min_value=1)


class EvaluationConditionSerializer(serializers.Serializer):
    path = serializers.CharField()
    property = serializers.CharField()
    operator = serializers.CharField()
    expected = serializers.JSONField(allow_null=True)
    actual = serializers.JSONField(allow_null=True)
    status = serializers.CharField()
    reason = serializers.CharField()
    via = serializers.ListField(child=serializers.CharField(), required=False)


class RuleEvaluationSerializer(serializers.Serializer):
    rule_id = serializers.IntegerField()
    version = serializers.IntegerField()
    status = serializers.CharField()
    conditions = EvaluationConditionSerializer(many=True)


class EvaluationSummarySerializer(serializers.Serializer):
    rules = serializers.IntegerField()
    passed = serializers.IntegerField()
    failed = serializers.IntegerField()
    unknown = serializers.IntegerField()


class EvaluationOutputSerializer(serializers.Serializer):
    evaluation_id = serializers.IntegerField()
    outcome = serializers.CharField()
    compliant = serializers.SerializerMethodField(
        help_text=(
            "True only when outcome is COMPLIANT. False also includes INDETERMINATE."
        )
    )
    as_of = serializers.DateTimeField()
    summary = EvaluationSummarySerializer()
    results = RuleEvaluationSerializer(many=True)

    def get_compliant(self, instance) -> bool:
        outcome = instance["outcome"] if isinstance(instance, dict) else instance.outcome
        return outcome == EvaluationResult.Outcome.COMPLIANT


def _tenant_id(request: Request) -> str:
    tenant_id = getattr(request.user, "tenant_id", None)
    if not tenant_id:
        raise NotFound("Tenant not found.", code="TENANT_ACCESS_DENIED")
    return str(tenant_id)


class EvaluateCheckView(APIView):
    @extend_schema(
        request=EvaluateInputSerializer,
        responses={200: EvaluationOutputSerializer},
    )
    def post(self, request: Request, check_id: int) -> Response:
        serializer = EvaluateInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant_id = _tenant_id(request)
        contract_id = serializer.validated_data["contract_id"]
        idempotency_key = request.headers.get("Idempotency-Key") or str(uuid.uuid4())
        if len(idempotency_key) > 255 or not idempotency_key.strip():
            raise serializers.ValidationError(
                {"Idempotency-Key": "The idempotency key must be 1-255 non-whitespace characters."}
            )
        request_hash = hashlib.sha256(
            json.dumps(
                {"check_id": check_id, "contract_id": contract_id},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

        existing = EvaluationResult.objects.filter(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
        ).first()
        if existing is not None:
            return _idempotent_response(existing, request_hash)

        try:
            started = time.perf_counter()
            with transaction.atomic():
                check = (
                    ComplianceCheck.objects.filter(tenant_id=tenant_id, id=check_id)
                    .only("id", "tenant_id", "current_version")
                    .first()
                )
                if check is None or check.current_version is None:
                    raise NotFound("Check not found.", code="CHECK_NOT_FOUND")
                check_version = (
                    check.versions.filter(version=check.current_version)
                    .only("id", "tenant_id", "version")
                    .first()
                )
                if check_version is None:
                    raise NotFound("Check not found.", code="CHECK_NOT_FOUND")
                try:
                    contract = Contract.objects.select_related("worker", "company").get(
                        tenant_id=tenant_id, id=contract_id
                    )
                except Contract.DoesNotExist as exc:
                    raise NotFound("Contract not found.", code="CONTRACT_NOT_FOUND") from exc

                as_of = timezone.now()
                result_data = evaluate_check_version(check_version, contract, as_of)
                duration_ms = max(0, round((time.perf_counter() - started) * 1000))
                body = {
                    "evaluation_id": None,
                    "outcome": result_data["outcome"],
                    "as_of": as_of,
                    "summary": result_data["summary"],
                    "results": result_data["results"],
                }
                result = EvaluationResult.objects.create(
                    tenant_id=tenant_id,
                    check_version=check_version,
                    contract=contract,
                    outcome=result_data["outcome"],
                    details={
                        "summary": result_data["summary"],
                        "results": result_data["results"],
                    },
                    as_of=as_of,
                    duration_ms=duration_ms,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
                body["evaluation_id"] = result.id
        except IntegrityError as exc:
            if _constraint_name(exc) != "result_tenant_idempotency_uniq":
                raise
            result = EvaluationResult.objects.get(
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
            )
            return _idempotent_response(result, request_hash)

        body["outcome"] = result.outcome
        body["as_of"] = result.as_of
        response = Response(EvaluationOutputSerializer(body).data, status=status.HTTP_200_OK)
        return response


def _idempotent_response(result: EvaluationResult, request_hash: str) -> Response:
    if result.request_hash != request_hash:
        raise IdempotencyConflictAPIError()
    body = {
        "evaluation_id": result.id,
        "outcome": result.outcome,
        "as_of": result.as_of,
        **result.details,
    }
    return Response(
        EvaluationOutputSerializer(body).data,
        status=status.HTTP_200_OK,
    )


def _constraint_name(exc: IntegrityError) -> str | None:
    cause = exc.__cause__
    diagnostic = getattr(cause, "diag", None)
    return getattr(diagnostic, "constraint_name", None)
