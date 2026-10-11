import pytest

from freshdesk_connector.errors import ValidationError
from freshdesk_connector.query import build_ticket_query


def test_combines_filters_with_and_and_lists_with_or():
    q = build_ticket_query(status=["open", "pending"], priority=["urgent"], tags=["refund"], created_after="2026-09-01")
    assert q == "(status:2 OR status:3) AND priority:4 AND tag:'refund' AND created_at:>'2026-09-01'"


def test_requires_at_least_one_filter():
    with pytest.raises(ValidationError, match="at least one filter"):
        build_ticket_query()


@pytest.mark.parametrize("bad", ["x' OR status:2", 'a"b', "a)b", ""])
def test_rejects_query_injection_in_text_fields(bad):
    with pytest.raises(ValidationError):
        build_ticket_query(tags=[bad])


def test_rejects_unknown_enum_and_bad_date():
    with pytest.raises(ValidationError, match="Unknown status"):
        build_ticket_query(status=["waiting"])
    with pytest.raises(ValidationError, match="YYYY-MM-DD"):
        build_ticket_query(created_after="last week")


def test_enforces_freshdesk_length_limit():
    with pytest.raises(ValidationError, match="too complex"):
        build_ticket_query(tags=[f"tag-number-{i:040d}" for i in range(12)])
