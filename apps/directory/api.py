from decimal import Decimal

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ParseError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.directory.countries import COUNTRY_CHOICES
from apps.directory.models import Company, Contract, Worker
from apps.tenants.cursors import decode_eligibility_cursor, encode_eligibility_cursor
from apps.tenants.schema import PAGINATION_PARAMETERS


class WorkerWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    date_of_birth = serializers.DateField()
    nationality = serializers.ChoiceField(choices=COUNTRY_CHOICES)


class WorkerOutputSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    date_of_birth = serializers.DateField()
    nationality = serializers.CharField()
    created_at = serializers.DateTimeField()


class CompanyWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    industry = serializers.CharField(max_length=200)
    country = serializers.ChoiceField(choices=COUNTRY_CHOICES)
    bank_deposit = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        min_value=Decimal("0.00"),
    )


class CompanyOutputSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    industry = serializers.CharField()
    country = serializers.CharField()
    bank_deposit = serializers.DecimalField(max_digits=18, decimal_places=2)
    created_at = serializers.DateTimeField()


class ContractWriteSerializer(serializers.Serializer):
    worker_id = serializers.IntegerField(min_value=1)
    company_id = serializers.IntegerField(min_value=1)
    start_date = serializers.DateTimeField()
    end_date = serializers.DateTimeField()
    country = serializers.ChoiceField(choices=COUNTRY_CHOICES)

    def validate(self, attrs):
        if attrs["end_date"] <= attrs["start_date"]:
            raise serializers.ValidationError(
                {"end_date": "Must be later than start_date."}
            )
        tenant_id = _tenant_id(self.context["request"])
        try:
            attrs["worker"] = Worker.objects.get(
                tenant_id=tenant_id,
                id=attrs.pop("worker_id"),
            )
            attrs["company"] = Company.objects.get(
                tenant_id=tenant_id,
                id=attrs.pop("company_id"),
            )
        except (Worker.DoesNotExist, Company.DoesNotExist) as exc:
            raise NotFound("Contract party not found.", code="CONTRACT_NOT_FOUND") from exc
        return attrs


class ContractOutputSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    worker_id = serializers.IntegerField()
    company_id = serializers.IntegerField()
    start_date = serializers.DateTimeField()
    end_date = serializers.DateTimeField()
    country = serializers.CharField()
    created_at = serializers.DateTimeField()


class WorkerPageSerializer(serializers.Serializer):
    results = WorkerOutputSerializer(many=True)
    next_cursor = serializers.CharField(allow_null=True)


class CompanyPageSerializer(serializers.Serializer):
    results = CompanyOutputSerializer(many=True)
    next_cursor = serializers.CharField(allow_null=True)


class ContractPageSerializer(serializers.Serializer):
    results = ContractOutputSerializer(many=True)
    next_cursor = serializers.CharField(allow_null=True)


def _tenant_id(request: Request) -> str:
    tenant_id = getattr(request, "tenant_id", None)
    if not tenant_id:
        raise NotFound("Tenant not found.", code="TENANT_ACCESS_DENIED")
    return str(tenant_id)


def _page(
    queryset: QuerySet,
    request: Request,
    tenant_id: str,
    resource: str,
    serializer_class: type[serializers.Serializer],
) -> Response:
    raw_limit = request.query_params.get("limit", "100")
    try:
        limit = int(raw_limit)
    except ValueError as exc:
        raise ParseError("limit must be an integer.", code="MALFORMED_REQUEST") from exc
    if not 1 <= limit <= 1000:
        raise ParseError("limit must be between 1 and 1000.", code="MALFORMED_REQUEST")
    scope = {"tenant_id": tenant_id, "resource": resource}
    cursor = request.query_params.get("cursor")
    after_id = decode_eligibility_cursor(cursor, scope=scope) if cursor else 0
    page = list(queryset.filter(id__gt=after_id).order_by("id")[: limit + 1])
    has_more = len(page) > limit
    page = page[:limit]
    next_cursor = (
        encode_eligibility_cursor(page[-1].id, scope)
        if has_more and page
        else None
    )
    return Response(
        {
            "results": serializer_class(page, many=True).data,
            "next_cursor": next_cursor,
        }
    )


class WorkerCollectionView(APIView):
    @extend_schema(
        operation_id="workers_list",
        responses={200: WorkerPageSerializer},
        parameters=PAGINATION_PARAMETERS,
    )
    def get(self, request: Request) -> Response:
        tenant_id = _tenant_id(request)
        return _page(
            Worker.objects.filter(tenant_id=tenant_id),
            request,
            tenant_id,
            "workers",
            WorkerOutputSerializer,
        )

    @extend_schema(
        operation_id="workers_create",
        request=WorkerWriteSerializer,
        responses={201: WorkerOutputSerializer},
    )
    def post(self, request: Request) -> Response:
        serializer = WorkerWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        worker = Worker.objects.create(
            tenant_id=_tenant_id(request),
            **serializer.validated_data,
        )
        return Response(WorkerOutputSerializer(worker).data, status=status.HTTP_201_CREATED)


class WorkerDetailView(APIView):
    @extend_schema(operation_id="workers_retrieve", responses={200: WorkerOutputSerializer})
    def get(self, request: Request, worker_id: int) -> Response:
        try:
            worker = Worker.objects.get(tenant_id=_tenant_id(request), id=worker_id)
        except Worker.DoesNotExist as exc:
            raise NotFound("Worker not found.", code="WORKER_NOT_FOUND") from exc
        return Response(WorkerOutputSerializer(worker).data)


class CompanyCollectionView(APIView):
    @extend_schema(
        operation_id="companies_list",
        responses={200: CompanyPageSerializer},
        parameters=PAGINATION_PARAMETERS,
    )
    def get(self, request: Request) -> Response:
        tenant_id = _tenant_id(request)
        return _page(
            Company.objects.filter(tenant_id=tenant_id),
            request,
            tenant_id,
            "companies",
            CompanyOutputSerializer,
        )

    @extend_schema(
        operation_id="companies_create",
        request=CompanyWriteSerializer,
        responses={201: CompanyOutputSerializer},
    )
    def post(self, request: Request) -> Response:
        serializer = CompanyWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        company = Company.objects.create(
            tenant_id=_tenant_id(request),
            **serializer.validated_data,
        )
        return Response(CompanyOutputSerializer(company).data, status=status.HTTP_201_CREATED)


class CompanyDetailView(APIView):
    @extend_schema(operation_id="companies_retrieve", responses={200: CompanyOutputSerializer})
    def get(self, request: Request, company_id: int) -> Response:
        try:
            company = Company.objects.get(tenant_id=_tenant_id(request), id=company_id)
        except Company.DoesNotExist as exc:
            raise NotFound("Company not found.", code="COMPANY_NOT_FOUND") from exc
        return Response(CompanyOutputSerializer(company).data)


class ContractCollectionView(APIView):
    @extend_schema(
        operation_id="contracts_list",
        responses={200: ContractPageSerializer},
        parameters=PAGINATION_PARAMETERS,
    )
    def get(self, request: Request) -> Response:
        tenant_id = _tenant_id(request)
        return _page(
            Contract.objects.filter(tenant_id=tenant_id),
            request,
            tenant_id,
            "contracts",
            ContractOutputSerializer,
        )

    @extend_schema(
        operation_id="contracts_create",
        request=ContractWriteSerializer,
        responses={201: ContractOutputSerializer},
    )
    def post(self, request: Request) -> Response:
        serializer = ContractWriteSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        contract = Contract.objects.create(
            tenant_id=_tenant_id(request),
            **serializer.validated_data,
        )
        return Response(ContractOutputSerializer(contract).data, status=status.HTTP_201_CREATED)


class ContractDetailView(APIView):
    @extend_schema(operation_id="contracts_retrieve", responses={200: ContractOutputSerializer})
    def get(self, request: Request, contract_id: int) -> Response:
        try:
            contract = Contract.objects.get(tenant_id=_tenant_id(request), id=contract_id)
        except Contract.DoesNotExist as exc:
            raise NotFound("Contract not found.", code="CONTRACT_NOT_FOUND") from exc
        return Response(ContractOutputSerializer(contract).data)
