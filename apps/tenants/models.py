import uuid

from django.db import models


class Tenant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tenants_tenant"

    def __str__(self) -> str:
        return self.name


class APIKey(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    key_hash = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tenants_api_key"
        indexes = [models.Index(fields=["tenant", "active"], name="api_key_tenant_active_idx")]

    def __str__(self) -> str:
        return f"{self.name} ({self.tenant_id})"
