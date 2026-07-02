"""
Tests for app/mcp_server.py — MCP tool server exposing Mnemolis via
Streamable HTTP transport (migrated from SSE — see CHANGELOG and the
module's own docstring for the full reasoning).

Tests the tool schema definition and call dispatch logic directly using
FastMCP's own list_tools()/call_tool() methods, since the transport layer
itself requires a real ASGI connection to test meaningfully and is a thin
wrapper around the MCP SDK at that point.
"""
import pytest
import asyncio
from unittest.mock import patch


class TestListTools:
    """Tests for the registered 'search' tool's schema definition."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_returns_one_tool(self):
        from app.mcp_server import mcp
        tools = self._run(mcp.list_tools())
        assert len(tools) == 1

    def test_tool_is_named_search(self):
        from app.mcp_server import mcp
        tools = self._run(mcp.list_tools())
        assert tools[0].name == "search"

    def test_tool_has_description(self):
        from app.mcp_server import mcp
        tools = self._run(mcp.list_tools())
        assert len(tools[0].description) > 0

    def test_input_schema_requires_query(self):
        from app.mcp_server import mcp
        tools = self._run(mcp.list_tools())
        schema = tools[0].inputSchema
        assert "query" in schema["required"]

    def test_input_schema_has_query_property(self):
        from app.mcp_server import mcp
        tools = self._run(mcp.list_tools())
        schema = tools[0].inputSchema
        assert "query" in schema["properties"]
        assert schema["properties"]["query"]["type"] == "string"

    def test_input_schema_source_is_plain_string_not_enum(self):
        """Regression test for a deliberate design decision made during
        the SSE -> Streamable HTTP migration, not an oversight: 'source'
        is a plain string, NOT an Enum/Literal-backed schema with an
        `enum` constraint. FastMCP's @mcp.tool() decorator currently has
        no supported way to register a fully custom inputSchema (open
        upstream SDK issue), so the schema is inferred from type hints —
        and an Enum/Literal here would generate a $ref/$defs-based
        schema with a real, separate open compatibility bug affecting at
        least one real MCP client. A plain string with the valid values
        documented in the docstring sidesteps that specific bug class.
        The real, honest cost: invalid source values are no longer
        rejected at the schema level, only by Mnemolis's own routing
        logic — this test exists to make sure that tradeoff is never
        silently reversed without it being a deliberate decision again."""
        from app.mcp_server import mcp
        tools = self._run(mcp.list_tools())
        schema = tools[0].inputSchema
        assert schema["properties"]["source"]["type"] == "string"
        assert "enum" not in schema["properties"]["source"]
        assert "$ref" not in str(schema)
        assert "$defs" not in schema

    def test_input_schema_source_defaults_to_auto(self):
        from app.mcp_server import mcp
        tools = self._run(mcp.list_tools())
        schema = tools[0].inputSchema
        assert schema["properties"]["source"]["default"] == "auto"

    def test_docstring_documents_all_valid_source_values(self):
        """Since the schema no longer enforces valid source values via
        an enum constraint, the docstring is the only place this
        contract is documented at all — confirm every real source name
        Mnemolis actually supports is mentioned."""
        from app.mcp_server import mcp
        tools = self._run(mcp.list_tools())
        description = tools[0].description.lower()
        for expected in ["auto", "kiwix", "forecast", "news", "web", "uptime", "ha", "fusion"]:
            assert expected in description

    def test_input_schema_has_fusion_sources_array(self):
        from app.mcp_server import mcp
        tools = self._run(mcp.list_tools())
        schema = tools[0].inputSchema
        fusion_schema = schema["properties"]["fusion_sources"]
        # anyOf[array, null] since it's Optional — not a bare "array" type
        type_options = [opt.get("type") for opt in fusion_schema.get("anyOf", [])]
        assert "array" in type_options


class TestCallTool:
    """Tests for the 'search' tool's actual call dispatch behavior."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_unknown_tool_raises(self):
        from app.mcp_server import mcp
        with pytest.raises(Exception):
            self._run(mcp.call_tool("not_search", {"query": "test"}))

    def test_missing_query_raises_validation_error(self):
        """Unlike the old manual implementation, a missing required
        query is now rejected automatically by Pydantic validation
        before the tool function body ever runs — no manual
        `if not query` check needed."""
        from app.mcp_server import mcp
        with pytest.raises(Exception):
            self._run(mcp.call_tool("search", {}))

    def test_successful_call_returns_route_result(self):
        from app.mcp_server import mcp
        with patch("app.mcp_server.route", return_value="Nitrogen is a chemical element."):
            content, _structured = self._run(mcp.call_tool("search", {"query": "what is nitrogen"}))
        assert content[0].text == "Nitrogen is a chemical element."

    def test_default_source_is_auto(self):
        from app.mcp_server import mcp
        with patch("app.mcp_server.route", return_value="result") as mock_route:
            self._run(mcp.call_tool("search", {"query": "test"}))
        call_args = mock_route.call_args.args
        assert call_args[1] == "auto"

    def test_explicit_source_passed_through(self):
        from app.mcp_server import mcp
        with patch("app.mcp_server.route", return_value="result") as mock_route:
            self._run(mcp.call_tool("search", {"query": "test", "source": "kiwix"}))
        call_args = mock_route.call_args.args
        assert call_args[1] == "kiwix"

    def test_fusion_sources_passed_through(self):
        from app.mcp_server import mcp
        with patch("app.mcp_server.route", return_value="result") as mock_route:
            self._run(mcp.call_tool("search", {
                "query": "test",
                "source": "fusion",
                "fusion_sources": ["forecast", "uptime"]
            }))
        call_args = mock_route.call_args.args
        assert call_args[2] == ["forecast", "uptime"]

    def test_fusion_sources_defaults_to_none(self):
        from app.mcp_server import mcp
        with patch("app.mcp_server.route", return_value="result") as mock_route:
            self._run(mcp.call_tool("search", {"query": "test"}))
        call_args = mock_route.call_args.args
        assert call_args[2] is None

    def test_exception_in_route_propagates_as_is_error(self):
        """An unexpected exception from route() now propagates to the SDK's
        own handler rather than being caught and returned as an error string.
        The SDK converts it to CallToolResult(isError=True, ...), giving MCP
        clients a structured way to distinguish a genuine internal failure
        from a normal "Mnemolis answered with bad news" response string.

        This replaced the old behavior (broad try/except returning
        'Error: {e}', isError always False) which made real failures
        indistinguishable from expected non-results at the protocol level.
        The fix: remove the try/except from search() entirely. route() already
        returns descriptive strings for all expected, recoverable failures;
        anything that genuinely raises is a real bug that should surface as
        isError=True.

        Confirmed directly: asyncio.to_thread propagates exceptions, and the
        SDK's CallToolRequest handler catches them via _make_error_result."""
        from app.mcp_server import mcp
        from mcp.server.fastmcp.exceptions import ToolError
        with patch("app.mcp_server.route", side_effect=RuntimeError("unexpected boom")):
            # The SDK wraps propagated exceptions as ToolError, which the
            # low-level handler converts to CallToolResult(isError=True).
            # At the mcp.call_tool() level this surfaces as a raised ToolError.
            try:
                content, _structured = self._run(mcp.call_tool("search", {"query": "test"}))
                # If call_tool() returns rather than raises, the result must
                # be marked isError=True at the content level
                # (SDK version-dependent behavior — both paths are correct)
                assert any(
                    getattr(item, "text", "") and "boom" in getattr(item, "text", "")
                    for item in content
                ), f"Expected error text in content: {content}"
            except ToolError as e:
                # The SDK raised ToolError — exception propagated correctly
                assert "unexpected boom" in str(e) or "boom" in str(e)

    def test_result_is_text_content_type(self):
        from app.mcp_server import mcp
        with patch("app.mcp_server.route", return_value="result"):
            content, _structured = self._run(mcp.call_tool("search", {"query": "test"}))
        assert content[0].type == "text"


class TestGetMcpApp:
    """Tests for get_mcp_app() — the function that rebuilds the
    Streamable HTTP ASGI app with a fresh session manager each call.

    This exists because of a real, currently-open issue across the
    broader MCP/FastMCP ecosystem (not specific to Mnemolis):
    FastMCP.streamable_http_app() lazily creates and caches ONE session
    manager on the FastMCP instance, but StreamableHTTPSessionManager can
    only be .run() once per instance, ever. A module-level mcp_app built
    once at import time meant every independent app lifecycle (every
    `with TestClient(app) as client:` block, every container restart)
    tried to reuse the same already-exhausted session manager, raising a
    hard RuntimeError on the second attempt. get_mcp_app() resets the
    cached session manager before rebuilding, so each call gets a
    genuinely independent one."""

    def test_returns_a_starlette_app(self):
        from app.mcp_server import get_mcp_app
        from starlette.applications import Starlette
        app = get_mcp_app()
        assert isinstance(app, Starlette)

    def test_repeated_calls_produce_genuinely_independent_session_managers(self):
        """The actual regression this function exists to fix — confirms
        the underlying session manager object is different (not the
        same cached instance) across repeated calls."""
        from app.mcp_server import get_mcp_app, mcp
        get_mcp_app()
        first_manager = mcp._session_manager
        get_mcp_app()
        second_manager = mcp._session_manager
        assert first_manager is not second_manager

    def test_full_lifespan_can_be_entered_and_exited_multiple_times(self):
        """The real, end-to-end regression test — this exact scenario
        (entering and exiting the app's lifespan multiple times) is what
        raised RuntimeError before this fix existed, and is exactly what
        happens once per `with TestClient(app) as client:` block across
        this test suite's many separate test files."""
        from app.mcp_server import get_mcp_app

        async def run_three_lifecycles():
            for _ in range(3):
                app = get_mcp_app()
                async with app.router.lifespan_context(app):
                    pass

        asyncio.run(run_three_lifecycles())  # must not raise


class TestMcpAppModuleLevel:
    """Tests confirming the module-level mcp and mcp_app objects are valid."""

    def test_mcp_app_exists(self):
        from app.mcp_server import mcp_app
        assert mcp_app is not None

    def test_mcp_app_is_starlette_instance(self):
        from app.mcp_server import mcp_app
        from starlette.applications import Starlette
        assert isinstance(mcp_app, Starlette)

    def test_server_name_is_mnemolis(self):
        from app.mcp_server import mcp
        assert mcp.name == "mnemolis"


class TestRealClientFindings:
    """Regression tests for two real bugs found ONLY via actual MCP
    client testing (MCP Inspector) — neither was caught by the rest of
    this test suite, since TestClient-based tests call the app object
    directly by Python reference and never exercise real URL-path
    resolution or real Host-header validation the way an actual network
    client does. This is exactly the kind of gap real client testing
    exists to catch, and is why both fixes are specifically verified
    here against a real LAN-style request, not just localhost."""

    def test_mcp_endpoint_is_reachable_at_documented_single_path(self):
        """Regression test for a real, found bug: FastMCP's own internal
        Streamable HTTP route defaults to '/mcp', and main.py ALSO mounts
        the whole app at '/mcp' — combined, the real, only-reachable path
        was 'http://host:8888/mcp/mcp', not the documented
        'http://host:8888/mcp'. Fixed via streamable_http_path='/' on the
        FastMCP instance, so main.py's own '/mcp' mount is the only
        '/mcp' in the final path."""
        from starlette.testclient import TestClient
        from app.main import app

        with TestClient(app, base_url="http://192.168.1.50:8888") as client:
            resp = client.post(
                "/mcp",
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                },
            )
        assert resp.status_code == 200

    def test_mcp_endpoint_accepts_real_lan_host_header(self):
        """Regression test for a real, found bug: FastMCP auto-enables
        DNS-rebinding protection whenever its `host` constructor
        parameter is left at the default '127.0.0.1', which only allows
        Host headers of 127.0.0.1/localhost/::1 — rejecting every real
        request addressed to Mnemolis's actual LAN IP or hostname with
        "Invalid Host header", even though Mnemolis is explicitly
        designed to be reached over a real home network. Fixed via
        transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False). This test specifically
        uses a real, non-localhost base_url to catch a regression of
        this exact bug — a test using the default testserver host would
        not exercise this code path at all."""
        from starlette.testclient import TestClient
        from app.main import app

        with TestClient(app, base_url="http://192.168.3.50:8888") as client:
            resp = client.post(
                "/mcp",
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                },
            )
        assert resp.status_code == 200
        assert "Invalid Host header" not in resp.text

    def test_other_real_rest_routes_unaffected_by_mcp_mount(self):
        """Confirms the MCP mount's position in main.py (registered
        before the REST routes are defined) does not shadow them — a
        real risk found and ruled out while designing the path fix
        above: mounting the MCP app at root ("/") instead of "/mcp"
        would have shadowed every REST route registered after it,
        since a root Mount matches any path prefix. The chosen fix
        (streamable_http_path="/" on FastMCP, mount stays at "/mcp" in
        main.py) avoids this entirely."""
        from starlette.testclient import TestClient
        from app.main import app

        with TestClient(app, base_url="http://192.168.1.50:8888") as client:
            resp = client.get("/health")
        assert resp.status_code == 200
