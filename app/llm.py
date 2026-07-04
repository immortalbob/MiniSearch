"""
LLM client helper — supports both Ollama native API and OpenAI-compatible API.
Used by router.py and kiwix.py for routing and book selection calls.

Supported backends via LLM_API_TYPE:
  "ollama"  — Ollama native /api/generate (default)
  "openai"  — OpenAI-compatible /v1/chat/completions (llama-server, LM Studio, etc.)
"""

import logging
import requests
from app.config import settings

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persistent HTTP session — connection reuse across every LLM call
# ---------------------------------------------------------------------------
#
# Found while investigating why singleflight (v3.50.13) didn't move `auto`'s
# cold-path p99 plateau, despite directly confirming the deduplication
# mechanism itself works in isolation: every single call into this module
# used the bare `requests.post()` module function, never a `requests.Session`
# — meaning every LLM call (book selection, source routing, fusion-source
# selection, disambiguation candidates) opened a brand-new TCP connection to
# the LLM backend and tore it down again immediately after, on every single
# call, with zero reuse. This is the identical class of bug
# `uptime_kuma.py`'s own connection used to have (a fresh Socket.IO
# connect+login cycle on every call) before that was found and fixed — see
# wiki/Caching.md's "Why uptime's connection is persistent..." section and
# wiki/The-Benchmark-Investigation-Log.md's Thread 1 for the full history of
# that fix and how long it took to actually find, despite looking like a
# caching problem at first.
#
# This module's case is structurally simpler than Uptime Kuma's — there's no
# login/session state to track and no liveness check needed (a plain HTTP
# connection pool has no equivalent of "logged in vs not"; `requests`' own
# adapter already transparently opens a fresh connection if a pooled one has
# gone stale or dead), so a single, eagerly-created module-level `Session`
# is sufficient. requests.Session() is genuinely safe for concurrent READ-
# ONLY use across threads — confirmed against the library maintainers' own
# stated position (a real psf/requests GitHub issue on this exact question),
# which is more precise than the library's own homepage "thread-safe" bullet
# alone suggests: the underlying urllib3 connection pool is thread-safe per
# individual request, but the Session object's own shared mutable state
# (headers, cookies) is NOT safe under concurrent MUTATION — one thread
# changing session.headers while another reads it is the real, documented
# risk, not concurrent requests through an unmodified session. Verified this
# module's actual usage never crosses that line: grep-confirmed `_session`
# is only ever read from (`.post()`) after the two `.mount()` calls at
# import time below, never mutated again from anywhere. Mnemolis's own
# concurrent request model (FastAPI's /search route is synchronous, so
# Starlette already runs real concurrent requests on its own thread pool)
# makes this the actual, live concurrency shape this needs to be safe
# under, not a theoretical one.
#
# Plain module-level singleton, not a lazy-init-with-lock accessor like
# `uptime_kuma.get_connection()` — Session() construction does no I/O at
# all (it just builds an empty connection-pool adapter), so there's no
# "first caller pays a real connection cost" race to guard against the way
# Uptime Kuma's actual login call has.
#
# Pool size explicitly set via settings.llm_connection_pool_size rather
# than left at requests' own library default (10) — see that setting's
# own comment in app/config.py for the real concurrency numbers
# (Starlette's 40-thread default limiter; a 20-concurrent-user Locust
# benchmark) behind the chosen size. Mounted on both schemes since
# LLM_URL could plausibly be configured with either, even though every
# real deployment this project documents uses plain http://.
_session = requests.Session()
_pool_adapter = requests.adapters.HTTPAdapter(
    pool_connections=settings.llm_connection_pool_size,
    pool_maxsize=settings.llm_connection_pool_size,
)
_session.mount("http://", _pool_adapter)
_session.mount("https://", _pool_adapter)


def is_configured() -> bool:
    """Return True if an LLM backend is configured."""
    return bool(settings.llm_url and settings.llm_model)


def complete(prompt: str, max_tokens: int = 100, temperature: float = 0.0) -> str | None:
    """
    Send a prompt to the configured LLM backend and return the response text.
    Returns None on failure.

    Supports:
    - Ollama native API (LLM_API_TYPE=ollama)
    - OpenAI-compatible API (LLM_API_TYPE=openai)
    """
    if not is_configured():
        return None

    api_type = settings.llm_api_type.lower().strip()

    try:
        if api_type == "openai":
            return _complete_openai(prompt, max_tokens, temperature)
        else:
            return _complete_ollama(prompt, max_tokens, temperature)
    except Exception as e:
        _LOGGER.warning("LLM completion failed (%s): %s", api_type, e)
        return None


def _complete_ollama(prompt: str, max_tokens: int, temperature: float) -> str | None:
    """Call Ollama native /api/generate endpoint.

    Sends keep_alive — see settings.llm_keep_alive's own comment in
    app/config.py for why this exists and why it's configurable to any
    value Ollama itself accepts (a duration string, plain seconds, "-1"
    for never-unload, "0" for unload-immediately), not a fixed Mnemolis-
    specific shape.

    "think": False is sent as a TOP-LEVEL request field, not nested
    inside "options" — confirmed via real-world research this placement
    matters for at least one real Ollama bug class, not just style:
    multiple independently-reported issues against the newer qwen3.5/
    qwen3-vl model family describe /api/generate ignoring think:false
    specifically when it's nested under options, with the model burning
    its entire token budget on hidden reasoning and returning a
    genuinely empty response field regardless of max_tokens. Checked
    specifically whether this affects this project's own documented
    model (qwen3:8b, not qwen3.5) before treating it as relevant here:
    it doesn't — the reported bug is scoped to qwen3.5/qwen3-vl's newer
    built-in renderer/parser mechanism, architecturally different from
    qwen3:8b's own template-based thinking-control logic, which
    correctly respects think:false regardless of nesting. Worth
    re-verifying this placement still matters (or doesn't) if this
    project's documented model ever changes to a qwen3.5-family model.
    """
    resp = _session.post(
        f"{settings.llm_url}/api/generate",
        json={
            "model": settings.llm_model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "keep_alive": settings.llm_keep_alive,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    # Handle thinking models (qwen3 etc) that return empty response with thinking field
    raw = data.get("response", "").strip()
    if not raw:
        thinking = data.get("thinking", "")
        lines = [line.strip() for line in thinking.splitlines() if line.strip()]
        raw = lines[-1] if lines else ""

    return raw.strip(".").strip() or None


def _complete_openai(prompt: str, max_tokens: int, temperature: float) -> str | None:
    """Call OpenAI-compatible /v1/chat/completions endpoint.

    Deliberately does NOT send keep_alive, unlike _complete_ollama()
    above. Confirmed via a real, externally-reported gap (not assumed):
    Ollama's own OpenAI-compatible endpoint silently ignores keep_alive
    when passed through OpenAI-SDK-style requests, falling back to
    whatever the server's own ambient default is regardless of what's
    sent — and a genuinely different OpenAI-compatible backend
    (llama-server, LM Studio) has no standard equivalent concept at all,
    since "keep a model resident in VRAM between calls" isn't a concern
    those typically expose as a per-request parameter the same way.
    Sending a field that's either silently dropped or meaningless to the
    actual backend would be a false promise of control this setting
    can't actually deliver on this path — left out rather than sent and
    hoped for.
    """
    resp = _session.post(
        f"{settings.llm_url}/v1/chat/completions",
        json={
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    choices = data.get("choices", [])
    if not choices:
        return None

    message = choices[0].get("message", {})
    raw = message.get("content", "").strip()

    # Found via a deliberate "bulletproofing" pass, confirmed against
    # multiple independent real-world bug reports of this exact failure
    # mode: thinking models served via an OpenAI-compatible endpoint
    # (the actual real backend this project uses — llama-server with
    # Qwen3-Coder-30B) routinely return an EMPTY content field with all
    # real output sitting in a separate reasoning_content field instead
    # — the same underlying problem _complete_ollama already has a
    # real, working fallback for via Ollama's own "thinking" field, just
    # never mirrored here. llama.cpp's server defaults to this exact
    # "deepseek" reasoning_format convention (message.reasoning_content),
    # which is also the convention most other OpenAI-compatible servers
    # use. Without this fallback, a thinking model on this code path
    # would silently return None for every single completion — not a
    # contrived edge case, but the literal default behavior for the
    # specific kind of model this project's own README documents using
    # on this backend.
    if not raw:
        # Defensive: reasoning_content/reasoning are expected as plain
        # strings per llama.cpp's own documented "deepseek" reasoning
        # format (confirmed via llama.cpp's real server README) — the
        # actual, documented target for this fallback. A different,
        # OpenAI-proper convention exists where `reasoning` is itself a
        # dict (e.g. {"effort": "none"}), distinct from the string-
        # shaped field this fallback is actually built for; .splitlines()
        # against a dict would raise. Not reachable through this
        # project's own documented backend (llama-server's real response
        # shape uses a plain string), but checked anyway since the outer
        # complete() already has a real safety net (its own
        # except Exception) — this just keeps the failure honestly
        # logged as "field wasn't usable" rather than a less specific
        # AttributeError, for free.
        reasoning = message.get("reasoning_content", "") or message.get("reasoning", "")
        if not isinstance(reasoning, str):
            reasoning = ""
        lines = [line.strip() for line in reasoning.splitlines() if line.strip()]
        raw = lines[-1] if lines else ""

    return raw.strip(".").strip() or None


# ---------------------------------------------------------------------------
# Full generations — for answer synthesis
# ---------------------------------------------------------------------------

def generate(
    prompt: str,
    max_tokens: int = 512,
    temperature: float = 0.1,
    timeout: int = 20,
    model: str | None = None,
) -> str | None:
    """Send a prompt expecting a full, multi-sentence generation and
    return the response text, or None on any failure.

    Deliberately a separate entry point from complete(), not a
    parameter on it, because complete()'s post-processing is actively
    WRONG for a real generation and exists specifically for one-token
    routing picks:

    - complete() strips trailing periods (`.strip(".")`) so a routing
      answer like "kiwix." normalizes to "kiwix" — applied to a real
      answer it amputates the final sentence's punctuation.
    - complete()'s empty-response fallback takes the LAST LINE of the
      model's hidden thinking field — a reasonable salvage when the
      expected answer is one word, but for a synthesized answer the
      last line of chain-of-thought is exactly the kind of ungrounded
      text the synthesis gates exist to keep out. Here an empty
      response is honestly returned as None instead (gate 1 rejects it
      and the caller gets today's exact behavior), never salvaged.
    - complete()'s timeout is a hardcoded 10s sized for routing picks;
      a synthesis generation gets its own caller-supplied budget.

    `model` overrides settings.llm_model when set (SYNTHESIS_MODEL's
    split-model support); backend selection, keep_alive handling, and
    the persistent session are identical to complete()'s — see those
    functions' comments for the reasoning behind each.
    """
    if not is_configured():
        return None

    use_model = model or settings.llm_model
    api_type = settings.llm_api_type.lower().strip()

    try:
        if api_type == "openai":
            resp = _session.post(
                f"{settings.llm_url}/v1/chat/completions",
                json={
                    "model": use_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "stream": False,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            choices = resp.json().get("choices", [])
            if not choices:
                return None
            raw = choices[0].get("message", {}).get("content", "") or ""
        else:
            resp = _session.post(
                f"{settings.llm_url}/api/generate",
                json={
                    "model": use_model,
                    "prompt": prompt,
                    "stream": False,
                    "think": False,
                    "keep_alive": settings.llm_keep_alive,
                    "options": {"temperature": temperature, "num_predict": max_tokens},
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "") or ""
        return raw.strip() or None
    except Exception as e:
        _LOGGER.warning("LLM generation failed (%s): %s", api_type, e)
        return None


# ---------------------------------------------------------------------------
# Embeddings — for the semantic routing cache
# ---------------------------------------------------------------------------

def embeddings_configured() -> bool:
    """Return True if an embedding model is configured.

    Deliberately a separate switch from is_configured() — routing/
    completion and embeddings are independent capabilities, and plenty
    of real deployments (including anyone trying this project fresh
    from GitHub) will run a chat model with no embedding model pulled
    at all. EMBEDDING_MODEL defaults to empty, which cleanly disables
    the semantic routing cache with zero behavior change rather than
    producing a failed HTTP call on every cold routing decision.
    """
    return bool(settings.embedding_model and (settings.embedding_url or settings.llm_url))


def embed(text: str) -> list[float] | None:
    """Return an embedding vector for `text`, or None on any failure.

    Backend selection mirrors complete(): LLM_API_TYPE=openai hits the
    OpenAI-compatible /v1/embeddings shape; anything else hits Ollama's
    native /api/embed (the current-generation endpoint — its response
    is {"embeddings": [[...]]}, a batch shape even for one input, which
    is why the [0] below isn't a typo; the older /api/embeddings
    endpoint with its singular {"embedding": [...]} shape is deprecated
    in Ollama's own docs and not targeted here).

    Uses the same persistent _session as every completion call —
    embeddings ride the identical connection pool, so the semantic
    cache's lookup cost is one small POST on an already-open
    connection, not a fresh TCP setup.

    Never raises: every caller treats None as "no embedding available,
    proceed without semantic matching" — an embedding failure must
    never make routing worse than it was before this feature existed.
    """
    if not embeddings_configured():
        return None
    base_url = settings.embedding_url or settings.llm_url
    api_type = settings.llm_api_type.lower().strip()
    try:
        if api_type == "openai":
            resp = _session.post(
                f"{base_url}/v1/embeddings",
                json={"model": settings.embedding_model, "input": text},
                timeout=settings.embedding_timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            vector = data[0].get("embedding") if data else None
        else:
            resp = _session.post(
                f"{base_url}/api/embed",
                json={"model": settings.embedding_model, "input": text},
                timeout=settings.embedding_timeout_seconds,
            )
            resp.raise_for_status()
            vectors = resp.json().get("embeddings", [])
            vector = vectors[0] if vectors else None
        if not vector or not isinstance(vector, list):
            _LOGGER.warning("Embedding response missing vector (model=%s)", settings.embedding_model)
            return None
        return [float(x) for x in vector]
    except Exception as e:
        _LOGGER.warning("Embedding call failed (%s): %s", api_type, e)
        return None


def embed_batch(texts: list[str], timeout: int = 30) -> list[list[float]] | None:
    """Return embedding vectors for `texts` in order, or None on any
    failure. One HTTP round trip for the whole batch — both backends
    accept list input natively (Ollama /api/embed's "input" and the
    OpenAI-compatible /v1/embeddings' "input" are each documented as
    string-or-list) — which is what makes the semantic cache's startup
    warmup a handful of requests instead of one per persisted routing
    key.

    Its own generous default timeout rather than
    embedding_timeout_seconds: that setting is sized for ONE embedding
    on the request path (where the fallback is just "call the routing
    LLM as before", so waiting long only delays the inevitable); a
    warmup batch is dozens of texts on a background thread where a few
    extra seconds cost nothing and a premature timeout throws away an
    otherwise-fine batch.

    All-or-nothing per call, never raises: the sole caller treats None
    as "skip this batch" and decides for itself whether to keep going.
    """
    if not embeddings_configured() or not texts:
        return None
    base_url = settings.embedding_url or settings.llm_url
    api_type = settings.llm_api_type.lower().strip()
    try:
        if api_type == "openai":
            resp = _session.post(
                f"{base_url}/v1/embeddings",
                json={"model": settings.embedding_model, "input": texts},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            vectors = [item.get("embedding") for item in data]
        else:
            resp = _session.post(
                f"{base_url}/api/embed",
                json={"model": settings.embedding_model, "input": texts},
                timeout=timeout,
            )
            resp.raise_for_status()
            vectors = resp.json().get("embeddings", [])
        if len(vectors) != len(texts) or any(not v or not isinstance(v, list) for v in vectors):
            _LOGGER.warning(
                "Batch embedding response shape mismatch: %d texts -> %d vectors (model=%s)",
                len(texts), len(vectors), settings.embedding_model,
            )
            return None
        return [[float(x) for x in v] for v in vectors]
    except Exception as e:
        _LOGGER.warning("Batch embedding call failed (%s): %s", api_type, e)
        return None
