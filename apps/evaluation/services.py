import logging
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from apps.directory.models import Contract
from apps.evaluation.models import CheckVersion, CheckVersionRule, EvaluationResult
from apps.rules.services import get_expanded_rule_definition
from domain.engine import EvaluationResult as EngineResult
from domain.engine import TruthValue, evaluate_definition

logger = logging.getLogger(__name__)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    return value


def _trace_payload(result: EngineResult) -> list[dict[str, Any]]:
    payload = []
    for item in result.conditions:
        condition = {
            "path": item.path,
            "property": item.property,
            "operator": item.operator,
            "expected": _json_value(item.expected),
            "actual": _json_value(item.actual),
            "status": item.status.value,
            "reason": item.reason,
        }
        if item.via:
            condition["via"] = list(item.via)
        payload.append(condition)
    return payload


def evaluate_check_version(
    check_version: CheckVersion,
    contract: Contract,
    as_of: datetime,
    *,
    pins: Sequence[CheckVersionRule] | None = None,
) -> dict[str, Any]:
    if pins is None:
        pins = list(
            CheckVersionRule.objects.filter(
                tenant_id=check_version.tenant_id,
                check_version_id=check_version.id,
            )
            .select_related("rule_version", "rule_version__rule")
            .order_by("position")
        )

    context = {
        "worker": {
            "name": contract.worker.name,
            "date_of_birth": contract.worker.date_of_birth,
            "nationality": contract.worker.nationality,
        },
        "company": {
            "name": contract.company.name,
            "industry": contract.company.industry,
            "country": contract.company.country,
            "bank_deposit": contract.company.bank_deposit,
        },
        "contract": {
            "start_date": contract.start_date,
            "end_date": contract.end_date,
            "country": contract.country,
        },
    }

    results = []
    for pin in pins:
        rule_version = pin.rule_version
        expanded = get_expanded_rule_definition(rule_version)
        engine_result = evaluate_definition(
            expanded.definition,
            context,
            as_of,
            via_by_path=expanded.provenance,
        )
        if expanded.reference_count:
            logger.info(
                "Evaluated expanded rule references",
                extra={
                    "tenant_id": str(rule_version.tenant_id),
                    "rule_id": rule_version.rule_id,
                    "rule_version": rule_version.version,
                    "reference_count": expanded.reference_count,
                    "expanded_node_count": expanded.node_count,
                },
            )
        results.append(
            {
                "rule_id": rule_version.rule_id,
                "version": rule_version.version,
                "status": {
                    TruthValue.TRUE: "PASSED",
                    TruthValue.FALSE: "FAILED",
                    TruthValue.UNKNOWN: "UNKNOWN",
                }[engine_result.status],
                "conditions": _trace_payload(engine_result),
            }
        )

    passed = sum(item["status"] == "PASSED" for item in results)
    failed = sum(item["status"] == "FAILED" for item in results)
    unknown = sum(item["status"] == "UNKNOWN" for item in results)
    outcome = (
        EvaluationResult.Outcome.NON_COMPLIANT
        if failed
        else EvaluationResult.Outcome.INDETERMINATE
        if unknown
        else EvaluationResult.Outcome.COMPLIANT
    )
    return {
        "outcome": outcome,
        "summary": {
            "rules": len(results),
            "passed": passed,
            "failed": failed,
            "unknown": unknown,
        },
        "results": results,
    }
