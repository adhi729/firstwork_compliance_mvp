import logging

from django.db import IntegrityError, transaction
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.evaluation.models import CheckVersion, CheckVersionRule, ComplianceCheck
from apps.rules.models import Rule, RuleDependency, RuleVersion
from apps.rules.references import (
    create_dependency_records,
    normalize_rule_definition,
    prepare_rule_definition,
)
from apps.tenants.errors import (
    DuplicateNameAPIError,
    RuleDefinitionAPIError,
    VersionMismatchAPIError,
)
from domain.engine import RuleError

logger = logging.getLogger(__name__)


class RuleInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    definition = serializers.JSONField(
        help_text=(
            "Expression tree with conditions, AND/OR groups, and (when enabled) "
            "rule_ref nodes containing a rule_id and optional version. The user. "
            "entity prefix is accepted as an alias for worker. and stored canonically."
        )
    )

    def validate_definition(self, value):
        try:
            return normalize_rule_definition(value)
        except RuleError as exc:
            reason = exc.message
            if reason == "unknown property":
                code = "INVALID_PROPERTY"
            elif "operator" in reason:
                code = "UNSUPPORTED_OPERATOR"
            elif "value" in reason or reason.startswith("invalid "):
                code = "INVALID_VALUE_TYPE"
            else:
                code = "INVALID_RULE_DEFINITION"
            raise RuleDefinitionAPIError(f"{exc.path}: {reason}", code=code) from exc


class CheckInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    rule_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=1000,
    )

    def validate_rule_ids(self, values):
        if len(set(values)) != len(values):
            raise serializers.ValidationError("Rule ids must be unique.")
        return values


class RuleOutputSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    description = serializers.CharField()
    status = serializers.CharField()
    current_version = serializers.IntegerField(allow_null=True)
    definition = serializers.JSONField(allow_null=True)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class RuleVersionOutputSerializer(serializers.Serializer):
    version = serializers.IntegerField()
    schema_version = serializers.IntegerField()
    definition = serializers.JSONField()
    created_by = serializers.CharField()
    created_at = serializers.DateTimeField()


class RuleDependentOutputSerializer(serializers.Serializer):
    rule_id = serializers.IntegerField()
    version = serializers.IntegerField()
    name = serializers.CharField()
    referenced_version = serializers.IntegerField()


class CheckVersionRuleOutputSerializer(serializers.Serializer):
    rule_id = serializers.IntegerField()
    version = serializers.IntegerField()
    rule_version_id = serializers.IntegerField()
    name = serializers.CharField()


class CheckVersionOutputSerializer(serializers.Serializer):
    version = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    rules = CheckVersionRuleOutputSerializer(many=True)


class CheckOutputSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    description = serializers.CharField()
    current_version = serializers.IntegerField(allow_null=True)
    versions = CheckVersionOutputSerializer(many=True)


def _tenant_id(request: Request) -> str:
    tenant_id = getattr(request.user, "tenant_id", None)
    if not tenant_id:
        raise NotFound("Tenant not found.", code="TENANT_ACCESS_DENIED")
    return str(tenant_id)


def _definition_payload(version: RuleVersion) -> dict:
    return {
        "version": version.version,
        "schema_version": version.schema_version,
        "definition": version.definition,
        "created_by": version.created_by,
        "created_at": version.created_at,
    }


def _rule_payload(rule: Rule, version: RuleVersion | None) -> dict:
    return {
        "id": rule.id,
        "name": rule.name,
        "description": rule.description,
        "status": rule.status,
        "current_version": rule.current_version,
        "definition": version.definition if version else None,
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
    }


def _if_match_version(request: Request) -> int:
    value = request.headers.get("If-Match", "").strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    if not value.isdecimal():
        raise VersionMismatchAPIError("If-Match must contain the current version.")
    return int(value)


def _check_version_payload(check_version: CheckVersion) -> dict:
    pins = (
        CheckVersionRule.objects.filter(
            tenant_id=check_version.tenant_id,
            check_version_id=check_version.id,
        )
        .select_related("rule_version", "rule_version__rule")
        .order_by("position")
    )
    return {
        "version": check_version.version,
        "created_at": check_version.created_at,
        "rules": [
            {
                "rule_id": pin.rule_version.rule_id,
                "version": pin.rule_version.version,
                "rule_version_id": pin.rule_version_id,
                "name": pin.rule_version.rule.name,
            }
            for pin in pins
        ],
    }


def _resolve_active_rule_versions(tenant_id: str, rule_ids: list[int]) -> list[RuleVersion]:
    rules = list(
        Rule.objects.filter(
            tenant_id=tenant_id,
            id__in=rule_ids,
            status=Rule.Status.ACTIVE,
        )
    )
    rule_by_id = {rule.id: rule for rule in rules}
    if len(rule_by_id) != len(rule_ids):
        raise NotFound("One or more rules were not found.", code="RULE_NOT_FOUND")
    current_versions: list[int] = []
    for rule_id in rule_ids:
        version = rule_by_id[rule_id].current_version
        if version is None:
            raise NotFound("One or more rule versions were not found.", code="RULE_NOT_FOUND")
        current_versions.append(version)
    versions = {
        version.rule_id: version
        for version in RuleVersion.objects.filter(
            tenant_id=tenant_id,
            rule_id__in=rule_ids,
            version__in=current_versions,
        )
    }
    if any(rule_id not in versions for rule_id in rule_ids):
        raise NotFound("One or more rule versions were not found.", code="RULE_NOT_FOUND")
    return [versions[rule_id] for rule_id in rule_ids]


def _create_check_version(
    check: ComplianceCheck,
    versions: list[RuleVersion],
    version_number: int,
) -> CheckVersion:
    pin_payload = [rule_version.id for rule_version in versions]
    check_version = CheckVersion.objects.create(
        tenant_id=check.tenant_id,
        compliance_check=check,
        version=version_number,
        rule_versions=pin_payload,
    )
    CheckVersionRule.objects.bulk_create(
        [
            CheckVersionRule(
                tenant_id=check.tenant_id,
                check_version=check_version,
                rule_version=rule_version,
                position=position,
            )
            for position, rule_version in enumerate(versions)
        ]
    )
    check.current_version = version_number
    check.save(update_fields=["current_version"])
    return check_version


class RuleCollectionView(APIView):
    @extend_schema(
        request=RuleInputSerializer,
        responses={201: RuleOutputSerializer},
        description=(
            "Create an active tenant rule at immutable schema version 2. "
            "Rule references are accepted only when RULE_REFERENCES_ENABLED is true."
        ),
    )
    def post(self, request: Request) -> Response:
        serializer = RuleInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if not data.get("name"):
            raise ValidationError({"name": "This field is required."})
        tenant_id = _tenant_id(request)
        try:
            with transaction.atomic():
                rule = Rule.objects.create(
                    tenant_id=tenant_id,
                    name=data["name"],
                    description=data.get("description", ""),
                    current_version=1,
                )
                definition, dependencies = prepare_rule_definition(
                    tenant_id,
                    rule.id,
                    1,
                    data["definition"],
                )
                version = RuleVersion.objects.create(
                    tenant_id=tenant_id,
                    rule=rule,
                    version=1,
                    definition=definition,
                    schema_version=2,
                    created_by=str(getattr(request.user, "api_key_id", request.auth)),
                )
                create_dependency_records(version, dependencies)
        except IntegrityError as exc:
            if _constraint_name(exc) == "rule_tenant_name_uniq":
                raise DuplicateNameAPIError() from exc
            raise
        response = Response(_rule_payload(rule, version), status=status.HTTP_201_CREATED)
        response["ETag"] = '"1"'
        return response


class RuleDetailView(APIView):
    def get_object(self, request: Request, rule_id: int) -> Rule:
        tenant_id = _tenant_id(request)
        try:
            return Rule.objects.get(tenant_id=tenant_id, id=rule_id)
        except Rule.DoesNotExist as exc:
            raise NotFound("Rule not found.", code="RULE_NOT_FOUND") from exc

    @extend_schema(responses={200: RuleOutputSerializer})
    def get(self, request: Request, rule_id: int) -> Response:
        rule = self.get_object(request, rule_id)
        if rule.current_version is None:
            raise NotFound("Rule version not found.", code="RULE_NOT_FOUND")
        version = RuleVersion.objects.filter(
            tenant_id=rule.tenant_id,
            rule_id=rule.id,
            version=rule.current_version,
        ).first()
        response = Response(_rule_payload(rule, version))
        response["ETag"] = f'"{rule.current_version}"'
        return response

    @extend_schema(
        request=RuleInputSerializer,
        responses={200: RuleOutputSerializer},
        description=(
            "Create a new immutable rule version. Referenced rule versions remain pinned "
            "until explicitly changed in a later parent version."
        ),
    )
    def put(self, request: Request, rule_id: int) -> Response:
        data_serializer = RuleInputSerializer(data=request.data)
        data_serializer.is_valid(raise_exception=True)
        data = data_serializer.validated_data
        expected_version = _if_match_version(request)
        tenant_id = _tenant_id(request)
        try:
            with transaction.atomic():
                rule = (
                    Rule.objects.select_for_update().filter(tenant_id=tenant_id, id=rule_id).first()
                )
                if rule is None:
                    raise NotFound("Rule not found.", code="RULE_NOT_FOUND")
                if rule.status != Rule.Status.ACTIVE:
                    raise NotFound("Rule not found.", code="RULE_NOT_FOUND")
                if rule.current_version != expected_version:
                    raise VersionMismatchAPIError()
                next_version = expected_version + 1
                definition, dependencies = prepare_rule_definition(
                    tenant_id,
                    rule.id,
                    next_version,
                    data["definition"],
                )
                version = RuleVersion.objects.create(
                    tenant_id=tenant_id,
                    rule=rule,
                    version=next_version,
                    definition=definition,
                    schema_version=2,
                    created_by=str(getattr(request.user, "api_key_id", request.auth)),
                )
                create_dependency_records(version, dependencies)
                if "name" in data:
                    rule.name = data["name"]
                if "description" in data:
                    rule.description = data["description"]
                rule.current_version = next_version
                rule.save()
        except IntegrityError as exc:
            if _constraint_name(exc) == "rule_tenant_name_uniq":
                raise DuplicateNameAPIError() from exc
            raise
        response = Response(_rule_payload(rule, version))
        response["ETag"] = f'"{next_version}"'
        return response


class RuleVersionsView(APIView):
    @extend_schema(responses={200: RuleVersionOutputSerializer(many=True)})
    def get(self, request: Request, rule_id: int) -> Response:
        tenant_id = _tenant_id(request)
        if not Rule.objects.filter(tenant_id=tenant_id, id=rule_id).exists():
            raise NotFound("Rule not found.", code="RULE_NOT_FOUND")
        versions = RuleVersion.objects.filter(tenant_id=tenant_id, rule_id=rule_id).order_by(
            "-version"
        )
        return Response([_definition_payload(version) for version in versions])


class RuleDependentsView(APIView):
    @extend_schema(
        responses={200: RuleDependentOutputSerializer(many=True)},
        description="List immutable rule versions that directly pin a version of this rule.",
    )
    def get(self, request: Request, rule_id: int) -> Response:
        tenant_id = _tenant_id(request)
        if not Rule.objects.filter(tenant_id=tenant_id, id=rule_id).exists():
            raise NotFound("Rule not found.", code="RULE_NOT_FOUND")
        dependencies = (
            RuleDependency.objects.filter(
                tenant_id=tenant_id,
                depends_on_version__rule_id=rule_id,
            )
            .select_related("rule_version", "rule_version__rule", "depends_on_version")
            .order_by("rule_version__rule_id", "rule_version__version")
        )
        return Response(
            [
                {
                    "rule_id": dependency.rule_version.rule_id,
                    "version": dependency.rule_version.version,
                    "name": dependency.rule_version.rule.name,
                    "referenced_version": dependency.depends_on_version.version,
                }
                for dependency in dependencies
            ]
        )


class RuleArchiveView(APIView):
    @extend_schema(request=None, responses={204: None}, operation_id="archiveRule")
    def post(self, request: Request, rule_id: int) -> Response:
        tenant_id = _tenant_id(request)
        updated = Rule.objects.filter(
            tenant_id=tenant_id,
            id=rule_id,
            status=Rule.Status.ACTIVE,
        ).update(status=Rule.Status.ARCHIVED)
        if not updated:
            raise NotFound("Rule not found.", code="RULE_NOT_FOUND")
        return Response(status=status.HTTP_204_NO_CONTENT)


class CheckCollectionView(APIView):
    @extend_schema(
        request=CheckInputSerializer,
        responses={201: CheckOutputSerializer},
    )
    def post(self, request: Request) -> Response:
        serializer = CheckInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if not data.get("name"):
            raise ValidationError({"name": "This field is required."})
        tenant_id = _tenant_id(request)
        try:
            with transaction.atomic():
                versions = _resolve_active_rule_versions(tenant_id, data["rule_ids"])
                check = ComplianceCheck.objects.create(
                    tenant_id=tenant_id,
                    name=data["name"],
                    description=data.get("description", ""),
                )
                check_version = _create_check_version(check, versions, 1)
        except IntegrityError as exc:
            if _constraint_name(exc) == "check_tenant_name_uniq":
                raise DuplicateNameAPIError() from exc
            raise
        return Response(
            {
                "id": check.id,
                "name": check.name,
                "description": check.description,
                "current_version": check_version.version,
                "versions": [_check_version_payload(check_version)],
            },
            status=status.HTTP_201_CREATED,
        )


class CheckDetailView(APIView):
    def get_object(self, request: Request, check_id: int) -> ComplianceCheck:
        tenant_id = _tenant_id(request)
        try:
            return ComplianceCheck.objects.get(tenant_id=tenant_id, id=check_id)
        except ComplianceCheck.DoesNotExist as exc:
            raise NotFound("Check not found.", code="CHECK_NOT_FOUND") from exc

    @extend_schema(responses={200: CheckOutputSerializer})
    def get(self, request: Request, check_id: int) -> Response:
        check = self.get_object(request, check_id)
        versions = CheckVersion.objects.filter(
            tenant_id=check.tenant_id,
            compliance_check=check,
        ).order_by("-version")
        return Response(
            {
                "id": check.id,
                "name": check.name,
                "description": check.description,
                "current_version": check.current_version,
                "versions": [_check_version_payload(item) for item in versions],
            }
        )

    @extend_schema(
        request=CheckInputSerializer,
        responses={200: CheckOutputSerializer},
    )
    def put(self, request: Request, check_id: int) -> Response:
        serializer = CheckInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        expected_version = _if_match_version(request)
        tenant_id = _tenant_id(request)
        try:
            with transaction.atomic():
                check = (
                    ComplianceCheck.objects.select_for_update()
                    .filter(tenant_id=tenant_id, id=check_id)
                    .first()
                )
                if check is None:
                    raise NotFound("Check not found.", code="CHECK_NOT_FOUND")
                if check.current_version != expected_version:
                    raise VersionMismatchAPIError()
                versions = _resolve_active_rule_versions(tenant_id, data["rule_ids"])
                if "name" in data:
                    check.name = data["name"]
                if "description" in data:
                    check.description = data["description"]
                check_version = _create_check_version(check, versions, expected_version + 1)
                check.save()
        except IntegrityError as exc:
            if _constraint_name(exc) == "check_tenant_name_uniq":
                raise DuplicateNameAPIError() from exc
            raise
        response = Response(
            {
                "id": check.id,
                "name": check.name,
                "description": check.description,
                "current_version": check_version.version,
                "versions": [_check_version_payload(check_version)],
            }
        )
        response["ETag"] = f'"{check_version.version}"'
        return response


def _constraint_name(exc: IntegrityError) -> str | None:
    cause = exc.__cause__
    diagnostic = getattr(cause, "diag", None)
    return getattr(diagnostic, "constraint_name", None)
