import json
import math
import time
from datetime import UTC, datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.directory.models import Company, Contract
from apps.rules.models import Rule, RuleVersion
from apps.rules.services import get_expanded_rule_definition
from apps.tenants.models import Tenant
from domain.engine import evaluate_definition
from domain.engine.sql import compile_definition


class Command(BaseCommand):
    help = "Measure 20-rule evaluation and eligible-contract query latency."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default="benchmark-0000")
        parser.add_argument("--company-id", type=int)
        parser.add_argument("--iterations", type=int, default=50)
        parser.add_argument(
            "--composite-rule-id",
            type=int,
            help="Include this tenant rule as the composite rule under test.",
        )

    def handle(self, *args, **options):
        iterations = options["iterations"]
        if iterations < 1:
            raise CommandError("--iterations must be positive.")
        try:
            tenant = Tenant.objects.get(name=options["tenant"])
        except Tenant.DoesNotExist as exc:
            raise CommandError(f"Tenant {options['tenant']!r} does not exist.") from exc

        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true)",
                    [str(tenant.pk)],
                )
            contracts = Contract.objects.filter(tenant=tenant).select_related(
                "worker", "company"
            )
            try:
                contract = contracts.order_by("id").first()
                if contract is None:
                    raise CommandError("The benchmark tenant has no contracts.")
                company_id = options["company_id"] or contract.company_id
                company = Company.objects.get(tenant=tenant, id=company_id)
            except Company.DoesNotExist as exc:
                raise CommandError("The selected company does not exist for this tenant.") from exc

            composite_rule_id = options["composite_rule_id"]
            if composite_rule_id is not None:
                try:
                    composite_rule = Rule.objects.get(
                        tenant=tenant,
                        id=composite_rule_id,
                        status=Rule.Status.ACTIVE,
                    )
                except Rule.DoesNotExist as exc:
                    raise CommandError(
                        "The selected composite rule is not active in this tenant."
                    ) from exc
                if composite_rule.current_version is None:
                    raise CommandError("The selected composite rule has no current version.")
                try:
                    composite_version = RuleVersion.objects.get(
                        tenant=tenant,
                        rule=composite_rule,
                        version=composite_rule.current_version,
                    )
                except RuleVersion.DoesNotExist as exc:
                    raise CommandError(
                        "The selected composite rule version does not exist."
                    ) from exc
                rule_versions = [composite_version]
                rule_versions.extend(
                    RuleVersion.objects.filter(tenant=tenant)
                    .exclude(rule_id=composite_rule.id)
                    .select_related("rule")
                    .order_by("rule_id", "version")[:19]
                )
            else:
                rule_versions = list(
                    RuleVersion.objects.filter(tenant=tenant)
                    .select_related("rule")
                    .order_by("rule_id", "version")[:20]
                )
            if len(rule_versions) != 20:
                raise CommandError(f"Expected 20 rule versions; found {len(rule_versions)}.")
            expanded_definitions = [
                get_expanded_rule_definition(item) for item in rule_versions
            ]
            if composite_rule_id is not None and expanded_definitions[0].reference_count == 0:
                raise CommandError("The selected rule contains no rule references.")
            definitions = [item.definition for item in expanded_definitions]
            cache_hits = 0
            cache_lookups = 0
            for _ in range(min(iterations, 10)):
                for version in rule_versions:
                    expanded = get_expanded_rule_definition(version)
                    cache_hits += int(expanded.cache_hit)
                    cache_lookups += 1
            context = {
                "worker": {
                    "name": contract.worker.name,
                    "date_of_birth": contract.worker.date_of_birth,
                    "nationality": contract.worker.nationality,
                },
                "company": {
                    "name": contract.company.name,
                    "industry": contract.company.industry,
                    "country": contract.company.country,
                    "bank_deposit": contract.company.bank_deposit,
                },
                "contract": {
                    "start_date": contract.start_date,
                    "end_date": contract.end_date,
                    "country": contract.country,
                },
            }
            as_of = timezone.now()
            for _ in range(5):
                for definition in definitions:
                    evaluate_definition(definition, context, as_of)

            evaluation_samples = []
            for _ in range(iterations):
                started = time.perf_counter()
                for definition in definitions:
                    evaluate_definition(definition, context, as_of)
                evaluation_samples.append((time.perf_counter() - started) * 1000)

            definition = definitions[0]
            fragment = compile_definition(definition, as_of)
            eligibility_sql = f"""
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
            query_params = [
                str(tenant.pk),
                company.id,
                0,
                *fragment.params,
                101,
            ]
            eligibility_samples = []
            with connection.cursor() as cursor:
                for _ in range(5):
                    cursor.execute(eligibility_sql, query_params)
                    cursor.fetchall()
                for _ in range(iterations):
                    started = time.perf_counter()
                    cursor.execute(eligibility_sql, query_params)
                    cursor.fetchall()
                    eligibility_samples.append((time.perf_counter() - started) * 1000)
                cursor.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                    + eligibility_sql,
                    query_params,
                )
                explain = cursor.fetchone()[0]

            contract_count = contracts.count()
            report = {
                "tenant": tenant.name,
                "tenant_contract_count": contract_count,
                "company_id": company.id,
                "composite_rule_id": composite_rule_id,
                "rule_versions_evaluated": len(rule_versions),
                "iterations": iterations,
                "expanded_rule_cache_hit_rate": (
                    round(cache_hits / cache_lookups, 4) if cache_lookups else 0
                ),
                "expanded_rule_cache_lookups": cache_lookups,
                "evaluation_20_rules_p95_ms": self._p95(evaluation_samples),
                "eligible_first_page_p95_ms": self._p95(eligibility_samples),
                "eligible_first_page_explain_analyze": explain,
            }
            self.stdout.write(json.dumps(report, indent=2, default=self._json_default))

    @staticmethod
    def _p95(samples: list[float]) -> float:
        ordered = sorted(samples)
        return round(ordered[math.ceil(0.95 * len(ordered)) - 1], 3)

    @staticmethod
    def _json_default(value):
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat()
        raise TypeError(f"Cannot serialize {type(value).__name__}.")
