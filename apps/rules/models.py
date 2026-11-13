from django.db import models
from django.db.models import Q

from apps.tenants.models import Tenant


class Rule(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archived"

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.ACTIVE)
    current_version = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rules_rule"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="rule_tenant_name_uniq"),
            models.UniqueConstraint(fields=["id", "tenant"], name="rule_id_tenant_uniq"),
            models.CheckConstraint(condition=~Q(name=""), name="rule_name_nonempty"),
            models.CheckConstraint(
                condition=Q(status__in=["active", "archived"]), name="rule_status_valid"
            ),
            models.CheckConstraint(
                condition=Q(current_version__isnull=True) | Q(current_version__gte=1),
                name="rule_current_version_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "id"], condition=Q(status="active"), name="rule_active_tenant_idx"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class RuleVersion(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    rule = models.ForeignKey(Rule, on_delete=models.PROTECT, related_name="versions")
    version = models.PositiveIntegerField()
    definition = models.JSONField()
    schema_version = models.PositiveIntegerField()
    created_by = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "rules_rule_version"
        constraints = [
            models.UniqueConstraint(fields=["rule", "version"], name="rule_version_uniq"),
            models.UniqueConstraint(fields=["id", "tenant"], name="rule_version_id_tenant_uniq"),
            models.UniqueConstraint(
                fields=["rule", "version", "tenant"], name="rule_version_owner_uniq"
            ),
            models.CheckConstraint(condition=Q(version__gte=1), name="rule_version_positive"),
            models.CheckConstraint(
                condition=Q(schema_version__gte=1), name="rule_schema_version_positive"
            ),
            models.CheckConstraint(condition=~Q(created_by=""), name="rule_created_by_nonempty"),
        ]

    def __str__(self) -> str:
        return f"Rule {self.rule_id} v{self.version}"


class RuleDependency(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    rule_version = models.ForeignKey(
        RuleVersion,
        on_delete=models.PROTECT,
        related_name="rule_dependencies",
    )
    depends_on_version = models.ForeignKey(
        RuleVersion,
        on_delete=models.PROTECT,
        related_name="referenced_by",
    )

    class Meta:
        db_table = "rules_rule_dependency"
        constraints = [
            models.UniqueConstraint(
                fields=["rule_version", "depends_on_version"],
                name="rule_dependency_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "depends_on_version"],
                name="rule_dep_target_tenant_idx",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"Rule {self.rule_version.rule_id} v{self.rule_version.version} references "
            f"rule {self.depends_on_version.rule_id} v{self.depends_on_version.version}"
        )
