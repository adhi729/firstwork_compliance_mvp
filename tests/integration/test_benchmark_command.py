import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection, transaction

from apps.directory.models import Company, Contract, Worker
from apps.rules.models import Rule, RuleDependency, RuleVersion
from apps.tenants.models import Tenant


@pytest.mark.django_db(transaction=True)
def test_benchmark_command_reports_measured_results_and_query_plan():
    tenant = Tenant.objects.create(name="benchmark-command-test")
    now = datetime.now(UTC)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.tenant_id', %s, true)",
                [str(tenant.pk)],
            )
        company = Company.objects.create(
            tenant=tenant,
            name="Benchmark Company",
            industry="technology",
            country="DE",
            bank_deposit=Decimal("1000.00"),
        )
        worker = Worker.objects.create(
            tenant=tenant,
            name="Benchmark Worker",
            date_of_birth=date(1990, 1, 1),
            nationality="DE",
        )
        Contract.objects.create(
            tenant=tenant,
            company=company,
            worker=worker,
            start_date=now - timedelta(days=30),
            end_date=now + timedelta(days=365),
            country="DE",
        )
        rules = Rule.objects.bulk_create(
            [
                Rule(
                    tenant=tenant,
                    name=f"Benchmark Rule {index}",
                    status=Rule.Status.ACTIVE,
                    current_version=1,
                )
                for index in range(20)
            ]
        )
        versions = RuleVersion.objects.bulk_create(
            [
                RuleVersion(
                    tenant=tenant,
                    rule=rule,
                    version=1,
                    definition=(
                        {
                            "op": "AND",
                            "children": [
                                {
                                    "rule_ref": {
                                        "rule_id": rules[1].id,
                                        "version": 1,
                                    }
                                },
                                {
                                    "property": "worker.age",
                                    "operator": ">=",
                                    "value": 18,
                                },
                            ],
                        }
                        if index == 0
                        else {
                            "property": "worker.age",
                            "operator": ">=",
                            "value": 18,
                        }
                    ),
                    schema_version=2,
                    created_by="benchmark-test",
                )
                for index, rule in enumerate(rules)
            ]
        )
        RuleDependency.objects.create(
            tenant=tenant,
            rule_version=versions[0],
            depends_on_version=versions[1],
        )

    output = StringIO()
    call_command(
        "run_compliance_benchmark",
        tenant=tenant.name,
        iterations=1,
        composite_rule_id=rules[0].id,
        stdout=output,
    )
    report = json.loads(output.getvalue())
    assert report["tenant_contract_count"] == 1
    assert report["rule_versions_evaluated"] == 20
    assert report["composite_rule_id"] == rules[0].id
    assert report["expanded_rule_cache_hit_rate"] == 1.0
    assert report["expanded_rule_cache_lookups"] == 20
    assert report["evaluation_20_rules_p95_ms"] >= 0
    assert report["eligible_first_page_p95_ms"] >= 0
    assert report["eligible_first_page_explain_analyze"][0]["Plan"]
