"""An in-process fake of the Freshdesk v2 API, used when FRESHDESK_MOCK=1 and in tests.

All data here is fictional. The fake reproduces the behaviours the connector depends on:
Basic auth, Link-header pagination, `include=`, search query syntax, rate-limit headers,
and 401/404/429 responses.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from urllib.parse import urlencode

import httpx

MOCK_API_KEY = "mock-api-key"

CONTACTS = [
    {"id": 9001, "name": "Aarav Mehta", "email": "aarav.mehta@example.com", "phone": "+91 90000 00001", "company_id": None, "language": "en", "time_zone": "Chennai", "tags": ["vip"], "active": True, "created_at": "2025-11-02T09:12:00Z", "updated_at": "2026-09-30T10:00:00Z", "custom_fields": {"customer_tier": "gold"}},
    {"id": 9002, "name": "Diya Sharma", "email": "diya.sharma@example.com", "phone": "+91 90000 00002", "company_id": None, "language": "en", "time_zone": "Chennai", "tags": [], "active": True, "created_at": "2026-01-14T12:00:00Z", "updated_at": "2026-09-28T08:00:00Z", "custom_fields": {}},
    {"id": 9003, "name": "Kabir Rao", "email": "kabir.rao@example.org", "mobile": "+91 90000 00003", "company_id": 501, "language": "en", "time_zone": "Chennai", "tags": ["b2b"], "active": True, "created_at": "2026-03-03T07:30:00Z", "updated_at": "2026-10-01T11:00:00Z", "custom_fields": {}},
    {"id": 9004, "name": "Meera Iyer", "email": "meera.iyer@example.net", "phone": None, "company_id": None, "language": "en", "time_zone": "Chennai", "tags": [], "active": False, "created_at": "2026-05-20T15:45:00Z", "updated_at": "2026-09-15T09:10:00Z", "custom_fields": {}},
]

_T = [
    # id, subject, status, priority, source, type, tags, requester, created, updated, description
    (101, "Payment debited but order not confirmed", 2, 4, 1, "Problem", ["payment", "refund"], 9001, "2026-10-01T08:15:00Z", "2026-10-05T10:20:00Z", "I paid Rs 2,499 via UPI for order #ORD-55821 but the site says payment failed. Money was debited from my account. Please confirm or refund."),
    (102, "Where is my order #ORD-55790?", 3, 2, 2, "Question", ["shipping"], 9002, "2026-09-29T11:00:00Z", "2026-10-04T09:00:00Z", "It's been 6 days and tracking hasn't updated since it left the warehouse."),
    (103, "Refund not received after return", 2, 3, 1, "Refund", ["refund", "returns"], 9003, "2026-09-25T14:40:00Z", "2026-10-03T16:00:00Z", "Returned the jacket on 15 Sep, pickup was done, but refund of Rs 3,199 hasn't come back to my card."),
    (104, "Wrong size delivered", 4, 2, 7, "Problem", ["returns"], 9002, "2026-09-20T10:05:00Z", "2026-09-24T12:30:00Z", "Ordered M, received L. Requesting exchange."),
    (105, "Bulk order invoice with GST details", 2, 2, 1, "Question", ["b2b", "invoice"], 9003, "2026-10-02T06:50:00Z", "2026-10-02T07:10:00Z", "We need a GST invoice for PO-7781 (40 units). Our GSTIN is on file."),
    (106, "Coupon FESTIVE20 not applying", 5, 1, 2, "Question", ["coupon"], 9004, "2026-09-10T18:00:00Z", "2026-09-12T09:00:00Z", "The coupon says invalid at checkout."),
    (107, "Charged twice for one order", 2, 4, 3, "Problem", ["payment", "duplicate-charge"], 9001, "2026-10-04T13:25:00Z", "2026-10-05T08:00:00Z", "My card statement shows two charges of Rs 1,250 for order #ORD-56002."),
    (108, "Cash on delivery not available for my pincode", 4, 1, 2, "Question", ["cod"], 9002, "2026-09-18T07:40:00Z", "2026-09-19T10:00:00Z", "COD option is greyed out for 560034."),
    (109, "Cancel order before dispatch", 3, 3, 1, "Request", ["cancellation"], 9004, "2026-10-05T05:30:00Z", "2026-10-05T06:00:00Z", "Please cancel #ORD-56110, I ordered by mistake."),
    (110, "Damaged package received", 2, 3, 1, "Problem", ["returns", "damaged"], 9001, "2026-10-03T09:00:00Z", "2026-10-05T11:45:00Z", "The box was torn and the shoes are scuffed. Photos attached."),
    (111, "EMI option missing at checkout", 3, 2, 7, "Question", ["payment", "emi"], 9003, "2026-09-30T16:20:00Z", "2026-10-01T08:00:00Z", "I want to pay with card EMI but don't see the option."),
    (112, "Request account deletion", 5, 2, 1, "Request", ["privacy"], 9004, "2026-09-05T12:00:00Z", "2026-09-08T12:00:00Z", "Please delete my account and data."),
]

TICKETS = [
    {
        "id": tid, "subject": subj, "status": st, "priority": pr, "source": src, "type": typ,
        "tags": tags, "requester_id": req, "responder_id": 7001 if st != 2 else None, "group_id": 301,
        "company_id": 501 if req == 9003 else None, "created_at": cr, "updated_at": up,
        "due_by": "2026-10-08T10:00:00Z", "fr_due_by": "2026-10-06T10:00:00Z", "is_escalated": pr == 4,
        "description": f"<div>{desc}</div>", "description_text": desc,
        "custom_fields": {"cf_order_id": (re.search(r"#?(ORD-\d+)", desc) or [None, None])[1]},
    }
    for (tid, subj, st, pr, src, typ, tags, req, cr, up, desc) in _T
]

CONVERSATIONS = {
    101: [
        {"id": 50001, "incoming": False, "private": True, "user_id": 7001, "from_email": "support@demo-merchant.example", "body_text": "Checked the payment gateway dashboard: payment captured, order webhook failed. Escalating to tech.", "created_at": "2026-10-01T09:00:00Z", "attachments": []},
        {"id": 50002, "incoming": False, "private": False, "user_id": 7001, "from_email": "support@demo-merchant.example", "body_text": "Hi Aarav, we can see your payment and are confirming the order manually. You'll get an email within 24 hours.", "created_at": "2026-10-01T09:05:00Z", "attachments": []},
        {"id": 50003, "incoming": True, "private": False, "user_id": 9001, "from_email": "aarav.mehta@example.com", "body_text": "It's been 4 days and still nothing. Please just refund.", "created_at": "2026-10-05T10:20:00Z", "attachments": []},
    ],
    110: [
        {"id": 50101, "incoming": True, "private": False, "user_id": 9001, "from_email": "aarav.mehta@example.com", "body_text": "Attaching photos of the damage.", "created_at": "2026-10-03T09:02:00Z", "attachments": [{"name": "box.jpg"}, {"name": "shoe.jpg"}]},
    ],
}


class MockFreshdesk:
    def __init__(self, *, rate_limit_total: int = 50, fail_with_429: int = 0, retry_after: int = 1):
        self.rate_limit_total = rate_limit_total
        self.used = 0
        self.fail_with_429 = fail_with_429  # number of upcoming requests that return 429
        self.retry_after = retry_after
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ dispatch

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        auth = request.headers.get("authorization", "")
        try:
            user = base64.b64decode(auth.removeprefix("Basic ")).decode().split(":")[0]
        except Exception:
            user = ""
        if user != MOCK_API_KEY:
            return self._json({"code": "invalid_credentials", "message": "You have to be logged in to perform this action."}, 401)

        if self.fail_with_429 > 0:
            self.fail_with_429 -= 1
            return httpx.Response(429, headers={"Retry-After": str(self.retry_after)}, json={"message": "Rate limit exceeded"})

        self.used += 1
        path = request.url.path.removeprefix("/api/v2")
        q = request.url.params

        if path == "/agents/me":
            return self._json({"id": 7001, "contact": {"name": "Demo Agent", "email": "agent@demo-merchant.example"}})
        if path == "/tickets":
            return self._list_tickets(request)
        if m := re.fullmatch(r"/tickets/(\d+)", path):
            return self._get_ticket(int(m[1]), q.get("include", ""))
        if m := re.fullmatch(r"/tickets/(\d+)/conversations", path):
            if not self._find(TICKETS, int(m[1])):
                return self._not_found()
            return self._paginate(request, CONVERSATIONS.get(int(m[1]), []))
        if path == "/search/tickets":
            return self._search(q.get("query", ""), int(q.get("page", 1)))
        if m := re.fullmatch(r"/contacts/(\d+)", path):
            c = self._find(CONTACTS, int(m[1]))
            return self._json(c) if c else self._not_found()
        if path == "/contacts":
            items = CONTACTS
            for key in ("email", "phone", "mobile"):
                if key in q:
                    items = [c for c in items if (c.get(key) or "").lower() == q[key].lower()]
            return self._paginate(request, items)
        return self._not_found()

    # ------------------------------------------------------------------ endpoints

    def _list_tickets(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params
        items = list(TICKETS)
        if "updated_since" in q:
            items = [t for t in items if t["updated_at"] >= q["updated_since"]]
        if "email" in q:
            ids = {c["id"] for c in CONTACTS if c["email"] == q["email"]}
            items = [t for t in items if t["requester_id"] in ids]
        if "requester_id" in q:
            items = [t for t in items if t["requester_id"] == int(q["requester_id"])]
        order_by = q.get("order_by", "created_at")
        items.sort(key=lambda t: t.get(order_by) or "", reverse=q.get("order_type", "desc") == "desc")
        # The real list endpoint omits description unless include=description is passed.
        include = q.get("include", "")
        items = [self._with_includes({k: v for k, v in t.items() if k not in ("description", "description_text")} | ({"description_text": t["description_text"]} if "description" in include else {}), include) for t in items]
        return self._paginate(request, items)

    def _get_ticket(self, tid: int, include: str) -> httpx.Response:
        t = self._find(TICKETS, tid)
        if not t:
            return self._not_found()
        return self._json(self._with_includes(dict(t), include))

    def _with_includes(self, t: dict, include: str) -> dict:
        parts = set(include.split(",")) if include else set()
        if "requester" in parts:
            c = self._find(CONTACTS, t["requester_id"])
            t["requester"] = {k: c.get(k) for k in ("id", "name", "email", "mobile", "phone")}
        if "conversations" in parts:
            t["conversations"] = CONVERSATIONS.get(t["id"], [])[:10]
        if "stats" in parts:
            t["stats"] = {"first_responded_at": t["created_at"] if t["status"] != 2 else None, "resolved_at": t["updated_at"] if t["status"] in (4, 5) else None}
        return t

    def _search(self, query: str, page: int) -> httpx.Response:
        query = query.strip()
        if not (query.startswith('"') and query.endswith('"')):
            return self._json({"description": "Validation failed", "errors": [{"field": "query", "message": "query must be enclosed in double quotes", "code": "invalid_value"}]}, 400)
        try:
            pred = _compile(query[1:-1])
        except ValueError as exc:
            return self._json({"description": "Validation failed", "errors": [{"field": "query", "message": str(exc), "code": "invalid_value"}]}, 400)
        matches = [t for t in TICKETS if pred(t)]
        return self._json({"results": matches[(page - 1) * 30 : page * 30], "total": len(matches)})

    # ------------------------------------------------------------------ helpers

    def _paginate(self, request: httpx.Request, items: list) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        per_page = int(request.url.params.get("per_page", 30))
        chunk = items[(page - 1) * per_page : page * per_page]
        headers = {}
        if page * per_page < len(items):
            params = dict(request.url.params) | {"page": str(page + 1)}
            url = request.url.copy_with(query=urlencode(params).encode())
            headers["Link"] = f'<{url}>; rel="next"'
        return self._json(chunk, headers=headers)

    def _json(self, body, status: int = 200, headers: dict | None = None) -> httpx.Response:
        h = {
            "X-RateLimit-Total": str(self.rate_limit_total),
            "X-RateLimit-Remaining": str(max(0, self.rate_limit_total - self.used)),
            "X-RateLimit-Used-CurrentRequest": "1",
            "Content-Type": "application/json",
        } | (headers or {})
        return httpx.Response(status, headers=h, content=json.dumps(body).encode())

    def _not_found(self) -> httpx.Response:
        return httpx.Response(404, headers={"Content-Type": "application/json"}, content=b"")

    @staticmethod
    def _find(items: list[dict], id_: int) -> dict | None:
        return next((i for i in items if i["id"] == id_), None)


# ---------------------------------------------------------------------- tiny search evaluator
# Supports the subset of Freshdesk's query language that query.py produces:
#   clause := field:value | field:>'date' | field:<'date' ; groups of clauses joined by OR in (),
#   groups joined by AND.

_CLAUSE = re.compile(r"^(\w+):([<>]?)('?)(.*?)\3$")


def _compile(q: str):
    groups = []
    for part in re.split(r"\s+AND\s+", q):
        part = part.strip()
        if part.startswith("(") and part.endswith(")"):
            part = part[1:-1]
        groups.append([_clause(c.strip()) for c in re.split(r"\s+OR\s+", part)])
    return lambda t: all(any(c(t) for c in g) for g in groups)


def _clause(c: str):
    m = _CLAUSE.match(c)
    if not m:
        raise ValueError(f"Invalid query clause: {c}")
    field, op, _, val = m.groups()
    if field == "tag":
        return lambda t: val in t.get("tags", [])
    if field in ("created_at", "updated_at", "due_by"):
        d = datetime.fromisoformat(val).date()
        if op == ">":
            return lambda t: datetime.fromisoformat(t[field].replace("Z", "+00:00")).date() >= d
        if op == "<":
            return lambda t: datetime.fromisoformat(t[field].replace("Z", "+00:00")).date() <= d
        return lambda t: datetime.fromisoformat(t[field].replace("Z", "+00:00")).date() == d
    if field in ("status", "priority", "agent_id", "group_id"):
        key = "responder_id" if field == "agent_id" else field
        return lambda t: str(t.get(key)) == val
    if field == "type":
        return lambda t: t.get("type") == val
    raise ValueError(f"Unsupported field in mock: {field}")
