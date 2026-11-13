"""Registry-guarded PostgreSQL predicates for eligible-contract queries."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import cast

from domain.engine.engine import (
    PROPERTY_REGISTRY,
    Definition,
    Group,
    RuleError,
    Scalar,
    _parse,
    validate_definition,
)

_SQL_COLUMNS = {
    "worker.name": "w.name",
    "worker.nationality": "w.nationality",
    "worker.date_of_birth": "w.date_of_birth",
    "company.name": "co.name",
    "company.industry": "co.industry",
    "company.country": "co.country",
    "company.bank_deposit": "co.bank_deposit",
    "contract.start_date": "c.start_date",
    "contract.end_date": "c.end_date",
    "contract.country": "c.country",
}
_OPERATORS = {"==": "=", "!=": "<>", ">": ">", "<": "<", ">=": ">=", "<=": "<="}


@dataclass(frozen=True)
class SQLFragment:
    sql: str
    params: tuple[Scalar, ...]


def _anniversary(year: int, month: int, day: int) -> date:
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1


def _age_predicate(operator: str, age: int, as_of: datetime) -> SQLFragment:
    birth_date = "w.date_of_birth"
    as_of_date = as_of.astimezone(UTC).date()

    def cutoff(years: int) -> date:
        return _anniversary(
            as_of_date.year - years,
            as_of_date.month,
            as_of_date.day,
        )

    if operator == "==":
        return SQLFragment(
            f"({birth_date} > %s AND {birth_date} <= %s)",
            (cutoff(age + 1), cutoff(age)),
        )
    if operator == "!=":
        return SQLFragment(
            f"NOT ({birth_date} > %s AND {birth_date} <= %s)",
            (cutoff(age + 1), cutoff(age)),
        )
    if operator == ">":
        return SQLFragment(f"{birth_date} <= %s", (cutoff(age + 1),))
    if operator == ">=":
        return SQLFragment(f"{birth_date} <= %s", (cutoff(age),))
    if operator == "<":
        return SQLFragment(f"{birth_date} > %s", (cutoff(age),))
    return SQLFragment(f"{birth_date} > %s", (cutoff(age + 1),))


def _node(node: Definition, as_of: datetime) -> SQLFragment:
    if "property" in node:
        condition = cast(dict[str, object], node)
        name, operator = condition["property"], condition["operator"]
        if not isinstance(name, str) or not isinstance(operator, str):
            raise RuleError("$", "malformed condition")
        metadata = PROPERTY_REGISTRY[name]
        if operator not in metadata.operators:
            raise RuleError("$.operator", "operator not allowed for property")
        if name == "worker.age":
            age = cast(int, _parse(condition["value"], "integer", "$.value"))
            return _age_predicate(operator, age, as_of)
        if name == "contract.is_active":
            expected = cast(bool, _parse(condition["value"], "boolean", "$.value"))
            active = "(c.start_date <= %s AND c.end_date > %s)"
            if (operator == "==" and expected) or (operator == "!=" and not expected):
                return SQLFragment(active, (as_of, as_of))
            return SQLFragment(f"NOT {active}", (as_of, as_of))
        column = _SQL_COLUMNS[name]
        value = _parse(condition["value"], metadata.value_type, "$.value")
        return SQLFragment(f"{column} {_OPERATORS[operator]} %s", (value,))

    group = cast(Group, node)
    fragments = [_node(child, as_of) for child in group["children"]]
    operator = " AND " if group["op"] == "AND" else " OR "
    return SQLFragment(
        "(" + operator.join(fragment.sql for fragment in fragments) + ")",
        tuple(value for fragment in fragments for value in fragment.params),
    )


def compile_definition(definition: object, as_of: datetime) -> SQLFragment:
    """Compile a validated rule tree to SQL with all caller values parameterized."""
    if as_of.utcoffset() is None:
        raise RuleError("$.as_of", "expected a timezone-aware timestamp")
    tree = validate_definition(definition)
    return _node(tree, as_of.astimezone(UTC))


def compile_all(definitions: list[object], as_of: datetime) -> SQLFragment:
    """Compile an AND check over its pinned rule definitions."""
    if not definitions:
        return SQLFragment("TRUE", ())
    fragments = [compile_definition(definition, as_of) for definition in definitions]
    return SQLFragment(
        "(" + " AND ".join(fragment.sql for fragment in fragments) + ")",
        tuple(value for fragment in fragments for value in fragment.params),
    )
