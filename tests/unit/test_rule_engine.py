import ast
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from domain.engine import (
    PROPERTY_REGISTRY,
    RuleError,
    TruthValue,
    evaluate_definition,
    validate_definition,
)
from domain.engine.engine import Condition, Group

NOW = datetime(2024, 3, 1, tzinfo=UTC)


def condition(name="worker.age", operator=">=", value=18):
    return {"property": name, "operator": operator, "value": value}


def group(op, *children):
    return {"op": op, "children": list(children)}


@pytest.mark.parametrize(
    ("operator", "value", "expected"),
    [
        ("==", 20, True),
        ("!=", 19, True),
        (">", 19, True),
        ("<", 21, True),
        (">=", 20, True),
        ("<=", 20, True),
        (">", 20, False),
        ("<", 20, False),
    ],
)
def test_six_operators(operator, value, expected):
    result = evaluate_definition(
        condition(operator=operator, value=value), {"worker": {"age": 20}}, NOW
    )
    assert result.status == (TruthValue.TRUE if expected else TruthValue.FALSE)
    trace = result.conditions[0]
    assert (trace.actual, trace.expected, trace.path) == (20, value, "$")
    assert trace.reason


@pytest.mark.parametrize(
    ("definition", "path"),
    [
        (condition("worker.unknown"), "$.property"),
        (condition("worker.name", ">=", "John"), "$.operator"),
        (condition("worker.age", "==", True), "$.value"),
        (condition("worker.age", "==", 18.0), "$.value"),
        (condition("worker.name", "==", 1), "$.value"),
        (condition("company.bank_deposit", "==", 1.0), "$.value"),
        (condition("company.bank_deposit", "==", "NaN"), "$.value"),
        (condition("contract.is_active", "==", "true"), "$.value"),
        (condition("company.country", "==", "ZZ"), "$.value"),
        (condition("worker.date_of_birth", "==", "2024-02-30"), "$.value"),
        (condition("contract.start_date", "==", "2024-01-01T00:00:00+01:00"), "$.value"),
        (condition("contract.start_date", "==", "2024-01-01T00:00:00"), "$.value"),
        ({"property": "worker.age", "operator": "=="}, "$.value"),
        ({**condition(), "extra": 1}, "$.extra"),
        ({"op": "AND", "children": [], "extra": 1}, "$.extra"),
        (group("X", condition()), "$.op"),
        (group("AND"), "$.children"),
        ({"op": "AND", "children": {}}, "$.children"),
        (
            group("AND", {"op": "OR", "children": [condition("bad")]}),
            "$.children[0].children[0].property",
        ),
        ([], "$"),
    ],
)
def test_validation_errors_have_paths(definition, path):
    with pytest.raises(RuleError) as exc:
        validate_definition(definition)
    assert exc.value.path == path
    assert path in str(exc.value)


def test_normalization_and_typed_context():
    definition = cast(
        Group,
        group(
            "AND",
            condition("worker.nationality", "==", "gb"),
            condition("company.bank_deposit", ">=", "100.50"),
            condition("worker.date_of_birth", "==", "2000-02-29"),
            condition("contract.start_date", "==", "2024-01-01T00:00:00+00:00"),
            condition("contract.is_active", "==", True),
        ),
    )
    normalized = cast(Group, validate_definition(definition))
    assert cast(Condition, normalized["children"][0])["value"] == "GB"
    assert cast(Condition, normalized["children"][3])["value"] == "2024-01-01T00:00:00Z"
    assert cast(Condition, definition["children"][0])["value"] == "gb"
    context = {
        "worker": {"nationality": "Gb", "date_of_birth": date(2000, 2, 29)},
        "company": {"bank_deposit": Decimal("100.50")},
        "contract": {"start_date": datetime(2024, 1, 1, tzinfo=UTC), "is_active": True},
    }
    result = evaluate_definition(normalized, context, NOW)
    assert result.status == TruthValue.TRUE
    assert result.conditions[1].actual == Decimal("100.50")
    assert result.conditions[2].expected == date(2000, 2, 29)
    assert result.conditions[3].expected == datetime(2024, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("property_name", ["User.age", "user.age", "WORKER.age"])
def test_user_and_case_insensitive_entity_aliases_are_canonical(property_name):
    normalized = validate_definition(condition(property_name, ">=", 18))
    assert normalized == condition("worker.age", ">=", 18)
    result = evaluate_definition(
        condition(property_name, ">=", 18),
        {"worker": {"age": 20}},
        NOW,
    )
    assert result.status == TruthValue.TRUE
    assert result.conditions[0].property == "worker.age"


@pytest.mark.parametrize(
    ("op", "a", "b", "result"),
    [
        ("AND", True, True, "TRUE"),
        ("AND", True, None, "UNKNOWN"),
        ("AND", None, True, "UNKNOWN"),
        ("AND", None, None, "UNKNOWN"),
        ("AND", None, False, "FALSE"),
        ("AND", False, None, "FALSE"),
        ("OR", False, False, "FALSE"),
        ("OR", False, None, "UNKNOWN"),
        ("OR", None, False, "UNKNOWN"),
        ("OR", None, None, "UNKNOWN"),
        ("OR", None, True, "TRUE"),
        ("OR", True, None, "TRUE"),
    ],
)
def test_three_valued_truth_tables(op, a, b, result):
    definition = group(
        op,
        condition("contract.is_active", "==", True),
        condition("contract.is_active", "==", False),
    )
    # Distinct values permit each operand to independently be true, false or unknown.
    definition["children"][1] = condition("worker.age", "==", 1)
    context = {"contract": {"is_active": a}, "worker": {"age": None if b is None else int(b)}}
    assert evaluate_definition(definition, context, NOW).status == result


def test_nested_short_circuit_traces_include_every_condition():
    definition = group(
        "AND",
        condition("worker.age", ">", 18),
        group(
            "OR",
            condition("company.name", "==", "x"),
            group(
                "AND",
                condition("worker.nationality", "==", "gb"),
                condition("contract.is_active", "==", True),
            ),
        ),
    )
    result = evaluate_definition(definition, {"worker": {"age": 10}, "company": "invalid"}, NOW)
    assert result.status == TruthValue.FALSE
    assert [t.status for t in result.conditions] == [
        TruthValue.FALSE,
        TruthValue.NOT_EVALUATED,
        TruthValue.NOT_EVALUATED,
        TruthValue.NOT_EVALUATED,
    ]
    assert result.conditions[2].path == "$.children[1].children[1].children[0]"
    assert result.conditions[2].expected == "GB"
    assert result.conditions[2].actual is None
    assert result.conditions[2].reason == "short-circuited"


def test_nested_unknown_then_decisive_and_or_short_circuit():
    tree = group(
        "OR",
        group("AND", condition("worker.age", ">", 18), condition("company.name", "==", "A")),
        condition("contract.is_active", "==", True),
        condition("worker.name", "==", "unused"),
    )
    result = evaluate_definition(
        tree, {"company": {"name": "A"}, "contract": {"is_active": True}}, NOW
    )
    assert result.status == TruthValue.TRUE
    assert [t.status for t in result.conditions] == [
        TruthValue.UNKNOWN,
        TruthValue.TRUE,
        TruthValue.TRUE,
        TruthValue.NOT_EVALUATED,
    ]


def test_limits_include_root_and_all_nodes():
    tree = group("AND", group("OR", condition()))
    assert validate_definition(tree, max_depth=3, max_nodes=3)
    with pytest.raises(RuleError, match="depth") as exc:
        validate_definition(tree, max_depth=2)
    assert exc.value.path == "$.children[0].children[0]"
    with pytest.raises(RuleError, match="node count") as exc:
        validate_definition(tree, max_nodes=2)
    assert exc.value.path == "$.children[0].children[0]"
    with pytest.raises(ValueError):
        validate_definition(tree, max_nodes=True)
    wide = group("AND", *(condition() for _ in range(200)))
    with pytest.raises(RuleError, match="node count"):
        validate_definition(wide)
    assert validate_definition(wide, max_nodes=201)


@pytest.mark.parametrize(
    ("birth", "instant", "age"),
    [
        ("2000-02-29", datetime(2023, 2, 27, tzinfo=UTC), 22),
        ("2000-02-29", datetime(2023, 2, 28, tzinfo=UTC), 23),
        ("2000-02-29", datetime(2024, 2, 28, tzinfo=UTC), 23),
        ("2000-02-29", datetime(2024, 2, 29, tzinfo=UTC), 24),
        ("2000-03-01", datetime(2023, 2, 28, tzinfo=UTC), 22),
        ("2000-03-01", datetime(2023, 3, 1, tzinfo=UTC), 23),
    ],
)
def test_completed_age_and_leap_day(birth, instant, age):
    result = evaluate_definition(
        condition("worker.age", "==", age), {"worker": {"date_of_birth": birth}}, instant
    )
    assert result.status == TruthValue.TRUE
    assert result.conditions[0].actual == age


@pytest.mark.parametrize(
    ("instant", "active"),
    [
        (datetime(2024, 1, 1, tzinfo=UTC) - timedelta(microseconds=1), False),
        (datetime(2024, 1, 1, tzinfo=UTC), True),
        (datetime(2024, 2, 1, tzinfo=UTC) - timedelta(microseconds=1), True),
        (datetime(2024, 2, 1, tzinfo=UTC), False),
    ],
)
def test_active_half_open_interval(instant, active):
    context = {
        "contract": {"start_date": "2024-01-01T00:00:00Z", "end_date": "2024-02-01T00:00:00Z"}
    }
    result = evaluate_definition(condition("contract.is_active", "==", active), context, instant)
    assert result.status == TruthValue.TRUE


def test_missing_and_invalid_context_and_as_of():
    rule = condition("contract.is_active", "==", True)
    assert evaluate_definition(rule, {"contract": {"start_date": NOW}}, NOW).status == "UNKNOWN"
    assert evaluate_definition(condition(), {}, NOW).status == "UNKNOWN"
    assert evaluate_definition(condition(), {"worker": {"age": None}}, NOW).status == "UNKNOWN"
    with pytest.raises(RuleError, match=r"\$\.context\.worker\.age"):
        evaluate_definition(condition(), {"worker": {"age": "18"}}, NOW)
    with pytest.raises(RuleError, match=r"\$\.as_of"):
        evaluate_definition(condition(), {}, datetime(2024, 1, 1))
    offset = timezone(timedelta(hours=2))
    result = evaluate_definition(
        condition("worker.age", "==", 23),
        {"worker": {"date_of_birth": "2000-03-01"}},
        datetime(2024, 3, 1, 1, tzinfo=offset),
    )
    assert result.status == TruthValue.TRUE


def test_registry_metadata_and_no_django_imports():
    assert PROPERTY_REGISTRY["worker.age"].context_fields == ("worker.date_of_birth",)
    assert PROPERTY_REGISTRY["contract.is_active"].context_fields == (
        "contract.start_date",
        "contract.end_date",
    )
    assert PROPERTY_REGISTRY["worker.age"].derived
    engine = Path(__file__).parents[2] / "domain" / "engine"
    for source in engine.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Import):
                assert all(
                    not (alias.name == "django" or alias.name.startswith("django."))
                    for alias in node.names
                )
            if isinstance(node, ast.ImportFrom):
                assert not (node.module == "django" or (node.module or "").startswith("django."))
