from django.db import models
from django.db.models import F, Q

from apps.directory.countries import COUNTRY_CHOICES, COUNTRY_CODES
from apps.tenants.models import Tenant


class Worker(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    date_of_birth = models.DateField()
    nationality = models.CharField(max_length=2, choices=COUNTRY_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "directory_worker"
        constraints = [
            models.UniqueConstraint(fields=["id", "tenant"], name="worker_id_tenant_uniq"),
            models.CheckConstraint(
                condition=Q(nationality__in=COUNTRY_CODES), name="worker_nationality_valid"
            ),
        ]
        indexes = [models.Index(fields=["tenant", "date_of_birth"], name="worker_tenant_dob_idx")]

    def __str__(self) -> str:
        return self.name


class Company(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    industry = models.CharField(max_length=200)
    country = models.CharField(max_length=2, choices=COUNTRY_CHOICES)
    bank_deposit = models.DecimalField(max_digits=18, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "directory_company"
        constraints = [
            models.UniqueConstraint(fields=["id", "tenant"], name="company_id_tenant_uniq"),
            models.CheckConstraint(
                condition=Q(country__in=COUNTRY_CODES), name="company_country_valid"
            ),
            models.CheckConstraint(
                condition=Q(bank_deposit__gte=0), name="company_deposit_nonnegative"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Contract(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    worker = models.ForeignKey(Worker, on_delete=models.PROTECT)
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    country = models.CharField(max_length=2, choices=COUNTRY_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "directory_contract"
        constraints = [
            models.UniqueConstraint(fields=["id", "tenant"], name="contract_id_tenant_uniq"),
            models.CheckConstraint(
                condition=Q(end_date__gt=F("start_date")), name="contract_valid_dates"
            ),
            models.CheckConstraint(
                condition=Q(country__in=COUNTRY_CODES), name="contract_country_valid"
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "company", "id"], name="contract_tenant_company_idx"),
        ]

    def __str__(self) -> str:
        return f"Contract #{self.pk}"
