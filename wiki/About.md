# About Mnemolis

Mnemolis is a self-hosted knowledge broker for homelabs. It sits between you and the seven things you might actually want to ask about — offline encyclopedic knowledge, weather, your own RSS feeds, live web search, service uptime, smart home state, and what's changed recently — and figures out which one (or which several) should answer a given question, without you having to know or care which backend actually holds the answer.

## What it actually is

A single Docker container exposing a REST API and an [MCP server](MCP-Server), both backed by the same underlying logic: [routing](Routing) decides which [source](Sources) applies, [decomposition](Query-Decomposition) splits compound questions into independent intents, [fusion](Fusion) merges answers from more than one source when a question genuinely needs it, and [conditional detection](Conditional-Query-Detection) handles "if X, Y" phrasing honestly rather than guessing. When a client wants an answer rather than raw source text — a voice assistant especially — [answer synthesis](Answer-Synthesis) composes a short one *from the retrieved material only*, returned alongside the raw result.

## What it deliberately isn't

It isn't a general-purpose AI assistant, and it doesn't try to be. Mnemolis has no memory of past conversations, no ability to take actions (it can't actually set a reminder, lock a door, or send a notification — see [Conditional Query Detection](Conditional-Query-Detection) for exactly how it handles this limitation honestly rather than pretending otherwise), and no creative or open-ended generation beyond what its sources actually contain. It answers questions using real data from real backends — if none of its sources has the answer, it says so, rather than filling the gap with something plausible-sounding.

It also isn't trying to be a polished, generalized product for a broad audience. It's built for one specific kind of deployment — a self-hosted, locally-running stack on hardware you control — and a lot of its design decisions (the explicit, non-auto-discovered [source registration](Adding-a-New-Source), the deliberately narrow scope of [conditional detection](Conditional-Query-Detection#why-the-pattern-is-this-narrow), the choice to expose one MCP tool rather than several) follow directly from that.

## The philosophy behind the architecture

**Local-first, privacy-preserving, subscription-free.** Every source except [`forecast`](Sources#forecast--weather) runs on your own infrastructure — your own Kiwix server, your own FreshRSS instance, your own SearXNG, your own Uptime Kuma, your own Home Assistant. Open-Meteo (weather) is the one deliberate exception, since there's no practical way to run a global weather model yourself. Nothing about how Mnemolis works requires a cloud account, an API subscription, or sending your queries anywhere outside your own network — the LLM it uses for routing decisions and disambiguation is itself expected to be self-hosted (Ollama or an OpenAI-compatible local endpoint), not a third-party API call.

This isn't just a privacy stance, it's a real architectural constraint that shapes how features get built. [Kiwix Disambiguation](Kiwix-Disambiguation)'s entire design — generate candidates, search them all for real, let actual results decide — exists specifically because a small local LLM genuinely can't see what's in your index and shouldn't be trusted to guess blind. A larger, cloud-hosted model might guess correctly more often, but the architecture is built around the assumption that the model doing the reasoning is one you're running yourself, with all the real constraints that implies.

## Why this much testing rigor

A recurring pattern across this project's actual history — documented directly in [Design History](Home#design-history--real-bugs-real-fixes) — is that a fix which looks complete after the test suite passes sometimes isn't, and the gap only shows up against real, messy, adversarially-constructed queries, genuine production data, or — as a fifth chapter of one particular saga showed — a careful, deliberate re-read of code that already had a passing test suite. That's not a reflection of the test suite being weak; it's an honest acknowledgment that natural language is genuinely harder to fully specify than most code, and that the actual discipline this project has settled into — escalate to harder real phrasing, verify against live data, trace root causes with real debugging rather than guessing, and read thoroughly even when nothing looks obviously broken — has repeatedly found bugs that would otherwise have shipped quietly. The [Proper-Noun-Pair Saga](The-Proper-Noun-Pair-Saga) alone is five bugs in one piece of logic, found two different ways.

## Where to go next

[First-Time Setup](First-Time-Setup) if you're installing this for the first time. [Sources](Sources) and [Routing](Routing) if you want to understand how a query actually gets answered. [Design History](Home#design-history--real-bugs-real-fixes) if you want the real stories behind the architecture's sharper edges.
