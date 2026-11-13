import logging
from typing import Any

from django.db import connection
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ParseError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.directory.models import Company
from apps.evaluation.models import CheckVersionRule, ComplianceCheck
from apps.rules.models import Rule, RuleVersion
from apps.rules.services import get_expanded_rule_definition
from apps.tenants.cursors import decode_eligibility_cursor, encode_eligibility_cursor
from domain.engine.sql import SQLFragment, compile_all, compile_definition

logger = logging.getLogger(__name__)


class EligibleContractSerializer(serializers.Serializer):
    contract_id = serializers.IntegerField()
    worker_id = serializers.IntegerField()


class EligibleContractsOutputSerializer(serializers.Serializer):
    company_id = serializers.IntegerField()
    rule_id = serializers.IntegerField(required=False)
    rule_version = serializers.IntegerField(required=False)
    check_id = serializers.IntegerField(required=False)
    rules = serializers.ListField(child=serializers.DictField(), required=False)
    results = EligibleContractSerializer(many=True)
    next_cursor = serializers.CharField(allow_null=True)


def _tenant_id(request: Request) -> str:
    tenant_id = getattr(request.user, "tenant_id", None)
    if not tenant_id:
        raise NotFound("Tenant not found.", code="TENANT_ACCESS_DENIED")
    return str(tenant_id)


def _query_int(request: Request, key: str, *, minimum: int, maximum: int | None = None):
    value = request.query_params.get(key)
    if value is None:
        return None
    try:
        number = int(value)
    except ValueError as exc:
        raise ParseError(f"{key} must be an integer.", code="MALFORMED_REQUEST") from exc
    if number < minimum or (maximum is not None and number > maximum):
        bounds = f"{minimum}..{maximum}" if maximum is not None else f">={minimum}"
        raise ParseError(f"{key} must be {bounds}.", code="MALFORMED_REQUEST")
    return number


def _selected_rule_version(tenant_id: str, rule_id: int, version: int | None) -> RuleVersion:
    try:
        rule = Rule.objects.get(tenant_id=tenant_id, id=rule_id, status=Rule.Status.ACTIVE)
    except Rule.DoesNotExist as exc:
        raise NotFound("Rule not found.", code="RULE_NOT_FOUND") from exc
    version_number = version or rule.current_version
    if version_number is None:
        raise NotFound("Rule version not found.", code="RULE_NOT_FOUND")
    try:
        return RuleVersion.objects.get(
            tenant_id=tenant_id,
            rule_id=rule.id,
            version=version_number,
        )
    except RuleVersion.DoesNotExist as exc:
        raise NotFound("Rule version not found.", code="RULE_NOT_FOUND") from exc


def _selected_check_versions(tenant_id: str, check_id: int) -> list[RuleVersion]:
    try:
        check = ComplianceCheck.objects.get(tenant_id=tenant_id, id=check_id)
    except ComplianceCheck.DoesNotExist as exc:
        raise NotFound("Check not found.", code="CHECK_NOT_FOUND") from exc
    if check.current_version is None:
        raise NotFound("Check version not found.", code="CHECK_NOT_FOUND")
    pins = (
        CheckVersionRule.objects.filter(
            tenant_id=tenant_id,
            check_version__compliance_check=check,
            check_version__version=check.current_version,
        )
        .select_related("rule_version")
        .order_by("position")
    )
    return [pin.rule_version for pin in pins]


def _cursor_scope(
    tenant_id: str,
    company_id: int,
    rule_id: int | None,
    check_id: int | None,
    version_key: str,
) -> dict[str, str]:
    return {
        "tenant_id": tenant_id,
        "company_id": str(company_id),
        "rule_id": str(rule_id or ""),
        "check_id": str(check_id or ""),
        "version": version_key,
    }


def _eligible_query(
    *,
    tenant_id: str,
    company_id: int,
    fragment: SQLFragment,
    after_id: int,
    page_size: int,
) -> list[tuple[int, int]]:
    sql = f"""
        SELECT c.id, c.worker_id
        FROM directory_contract AS c
        JOIN directory_worker AS w
          ON w.id = c.worker_id AND w.tenant_id = c.tenant_id
        JOIN directory_company AS co
          ON co.id = c.company_id AND co.tenant_id = c.tenant_id
        WHERE c.tenant_id = %s
          AND c.company_id = %s
          AND c.id > %s
          AND ({fragment.sql})
        ORDER BY c.id
        LIMIT %s
    """
    with connection.cursor() as cursor:
        params: list[Any] = [tenant_id, company_id, after_id, *fragment.params, page_size]
        cursor.execute(
            sql,
            params,
        )
        return [(int(contract_id), int(worker_id)) for contract_id, worker_id in cursor.fetchall()]


class EligibleContractsView(APIView):
    @extend_schema(
        responses={200: EligibleContractsOutputSerializer},
        description=(
            "Return contracts that pass a rule or every pinned rule in a check. "
            "Pagination is signed keyset-based; UNKNOWN outcomes are excluded."
        ),
    )
    def get(self, request: Request, company_id: int) -> Response:
        tenant_id = _tenant_id(request)
        try:
            company = Company.objects.only("id").get(tenant_id=tenant_id, id=company_id)
        except Company.DoesNotExist as exc:
            raise NotFound("Company not found.", code="CONTRACT_NOT_FOUND") from exc

        rule_id = _query_int(request, "rule_id", minimum=1)
        check_id = _query_int(request, "check_id", minimum=1)
        version = _query_int(request, "version", minimum=1)
        limit = _query_int(request, "limit", minimum=1, maximum=1000) or 100
        if (rule_id is None) == (check_id is None):
            raise ParseError("Specify exactly one of rule_id or check_id.")
        if version is not None and rule_id is None:
            raise ParseError("version is only supported with rule_id.")

        if rule_id is not None:
            rule_versions = [_selected_rule_version(tenant_id, rule_id, version)]
            version_key = str(rule_versions[0].version)
            expanded = get_expanded_rule_definition(rule_versions[0])
            definitions = [expanded.definition]
            fragment = compile_definition(definitions[0], timezone.now())
            if expanded.reference_count:
                logger.info(
                    "Compiled eligibility rule references",
                    extra={
                        "tenant_id": tenant_id,
                        "rule_id": rule_id,
                        "rule_version": rule_versions[0].version,
                        "reference_count": expanded.reference_count,
                        "expanded_node_count": expanded.node_count,
                    },
                )
        else:
            rule_versions = _selected_check_versions(tenant_id, check_id)
            version_key = ",".join(f"{item.rule_id}:{item.version}" for item in rule_versions)
            expanded_definitions = [
                get_expanded_rule_definition(item) for item in rule_versions
            ]
            definitions = [item.definition for item in expanded_definitions]
            for rule_version, expanded in zip(rule_versions, expanded_definitions, strict=True):
                if expanded.reference_count:
                    logger.info(
                        "Compiled eligibility check references",
                        extra={
                            "tenant_id": tenant_id,
                            "rule_id": rule_version.rule_id,
                            "rule_version": rule_version.version,
                            "reference_count": expanded.reference_count,
                            "expanded_node_count": expanded.node_count,
                        },
                    )
            fragment = compile_all(definitions, timezone.now())

        scope = _cursor_scope(tenant_id, company.id, rule_id, check_id, version_key)
        cursor = request.query_params.get("cursor")
        after_id = decode_eligibility_cursor(cursor, scope=scope) if cursor else 0
        rows = _eligible_query(
            tenant_id=tenant_id,
            company_id=company.id,
            fragment=fragment,
            after_id=after_id,
            page_size=limit + 1,
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = encode_eligibility_cursor(page[-1][0], scope) if has_more and page else None
        data = {
            "company_id": company.id,
            "results": [
                {"contract_id": contract_id, "worker_id": worker_id}
                for contract_id, worker_id in page
            ],
            "next_cursor": next_cursor,
        }
        if rule_id is not None:
            data.update({"rule_id": rule_id, "rule_version": rule_versions[0].version})
        else:
            data.update(
                {
                    "check_id": check_id,
                    "rules": [
                        {"rule_id": item.rule_id, "version": item.version} for item in rule_versions
                    ],
                }
            )
        return Response(data, status=status.HTTP_200_OK)
