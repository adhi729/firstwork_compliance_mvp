from django.urls import path

from apps.rules.api import (
    CheckCollectionView,
    CheckDetailView,
    RuleArchiveView,
    RuleCollectionView,
    RuleDependentsView,
    RuleDetailView,
    RuleVersionsView,
)

urlpatterns = [
    path("rules", RuleCollectionView.as_view(), name="rule-list-create"),
    path("rules/<int:rule_id>", RuleDetailView.as_view(), name="rule-detail"),
    path("rules/<int:rule_id>/versions", RuleVersionsView.as_view(), name="rule-versions"),
    path("rules/<int:rule_id>/dependents", RuleDependentsView.as_view(), name="rule-dependents"),
    path("rules/<int:rule_id>/archive", RuleArchiveView.as_view(), name="rule-archive"),
    path("compliance-checks", CheckCollectionView.as_view(), name="check-list-create"),
    path(
        "compliance-checks/<int:check_id>",
        CheckDetailView.as_view(),
        name="check-detail",
    ),
]
