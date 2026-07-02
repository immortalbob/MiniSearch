# MCP Server

Mnemolis exposes everything through two interfaces at once: a REST API and an [MCP](https://modelcontextprotocol.io/) server, both backed by the exact same routing/decomposition/fusion logic underneath. The REST API is what Home Assistant and most automation scripts use; MCP is what lets Claude Desktop, Cursor, or any other MCP-aware client query Mnemolis directly as a tool.

## What's actually exposed

The MCP server exposes exactly **one tool**, called `search`, built with FastMCP's decorator-based registration:

```python
@mcp.tool()
async def search(
    query: str,
    source: str = "auto",
    fusion_sources: list[str] | None = None,
) -> str:
    """Search across local and remote knowledge sources via Mnemolis. ..."""
```

This mirrors `POST /search`'s own request shape almost exactly — `query`, `source`, `fusion_sources` mean the same thing in both places, and the same [Routing](Routing), [Query Decomposition](Query-Decomposition), and [Conditional Query Detection](Conditional-Query-Detection) logic runs underneath regardless of which interface the call came through.

**A real, deliberate tradeoff worth knowing about:** `source` is typed as a plain `str`, not an `Enum`/`Literal`, even though the values are genuinely constrained to a fixed set (`auto`, `kiwix`, `forecast`, `news`, `web`, `uptime`, `ha`, `fusion`). This isn't an oversight — FastMCP's `@mcp.tool()` decorator currently has no supported way to register a fully custom JSON Schema (an open, unresolved upstream SDK issue), so the schema is inferred entirely from type hints. Using `Enum`/`Literal` here would generate a `$ref`/`$defs`-based schema with a separate, real, open compatibility bug that gets at least one real MCP client to reject the tool outright. A plain string with the valid values documented in the docstring sidesteps that specific bug, at the honest cost of losing schema-level enforcement — an invalid `source` value is now only caught by Mnemolis's own routing logic, not at the protocol layer.

## A real difference worth knowing about

MCP's `search` tool calls `route()`, the simpler, backward-compatible wrapper that returns just the result string — not `route_with_source()`, which is what the REST API's `/search` endpoint uses to also report `source_used` (including whether a [fallback](Routing#fallback--when-a-source-comes-back-empty) occurred). This means an MCP client gets the plain answer text, the same content a REST caller would get, but **never** learns which source actually answered, or whether a fallback happened along the way. If you need that metadata, you need the REST API — MCP's contract is intentionally just "ask a question, get an answer," with no provenance attached.

**Expected failures come back as a successful response with descriptive text — genuine unexpected failures now surface as `isError=True`.** The MCP protocol’s own `CallToolResult` has a real `isError` boolean for this — and the underlying SDK uses it: an uncaught exception inside a tool function propagates to the SDK’s own dispatcher, which calls `_make_error_result()` and returns `CallToolResult(isError=True, ...)`.

Mnemolis’s `search` tool now takes full advantage of this distinction. All expected, recoverable failures — a source not configured, an unreachable backend, empty results, an unknown source name — are handled by `route()` itself, which returns a descriptive string rather than raising. Those come back as a normal successful response whose content explains what happened. A genuine, unexpected exception (a real bug in routing or source logic) propagates to the SDK and surfaces as `isError=True`, giving a real MCP client a structured way to tell the two cases apart.

Previously `search()` had a broad `try/except` that caught everything and returned `"Error: {e}"`, so `isError` was always `False` regardless of what actually happened underneath. That made a real internal failure indistinguishable from a normal “source not configured” response at the protocol level. The fix was simply removing the `try/except` — `route()` doesn’t raise for expected failures, and `asyncio.to_thread` propagates genuine exceptions correctly. If you’re building automation against this tool: check `isError` to detect real failures; check the response text for expected non-results like misconfigured sources.
## How it's actually transported

MCP runs over **Streamable HTTP**, mounted at `/mcp` on the main FastAPI app as a sub-application — shares the same container, port, and network exposure as the REST API, no separate process to manage.

This wasn't always the case — Mnemolis originally used SSE transport, until an external community-run audit flagged a private-API pattern in it, prompting a full migration to Streamable HTTP. The migration itself surfaced a real upstream bug in how `FastMCP.streamable_http_app()` caches its session manager, found and fixed before it ever shipped — and the very first real client connection attempted right after surfaced two more bugs (a doubled endpoint path, a real LAN-connection rejection) that no in-process test could have caught. Both are fixed; the URL below has been correct and reachable over a real network since. See [The MCP Transport Migration](The-MCP-Transport-Migration) for the full history.

## Connecting a client

**Claude Desktop** — add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mnemolis": {
      "url": "http://your-host-ip:8888/mcp"
    }
  }
}
```

**Any other MCP client** — point it at the same Streamable HTTP endpoint, `http://your-host-ip:8888/mcp`.

## Why one tool instead of several

It would be possible to expose `search_kiwix`, `search_weather`, `search_news`, and so on as separate MCP tools, letting the calling model pick directly. Mnemolis deliberately doesn't do this — the entire point of [Routing](Routing) is that Mnemolis itself is better positioned to decide which source(s) actually apply than a general-purpose model guessing from tool names alone, especially once [Query Decomposition](Query-Decomposition) and [Fusion](Fusion) are involved. A single `search` tool keeps that decision where it belongs.

This is also exactly the reasoning behind the [Open WebUI System Prompt Guide](Open-WebUI-System-Prompt-Guide) — the real, observed failure mode for tool-calling models isn't picking the wrong tool, it's *pre-splitting* a compound question into multiple narrow calls (or just answering part of it) before Mnemolis's own decomposition logic ever gets a chance to run on the full, original question.
