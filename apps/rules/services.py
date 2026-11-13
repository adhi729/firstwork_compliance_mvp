import logging
from dataclasses import replace

import redis
from django.core.cache import cache

from apps.rules.models import RuleVersion
from apps.rules.references import ExpandedRuleDefinition, expand_rule_definition

logger = logging.getLogger(__name__)


def get_rule_definition(version: RuleVersion) -> object:
    key = f"rule-definition:{version.tenant_id}:{version.rule_id}:{version.version}"
    try:
        definition = cache.get(key)
    except redis.RedisError:
        logger.warning("Rule cache unavailable; loading immutable version from database")
        definition = None
    if definition is not None:
        return definition

    definition = version.definition
    try:
        cache.set(key, definition, timeout=None)
    except redis.RedisError:
        logger.warning("Rule cache unavailable; continuing with database definition")
    return definition


def get_expanded_rule_definition(version: RuleVersion) -> ExpandedRuleDefinition:
    key = f"expanded-rule-definition:{version.tenant_id}:{version.rule_id}:{version.version}"
    try:
        definition = cache.get(key)
    except redis.RedisError:
        logger.warning("Rule cache unavailable; expanding immutable version from database")
        definition = None
    if isinstance(definition, ExpandedRuleDefinition):
        return replace(definition, cache_hit=True)

    expanded = expand_rule_definition(
        str(version.tenant_id),
        get_rule_definition(version),
        root_rule_id=version.rule_id,
        root_version=version.version,
        root_schema_version=version.schema_version,
    )
    try:
        cache.set(key, expanded, timeout=None)
    except redis.RedisError:
        logger.warning("Rule cache unavailable; continuing with expanded database definition")
    return expanded
