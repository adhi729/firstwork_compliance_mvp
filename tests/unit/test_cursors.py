import pytest
from django.core import signing
from rest_framework.exceptions import ParseError

from apps.tenants.cursors import decode_eligibility_cursor, encode_eligibility_cursor


def test_eligibility_cursor_is_signed_and_query_scoped():
    scope = {
        "tenant_id": "tenant-a",
        "company_id": "42",
        "rule_id": "rule-a",
        "version": "2",
    }
    token = encode_eligibility_cursor(125, scope)
    assert decode_eligibility_cursor(token, scope=scope) == 125

    with pytest.raises(ParseError):
        decode_eligibility_cursor(token, scope={**scope, "company_id": "43"})


def test_tampered_eligibility_cursor_is_rejected():
    scope = {"tenant_id": "tenant-a"}
    token = encode_eligibility_cursor(1, scope)
    with pytest.raises(ParseError):
        decode_eligibility_cursor(token + "tampered", scope=scope)


def test_cursor_helper_uses_django_signing_salt():
    scope = {"tenant_id": "tenant-a"}
    token = encode_eligibility_cursor(5, scope)
    with pytest.raises(signing.BadSignature):
        signing.loads(token, salt="wrong-salt")
