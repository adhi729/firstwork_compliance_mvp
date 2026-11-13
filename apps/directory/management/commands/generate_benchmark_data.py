from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.directory.models import Company, Contract, Worker
from apps.rules.models import Rule, RuleVersion
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Generate tenant-distributed rules and contracts for scale benchmarks."

    def add_arguments(self, parser):
        parser.add_argument("--tenants", type=int, default=100)
        parser.add_argument("--rules-per-tenant", type=int, default=1000)
        parser.add_argument("--contracts-per-tenant", type=int, default=10000)
        parser.add_argument("--batch-size", type=int, default=2000)
        parser.add_argument("--prefix", default="benchmark")
        parser.add_argument("--diverse-rules", action="store_true")
        parser.add_argument("--adult-workers", action="store_true")

    def handle(self, *args, **options):
        tenant_count = options["tenants"]
        rules_per_tenant = options["rules_per_tenant"]
        contracts_per_tenant = options["contracts_per_tenant"]
        batch_size = options["batch_size"]
        if min(tenant_count, rules_per_tenant, contracts_per_tenant, batch_size) < 1:
            raise CommandError("All counts and batch size must be positive.")
        if batch_size > 10000:
            raise CommandError("Batch size cannot exceed 10000.")

        prefix = options["prefix"]
        now = datetime.now(UTC)
        created_tenants = created_rules = created_contracts = 0
        for tenant_index in range(tenant_count):
            name = f"{prefix}-{tenant_index:04d}"
            tenant, created = Tenant.objects.get_or_create(name=name)
            created_tenants += int(created)
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('app.tenant_id', %s, true)",
                        [str(tenant.pk)],
                    )

                companies = Company.objects.bulk_create(
                    [
                        Company(
                            tenant=tenant,
                            name=f"{name}-company-{index:03d}",
                            industry=("technology" if index % 2 == 0 else "manufacturing"),
                            country=("DE" if index % 2 == 0 else "FR"),
                            bank_deposit=Decimal("10000.00") + index,
                        )
                        for index in range(min(20, contracts_per_tenant))
                    ],
                    batch_size=batch_size,
                )
                worker_count = max(1, min(contracts_per_tenant, 1000))
                workers = Worker.objects.bulk_create(
                    [
                        Worker(
                            tenant=tenant,
                            name=f"{name}-worker-{index:05d}",
                            date_of_birth=(
                                date(now.year - 25 - index % 40, 1 + index % 12, 1)
                                if options["adult_workers"]
                                else date(1970 + index % 40, 1 + index % 12, 1)
                            ),
                            nationality=("DE" if index % 2 == 0 else "FR"),
                        )
                        for index in range(worker_count)
                    ],
                    batch_size=batch_size,
                )
                rules = Rule.objects.bulk_create(
                    [
                        Rule(
                            tenant=tenant,
                            name=f"{name}-rule-{index:05d}",
                            status=Rule.Status.ACTIVE,
                            current_version=1,
                        )
                        for index in range(rules_per_tenant)
                    ],
                    batch_size=batch_size,
                )
                RuleVersion.objects.bulk_create(
                    [
                        RuleVersion(
                            tenant=tenant,
                            rule=rule,
                            version=1,
                            definition=(
                                [
                                    {
                                        "property": "worker.age",
                                        "operator": ">=",
                                        "value": 18 + index % 5,
                                    },
                                    {
                                        "property": "worker.nationality",
                                        "operator": "==",
                                        "value": "DE",
                                    },
                                    {
                                        "property": "company.bank_deposit",
                                        "operator": ">=",
                                        "value": "10010.00",
                                    },
                                    {
                                        "property": "contract.country",
                                        "operator": "==",
                                        "value": "DE",
                                    },
                                ][index % 4]
                                if options["diverse_rules"]
                                else {
                                    "property": "worker.age",
                                    "operator": ">=",
                                    "value": 18 + index % 5,
                                }
                            ),
                            schema_version=1,
                            created_by="benchmark-generator",
                        )
                        for index, rule in enumerate(rules)
                    ],
                    batch_size=batch_size,
                )
                created_rules += len(rules)

                contracts_created = 0
                while contracts_created < contracts_per_tenant:
                    count = min(batch_size, contracts_per_tenant - contracts_created)
                    start_index = contracts_created
                    Contract.objects.bulk_create(
                        [
                            Contract(
                                tenant=tenant,
                                company=companies[index % len(companies)],
                                worker=workers[index % len(workers)],
                                start_date=now - timedelta(days=30 + index % 365),
                                end_date=now + timedelta(days=365 + index % 365),
                                country=("DE" if index % 2 == 0 else "FR"),
                            )
                            for index in range(start_index, start_index + count)
                        ],
                        batch_size=batch_size,
                    )
                    contracts_created += count
                created_contracts += contracts_created

            self.stdout.write(
                f"tenant {tenant_index + 1}/{tenant_count}: "
                f"{rules_per_tenant} rules, {contracts_per_tenant} contracts"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Finished {prefix}: {created_tenants} tenants created; "
                f"{created_rules} rule versions and {created_contracts} contracts inserted."
            )
        )
