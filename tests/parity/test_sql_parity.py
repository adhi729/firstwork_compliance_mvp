from datetime import UTC, datetime
from uuid import uuid4

import pytest
from django.db import connection, transaction
from django.test import override_settings
from hypothesis import given, settings
from hypothesis import strategies as st

from apps.rules.models import Rule, RuleVersion
from apps.rules.references import (
    expand_rule_definition,
    normalize_rule_definition,
    pin_rule_references,
)
from apps.tenants.models import Tenant
from domain.engine.engine import TruthValue, evaluate_definition
from domain.engine.sql import compile_definition

_AS_OF = datetime(2026, 11, 13, 9, 30, tzinfo=UTC)
_NAME_CONDITION = st.builds(
    lambda operator, value: {
        "property": "worker.name",
        "operator": operator,
        "value": value,
    },
    st.sampled_from(["==", "!="]),
    st.sampled_from(["Alex", "Sam", ""]),
)
_NAME_DEFINITION = st.recursive(
    _NAME_CONDITION,
    lambda children: st.builds(
        lambda operator, members: {"op": operator, "children": members},
        st.sampled_from(["AND", "OR"]),
        st.lists(children, min_size=1, max_size=3),
    ),
    max_leaves=8,
)


@pytest.mark.django_db
@settings(max_examples=60, deadline=None)
@given(
    definition=_NAME_DEFINITION,
    name=st.one_of(st.none(), st.text(alphabet="abcXYZé ", max_size=10)),
)
def test_interpreter_and_sql_agree_for_nullable_string_trees(definition, name):
    interpreted = evaluate_definition(
        definition,
        {"worker": {"name": name}},
        _AS_OF,
    ).status
    fragment = compile_definition(definition, _AS_OF)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT ({fragment.sql}) "
            "FROM (SELECT %s::text AS name) AS w",
            [*fragment.params, name],
        )
        sql_result = cursor.fetchone()[0]

    sql_status = (
        TruthValue.UNKNOWN
        if sql_result is None
        else TruthValue.TRUE
        if sql_result
        else TruthValue.FALSE
    )
    assert sql_status == interpreted


@pytest.mark.django_db(transaction=True)
@settings(max_examples=20, deadline=None)
@given(
    operator=st.sampled_from(["AND", "OR"]),
    definition=_NAME_DEFINITION,
    name=st.one_of(st.none(), st.text(alphabet="abcXYZé ", max_size=10)),
)
def test_interpreter_and_sql_agree_with_pinned_rule_references(operator, definition, name):
    tenant = Tenant.objects.create(name=f"parity-{uuid4()}")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.tenant_id', %s, true)",
                [str(tenant.id)],
            )
        child = Rule.objects.create(
            tenant=tenant,
            name="Reusable name condition",
            status=Rule.Status.ACTIVE,
            current_version=1,
        )
        RuleVersion.objects.create(
            tenant=tenant,
            rule=child,
            version=1,
            definition={
                "property": "worker.name",
                "operator": "==",
                "value": "Alex",
            },
            schema_version=2,
            created_by="parity-test",
        )
        input_definition = normalize_rule_definition(
            {
                "op": operator,
                "children": [
                    {"rule_ref": {"rule_id": child.id}},
                    definition,
                ],
            }
        )
        with override_settings(RULE_REFERENCES_ENABLED=True):
            pinned, _ = pin_rule_references(
                str(tenant.id),
                child.id + 1_000_000_000,
                input_definition,
            )
        expanded = expand_rule_definition(
            str(tenant.id),
            pinned,
            root_rule_id=child.id + 1_000_000_000,
            root_version=1,
        )

        interpreted = evaluate_definition(
            expanded.definition,
            {"worker": {"name": name}},
            _AS_OF,
            via_by_path=expanded.provenance,
        ).status
        fragment = compile_definition(expanded.definition, _AS_OF)
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT ({fragment.sql}) "
                "FROM (SELECT %s::text AS name) AS w",
                [*fragment.params, name],
            )
            sql_result = cursor.fetchone()[0]

    sql_status = (
        TruthValue.UNKNOWN
        if sql_result is None
        else TruthValue.TRUE
        if sql_result
        else TruthValue.FALSE
    )
    assert sql_status == interpreted
