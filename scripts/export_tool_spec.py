"""Writes the MCP tool specification (names, descriptions, JSON Schemas, annotations) to mcp-tools.json."""

import json
from pathlib import Path

import anyio

from freshdesk_connector.server import INSTRUCTIONS, mcp


async def main() -> None:
    tools = await mcp.list_tools()
    spec = {
        "server": {"name": "freshdesk", "version": "0.1.0", "instructions": INSTRUCTIONS},
        "tools": [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools],
    }
    out = Path(__file__).resolve().parent.parent / "mcp-tools.json"
    out.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"Wrote {len(tools)} tools to {out}")


anyio.run(main)
