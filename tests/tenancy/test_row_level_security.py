import os
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

import psycopg
import pytest
from django.db import IntegrityError, connection, transaction
from django.test import Client

from apps.directory.models import Company, Contract, Worker
from apps.evaluation.models import (
    CheckVersion,
    ComplianceCheck,
    EvaluationJob,
    EvaluationJobResult,
    EvaluationResult,
)
from apps.rules.models import Rule, RuleVersion
from apps.tenants.models import Tenant


def set_tenant(tenant_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])


@pytest.mark.django_db(transaction=True)
def test_app_user_is_isolated_for_reads_and_writes():
    tenant_a = Tenant.objects.create(name="Tenant A")
    tenant_b = Tenant.objects.create(name="Tenant B")
    with transaction.atomic():
        set_tenant(tenant_a.id)
        company_a = Company.objects.create(
            tenant=tenant_a,
            name="Company A",
            industry="retail",
            country="DE",
            bank_deposit="1000.00",
        )
        worker_a = Worker.objects.create(
            tenant=tenant_a,
            name="Worker A",
            date_of_birth="1990-01-01",
            nationality="DE",
        )
    with transaction.atomic():
        set_tenant(tenant_b.id)
        company_b = Company.objects.create(
            tenant=tenant_b,
            name="Company B",
            industry="manufacturing",
            country="FR",
            bank_deposit="2000.00",
        )
        worker_b = Worker.objects.create(
            tenant=tenant_b,
            name="Worker B",
            date_of_birth="1992-02-02",
            nationality="FR",
        )
    with transaction.atomic():
        set_tenant(tenant_a.id)
        parent_rule = Rule.objects.create(
            tenant=tenant_a,
            name="Tenant A parent rule",
            status=Rule.Status.ACTIVE,
            current_version=1,
        )
        parent_version = RuleVersion.objects.create(
            tenant=tenant_a,
            rule=parent_rule,
            version=1,
            definition={"property": "worker.age", "operator": ">=", "value": 18},
            schema_version=2,
            created_by="rls-test",
        )
    with transaction.atomic():
        set_tenant(tenant_b.id)
        target_rule = Rule.objects.create(
            tenant=tenant_b,
            name="Tenant B target rule",
            status=Rule.Status.ACTIVE,
            current_version=1,
        )
        target_version = RuleVersion.objects.create(
            tenant=tenant_b,
            rule=target_rule,
            version=1,
            definition={"property": "worker.age", "operator": ">=", "value": 18},
            schema_version=2,
            created_by="rls-test",
        )

    with connection.cursor() as cursor:
        cursor.execute("GRANT USAGE ON SCHEMA public TO app_user")
        cursor.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE "
            "ON ALL TABLES IN SCHEMA public TO app_user"
        )
        cursor.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user")

    app_database_url = os.environ.get("APP_DATABASE_URL")
    if not app_database_url:
        raise RuntimeError("APP_DATABASE_URL is required for runtime-role tenancy tests")
    app_url = urlsplit(app_database_url)
    if not app_url.username or not app_url.password:
        raise RuntimeError("APP_DATABASE_URL must include runtime database credentials")

    with psycopg.connect(
        host=connection.settings_dict["HOST"],
        port=connection.settings_dict["PORT"] or 5432,
        dbname=connection.settings_dict["NAME"],
        user=unquote(app_url.username),
        password=unquote(app_url.password),
    ) as app_connection:
        with app_connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.tenant_id', %s, true)",
                [str(tenant_a.id)],
            )
            cursor.execute("SET CONSTRAINTS rule_dependency_target_tenant_fk IMMEDIATE")
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                cursor.execute(
                    "INSERT INTO rules_rule_dependency "
                    "(tenant_id, rule_version_id, depends_on_version_id) "
                    "VALUES (%s, %s, %s)",
                    [tenant_a.id, parent_version.id, target_version.id],
                )
            app_connection.rollback()
        with app_connection.cursor() as cursor:
            cursor.execute("SELECT id FROM directory_company ORDER BY id")
            assert cursor.fetchall() == []
            cursor.execute(
                "SELECT set_config('app.tenant_id', %s, true)",
                [str(tenant_a.id)],
            )
            cursor.execute("SELECT id FROM directory_company")
            assert cursor.fetchall() == [(company_a.id,)]
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "INSERT INTO directory_company "
                    "(tenant_id, name, industry, country, bank_deposit, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, NOW())",
                    [tenant_b.id, "forbidden", "retail", "DE", "0.00"],
                )
            app_connection.rollback()
            with app_connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true)",
                    [str(tenant_a.id)],
                )
                cursor.execute("SELECT id FROM directory_company")
                assert cursor.fetchall() == [(company_a.id,)]

    with transaction.atomic():
        set_tenant(tenant_a.id)
        with connection.cursor() as cursor:
            cursor.execute(
                "SET CONSTRAINTS "
                "contract_company_same_tenant_fk, "
                "contract_worker_same_tenant_fk IMMEDIATE"
            )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Contract.objects.create(tenant=tenant_a, company=company_b, worker=worker_a)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Contract.objects.create(tenant=tenant_a, company=company_a, worker=worker_b)


def test_tenant_header_does_not_authenticate_api_request():
    response = Client().get("/api/v1/rules", HTTP_X_TENANT_ID="not-a-uuid")
    assert response.status_code == 401


@pytest.mark.django_db(transaction=True)
def test_evaluation_job_tables_enforce_tenant_isolation_and_foreign_keys():
    tenant_a = Tenant.objects.create(name="Bulk RLS tenant A")
    tenant_b = Tenant.objects.create(name="Bulk RLS tenant B")

    def create_job(tenant, suffix):
        with transaction.atomic():
            set_tenant(tenant.id)
            company = Company.objects.create(
                tenant=tenant,
                name=f"Bulk company {suffix}",
                industry="retail",
                country="DE",
                bank_deposit="1000.00",
            )
            worker = Worker.objects.create(
                tenant=tenant,
                name=f"Bulk worker {suffix}",
                date_of_birth="1990-01-01",
                nationality="DE",
            )
            contract = Contract.objects.create(
                tenant=tenant,
                company=company,
                worker=worker,
                start_date=datetime.now(UTC) - timedelta(days=1),
                end_date=datetime.now(UTC) + timedelta(days=30),
                country="DE",
            )
            check = ComplianceCheck.objects.create(tenant=tenant, name=f"Check {suffix}")
            check_version = CheckVersion.objects.create(
                tenant=tenant,
                compliance_check=check,
                version=1,
                rule_versions=[],
            )
            job = EvaluationJob.objects.create(
                tenant=tenant,
                check_version=check_version,
                total_contracts=1,
                max_contract_id=contract.id,
            )
            EvaluationJobResult.objects.create(
                tenant=tenant,
                job=job,
                contract=contract,
                outcome=EvaluationResult.Outcome.COMPLIANT,
                details={},
                as_of=datetime.now(UTC),
                duration_ms=0,
            )
        return job.id, contract.id

    job_a_id, contract_a_id = create_job(tenant_a, "A")
    job_b_id, _ = create_job(tenant_b, "B")

    app_database_url = os.environ.get("APP_DATABASE_URL")
    if not app_database_url:
        raise RuntimeError("APP_DATABASE_URL is required for runtime-role tenancy tests")
    app_url = urlsplit(app_database_url)
    if not app_url.username or not app_url.password:
        raise RuntimeError("APP_DATABASE_URL must include runtime database credentials")

    with psycopg.connect(
        host=connection.settings_dict["HOST"],
        port=connection.settings_dict["PORT"] or 5432,
        dbname=connection.settings_dict["NAME"],
        user=unquote(app_url.username),
        password=unquote(app_url.password),
    ) as app_connection:
        with app_connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.tenant_id', %s, true)",
                [str(tenant_a.id)],
            )
            cursor.execute("SELECT id FROM evaluation_job")
            assert cursor.fetchall() == [(job_a_id,)]
            cursor.execute("SELECT job_id FROM evaluation_job_result")
            assert cursor.fetchall() == [(job_a_id,)]

            cursor.execute("SET CONSTRAINTS job_result_job_same_tenant_fk IMMEDIATE")
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                cursor.execute(
                    "INSERT INTO evaluation_job_result "
                    "(tenant_id, job_id, contract_id, outcome, details, as_of, duration_ms) "
                    "VALUES (%s, %s, %s, 'COMPLIANT', '{}'::jsonb, NOW(), 0)",
                    [tenant_a.id, job_b_id, contract_a_id],
                )
            app_connection.rollback()
