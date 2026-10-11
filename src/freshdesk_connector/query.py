"""Builds Freshdesk search queries from structured, validated arguments.

Freshdesk's /search/tickets endpoint takes a mini-language, e.g.
    "(status:2 OR status:3) AND priority:4 AND tag:'refund' AND created_at:>'2026-01-01'"

We never let the model write this string directly. Malformed queries are the most common
failure mode for LLM tool calls, and free-form strings are an injection surface. Instead the
agent passes typed filters and we compile them here, rejecting anything outside the grammar.
"""

from __future__ import annotations

import re
from datetime import date

from .errors import ValidationError

STATUS = {2: "open", 3: "pending", 4: "resolved", 5: "closed"}
PRIORITY = {1: "low", 2: "medium", 3: "high", 4: "urgent"}
SOURCE = {1: "email", 2: "portal", 3: "phone", 7: "chat", 9: "feedback_widget", 10: "outbound_email"}

STATUS_BY_NAME = {v: k for k, v in STATUS.items()}
PRIORITY_BY_NAME = {v: k for k, v in PRIORITY.items()}

MAX_QUERY_LENGTH = 512  # Freshdesk limit
_SAFE_TEXT = re.compile(r"^[\w .\-/@&]{1,64}$", re.UNICODE)


def _codes(values: list[str] | None, mapping: dict[str, int], field: str) -> list[int]:
    out = []
    for v in values or []:
        key = v.strip().lower()
        if key not in mapping:
            raise ValidationError(f"Unknown {field} {v!r}. Allowed: {', '.join(mapping)}")
        out.append(mapping[key])
    return out


def _text(value: str, field: str) -> str:
    if not _SAFE_TEXT.match(value):
        raise ValidationError(
            f"{field} {value!r} contains characters that are not allowed in a Freshdesk search "
            "(use letters, digits, spaces and - _ . / @ &)"
        )
    return value


def _date(value: str, field: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValidationError(f"{field} must be a date in YYYY-MM-DD format, got {value!r}") from exc


def _any_of(field: str, values: list[str]) -> str:
    clauses = [f"{field}:{v}" for v in values]
    return clauses[0] if len(clauses) == 1 else "(" + " OR ".join(clauses) + ")"


def build_ticket_query(
    *,
    status: list[str] | None = None,
    priority: list[str] | None = None,
    tags: list[str] | None = None,
    ticket_type: str | None = None,
    agent_id: int | None = None,
    group_id: int | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    updated_after: str | None = None,
    due_before: str | None = None,
) -> str:
    parts: list[str] = []

    if status:
        parts.append(_any_of("status", [str(c) for c in _codes(status, STATUS_BY_NAME, "status")]))
    if priority:
        parts.append(_any_of("priority", [str(c) for c in _codes(priority, PRIORITY_BY_NAME, "priority")]))
    if tags:
        parts.append(_any_of("tag", [f"'{_text(t, 'tag')}'" for t in tags]))
    if ticket_type:
        parts.append(f"type:'{_text(ticket_type, 'ticket_type')}'")
    if agent_id is not None:
        parts.append(f"agent_id:{int(agent_id)}")
    if group_id is not None:
        parts.append(f"group_id:{int(group_id)}")
    # Freshdesk's :> and :< on dates are inclusive.
    if created_after:
        parts.append(f"created_at:>'{_date(created_after, 'created_after')}'")
    if created_before:
        parts.append(f"created_at:<'{_date(created_before, 'created_before')}'")
    if updated_after:
        parts.append(f"updated_at:>'{_date(updated_after, 'updated_after')}'")
    if due_before:
        parts.append(f"due_by:<'{_date(due_before, 'due_before')}'")

    if not parts:
        raise ValidationError(
            "search_tickets needs at least one filter. To browse recent tickets without filters, use list_tickets."
        )

    query = " AND ".join(parts)
    if len(query) > MAX_QUERY_LENGTH:
        raise ValidationError(f"Search is too complex ({len(query)} chars; Freshdesk allows {MAX_QUERY_LENGTH}). Use fewer filters.")
    return query
