from django.db import models
from django.db.models import Q

from apps.directory.models import Contract
from apps.rules.models import RuleVersion
from apps.tenants.models import Tenant


class ComplianceCheck(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    current_version = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "evaluation_check"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="check_tenant_name_uniq"),
            models.UniqueConstraint(fields=["id", "tenant"], name="check_id_tenant_uniq"),
            models.CheckConstraint(condition=~Q(name=""), name="check_name_nonempty"),
            models.CheckConstraint(
                condition=Q(current_version__isnull=True) | Q(current_version__gte=1),
                name="check_current_version_positive",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class CheckVersion(models.Model):
    """Pinned IDs and their FK-backed rows must be created together in one transaction."""

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    compliance_check = models.ForeignKey(
        ComplianceCheck, on_delete=models.PROTECT, related_name="versions"
    )
    version = models.PositiveIntegerField()
    rule_versions = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "evaluation_check_version"
        constraints = [
            models.UniqueConstraint(
                fields=["compliance_check", "version"], name="check_version_uniq"
            ),
            models.UniqueConstraint(fields=["id", "tenant"], name="check_version_id_tenant_uniq"),
            models.UniqueConstraint(
                fields=["compliance_check", "version", "tenant"], name="check_version_owner_uniq"
            ),
            models.CheckConstraint(condition=Q(version__gte=1), name="check_version_positive"),
        ]

    def __str__(self) -> str:
        return f"Check {self.compliance_check_id} v{self.version}"


class CheckVersionRule(models.Model):
    """Ordered relational counterpart of CheckVersion.rule_versions (RuleVersion IDs)."""

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    check_version = models.ForeignKey(CheckVersion, on_delete=models.PROTECT, related_name="pins")
    rule_version = models.ForeignKey(RuleVersion, on_delete=models.PROTECT)
    position = models.PositiveIntegerField()

    class Meta:
        db_table = "evaluation_check_version_rule"
        constraints = [
            models.UniqueConstraint(
                fields=["check_version", "position"], name="check_pin_position_uniq"
            ),
            models.UniqueConstraint(
                fields=["check_version", "rule_version"], name="check_pin_rule_uniq"
            ),
        ]

    def __str__(self) -> str:
        return f"Check version {self.check_version_id}, pin {self.position}"


class EvaluationResult(models.Model):
    class Outcome(models.TextChoices):
        COMPLIANT = "COMPLIANT", "Compliant"
        NON_COMPLIANT = "NON_COMPLIANT", "Non-compliant"
        INDETERMINATE = "INDETERMINATE", "Indeterminate"

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    check_version = models.ForeignKey(CheckVersion, on_delete=models.PROTECT)
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT)
    outcome = models.CharField(max_length=13, choices=Outcome.choices)
    details = models.JSONField()
    as_of = models.DateTimeField()
    duration_ms = models.PositiveBigIntegerField()
    idempotency_key = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=128)

    class Meta:
        db_table = "evaluation_result"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"], name="result_tenant_idempotency_uniq"
            ),
            models.CheckConstraint(
                condition=Q(outcome__in=["COMPLIANT", "NON_COMPLIANT", "INDETERMINATE"]),
                name="result_outcome_valid",
            ),
            models.CheckConstraint(
                condition=~Q(idempotency_key=""), name="result_idempotency_nonempty"
            ),
            models.CheckConstraint(
                condition=~Q(request_hash=""), name="result_request_hash_nonempty"
            ),
        ]

    def __str__(self) -> str:
        return f"Evaluation #{self.pk}"


class EvaluationJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    check_version = models.ForeignKey(CheckVersion, on_delete=models.PROTECT)
    status = models.CharField(max_length=9, choices=Status.choices, default=Status.PENDING)
    total_contracts = models.PositiveBigIntegerField(default=0)
    completed_contracts = models.PositiveBigIntegerField(default=0)
    max_contract_id = models.PositiveBigIntegerField(default=0)
    last_contract_id = models.PositiveBigIntegerField(default=0)
    error = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "evaluation_job"
        indexes = [models.Index(fields=["tenant", "created_at"], name="job_tenant_created_idx")]
        constraints = [
            models.UniqueConstraint(fields=["id", "tenant"], name="job_id_tenant_uniq"),
            models.CheckConstraint(
                condition=Q(completed_contracts__lte=models.F("total_contracts")),
                name="job_progress_lte_total",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["PENDING", "RUNNING", "COMPLETED", "FAILED"]),
                name="job_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(last_contract_id__lte=models.F("max_contract_id")),
                name="job_cursor_lte_max",
            ),
        ]

    def __str__(self) -> str:
        return f"Evaluation job #{self.pk}"


class EvaluationJobResult(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    job = models.ForeignKey(EvaluationJob, on_delete=models.PROTECT, related_name="results")
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT)
    outcome = models.CharField(max_length=13, choices=EvaluationResult.Outcome.choices)
    details = models.JSONField()
    as_of = models.DateTimeField()
    duration_ms = models.PositiveBigIntegerField()

    class Meta:
        db_table = "evaluation_job_result"
        indexes = [
            models.Index(fields=["tenant", "job", "contract"], name="job_result_page_idx")
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "contract"], name="job_result_contract_uniq"
            ),
            models.CheckConstraint(
                condition=Q(outcome__in=EvaluationResult.Outcome.values),
                name="job_result_outcome_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"Evaluation job {self.job_id}, contract {self.contract_id}"
