"""Registry, validation and interpretation, independent of persistence."""

from __future__ import annotations

import calendar
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, TypedDict, cast

ValueType = Literal["string", "integer", "country", "date", "decimal", "boolean", "timestamp"]
Scalar = str | int | bool | Decimal | date | datetime
JsonScalar = str | int | bool


class Condition(TypedDict):
    property: str
    operator: str
    value: JsonScalar


class Group(TypedDict):
    op: Literal["AND", "OR"]
    children: list[Definition]


type Definition = Condition | Group


@dataclass(frozen=True)
class PropertyMetadata:
    """Value type, permitted operators, and fields needed to resolve a property."""

    value_type: ValueType
    operators: frozenset[str]
    context_fields: tuple[str, ...]
    derived: bool = False


_EQUALITY = frozenset({"==", "!="})
_ORDERED = _EQUALITY | frozenset({">", "<", ">=", "<="})
ENTITY_ALIASES = {"user": "worker"}


def canonical_property_name(name: object) -> object:
    if not isinstance(name, str) or "." not in name:
        return name
    entity, property_name = name.split(".", 1)
    canonical_entity = ENTITY_ALIASES.get(entity.lower(), entity.lower())
    return f"{canonical_entity}.{property_name}"


def _property(value_type: ValueType, *fields: str, derived: bool = False) -> PropertyMetadata:
    operators = _EQUALITY if value_type in {"string", "country", "boolean"} else _ORDERED
    return PropertyMetadata(value_type, operators, fields, derived)


PROPERTY_REGISTRY: Mapping[str, PropertyMetadata] = MappingProxyType(
    {
        "worker.name": _property("string", "worker.name"),
        "worker.age": _property("integer", "worker.date_of_birth", derived=True),
        "worker.nationality": _property("country", "worker.nationality"),
        "worker.date_of_birth": _property("date", "worker.date_of_birth"),
        "company.name": _property("string", "company.name"),
        "company.industry": _property("string", "company.industry"),
        "company.country": _property("country", "company.country"),
        "company.bank_deposit": _property("decimal", "company.bank_deposit"),
        "contract.is_active": _property(
            "boolean", "contract.start_date", "contract.end_date", derived=True
        ),
        "contract.start_date": _property("timestamp", "contract.start_date"),
        "contract.end_date": _property("timestamp", "contract.end_date"),
        "contract.country": _property("country", "contract.country"),
    }
)

# ISO 3166-1 alpha-2, including officially assigned territories.
_COUNTRIES = frozenset(
    """
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO
BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ
DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP
GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG
KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML
MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE
PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL
SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM
US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split()
)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z", re.ASCII)
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z",
    re.ASCII,
)


class RuleError(ValueError):
    """Invalid input with a JSON-style path identifying the offending value."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


class TruthValue(StrEnum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"
    NOT_EVALUATED = "NOT_EVALUATED"


@dataclass(frozen=True)
class ConditionTrace:
    path: str
    property: str
    operator: str
    status: TruthValue
    expected: Scalar
    actual: Scalar | None
    reason: str
    via: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvaluationResult:
    status: TruthValue
    conditions: tuple[ConditionTrace, ...]


def _parse(value: object, kind: ValueType, path: str, *, context: bool = False) -> Scalar:
    try:
        if kind == "string" and isinstance(value, str):
            return value
        if kind == "country" and isinstance(value, str):
            code = value.upper()
            if code in _COUNTRIES and value.isascii():
                return code
        if kind == "integer" and type(value) is int:
            return value
        if kind == "boolean" and type(value) is bool:
            return value
        if kind == "decimal" and (
            isinstance(value, str) or (context and isinstance(value, Decimal))
        ):
            number = Decimal(value)
            if number.is_finite():
                return number
        if kind == "date":
            if context and isinstance(value, date) and not isinstance(value, datetime):
                return value
            if isinstance(value, str) and _DATE.fullmatch(value):
                return date.fromisoformat(value)
        if kind == "timestamp":
            if context and isinstance(value, datetime):
                if value.utcoffset() is not None:
                    return value.astimezone(UTC)
            if isinstance(value, str) and _TIMESTAMP.fullmatch(value):
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, InvalidOperation, OverflowError):
        pass
    raise RuleError(path, f"invalid {kind} value")


def _json_value(value: Scalar) -> JsonScalar:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, (date, Decimal)):
        return str(value)
    return value


def validate_definition(
    definition: object, *, max_depth: int = 20, max_nodes: int = 200
) -> Definition:
    """Validate and normalize a definition; errors carry the offending path."""
    for name, limit in (("max_depth", max_depth), ("max_nodes", max_nodes)):
        if type(limit) is not int or limit < 1:
            raise ValueError(f"{name} must be a positive integer")
    count = 0

    def visit(node: object, path: str, depth: int) -> Definition:
        nonlocal count
        if depth > max_depth:
            raise RuleError(path, f"maximum depth {max_depth} exceeded")
        count += 1
        if count > max_nodes:
            raise RuleError(path, f"maximum node count {max_nodes} exceeded")
        if not isinstance(node, dict):
            raise RuleError(path, "node must be an object")
        if "property" in node:
            required = {"property", "operator", "value"}
            group = False
        elif "op" in node:
            required = {"op", "children"}
            group = True
        else:
            raise RuleError(path, "node must have property or op")
        for key in node:
            if key not in required:
                raise RuleError(f"{path}.{key}", "unknown key")
        for key in sorted(required):
            if key not in node:
                raise RuleError(f"{path}.{key}", "required key missing")
        if group:
            op = node["op"]
            if not isinstance(op, str) or op not in {"AND", "OR"}:
                raise RuleError(f"{path}.op", "expected AND or OR")
            children = node["children"]
            if not isinstance(children, list) or not children:
                raise RuleError(f"{path}.children", "expected a non-empty array")
            return {
                "op": cast(Literal["AND", "OR"], op),
                "children": [
                    visit(child, f"{path}.children[{i}]", depth + 1)
                    for i, child in enumerate(children)
                ],
            }
        name = canonical_property_name(node["property"])
        if not isinstance(name, str) or name not in PROPERTY_REGISTRY:
            raise RuleError(f"{path}.property", "unknown property")
        metadata = PROPERTY_REGISTRY[name]
        operator = node["operator"]
        if not isinstance(operator, str) or operator not in metadata.operators:
            raise RuleError(f"{path}.operator", "operator not allowed for property")
        parsed = _parse(node["value"], metadata.value_type, f"{path}.value")
        return {"property": name, "operator": operator, "value": _json_value(parsed)}

    return visit(definition, "$", 1)


def _lookup(context: Mapping[str, object], name: str) -> object:
    section, field = name.split(".")
    value = context.get(section)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise RuleError(f"$.context.{section}", "expected an object")
    return value.get(field)


def _resolve(context: Mapping[str, object], name: str, as_of: datetime) -> Scalar | None:
    section, field = name.split(".")
    container = context.get(section)
    supplied = isinstance(container, Mapping) and field in container
    raw = _lookup(context, name)
    if supplied or not PROPERTY_REGISTRY[name].derived:
        return (
            None
            if raw is None
            else _parse(raw, PROPERTY_REGISTRY[name].value_type, f"$.context.{name}", context=True)
        )
    if name == "worker.age":
        birth = _lookup(context, "worker.date_of_birth")
        if birth is None:
            return None
        dob = cast(date, _parse(birth, "date", "$.context.worker.date_of_birth", context=True))
        today = as_of.date()
        day = min(dob.day, calendar.monthrange(today.year, dob.month)[1])
        return today.year - dob.year - ((today.month, today.day) < (dob.month, day))
    start = _lookup(context, "contract.start_date")
    end = _lookup(context, "contract.end_date")
    if start is None or end is None:
        return None
    start_at = cast(
        datetime,
        _parse(start, "timestamp", "$.context.contract.start_date", context=True),
    )
    end_at = cast(datetime, _parse(end, "timestamp", "$.context.contract.end_date", context=True))
    return start_at <= as_of < end_at


def _compare(actual: Scalar, operator: str, expected: Scalar) -> bool:
    if operator == "==":
        return actual == expected
    if operator == "!=":
        return actual != expected
    # Registry and parser ensure both sides have the same ordered type.
    if operator == ">":
        return actual > expected  # type: ignore[operator]
    if operator == "<":
        return actual < expected  # type: ignore[operator]
    if operator == ">=":
        return actual >= expected  # type: ignore[operator]
    return actual <= expected  # type: ignore[operator]


def evaluate_definition(
    definition: object,
    context: Mapping[str, object],
    as_of: datetime,
    *,
    max_depth: int = 20,
    max_nodes: int = 200,
    via_by_path: Mapping[str, tuple[str, ...]] | None = None,
) -> EvaluationResult:
    """Evaluate one rule at a single aware instant; all conditions are traced."""
    tree = validate_definition(definition, max_depth=max_depth, max_nodes=max_nodes)
    if not isinstance(context, Mapping):
        raise RuleError("$.context", "expected an object")
    if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
        raise RuleError("$.as_of", "expected a timezone-aware timestamp")
    as_of = as_of.astimezone(UTC)
    traces: list[ConditionTrace] = []

    def visit(node: Definition, path: str, skipped: bool = False) -> TruthValue:
        if "property" in node:
            condition = cast(Condition, node)
            name, operator = condition["property"], condition["operator"]
            expected = _parse(
                condition["value"],
                PROPERTY_REGISTRY[name].value_type,
                f"{path}.value",
            )
            actual = None if skipped else _resolve(context, name, as_of)
            if skipped:
                status, reason = TruthValue.NOT_EVALUATED, "short-circuited"
            elif actual is None:
                status, reason = TruthValue.UNKNOWN, "property value is missing"
            elif _compare(actual, operator, expected):
                status, reason = TruthValue.TRUE, "condition matched"
            else:
                status, reason = TruthValue.FALSE, "condition did not match"
            traces.append(
                ConditionTrace(
                    path,
                    name,
                    operator,
                    status,
                    expected,
                    actual,
                    reason,
                    (via_by_path or {}).get(path, ()),
                )
            )
            return status
        group = cast(Group, node)
        is_and = group["op"] == "AND"
        decisive = TruthValue.FALSE if is_and else TruthValue.TRUE
        result = TruthValue.TRUE if is_and else TruthValue.FALSE
        for index, child in enumerate(group["children"]):
            child_result = visit(child, f"{path}.children[{index}]", skipped or result == decisive)
            if child_result == decisive:
                result = decisive
            elif child_result == TruthValue.UNKNOWN and result != decisive:
                result = TruthValue.UNKNOWN
        return TruthValue.NOT_EVALUATED if skipped else result

    result = visit(tree, "$")
    return EvaluationResult(result, tuple(traces))
