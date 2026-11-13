from dataclasses import dataclass
from functools import reduce
from operator import or_

from django.conf import settings
from django.db.models import Q

from apps.rules.models import Rule, RuleDependency, RuleVersion
from apps.tenants.errors import RuleDefinitionAPIError
from domain.engine import RuleError, validate_definition
from domain.engine.engine import canonical_property_name

_MAX_DEPTH = 20
_MAX_NODES = 200


@dataclass(frozen=True)
class ExpandedRuleDefinition:
    definition: object
    provenance: dict[str, tuple[str, ...]]
    reference_count: int
    node_count: int
    cache_hit: bool = False


def _reference_error(path: str, message: str) -> RuleDefinitionAPIError:
    return RuleDefinitionAPIError(
        f"{path}: {message}",
        code="INVALID_RULE_REFERENCE",
    )


def _with_path(path: str, error: RuleError) -> RuleError:
    suffix = error.path[1:] if error.path.startswith("$") else f".{error.path}"
    return RuleError(f"{path}{suffix}", error.message)


def normalize_rule_definition(definition: object) -> object:
    """Normalize property aliases and validate syntax while preserving rule references."""
    count = 0

    def visit(node: object, path: str, depth: int, *, allow_reference: bool) -> object:
        nonlocal count
        if depth > _MAX_DEPTH:
            raise RuleError(path, f"maximum depth {_MAX_DEPTH} exceeded")
        count += 1
        if count > _MAX_NODES:
            raise RuleError(path, f"maximum node count {_MAX_NODES} exceeded")
        if not isinstance(node, dict):
            raise RuleError(path, "node must be an object")

        if "rule_ref" in node:
            if not allow_reference or set(node) != {"rule_ref"}:
                raise _reference_error(path, "rule_ref is only valid as a group child")
            reference = node["rule_ref"]
            if not isinstance(reference, dict) or set(reference) - {"rule_id", "version"}:
                raise _reference_error(f"{path}.rule_ref", "expected rule_id and optional version")
            rule_id = reference.get("rule_id")
            if type(rule_id) is not int or rule_id < 1:
                raise _reference_error(f"{path}.rule_ref.rule_id", "expected a positive integer")
            normalized_reference = {"rule_id": rule_id}
            if "version" in reference:
                version = reference["version"]
                if type(version) is not int or version < 1:
                    raise _reference_error(
                        f"{path}.rule_ref.version",
                        "expected a positive integer when supplied",
                    )
                normalized_reference["version"] = version
            return {"rule_ref": normalized_reference}

        if "property" in node:
            condition = dict(node)
            condition["property"] = canonical_property_name(condition["property"])
            try:
                return validate_definition(condition)
            except RuleError as exc:
                raise _with_path(path, exc) from exc

        if "op" not in node:
            raise RuleError(path, "node must have property or op")
        for key in node:
            if key not in {"op", "children"}:
                raise RuleError(f"{path}.{key}", "unknown key")
        if not isinstance(node.get("op"), str) or node["op"] not in {"AND", "OR"}:
            raise RuleError(f"{path}.op", "expected AND or OR")
        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise RuleError(f"{path}.children", "expected a non-empty array")
        if set(node) != {"op", "children"}:
            raise RuleError(f"{path}.children", "required key missing")
        return {
            "op": node["op"],
            "children": [
                visit(child, f"{path}.children[{index}]", depth + 1, allow_reference=True)
                for index, child in enumerate(children)
            ],
        }

    return visit(definition, "$", 1, allow_reference=False)


def pin_rule_references(
    tenant_id: str,
    parent_rule_id: int,
    definition: object,
) -> tuple[object, list[RuleVersion]]:
    """Resolve unversioned references and persist only explicit immutable pins."""
    references: list[tuple[str, int, int | None]] = []

    def collect(node: object, path: str) -> None:
        if not isinstance(node, dict):
            return
        if "rule_ref" in node:
            reference = node["rule_ref"]
            references.append((path, reference["rule_id"], reference.get("version")))
            return
        if "children" in node:
            for index, child in enumerate(node["children"]):
                collect(child, f"{path}.children[{index}]")

    collect(definition, "$")
    if not references:
        return definition, []
    if not settings.RULE_REFERENCES_ENABLED:
        raise RuleDefinitionAPIError(
            "$: rule references are disabled during the compatibility rollout",
            code="RULE_REFERENCES_DISABLED",
        )

    for path, target_rule_id, _ in references:
        if target_rule_id == parent_rule_id:
            raise _reference_error(path, "a rule cannot reference itself")

    target_ids = {rule_id for _, rule_id, _ in references}
    active_rules = {
        rule.id: rule
        for rule in Rule.objects.filter(
            tenant_id=tenant_id,
            id__in=target_ids,
            status=Rule.Status.ACTIVE,
        )
    }
    selected_pairs: dict[tuple[int, int], str] = {}
    selected_pair_by_path: dict[str, tuple[int, int]] = {}
    for path, rule_id, requested_version in references:
        rule = active_rules.get(rule_id)
        if rule is None:
            raise _reference_error(path, "referenced rule is unavailable")
        version = requested_version or rule.current_version
        if version is None:
            raise _reference_error(path, "referenced rule has no current version")
        pair = (rule_id, version)
        selected_pairs.setdefault(pair, path)
        selected_pair_by_path[path] = pair

    pair_filters = [
        Q(rule_id=rule_id, version=version)
        for rule_id, version in selected_pairs
    ]
    target_versions = {
        (version.rule_id, version.version): version
        for version in RuleVersion.objects.filter(
            reduce(or_, pair_filters),
            tenant_id=tenant_id,
        )
    }
    for pair, path in selected_pairs.items():
        if pair not in target_versions:
            raise _reference_error(path, "referenced rule version is unavailable")

    pinned_by_path = {
        path: target_versions[pair] for path, pair in selected_pair_by_path.items()
    }

    def replace(node: object, path: str) -> object:
        if not isinstance(node, dict):
            return node
        if "rule_ref" in node:
            target = pinned_by_path[path]
            return {"rule_ref": {"rule_id": target.rule_id, "version": target.version}}
        if "children" in node:
            return {
                "op": node["op"],
                "children": [
                    replace(child, f"{path}.children[{index}]")
                    for index, child in enumerate(node["children"])
                ],
            }
        return node

    dependencies = list({version.id: version for version in target_versions.values()}.values())
    return replace(definition, "$"), dependencies


def prepare_rule_definition(
    tenant_id: str,
    parent_rule_id: int,
    parent_version: int,
    definition: object,
) -> tuple[object, list[RuleVersion]]:
    pinned, dependencies = pin_rule_references(tenant_id, parent_rule_id, definition)
    try:
        expand_rule_definition(
            tenant_id,
            pinned,
            root_rule_id=parent_rule_id,
            root_version=parent_version,
            root_schema_version=2,
        )
    except RuleDefinitionAPIError:
        raise
    except RuleError as exc:
        if dependencies:
            raise _reference_error(exc.path, exc.message) from exc
        raise RuleDefinitionAPIError(
            f"{exc.path}: {exc.message}",
            code="INVALID_RULE_DEFINITION",
        ) from exc
    return pinned, dependencies


def expand_rule_definition(
    tenant_id: str,
    definition: object,
    *,
    root_rule_id: int,
    root_version: int,
    root_schema_version: int = 2,
) -> ExpandedRuleDefinition:
    """Expand pinned references, validate the expanded size, and retain trace provenance."""
    if root_schema_version not in {1, 2}:
        raise RuleError("$.schema_version", "unsupported rule schema version")
    provenance: dict[str, tuple[str, ...]] = {}
    versions: dict[tuple[int, int], RuleVersion] = {}
    count = 0
    reference_count = 0
    root_key = (root_rule_id, root_version)

    frontier = [(definition, root_schema_version)]
    scanned_versions = {root_key}
    while frontier:
        references_to_load: dict[tuple[int, int], str] = {}

        def scan(node: object, path: str, schema_version: int) -> None:
            if not isinstance(node, dict):
                return
            if "rule_ref" in node:
                if schema_version != 2:
                    raise _reference_error(path, "rule references require schema version 2")
                reference = node["rule_ref"]
                if not isinstance(reference, dict):
                    raise _reference_error(path, "malformed rule reference")
                rule_id = reference.get("rule_id")
                version = reference.get("version")
                if (
                    type(rule_id) is not int
                    or rule_id < 1
                    or type(version) is not int
                    or version < 1
                ):
                    raise _reference_error(path, "rule reference must contain a pinned version")
                key = (rule_id, version)
                if key not in versions and key not in scanned_versions:
                    references_to_load.setdefault(key, path)
                return
            children = node.get("children")
            if isinstance(children, list):
                for index, child in enumerate(children):
                    scan(child, f"{path}.children[{index}]", schema_version)

        for tree, schema_version in frontier:
            scan(tree, "$", schema_version)
        if not references_to_load:
            break
        query = reduce(
            or_,
            [
                Q(rule_id=rule_id, version=version)
                for rule_id, version in references_to_load
            ],
        )
        loaded = {
            (version.rule_id, version.version): version
            for version in RuleVersion.objects.filter(query, tenant_id=tenant_id)
        }
        for key, path in references_to_load.items():
            if key not in loaded:
                raise _reference_error(path, "referenced rule version is unavailable")
        versions.update(loaded)
        frontier = []
        for key, version in loaded.items():
            if key not in scanned_versions:
                scanned_versions.add(key)
                frontier.append((version.definition, version.schema_version))

    def load_version(rule_id: int, version: int, path: str) -> RuleVersion:
        key = (rule_id, version)
        try:
            return versions[key]
        except KeyError as exc:
            raise _reference_error(path, "referenced rule version is unavailable") from exc

    def visit(
        node: object,
        path: str,
        depth: int,
        via: tuple[str, ...],
        stack: tuple[tuple[int, int], ...],
        schema_version: int,
    ) -> object:
        nonlocal count, reference_count
        if not isinstance(node, dict):
            raise RuleError(path, "node must be an object")
        if "rule_ref" in node:
            if schema_version != 2:
                raise _reference_error(path, "rule references require schema version 2")
            reference = node["rule_ref"]
            if not isinstance(reference, dict):
                raise _reference_error(path, "malformed rule reference")
            rule_id = reference.get("rule_id")
            version = reference.get("version")
            if (
                type(rule_id) is not int
                or rule_id < 1
                or type(version) is not int
                or version < 1
            ):
                raise _reference_error(path, "rule reference must contain a pinned version")
            key = (rule_id, version)
            if rule_id == root_rule_id or key in stack:
                raise _reference_error(path, "cyclic rule reference")
            child_version = load_version(rule_id, version, path)
            if child_version.schema_version not in {1, 2}:
                raise RuleError(path, "unsupported referenced rule schema version")
            reference_count += 1
            return visit(
                child_version.definition,
                path,
                depth,
                (*via, f"{rule_id}@{version}"),
                (*stack, key),
                child_version.schema_version,
            )

        if depth > _MAX_DEPTH:
            raise RuleError(path, f"maximum depth {_MAX_DEPTH} exceeded")
        count += 1
        if count > _MAX_NODES:
            raise RuleError(path, f"maximum node count {_MAX_NODES} exceeded")
        if "property" in node:
            condition = dict(node)
            condition["property"] = canonical_property_name(condition["property"])
            if via:
                provenance[path] = via
            return condition
        if "op" not in node:
            raise RuleError(path, "node must have property or op")
        children = node.get("children")
        if not isinstance(children, list):
            raise RuleError(f"{path}.children", "expected an array")
        return {
            "op": node.get("op"),
            "children": [
                visit(
                    child,
                    f"{path}.children[{index}]",
                    depth + 1,
                    via,
                    stack,
                    schema_version,
                )
                for index, child in enumerate(children)
            ],
        }

    expanded = visit(
        definition,
        "$",
        1,
        (),
        (root_key,),
        root_schema_version,
    )
    validated = validate_definition(expanded)
    return ExpandedRuleDefinition(validated, provenance, reference_count, count)


def create_dependency_records(
    version: RuleVersion,
    dependencies: list[RuleVersion],
) -> None:
    RuleDependency.objects.bulk_create(
        [
            RuleDependency(
                tenant_id=version.tenant_id,
                rule_version=version,
                depends_on_version=dependency,
            )
            for dependency in dependencies
        ]
    )
