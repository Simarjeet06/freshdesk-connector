"""Spawns the connector over stdio (like Agent Studio or Claude Desktop would) and walks
through a typical support-agent workflow against the built-in mock data.

    python scripts/demo.py            # mock data
    python scripts/demo.py --live     # your real Freshdesk (needs .env)
"""

import json
import os
import sys

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main(live: bool) -> None:
    env = dict(os.environ)
    if not live:
        env["FRESHDESK_MOCK"] = "1"
    params = StdioServerParameters(command=sys.executable, args=["-m", "freshdesk_connector"], env=env)

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        print("Tools:", ", ".join(t.name for t in tools.tools), "\n")

        async def step(title: str, tool: str, args: dict) -> dict | None:
            print(f"== {title}\n   -> {tool}({json.dumps(args)})")
            res = await session.call_tool(tool, args)
            text = res.content[0].text if res.content else ""
            if res.is_error:
                print(f"   ERROR: {text}\n")
                return None
            data = json.loads(text)
            print("   " + json.dumps(data, indent=2)[:900].replace("\n", "\n   ") + "\n")
            return data

        await step("Health check", "connector_status", {})
        await step("Urgent open payment issues", "search_tickets", {"status": ["open"], "priority": ["urgent"], "tags": ["payment"]})
        await step("Full context for one ticket", "get_ticket", {"ticket_id": 101})
        c = await step("Find customer by email", "find_contacts", {"email": "diya.sharma@example.com"})
        if c and c["contacts"]:
            await step("That customer's tickets", "list_tickets", {"requester_id": c["contacts"][0]["id"]})
        await step("Bad input is rejected before hitting Freshdesk", "search_tickets", {"tags": ["refund' OR status:5"]})
        await step("Unknown ticket", "get_ticket", {"ticket_id": 999999})


if __name__ == "__main__":
    anyio.run(main, "--live" in sys.argv)
