"""
hello_world — trivial stand-in tool for a real MCP tool.

Proves the connector chain (discovery -> OAuth login -> token -> tool call ->
usage counter) end to end without depending on any upstream data source.
Replace this module (and TOOLS in tools/__init__.py) with real tools once
that chain is confirmed working — see docs/design-notes.md §4 build order.
"""

NAME = "hello_world"
DESCRIPTION = "Returns a greeting. Takes an optional 'name' argument."
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Name to greet. Defaults to 'world'."},
    },
}


def call(arguments: dict, context) -> dict:
    name = arguments.get("name") or "world"
    return {"content": [{"type": "text", "text": f"Hello, {name}!"}], "isError": False}
