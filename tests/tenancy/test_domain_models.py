from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from apps.directory.models import Company, Contract, Worker
from apps.evaluation.models import CheckVersion, CheckVersionRule, ComplianceCheck, EvaluationResult
from apps.rules.models import Rule, RuleVersion
from apps.tenants.models import Tenant


def set_tenant(tenant):
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.pk)])


def enforce_constraints():
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def make_directory(tenant):
    worker = Worker.objects.create(
        tenant=tenant, name="Worker", date_of_birth=date(1990, 1, 1), nationality="GB"
    )
    company = Company.objects.create(
        tenant=tenant,
        name="Company",
        industry="Technology",
        country="GB",
        bank_deposit=Decimal("2.00"),
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)
    contract = Contract.objects.create(
        tenant=tenant,
        worker=worker,
        company=company,
        start_date=start,
        end_date=start + timedelta(days=1),
        country="GB",
    )
    return worker, company, contract


def make_rule(tenant, name="Rule"):
    rule = Rule.objects.create(tenant=tenant, name=name)
    version = RuleVersion.objects.create(
        tenant=tenant, rule=rule, version=1, definition={}, schema_version=1, created_by="tester"
    )
    rule.current_version = 1
    rule.save(update_fields=["current_version"])
    return rule, version


@pytest.mark.django_db(transaction=True)
def test_directory_constraints_and_country_validation():
    tenant = Tenant.objects.create(name="Directory")
    with transaction.atomic():
        set_tenant(tenant)
        worker, company, contract = make_directory(tenant)
        worker.nationality = "ZZ"
        with pytest.raises(ValidationError):
            worker.full_clean()
        for update in (
            lambda: Worker.objects.filter(pk=worker.pk).update(nationality="ZZ"),
            lambda: Company.objects.filter(pk=company.pk).update(bank_deposit=-1),
            lambda: Contract.objects.filter(pk=contract.pk).update(end_date=contract.start_date),
        ):
            with pytest.raises(IntegrityError), transaction.atomic():
                update()


@pytest.mark.django_db(transaction=True)
def test_versions_pins_results_and_cross_tenant_constraints():
    tenant = Tenant.objects.create(name="A")
    other = Tenant.objects.create(name="B")
    with transaction.atomic():
        set_tenant(other)
        other_worker, other_company, other_contract = make_directory(other)
        _, other_version = make_rule(other)
    with transaction.atomic():
        set_tenant(tenant)
        worker, company, contract = make_directory(tenant)
        rule, version = make_rule(tenant)
        check = ComplianceCheck.objects.create(tenant=tenant, name="Check")
        check_version = CheckVersion.objects.create(
            tenant=tenant, compliance_check=check, version=1, rule_versions=[version.pk]
        )
        pin = CheckVersionRule.objects.create(
            tenant=tenant, check_version=check_version, rule_version=version, position=0
        )
        check.current_version = 1
        check.save(update_fields=["current_version"])
        enforce_constraints()
        result_data = {
            "tenant": tenant,
            "check_version": check_version,
            "contract": contract,
            "outcome": "COMPLIANT",
            "details": {},
            "as_of": contract.start_date,
            "duration_ms": 1,
            "idempotency_key": "key",
            "request_hash": "hash",
        }
        EvaluationResult.objects.create(**result_data)
        failures = (
            lambda: Contract.objects.create(
                tenant=tenant,
                worker=other_worker,
                company=company,
                start_date=contract.start_date,
                end_date=contract.end_date,
                country="GB",
            ),
            lambda: Contract.objects.create(
                tenant=tenant,
                worker=worker,
                company=other_company,
                start_date=contract.start_date,
                end_date=contract.end_date,
                country="GB",
            ),
            lambda: RuleVersion.objects.filter(pk=version.pk).update(definition={"changed": True}),
            lambda: CheckVersion.objects.filter(pk=check_version.pk).update(rule_versions=[]),
            lambda: CheckVersionRule.objects.filter(pk=pin.pk).delete(),
            lambda: EvaluationResult.objects.create(**result_data),
            lambda: EvaluationResult.objects.create(
                **{**result_data, "idempotency_key": "other", "contract": other_contract}
            ),
            lambda: CheckVersionRule.objects.create(
                tenant=tenant, check_version=check_version, rule_version=other_version, position=1
            ),
            lambda: Rule.objects.filter(pk=rule.pk).update(current_version=99),
            lambda: Rule.objects.filter(pk=rule.pk).update(status="deleted"),
            lambda: RuleVersion.objects.create(
                tenant=tenant,
                rule=rule,
                version=0,
                definition={},
                schema_version=1,
                created_by="tester",
            ),
            lambda: ComplianceCheck.objects.filter(pk=check.pk).update(current_version=99),
            lambda: EvaluationResult.objects.create(
                **{**result_data, "idempotency_key": "bad-outcome", "outcome": "UNKNOWN"}
            ),
        )
        for fail in failures:
            with pytest.raises(IntegrityError), transaction.atomic():
                fail()


@pytest.mark.django_db(transaction=True)
def test_pins_cannot_be_added_later_or_mismatch_json():
    tenant = Tenant.objects.create(name="Pins")
    with transaction.atomic():
        set_tenant(tenant)
        _, version = make_rule(tenant)
        check = ComplianceCheck.objects.create(tenant=tenant, name="Check")
        empty = CheckVersion.objects.create(
            tenant=tenant, compliance_check=check, version=1, rule_versions=[]
        )
        enforce_constraints()
        with pytest.raises(IntegrityError), transaction.atomic():
            CheckVersionRule.objects.create(
                tenant=tenant, check_version=empty, rule_version=version, position=0
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            CheckVersion.objects.create(
                tenant=tenant, compliance_check=check, version=2, rule_versions=[version.pk]
            )


@pytest.mark.django_db(transaction=True)
def test_rls_enabled_forced_and_tenant_isolation():
    tenant = Tenant.objects.create(name="RLS A")
    other = Tenant.objects.create(name="RLS B")
    tables = [
        "rules_rule",
        "rules_rule_version",
        "evaluation_check",
        "evaluation_check_version",
        "evaluation_check_version_rule",
        "evaluation_result",
    ]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity "
            "FROM pg_class WHERE relname = ANY(%s)",
            [tables],
        )
        assert {name for name, enabled, forced in cursor.fetchall() if enabled and forced} == set(
            tables
        )
    with transaction.atomic():
        set_tenant(tenant)
        Rule.objects.create(tenant=tenant, name="A")
        set_tenant(other)
        Rule.objects.create(tenant=other, name="B")
        assert list(Rule.objects.values_list("name", flat=True)) == ["B"]
        set_tenant(tenant)
        assert list(Rule.objects.values_list("name", flat=True)) == ["A"]
        with pytest.raises(DatabaseError), transaction.atomic():
            Rule.objects.create(tenant=other, name="Forbidden")
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.tenant_id', '', true)")
        assert not Rule.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_domain_migrations_reverse_and_reapply():
    executor = MigrationExecutor(connection)
    executor.migrate([("evaluation", None), ("rules", None), ("directory", None)])
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
