"""End-to-end: MCP tool call -> connector -> fake Freshdesk -> normalized result."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from freshdesk_connector.client import FreshdeskClient
from freshdesk_connector.server import mcp, set_client

from conftest import make_settings

pytestmark = pytest.mark.anyio


async def call(name, **args):
    result = await mcp.call_tool(name, args)
    return json.loads(result.content[0].text)


async def test_all_tools_are_registered_read_only():
    tools = {t.name: t for t in await mcp.list_tools()}
    assert set(tools) == {
        "list_tickets", "get_ticket", "list_ticket_conversations", "search_tickets",
        "get_contact", "find_contacts", "connector_status",
    }
    for t in tools.values():
        assert t.annotations.read_only_hint is True
        assert t.annotations.destructive_hint is False


async def test_search_tickets_translates_filters(client, mock_fd):
    out = await call("search_tickets", status=["open"], tags=["payment"])
    assert out["query"] == "status:2 AND tag:'payment'"
    assert {t["id"] for t in out["tickets"]} == {101, 107}
    assert out["total_matches"] == 2 and out["next_page"] is None
    assert all(t["status"] == "open" for t in out["tickets"])


async def test_get_ticket_hides_private_notes_by_default(client):
    out = await call("get_ticket", ticket_id=101)
    assert out["requester"]["name"] == "Aarav Mehta"
    assert out["hidden_private_notes"] == 1
    assert all(c["kind"] != "private_note" for c in out["conversations"])
    assert out["url"] == "https://demo-merchant.freshdesk.com/a/tickets/101"


async def test_private_notes_and_pii_redaction_are_configurable(mock_fd, fake_sleep):
    set_client(FreshdeskClient(make_settings(include_private_notes=True, redact_pii=True), transport=mock_fd.transport(), sleep=fake_sleep))
    out = await call("get_ticket", ticket_id=101)
    assert any(c["kind"] == "private_note" for c in out["conversations"])
    assert out["requester"]["email"] == "a***@example.com"
    assert "90000" not in out["requester"]["phone"]


async def test_list_tickets_paginates(client):
    out = await call("list_tickets", per_page=5)
    assert out["count"] == 5 and out["next_page"] == 2
    assert "description" not in out["tickets"][0]


async def test_customer_lookup_flow(client):
    contacts = await call("find_contacts", email="diya.sharma@example.com")
    cid = contacts["contacts"][0]["id"]
    tickets = await call("list_tickets", requester_id=cid)
    assert {t["id"] for t in tickets["tickets"]} == {102, 104, 108}


async def test_errors_are_agent_readable(client, mock_fd):
    with pytest.raises(ToolError, match=r"\[not_found\]"):
        await call("get_ticket", ticket_id=999)
    with pytest.raises(ToolError, match=r"\[validation\]"):
        await call("search_tickets", tags=["x' OR 1"])
    mock_fd.fail_with_429, mock_fd.retry_after = 1, 300
    with pytest.raises(ToolError, match=r"\[rate_limited \(retryable\)\].*300 seconds"):
        await call("connector_status")


async def test_connector_status_reports_quota(client):
    out = await call("connector_status")
    assert out["connected"] and out["data_policy"]["write_access"] is False
    assert out["api_quota"]["per_minute_total"] == 50
