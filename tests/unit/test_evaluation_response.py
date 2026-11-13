from datetime import UTC, datetime

import pytest

from apps.evaluation.api import EvaluationOutputSerializer


@pytest.mark.parametrize(
    ("outcome", "compliant"),
    [
        ("COMPLIANT", True),
        ("NON_COMPLIANT", False),
        ("INDETERMINATE", False),
    ],
)
def test_compliant_flag_is_derived_from_outcome(outcome, compliant):
    payload = {
        "evaluation_id": 1,
        "outcome": outcome,
        "as_of": datetime(2026, 11, 13, tzinfo=UTC),
        "summary": {"rules": 1, "passed": 0, "failed": 0, "unknown": 1},
        "results": [],
    }

    assert EvaluationOutputSerializer(payload).data["compliant"] is compliant
