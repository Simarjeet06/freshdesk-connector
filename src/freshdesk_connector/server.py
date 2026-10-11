"""MCP server exposing read-only Freshdesk tools to an agent (e.g. Razorpay Agent Studio)."""

from __future__ import annotations

import functools
import re
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import FreshdeskClient, Page
from .config import Settings
from .errors import ConnectorError, ValidationError
from .normalize import Normalizer
from .query import build_ticket_query

INSTRUCTIONS = """\
Read-only access to the merchant's Freshdesk helpdesk (support tickets, conversations, contacts).

Choosing a tool:
- Know the ticket ID -> get_ticket (add include_conversations for the thread).
- Filter by status/priority/tag/type/date -> search_tickets.
- "Latest tickets" or "tickets from this customer email" -> list_tickets.
- Know a customer's email/phone -> find_contacts, then list_tickets(requester_id=...).

This connector cannot create, reply to, update, assign, merge or delete anything. If the user
asks for a change, say so and give them the ticket's `url` to act on it in Freshdesk.
Results are paginated: when `next_page` is not null and the user needs more, call again with
page=next_page. Don't fetch every page by default; each call spends the merchant's shared
Freshdesk API quota.
"""

mcp = MCPServer(name="freshdesk", title="Freshdesk (read-only)", instructions=INSTRUCTIONS, version="0.1.0")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

_client: FreshdeskClient | None = None
_norm: Normalizer | None = None


def set_client(client: FreshdeskClient) -> None:
    """Install a client (used by __main__ and by tests)."""
    global _client, _norm
    _client, _norm = client, Normalizer(client.settings)


def _deps() -> tuple[FreshdeskClient, Normalizer]:
    if _client is None:
        set_client(FreshdeskClient(Settings.from_env()))
    assert _client is not None and _norm is not None
    return _client, _norm


def _agent_errors(fn):
    """Convert connector errors to MCP tool errors with a stable, machine-readable prefix."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ConnectorError as exc:
            code = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__.removesuffix("Error")).lower() or "error"
            hint = " (retryable)" if exc.retryable else ""
            raise ToolError(f"[{code}{hint}] {exc}") from exc

    return wrapper


def _page_result(page: Page, key: str, items: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {key: items, "count": len(items), "page": page.page, "next_page": page.next_page}
    if page.total is not None:
        out["total_matches"] = page.total
    out.update(extra)
    return out


# --------------------------------------------------------------------------- tools

Page_ = Annotated[int, Field(ge=1, le=300, description="1-based page number. Use the next_page value from a previous call.")]
IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="Date as YYYY-MM-DD")]
StatusName = Literal["open", "pending", "resolved", "closed"]
PriorityName = Literal["low", "medium", "high", "urgent"]


@mcp.tool(annotations=READ_ONLY)
@_agent_errors
async def list_tickets(
    updated_since: Annotated[
        IsoDate | None,
        Field(description="Only tickets updated on/after this date (YYYY-MM-DD). Without it, Freshdesk returns only tickets created in the last 30 days."),
    ] = None,
    requester_email: Annotated[str | None, Field(description="Only tickets raised by this exact email address.")] = None,
    requester_id: Annotated[int | None, Field(description="Only tickets raised by this contact ID.")] = None,
    order_by: Literal["created_at", "updated_at", "due_by", "status"] = "updated_at",
    order_type: Literal["asc", "desc"] = "desc",
    page: Page_ = 1,
    per_page: Annotated[int, Field(ge=1, le=100, description="Results per page (max 100).")] = 20,
    include_description: Annotated[bool, Field(description="Include each ticket's text body. Costs extra Freshdesk API credits per call.")] = False,
) -> dict[str, Any]:
    """List helpdesk tickets, newest first. Use for browsing recent tickets or all tickets of one customer.
    For filtering by status, priority, tag or type use search_tickets instead."""
    if requester_email and requester_id:
        raise ValidationError("Pass either requester_email or requester_id, not both.")
    client, norm = _deps()
    params = {
        "updated_since": f"{updated_since}T00:00:00Z" if updated_since else None,
        "email": requester_email,
        "requester_id": requester_id,
        "order_by": order_by,
        "order_type": order_type,
        "per_page": per_page,
        "include": "description" if include_description else None,
    }
    p = await client.get_page("/tickets", params, page)
    return _page_result(p, "tickets", [norm.ticket(t) for t in p.items])


@mcp.tool(annotations=READ_ONLY)
@_agent_errors
async def get_ticket(
    ticket_id: Annotated[int, Field(ge=1, description="Freshdesk ticket ID (the number in #123).")],
    include_conversations: Annotated[
        bool, Field(description="Include up to the first 10 replies/notes. Use list_ticket_conversations for longer threads.")
    ] = True,
) -> dict[str, Any]:
    """Get one ticket in full: subject, body, status, priority, tags, SLA due dates, requester details, timing stats and (optionally) the conversation thread."""
    client, norm = _deps()
    include = "requester,stats" + (",conversations" if include_conversations else "")
    t = await client.get_json(f"/tickets/{ticket_id}", {"include": include})
    return norm.ticket(t)


@mcp.tool(annotations=READ_ONLY)
@_agent_errors
async def list_ticket_conversations(
    ticket_id: Annotated[int, Field(ge=1, description="Freshdesk ticket ID.")],
    page: Page_ = 1,
) -> dict[str, Any]:
    """List the full conversation thread of a ticket (customer messages and agent replies, oldest first), 30 per page.
    Internal private notes are hidden unless the operator has enabled them."""
    client, norm = _deps()
    p = await client.get_page(f"/tickets/{ticket_id}/conversations", {"per_page": 30}, page)
    convs = norm.conversations(p.items)
    return _page_result(p, "conversations", convs["items"], hidden_private_notes=convs["hidden_private_notes"])


@mcp.tool(annotations=READ_ONLY)
@_agent_errors
async def search_tickets(
    status: Annotated[list[StatusName] | None, Field(description="Match any of these statuses.")] = None,
    priority: Annotated[list[PriorityName] | None, Field(description="Match any of these priorities.")] = None,
    tags: Annotated[list[str] | None, Field(description="Match tickets having any of these tags, e.g. ['refund'].", max_length=10)] = None,
    ticket_type: Annotated[str | None, Field(description="Ticket type exactly as configured in Freshdesk, e.g. 'Refund', 'Question'.")] = None,
    agent_id: Annotated[int | None, Field(description="Assigned agent ID.")] = None,
    group_id: Annotated[int | None, Field(description="Assigned group ID.")] = None,
    created_after: Annotated[IsoDate | None, Field(description="Created on/after (YYYY-MM-DD).")] = None,
    created_before: Annotated[IsoDate | None, Field(description="Created on/before (YYYY-MM-DD).")] = None,
    updated_after: Annotated[IsoDate | None, Field(description="Updated on/after (YYYY-MM-DD).")] = None,
    due_before: Annotated[IsoDate | None, Field(description="Resolution due on/before (YYYY-MM-DD). Good for finding SLA risks.")] = None,
    page: Annotated[int, Field(ge=1, le=10, description="Page 1-10; 30 results per page. Freshdesk caps search at 300 results.")] = 1,
) -> dict[str, Any]:
    """Search tickets by structured filters (all filters are combined with AND; list values are OR'd).
    Example: open or pending, urgent, tagged 'refund' -> status=['open','pending'], priority=['urgent'], tags=['refund'].
    Returns total_matches so you can report counts without fetching every page.
    Note: Freshdesk's search index can lag a few minutes behind very recent changes."""
    client, norm = _deps()
    query = build_ticket_query(
        status=status, priority=priority, tags=tags, ticket_type=ticket_type, agent_id=agent_id, group_id=group_id,
        created_after=created_after, created_before=created_before, updated_after=updated_after, due_before=due_before,
    )
    p = await client.search("tickets", query, page)
    return _page_result(p, "tickets", [norm.ticket(t) for t in p.items], query=query)


@mcp.tool(annotations=READ_ONLY)
@_agent_errors
async def get_contact(contact_id: Annotated[int, Field(ge=1, description="Freshdesk contact ID (e.g. a ticket's requester_id).")]) -> dict[str, Any]:
    """Get a customer (contact) profile: name, email, phone, company, tags and custom fields."""
    client, norm = _deps()
    return norm.contact(await client.get_json(f"/contacts/{contact_id}"))


@mcp.tool(annotations=READ_ONLY)
@_agent_errors
async def find_contacts(
    email: Annotated[str | None, Field(description="Exact email address.")] = None,
    phone: Annotated[str | None, Field(description="Exact phone number as stored in Freshdesk.")] = None,
    mobile: Annotated[str | None, Field(description="Exact mobile number as stored in Freshdesk.")] = None,
    page: Page_ = 1,
) -> dict[str, Any]:
    """Look up customers by exact email, phone or mobile number. Use the returned id with list_tickets(requester_id=...)."""
    if not any([email, phone, mobile]):
        raise ValidationError("Provide at least one of email, phone or mobile.")
    client, norm = _deps()
    p = await client.get_page("/contacts", {"email": email, "phone": phone, "mobile": mobile, "per_page": 30}, page)
    return _page_result(p, "contacts", [norm.contact(c) for c in p.items])


@mcp.tool(annotations=READ_ONLY)
@_agent_errors
async def connector_status() -> dict[str, Any]:
    """Check that the Freshdesk connection works and report the remaining API quota. Use when other tools fail with auth or rate-limit errors."""
    client, _ = _deps()
    me = await client.get_json("/agents/me")
    rs = client.rate_state
    s = client.settings
    return {
        "connected": True,
        "helpdesk": s.portal_url,
        "mode": "mock" if s.mock else "live",
        "authenticated_as": (me.get("contact") or {}).get("name"),
        "api_quota": {"per_minute_total": rs.total, "remaining_this_minute": rs.remaining, "throttled_requests": rs.throttled_requests},
        "data_policy": {"private_notes_visible": s.include_private_notes, "pii_redacted": s.redact_pii, "write_access": False},
    }
