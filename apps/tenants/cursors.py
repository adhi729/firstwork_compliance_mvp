from typing import Any

from django.core import signing
from django.core.signing import BadSignature
from rest_framework.exceptions import ParseError

_CURSOR_SALT = "firstwork.eligible-contracts"


def encode_eligibility_cursor(after_id: int, scope: dict[str, Any]) -> str:
    return signing.dumps({**scope, "after_id": after_id}, salt=_CURSOR_SALT, compress=True)


def decode_eligibility_cursor(
    cursor: str,
    *,
    scope: dict[str, Any],
) -> int:
    try:
        payload = signing.loads(cursor, salt=_CURSOR_SALT)
    except BadSignature as exc:
        raise ParseError("Invalid pagination cursor.", code="MALFORMED_REQUEST") from exc

    if not isinstance(payload, dict) or any(
        payload.get(key) != value for key, value in scope.items()
    ):
        raise ParseError("Pagination cursor does not match this query.", code="MALFORMED_REQUEST")

    after_id = payload.get("after_id")
    if isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 1:
        raise ParseError("Invalid pagination cursor.", code="MALFORMED_REQUEST")
    return after_id
