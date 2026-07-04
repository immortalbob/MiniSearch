"""
Mnemolis MCP Server
Exposes Mnemolis as an MCP tool server via Streamable HTTP transport.
Mounted at /mcp by the FastAPI app in main.py.

Migrated from the low-level Server class + manual SSE transport wiring to
FastMCP + Streamable HTTP. Two real, independent reasons for this:

1. SSE is explicitly being superseded by Streamable HTTP across the MCP
   ecosystem — official FastMCP docs state "SSE exists only for backward
   compatibility and shouldn't be used in new projects." Patching the old
   transport would have meant polishing something already on its way out.

2. The old implementation manually accessed `request._send`, a private
   Starlette attribute, while wiring up SseServerTransport by hand. That
   pattern does match the official SDK's own low-level reference examples
   (it's not a Mnemolis-specific shortcut), but FastMCP's high-level
   `streamable_http_app()` avoids the need for any application code to
   touch private Starlette internals at all — that access still happens
   inside the SDK, but it's the SDK's concern to manage, not ours.

A real, deliberate tradeoff made during this migration: the tool's
`source` parameter is typed as a plain `str`, not an `Enum` or `Literal`,
even though the original implementation had a real JSON Schema `enum`
constraint. This is NOT an oversight — FastMCP's `@mcp.tool()` decorator
currently has no supported way to register a fully custom inputSchema
(open upstream issue, no workaround as of this writing), so the schema is
inferred entirely from type hints. Using Enum/Literal here would generate
a `$ref`/`$defs`-based schema that has an open, real compatibility bug
with at least one real client (a $ref resolution failure rejecting the
tool entirely). A plain `str` with the valid values documented in the
docstring sidesteps that specific bug class, at the real cost of losing
schema-level enum enforcement — invalid values are now only caught by
Mnemolis's own routing logic, not at the MCP protocol layer. Worth
revisiting if/when the upstream SDK issue is resolved.
A real, separate, more serious concern found during research but NOT
something this migration could fix on Mnemolis's side: an open upstream
issue (modelcontextprotocol/python-sdk#737) describes a race condition
where the StreamableHTTPSessionManager can report "shutting down"
immediately after a request starts, before the response is fully
streamed — causing empty/broken responses under certain timing
conditions. This is a genuine, currently-unresolved concern with the
underlying transport itself, not something our own session-manager-reset
fix touches. Worth monitoring for real-world reports of truncated MCP
responses; if seen, check that upstream issue for a resolution before
assuming it's a Mnemolis-side bug.
A second real bug found via actual MCP client testing (MCP Inspector),
not caught by the test suite at all: FastMCP's `transport_security`
defaults to DNS-rebinding protection that only allows the `Host` header
to be `127.0.0.1`/`localhost`/`::1` — auto-enabled specifically because
FastMCP's own `host` constructor parameter defaults to `127.0.0.1`. Since
Mnemolis is explicitly designed to be reached over a real LAN (the whole
point of running it as a homelab service), every real-network connection
attempt was being rejected with "Invalid Host header" before ever
reaching the actual tool logic. `TestClient`-based tests never caught
this because TestClient addresses the app as `testserver` internally,
never exercising real Host-header validation at all — this is exactly
the kind of gap real client testing exists to catch. Fixed by explicitly
disabling DNS-rebinding protection via `transport_security`, since
Mnemolis already assumes a trusted local network (see the README's API
key authentication section for the actual, intended trust model).
"""

import asyncio
import logging
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

from app.router import route, route_query

_LOGGER = logging.getLogger(__name__)

mcp = FastMCP(
    "mnemolis",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    # FastMCP's own internal Streamable HTTP route defaults to "/mcp" —
    # since main.py's app.mount("/mcp", mcp_app) already provides that
    # prefix, leaving this at the default produced a real, found-via-
    # actual-client-testing bug: a doubled path, http://host:8888/mcp/mcp,
    # not the documented http://host:8888/mcp. TestClient-based tests
    # never caught this because they call the app object directly by
    # Python reference, not by constructing and parsing a real URL path —
    # exactly the kind of gap real MCP client testing (MCP Inspector)
    # exists to catch. Setting this to "/" makes main.py's own "/mcp"
    # mount the only "/mcp" in the final, effective path.
    streamable_http_path="/",
)


@mcp.tool()
async def search(
    query: str,
    source: str = "auto",
    fusion_sources: list[str] | None = None,
    synthesize: bool = False,
    answer_style: str = "brief",
) -> str:
    """Search across local and remote knowledge sources via Mnemolis.

    Automatically selects the best source based on the query.

    Args:
        query: The search query or question.
        source: The source to query. One of: 'auto' (default, lets
            Mnemolis decide), 'kiwix' (offline knowledge), 'forecast'
            (weather), 'news' (RSS articles), 'web' (live search),
            'uptime' (service status), 'ha' (Home Assistant entity
            states), 'fusion' (query multiple sources concurrently —
            LLM picks the best 2-3 sources, or specify with
            fusion_sources).
        fusion_sources: Optional list of source names to fuse. Only used
            when source='fusion'. If omitted, the LLM picks the best 2-3
            sources automatically.
        synthesize: When true (and the server has SYNTHESIS_ENABLED set),
            the local LLM composes a short, grounded answer from the
            retrieved material and returns THAT instead of the raw
            source text. If synthesis is skipped, fails, or is disabled,
            the raw retrieved result is returned unchanged — this
            argument can only ever improve the reply, never degrade it.
        answer_style: Shape of the synthesized answer when synthesize is
            true. One of: 'voice' (≤2 sentences, for text-to-speech),
            'brief' (one short paragraph, default), 'detailed' (a few
            paragraphs), or 'digest' (preserves many distinct items rather
            than fusing them — use for "summarize", "list", or "read me
            all the ..." requests where breadth matters more than a single
            answer).
    """
    _LOGGER.info("MCP search: query=%r source=%r synthesize=%r", query[:80], source, synthesize)

    if synthesize:
        # route_query() runs synthesis inside its own stats context and
        # returns both the raw result and the synthesized answer. Return
        # the answer when one was produced, otherwise fall back to the
        # raw result — the same `answer ?? result` contract the voice
        # pipeline uses (Design Doc 4 constraint #1). Unlike route()
        # below, route_query() never raises (it catches and reports
        # failure in-band), so a routing bug surfaces here as an error
        # string rather than an MCP protocol-level isError — an
        # acceptable, documented difference on this opt-in path.
        outcome = await asyncio.to_thread(
            route_query, query, source, fusion_sources, synthesize, answer_style
        )
        return outcome.answer if outcome.answer is not None else outcome.result

    # No try/except here — deliberate. route() already returns descriptive
    # strings for all expected, recoverable failures (source not configured,
    # empty results, unknown source name) rather than raising, so those are
    # handled transparently. A genuine, unexpected exception (a real bug in
    # routing or source logic) propagates to the SDK's own handler one level
    # up, which converts it to CallToolResult(isError=True, ...) — giving
    # a real MCP client a structured way to distinguish "Mnemolis answered
    # with bad news" (isError=False, content starts with "Error: ...") from
    # "the call itself genuinely failed" (isError=True). The previous broad
    # try/except made both cases look identical at the protocol level, which
    # was the tracked gap in the roadmap and wiki/MCP-Server.md. Confirmed
    # directly: asyncio.to_thread propagates exceptions correctly, and the
    # SDK's CallToolRequest handler catches them and calls _make_error_result.
    result = await asyncio.to_thread(route, query, source, fusion_sources)
    return result


def get_mcp_app():
    """
    Build (or rebuild) the Streamable HTTP ASGI app, with a fresh session
    manager each time this is called.

    A real, genuine bug found while migrating off the old SSE transport:
    `FastMCP.streamable_http_app()` lazily creates ONE session manager and
    caches it on the FastMCP instance (`if self._session_manager is None`)
    — calling streamable_http_app() again still reuses that same cached
    manager. StreamableHTTPSessionManager.run() can only be entered once
    per instance, ever; entering it a second time raises a hard
    RuntimeError. The old SSE transport had no equivalent restriction.

    This is a real, currently-open issue across the broader MCP/FastMCP
    ecosystem, not something specific to how Mnemolis is built — multiple
    independent reports describe the exact same RuntimeError, both in
    test suites using TestClient (each `with TestClient(app) as client:`
    re-runs the full app lifespan from scratch) and in real production
    deployments under certain conditions (concurrent startup, serverless
    cold starts). Resetting `mcp._session_manager` to None before each
    call to `streamable_http_app()` forces a fresh session manager to be
    created, which is the actual root cause fix rather than a narrow
    workaround — it directly addresses why .run() could only be called
    once (a stale cached reference reused across what should be
    independent app lifecycles), mirroring how the snapshot scheduler in
    main.py's lifespan is already freshly created on every startup rather
    than reused as a module-level singleton.

    Verified directly: three consecutive simulated app lifecycles
    (entering and exiting this app's lifespan context) all completed
    cleanly with this reset in place, where they previously failed on the
    second lifecycle without it.
    """
    mcp._session_manager = None
    return mcp.streamable_http_app()


mcp_app = get_mcp_app()


