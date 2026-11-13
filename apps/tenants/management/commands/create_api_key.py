import hashlib
import secrets
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.tenants.models import APIKey, Tenant


class Command(BaseCommand):
    help = "Create a tenant API key and print its secret once."

    def add_arguments(self, parser):
        parser.add_argument("tenant_id", type=uuid.UUID)
        parser.add_argument("--name", default="local")

    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]
        if not Tenant.objects.filter(pk=tenant_id).exists():
            raise CommandError(f"Tenant {tenant_id} does not exist")
        raw_key = f"{tenant_id}.{secrets.token_urlsafe(32)}"
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true)",
                    [str(tenant_id)],
                )
            APIKey.objects.create(
                tenant_id=tenant_id,
                key_hash=hashlib.sha256(raw_key.encode()).hexdigest(),
                name=options["name"],
            )
        self.stdout.write(self.style.SUCCESS(f"API key (shown once): {raw_key}"))
