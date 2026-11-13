from django.urls import path

from apps.directory.api import (
    CompanyCollectionView,
    CompanyDetailView,
    ContractCollectionView,
    ContractDetailView,
    WorkerCollectionView,
    WorkerDetailView,
)

urlpatterns = [
    path("workers", WorkerCollectionView.as_view(), name="worker-list-create"),
    path("workers/<int:worker_id>", WorkerDetailView.as_view(), name="worker-detail"),
    path("companies", CompanyCollectionView.as_view(), name="company-list-create"),
    path("companies/<int:company_id>", CompanyDetailView.as_view(), name="company-detail"),
    path("contracts", ContractCollectionView.as_view(), name="contract-list-create"),
    path("contracts/<int:contract_id>", ContractDetailView.as_view(), name="contract-detail"),
]
