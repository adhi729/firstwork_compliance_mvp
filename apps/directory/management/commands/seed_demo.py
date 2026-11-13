from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.directory.models import Company, Contract, Worker
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Create a small repeatable tenant directory fixture."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-name", default="FirstWork Demo")
        parser.add_argument("--workers", type=int, default=20)
        parser.add_argument("--companies", type=int, default=5)

    @transaction.atomic
    def handle(self, *args, **options):
        tenant_name = options["tenant_name"]
        tenant = Tenant.objects.filter(name=tenant_name).first()
        if tenant is None:
            tenant = Tenant.objects.create(name=tenant_name)

        with transaction.atomic():
            from django.db import connection

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true)",
                    [str(tenant.id)],
                )
            companies = [
                Company.objects.create(
                    tenant=tenant,
                    name=f"Demo Company {index + 1}",
                    industry="technology",
                    country="US",
                    bank_deposit=Decimal("100000.00") + index,
                )
                for index in range(options["companies"])
            ]
            workers = [
                Worker.objects.create(
                    tenant=tenant,
                    name=f"Demo Worker {index + 1}",
                    date_of_birth=date(1980 + index % 20, 1 + index % 12, 1),
                    nationality="US",
                )
                for index in range(options["workers"])
            ]
            now = datetime.now(UTC)
            contracts = [
                Contract.objects.create(
                    tenant=tenant,
                    company=companies[index % len(companies)],
                    worker=workers[index % len(workers)],
                    start_date=now - timedelta(days=30),
                    end_date=now + timedelta(days=365),
                    country="US",
                )
                for index in range(max(options["companies"], options["workers"]))
            ]
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded tenant {tenant.id} with {len(workers)} workers, "
                f"{len(companies)} companies and {len(contracts)} contracts."
            )
        )
