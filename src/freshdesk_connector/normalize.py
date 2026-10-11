"""Turns raw Freshdesk JSON into compact, agent-friendly records.

Raw Freshdesk objects are noisy for an LLM: numeric enums, HTML bodies, dozens of null
fields. We translate enums to labels, prefer the plain-text body, truncate long text, add a
deep link a human can click, and optionally mask PII.
"""

from __future__ import annotations

import re
from typing import Any

from .config import Settings
from .query import PRIORITY, SOURCE, STATUS

MAX_TEXT_CHARS = 2000

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d\s-]{6,}\d)(?!\d)")


class Normalizer:
    def __init__(self, settings: Settings):
        self.s = settings

    # -------------------------------------------------------------- text helpers

    def _redact(self, text: str | None) -> str | None:
        if text is None or not self.s.redact_pii:
            return text
        text = _EMAIL_RE.sub(r"\1***@\2", text)
        return _PHONE_RE.sub(lambda m: "***" + re.sub(r"\D", "", m.group(1))[-2:], text)

    def _body(self, text: str | None) -> str | None:
        if not text:
            return text
        text = text.strip()
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + f"... [truncated, {len(text) - MAX_TEXT_CHARS} more chars]"
        return self._redact(text)

    @staticmethod
    def _compact(d: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in d.items() if v not in (None, [], {}, "")}

    # -------------------------------------------------------------- entities

    def ticket(self, t: dict[str, Any]) -> dict[str, Any]:
        out = {
            "id": t.get("id"),
            "subject": self._redact(t.get("subject")),
            "status": STATUS.get(t.get("status"), t.get("status")),
            "priority": PRIORITY.get(t.get("priority"), t.get("priority")),
            "source": SOURCE.get(t.get("source"), t.get("source")),
            "type": t.get("type"),
            "tags": t.get("tags"),
            "requester_id": t.get("requester_id"),
            "responder_id": t.get("responder_id"),
            "group_id": t.get("group_id"),
            "company_id": t.get("company_id"),
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
            "due_by": t.get("due_by"),
            "first_response_due_by": t.get("fr_due_by"),
            "is_escalated": t.get("is_escalated") or None,
            "description": self._body(t.get("description_text")),
            "custom_fields": self._compact(t.get("custom_fields") or {}),
            "url": f"{self.s.portal_url}/a/tickets/{t.get('id')}",
        }
        if isinstance(t.get("requester"), dict):
            out["requester"] = self.contact_summary(t["requester"])
        if isinstance(t.get("stats"), dict):
            st = t["stats"]
            out["stats"] = self._compact(
                {
                    "first_responded_at": st.get("first_responded_at"),
                    "agent_responded_at": st.get("agent_responded_at"),
                    "requester_responded_at": st.get("requester_responded_at"),
                    "resolved_at": st.get("resolved_at"),
                    "closed_at": st.get("closed_at"),
                }
            )
        if isinstance(t.get("conversations"), list):
            convs = self.conversations(t["conversations"])
            out["conversations"] = convs["items"]
            if convs["hidden_private_notes"]:
                out["hidden_private_notes"] = convs["hidden_private_notes"]
        return self._compact(out)

    def conversations(self, convs: list[dict[str, Any]]) -> dict[str, Any]:
        items, hidden = [], 0
        for c in convs:
            if c.get("private") and not self.s.include_private_notes:
                hidden += 1
                continue
            items.append(
                self._compact(
                    {
                        "id": c.get("id"),
                        "kind": "private_note" if c.get("private") else ("customer" if c.get("incoming") else "agent_reply"),
                        "from": self._redact(c.get("from_email")),
                        "user_id": c.get("user_id"),
                        "created_at": c.get("created_at"),
                        "body": self._body(c.get("body_text")),
                        "attachments": [a.get("name") for a in c.get("attachments") or []],
                    }
                )
            )
        return {"items": items, "hidden_private_notes": hidden}

    def contact_summary(self, c: dict[str, Any]) -> dict[str, Any]:
        return self._compact(
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "email": self._redact(c.get("email")),
                "phone": self._redact(c.get("phone") or c.get("mobile")),
            }
        )

    def contact(self, c: dict[str, Any]) -> dict[str, Any]:
        return self._compact(
            {
                **self.contact_summary(c),
                "company_id": c.get("company_id"),
                "job_title": c.get("job_title"),
                "language": c.get("language"),
                "time_zone": c.get("time_zone"),
                "tags": c.get("tags"),
                "active": c.get("active"),
                "created_at": c.get("created_at"),
                "updated_at": c.get("updated_at"),
                "custom_fields": self._compact(c.get("custom_fields") or {}),
                "url": f"{self.s.portal_url}/a/contacts/{c.get('id')}",
            }
        )
