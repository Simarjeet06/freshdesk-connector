# Freshdesk Connector for Agent Studio

A private, **read-only** connector that lets an AI agent read a merchant's Freshdesk helpdesk: tickets, conversation threads and customer contacts. It is exposed as an **MCP server**, so any MCP-capable agent platform (Agent Studio, Claude, etc.) can call it, over stdio for local use or over authenticated HTTP for hosted use.

```
 Agent Studio agent ──MCP (HTTP + bearer token)──▶ freshdesk-connector ──HTTPS (API key)──▶ <merchant>.freshdesk.com/api/v2
                                                   ├─ input validation / query builder
                                                   ├─ token-bucket + Retry-After rate limiting
                                                   ├─ retries with backoff (GET-only, so retries are safe)
                                                   └─ response normalization, PII controls
```

- **What the agent can and cannot do:** see [CAPABILITIES.md](CAPABILITIES.md)
- **MCP tool specification:** [mcp-tools.json](mcp-tools.json) (generated from the code by `scripts/export_tool_spec.py`)

## Verification status

> **Not yet run against a live Freshdesk account.** Freshdesk trial sign-up requires a business email, which I didn't have access to for this assignment.

What *is* verified:
- **31 automated tests** against an in-process fake Freshdesk (`mock.py`). The fake follows Freshdesk's documented v2 API contract: Basic auth with `<key>:X`, `Link`-header pagination, `include=`, `/search/tickets` query syntax and its 30-per-page / 10-page cap, `X-RateLimit-*` headers, and 401/404/429 + `Retry-After` responses.
- **Real MCP protocol** end to end: a real MCP client over stdio (`scripts/demo.py`), and over streamable HTTP with the bearer-token guard (401 without or with a wrong token, success with the right one).

What a live run would still need to confirm: exact field shapes on the reviewer's plan, per-plan rate-limit numbers, and any plan-gated endpoints. Plugging in a real account takes two environment variables (`FRESHDESK_DOMAIN`, `FRESHDESK_API_KEY`) and no code changes. `python scripts/demo.py --live` then exercises every tool.

## Why Freshdesk

Support tickets are where a merchant's operational problems surface: failed payments, refunds, delivery issues. An agent that can read them can triage, summarise and answer "what's going on with customer X?" questions. Freshdesk also has a well-specified REST API with explicit rate-limit headers and a real search endpoint, which makes it a good test of connector engineering.

## Quick start (no Freshdesk account needed)

Requires Python 3.10+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                      # 31 tests: auth, rate limits, retries, pagination, query safety, tools
python scripts/demo.py      # spawns the MCP server over stdio and runs a scripted agent session
```

`scripts/demo.py` runs against a built-in fake Freshdesk (`FRESHDESK_MOCK=1`) with fictional data. It shows a health check, a filtered search, a full ticket with its thread, a customer lookup, and two error cases.

## Running against a real Freshdesk

1. Use an existing helpdesk, or create a free trial at freshdesk.com (requires a business email).
2. In Freshdesk: profile picture → **Profile settings** → **View API key**.
   Use a dedicated agent with the narrowest role that can see the tickets the agent needs. The connector inherits that agent's permissions.
3. Configure:
   ```bash
   cp .env.example .env
   # set FRESHDESK_DOMAIN=yoursubdomain and FRESHDESK_API_KEY=...
   ```
4. Verify: `python scripts/demo.py --live`. The ticket IDs in the demo are mock IDs, so expect `not_found` for those steps. `connector_status` and `search_tickets` will show your real data.

### Connecting an agent

**Hosted (Agent Studio / any remote MCP client):** streamable HTTP, protected by a bearer token:

```bash
export CONNECTOR_AUTH_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
freshdesk-connector --transport http --host 0.0.0.0 --port 8000
# MCP endpoint:  http://<host>:8000/mcp     header: Authorization: Bearer $CONNECTOR_AUTH_TOKEN
# Health check:  http://<host>:8000/healthz (unauthenticated)
```

The server refuses to start in HTTP mode without `CONNECTOR_AUTH_TOKEN`. Put it behind TLS (a load balancer or reverse proxy) in production.

**Local (Claude Desktop, MCP Inspector, IDEs):** stdio:

```json
{
  "mcpServers": {
    "freshdesk": {
      "command": "/abs/path/to/.venv/bin/freshdesk-connector",
      "env": { "FRESHDESK_DOMAIN": "yoursubdomain", "FRESHDESK_API_KEY": "..." }
    }
  }
}
```

Or explore interactively: `npx @modelcontextprotocol/inspector .venv/bin/freshdesk-connector`.

## Tools

| Tool | Primitive | Freshdesk endpoint | Notes |
|---|---|---|---|
| `list_tickets` | list | `GET /tickets` | Filter by requester email/ID or `updated_since`; sort; paginated (Link header) |
| `get_ticket` | get | `GET /tickets/{id}?include=requester,stats,conversations` | One call gets the ticket, customer, SLA timing and first 10 messages |
| `list_ticket_conversations` | list | `GET /tickets/{id}/conversations` | Full thread, paginated |
| `search_tickets` | search | `GET /search/tickets?query=...` | Structured filters compiled into Freshdesk query syntax; returns `total_matches` |
| `get_contact` | get | `GET /contacts/{id}` | Customer profile |
| `find_contacts` | search | `GET /contacts?email=/phone=/mobile=` | Exact-match customer lookup |
| `connector_status` | health | `GET /agents/me` | Verifies auth and reports remaining API quota and data policy |

Every tool is annotated `readOnlyHint: true, destructiveHint: false`. Results are compact JSON: enums become labels (`status: "open"`, not `2`), bodies are plain text truncated to 2,000 chars, empty fields are dropped, and every ticket and contact includes a `url` a human can click.

## Design decisions

### Authentication
- **Freshdesk → connector:** API key over HTTP Basic (`<key>:X`), which is how Freshdesk's public REST API authenticates. It has no general-purpose OAuth flow for third-party server integrations, so an API key is the practical choice for a private connector. The key is read from the environment, never logged, and never returned by any tool.
- **Agent → connector:** in HTTP mode, a bearer token compared in constant time (`hmac.compare_digest`). The merchant's Freshdesk key stays on the connector, and the agent platform only holds a revocable connector token.
- Bad credentials produce a specific `[auth]` error rather than a generic failure, and `connector_status` lets the agent self-diagnose.

### Rate limiting
Freshdesk's limit is **per account, per minute, and shared with every other integration** the merchant runs. The connector must not starve the merchant's other tools, so it uses three layers (`rate_limit.py`, `client.py`):
1. **Client-side token bucket** (`FRESHDESK_RATE_LIMIT_PER_MIN`, default 50, which is safe for trial plans) paces requests before they're sent.
2. **Adaptive slowdown:** every response's `X-RateLimit-Remaining` / `X-RateLimit-Total` is tracked. When the account-wide budget drops below 10% because of other integrations, requests are spread out further.
3. **429 handling:** honours `Retry-After` exactly and retries up to 3 times. If Freshdesk asks for a wait longer than `FRESHDESK_MAX_RETRY_WAIT_SECONDS` (default 30), the connector returns `[rate_limited (retryable)] ... try again in N seconds` instead of blocking the agent's turn for minutes.

5xx and network errors are retried with exponential backoff and full jitter. This is only safe because every call is a GET.

### Agent-oriented API design
- **No raw query strings from the LLM.** `search_tickets` takes typed filters (`status=["open","pending"]`, `tags=["refund"]`, `created_after="2026-09-01"`), and `query.py` compiles them into Freshdesk's syntax. Enums are JSON-Schema `enum`s, dates are pattern-validated, free-text values are allow-listed (blocking query injection like `refund' OR status:5`), and Freshdesk's 512-char limit is enforced. Bad input fails *before* spending API quota, with a message the model can use to correct itself.
- **Errors are stable and machine-readable:** `[auth]`, `[permission_denied]`, `[not_found]`, `[validation]`, `[rate_limited (retryable)]`, `[upstream (retryable)]`.
- **Pagination is explicit** (`page`, `next_page`, `total_matches`), and the server instructions tell the agent not to page through everything by default, because each page costs shared quota.
- **Tool descriptions say when to use which tool,** which matters more for agent accuracy than the schemas do.

### Data minimisation
- Private (internal) notes are **hidden by default** (`FRESHDESK_INCLUDE_PRIVATE_NOTES=1` to enable). They often contain internal escalation details that a customer-facing agent shouldn't repeat. The response reports `hidden_private_notes: N` so the agent knows something was withheld.
- `FRESHDESK_REDACT_PII=1` masks emails (`a***@example.com`) and phone numbers in all output, for deployments where the model provider shouldn't see raw customer contact details.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FRESHDESK_DOMAIN` | (required) | Subdomain, e.g. `acme` (also accepts `acme.freshdesk.com`) |
| `FRESHDESK_API_KEY` | (required) | API key of the Freshdesk agent the connector acts as |
| `FRESHDESK_MOCK` | `0` | `1` = use built-in fictional data |
| `FRESHDESK_RATE_LIMIT_PER_MIN` | `50` | Client-side request budget |
| `FRESHDESK_MAX_RETRY_WAIT_SECONDS` | `30` | Longest the connector will wait on a 429 before returning an error |
| `FRESHDESK_MAX_RETRIES` | `3` | Retries for 429/5xx/network errors |
| `FRESHDESK_INCLUDE_PRIVATE_NOTES` | `0` | Expose internal notes to the agent |
| `FRESHDESK_REDACT_PII` | `0` | Mask emails and phone numbers |
| `CONNECTOR_AUTH_TOKEN` | (none) | Required for `--transport http` |

## Testing

`pytest` runs 31 tests in under a second, with no network access. They use an in-process fake Freshdesk (`mock.py`) that reproduces Basic auth, Link-header pagination, `include=`, search syntax, rate-limit headers and 401/404/429 responses, plus a fake `sleep` so retry and backoff timing is asserted exactly:
- `test_client.py`: auth header, 401 mapping, Retry-After honoured, long Retry-After fails fast, retry cap, 5xx backoff, network errors, pagination, rate-limit header tracking, token bucket pacing, domain validation
- `test_query.py`: query compilation, injection attempts, enum/date validation, length limit
- `test_tools.py`: end-to-end tool calls, read-only annotations, private-note hiding, PII redaction, customer→tickets flow, error codes

Manually verified: the stdio transport via `scripts/demo.py`, and the HTTP transport (401 without or with a wrong token, successful MCP session with the token).

## Assumptions

- Agent Studio can register an external MCP server over streamable HTTP with a static `Authorization` header. If it instead expects OpenAPI-style function specs, `mcp-tools.json` already carries the JSON Schemas needed to map each tool 1:1.
- One connector instance serves one merchant (one Freshdesk domain and key). Multi-tenant hosting would add a per-tenant credential lookup keyed by the inbound token (see Limitations).
- The Freshdesk agent behind the API key has been given the minimum role required. The connector cannot see more than that agent can.
- The ticket types, tags and custom fields used in examples are illustrative; they vary per helpdesk.

## Limitations

- **Read-only.** No replies, notes, status changes, assignments or ticket creation (see CAPABILITIES.md).
- **Search constraints come from Freshdesk:** max 300 results per query (10 pages × 30), date filters at day granularity, no full-text search over ticket bodies, and the search index can lag recent changes by a few minutes.
- `list_tickets` without `updated_since` only returns tickets **created in the last 30 days** (Freshdesk default).
- `get_ticket` embeds at most 10 conversation entries; longer threads need `list_ticket_conversations`.
- Attachments are listed by name only; their content is not downloaded.
- Rate-limit state is in-memory per process. Several replicas sharing one Freshdesk account would need a shared limiter (e.g. Redis).
- Single static connector token. Production would use per-tenant tokens or OAuth between Agent Studio and the connector, with credentials stored in a secrets manager rather than environment variables.
- PII redaction is regex-based (emails, phone numbers). It does not catch names or addresses inside free text.
- Not yet exercised against every Freshdesk plan. Plan-specific behaviour (e.g. endpoints disabled on Free) surfaces as `[permission_denied]`.

## Project layout

```
src/freshdesk_connector/
  server.py       MCP tools, descriptions, error mapping
  client.py       HTTP client: auth, retries, 429 handling, pagination
  rate_limit.py   token bucket + adaptive slowdown from rate-limit headers
  query.py        safe search-query compiler
  normalize.py    agent-friendly output, private-note filtering, PII redaction
  config.py       env config and validation
  mock.py         fake Freshdesk API for demo mode and tests
  __main__.py     CLI: stdio / HTTP transport, bearer-token middleware
scripts/          demo client, tool-spec exporter
tests/            pytest suite
mcp-tools.json    generated MCP tool specification
CAPABILITIES.md   what the agent can and cannot do
```
