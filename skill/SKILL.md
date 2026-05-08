---
name: Corpus
description: Remote second-brain on your VPS — ingest raw notes into the wiki, query with wiki-first context, lint, verify, deprecate, capture, and stats via one MCP connector.
---

# Corpus

Use this skill with your **single** remote MCP server URL for Corpus Manager (TLS + bearer token configured on the host). Treat user requests as natural language intents, not command syntax.

## When to use

- User wants to **compile** new or changed material from `raw/` into structured `wiki/` pages (Karpathy-style wiki).
- User wants to **ask questions** grounded in the vault (start from the wiki, not raw).
- User wants **quality checks** (lint/verify) or **retiring** a source (deprecate).
- User wants to **capture** a quick note into `raw/` or see **vault stats**.

## Intent map (natural language -> tool)

| User intent examples | MCP tool | Notes |
| -------------------- | -------- | ----- |
| “Capture this thought”, “Remember this”, “Store this note”, “Put this in my notes”, “Save this for later”, “Put this in my wiki” | `capture` | Save user-provided content for later processing into the knowledge base. |
| “Process captured thoughts”, “Update the wiki”, “Sync my notes into the knowledge base”, “Run ingest”, “Process unprocessed files”, “Initialize my wiki” | `ingest` | Start-or-report background ingest. If already running, return status/progress. If no captures exist yet, can initialize a starter wiki scaffold. |
| “Ingest this into the wiki”, “Process this file”, “Update from this source” | `ingest` | Use optional `filename` argument for targeted ingest when the user references a specific path/file. |
| “Initialize my wiki around X”, “Start my knowledge base about X” | `ingest` | Use optional `topic` argument to seed first-time starter wiki scaffolding when the vault has no captures yet. |
| “What’s the wiki status?”, “Check knowledge base status”, “What’s pending?”, “Do I have unprocessed captures?” | `stats` | Get current state and pending count. |
| “Ask the wiki…”, “What do my notes say about X?”, “Search my knowledge base for X”, “What have I captured about X?” | `query` | Use `question`; set `allow_raw=true` only when user requests direct source grounding/quotes. |
| “Audit this page”, “Verify this entry against sources” | `verify` | Read-only source-grounding check for one page. |
| “Run health check”, “Find broken links or consistency issues” | `lint` | Read-only quality check, no auto-fix. |
| “Retire this source”, “Deprecate this file because it’s obsolete” | `deprecate` | Requires `filename` and `reason`. |

## Routing rules

1. One user-facing operation → **one** MCP tool call from Claude.ai; loops run **on the VPS**.
2. Do **not** ask the user to connect a second MCP server for vault operations.
3. **Do not** claim files were edited unless the tool result indicates success.
4. Treat `ingest` as both launcher and status endpoint: call it again to check progress.
5. If user asks for status and pending sources are non-zero, ask once whether they want to process captures now.
6. If user asks a knowledge question, prefer `query` automatically even when they say “notes”, “memory”, “knowledge base”, or “second brain” instead of “wiki”.

## Vault assumptions

- Vault contains `raw/`, `wiki/`, `manifest.json`, and root `CLAUDE.md`.
- `drafts/` and `manuscript/` are higher authority than `raw/` when facts conflict; automation still primarily ingests from **`raw/`** for pending detection unless the user targets a specific path via `ingest(filename=...)`.
- `verify` and `lint` are report-only unless the user explicitly asks for edits afterward.

## Guardrails

- Never write outside the vault root exposed as `VAULT_ROOT` on the server.
- Prefer wiki citations in answers; use raw/drafts/manuscript only when the user asks for verification or exact sourcing.
