from django.urls import path

from apps.evaluation.api import EvaluateCheckView
from apps.evaluation.bulk_api import EvaluationJobProgressView, StartBulkEvaluationView
from apps.evaluation.eligibility import EligibleContractsView

urlpatterns = [
    path(
        "compliance-checks/<int:check_id>/evaluate",
        EvaluateCheckView.as_view(),
        name="evaluate-check",
    ),
    path(
        "compliance-checks/<int:check_id>/evaluate-bulk",
        StartBulkEvaluationView.as_view(),
        name="evaluate-check-bulk",
    ),
    path(
        "evaluation-jobs/<int:job_id>",
        EvaluationJobProgressView.as_view(),
        name="evaluation-job-progress",
    ),
    path(
        "companies/<int:company_id>/eligible-contracts",
        EligibleContractsView.as_view(),
        name="eligible-contracts",
    ),
]
