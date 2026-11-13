"""Pure rules engine.

``validate_definition`` returns a JSON-compatible, canonical tree.
``evaluate_definition`` validates the tree and returns a typed result with a
flat, depth-first trace of every condition, including skipped conditions.
Contexts are nested worker/company/contract mappings; native Python values or
the corresponding JSON representations may be used. Derived fields can be
provided explicitly or computed from their source fields.

Depth counts the root as one; the node limit counts groups and conditions.
Age is completed calendar years, with a February 29 birthday observed on
February 28 in non-leap years. ``as_of`` is one timezone-aware instant.
"""

from .engine import (
    PROPERTY_REGISTRY,
    ConditionTrace,
    EvaluationResult,
    PropertyMetadata,
    RuleError,
    TruthValue,
    evaluate_definition,
    validate_definition,
)

__all__ = [
    "PROPERTY_REGISTRY",
    "ConditionTrace",
    "EvaluationResult",
    "PropertyMetadata",
    "RuleError",
    "TruthValue",
    "evaluate_definition",
    "validate_definition",
]
