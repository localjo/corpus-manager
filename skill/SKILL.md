---
name: Corpus
description: Remote second-brain on your VPS — ingest raw notes into the wiki, query with wiki-first context, lint, verify, deprecate, capture, and stats via one MCP connector.
---

# Corpus

Use this skill with your **single** remote MCP server URL for Corpus Manager (TLS + bearer token configured on the host). Slash phrases below are **intent aliases**; prefer calling the listed MCP tools directly when the user’s goal matches.

## When to use

- User wants to **compile** new or changed material from `raw/` into structured `wiki/` pages (Karpathy-style wiki).
- User wants to **ask questions** grounded in the vault (start from the wiki, not raw).
- User wants **quality checks** (lint/verify) or **retiring** a source (deprecate).
- User wants to **capture** a quick note into `raw/` or see **vault stats**.

## Command map

| User phrase (examples) | MCP tool | Notes |
|------------------------|----------|--------|
| `/ingest`, “run ingest”, “compile pending raw” | `ingest` | Processes all **pending** files under `raw/` (server-side multi-step loop). |
| `/ingest path`, “ingest this file” | `ingest_file` | `filename` = path under `raw/`, `drafts/`, or `manuscript/` |
| `/query …`, “ask the wiki…” | `query` | Pass natural-language `question`; set `allow_raw` true only if user needs quotes or source verification. |
| `/capture …` | `capture` | Writes new markdown under `raw/`. |
| `/verify …` | `verify` | `wiki_page` path (e.g. `wiki/concepts/foo.md` or `concepts/foo.md`). Read-only audit. |
| `/lint` | `lint` | Deterministic checks + narrative report; **no auto-fix**. |
| `/deprecate …` | `deprecate` | `filename` + `reason` (manifest + wiki reconciliation on server). |
| `/stats` | `stats` | Vault summary including pending raw count. |

## Routing rules

1. One user-facing operation → **one** MCP tool call from Claude.ai; loops run **on the VPS**.
2. Do **not** ask the user to connect a second MCP server for vault operations.
3. **Do not** claim files were edited unless the tool result indicates success.

## Vault assumptions

- Vault contains `raw/`, `wiki/`, `manifest.json`, and root `CLAUDE.md`.
- `drafts/` and `manuscript/` are higher authority than `raw/` when facts conflict; automation still primarily ingests from **`raw/`** for pending detection unless the user targets a path with `ingest_file`.
- `verify` and `lint` are report-only unless the user explicitly asks for edits afterward.

## Guardrails

- Never write outside the vault root exposed as `VAULT_ROOT` on the server.
- Prefer wiki citations in answers; use raw/drafts/manuscript only when the user asks for verification or exact sourcing.
