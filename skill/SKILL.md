---
name: Corpus
description: Remote second-brain on your VPS — ingest raw notes into the wiki, query with wiki-first context, lint, verify, deprecate, capture, and stats via one MCP connector.
---

# Corpus

Use this skill with your **single** remote MCP server URL for Corpus Manager (TLS + bearer token configured on the host). Treat user requests as natural language intents, not command syntax.

When multiple vaults are configured, include `vault_id` in tool calls. If the user has not specified one and there is no project default, ask which vault they want.

## When to use

- User wants to **compile** new or changed material from `raw/` into structured `wiki/` pages (Karpathy-style wiki).
- User wants to **ask questions** grounded in the vault (start from the wiki, not raw).
- User wants **quality checks** (lint/verify) or **retiring** a source (deprecate).
- User wants to **capture** a quick note into `raw/` or see **vault stats**.

## Intent map (natural language -> tool)

| User intent examples | MCP tool | Notes |
| -------------------- | -------- | ----- |
| “Which vaults are configured?”, “List available vaults” | `list_corpora` | Use this when vault choice is ambiguous and multiple vaults are configured. |
| “Capture this thought”, “Remember this”, “Store this note”, “Put this in my notes”, “Save this for later”, “Put this in my wiki” | `capture` | Save user-provided content for later processing into the knowledge base. |
| “Process captured thoughts”, “Update the wiki”, “Sync my notes into the knowledge base”, “Run ingest”, “Process unprocessed files”, “Initialize my wiki” | `ingest` | Start background ingest. After launching, tell the user it runs in background and may take 10-20 minutes; do not auto-check status unless asked. If no captures exist yet, can initialize a starter wiki scaffold. |
| “Ingest this into the wiki”, “Process this file”, “Update from this source” | `ingest` | Use optional `filename` argument for targeted ingest when the user references a specific path/file. |
| “Initialize my wiki around X”, “Start my knowledge base about X” | `ingest` | Use optional `topic` argument to seed first-time starter wiki scaffolding when the vault has no captures yet. |
| “Try the ingest again”, “Retry the ingest”, “The previous ingest failed, run it again” | `ingest` | Pass `retry=true` to start a fresh attempt after a failed job. Without it, repeat calls return the previous failure so the user can see why it failed before retrying. |
| “Try ingesting with a different model”, “Fall back to Sonnet for ingest”, “Opus is failing, use a different model” | `ingest` | Pass `model` (e.g. `claude-sonnet-4-6`) to override the configured ingest model for this job; useful when the default model is having issues. Combine with `retry=true` to retry a failed job on a different model. |
| “Give the ingest more turns”, “Bump the turn budget”, “The ingest is hitting its turn limit” | `ingest` | Pass `max_turns` (e.g. `max_turns=40`) to raise the per-source agent turn budget for this job. Default is 35. Use when a source previously failed with `error="max_turns_exceeded"`, especially for complex reconciliation across many existing wiki pages. Combine with `retry=true`. |
| “What’s the wiki status?”, “Check knowledge base status”, “What’s pending?”, “Do I have unprocessed captures?” | `stats` | Get current state and pending count. |
| “Ask the wiki…”, “What do my notes say about X?”, “Search my knowledge base for X”, “What have I captured about X?” | `query` | Use `question`; set `allow_raw=true` only when user requests direct source grounding/quotes. |
| “Audit this page”, “Verify this entry against sources” | `verify` | Read-only source-grounding check for one page. |
| “Run health check”, “Find broken links or consistency issues” | `lint` | Read-only quality check, no auto-fix. |
| “Retire this source”, “Deprecate this file because it’s obsolete” | `deprecate` | Requires `filename` and `reason`. |
| “Read this specific file directly”, “Show me this wiki file as-is” | `direct_vault_op` | Use `operation="read"` and `path=...` for direct read-only file access when requested. |
| “Manual override requested: directly update this wiki file”, “Manual override requested: move this wiki file” | `direct_vault_op` | Use `operation="write"` or `operation="move"` only for explicit manual override requests; must pass `explicit_request=true` and include phrase `manual override requested` in `reason`. |

## Routing rules

1. One user-facing operation → **one** MCP tool call from Claude.ai; loops run **on the VPS**.
2. Do **not** ask the user to connect a second MCP server for vault operations.
3. If more than one vault is configured, include `vault_id` in each vault-scoped tool call (`capture`, `ingest`, `stats`, `query`, `verify`, `lint`, `deprecate`, `direct_vault_op`, `rebuild_index`).
4. If `vault_id` is missing/ambiguous in a multi-vault context, ask the user which vault to use or call `list_corpora` first.
5. **Do not** claim files were edited unless the tool result indicates success.
6. Treat `ingest` as both launcher and status endpoint. After starting ingest, do **not** call it repeatedly for status unless the user explicitly asks; instead tell the user it runs in the background and can take 10-20 minutes.
7. If user asks for status and pending sources are non-zero, ask once whether they want to process captures now.
8. If user asks a knowledge question, prefer `query` automatically even when they say “notes”, “memory”, “knowledge base”, or “second brain” instead of “wiki”.
9. If `ingest` reports a previous failed job, summarize `errors_preview` for the user and offer to retry. If the failure looks model-specific (e.g. repeated 5xx from the API on one model), suggest retrying with a `model` fallback (e.g. `claude-sonnet-4-6`).
10. If `errors_preview` shows `error="max_turns_exceeded"`, the per-source agent budget was hit. Surface `last_summary_text` (what the model was doing when cut off) and the `remedy` field, then offer to retry with a higher `max_turns` (e.g. `retry=true, max_turns=40`). If the model was already deep into the work (writing index/manifest) when cut off, retrying with a moderate bump (e.g. 35–40) is usually enough.
11. For direct file operations, prefer `direct_vault_op(operation="read")` for inspection requests. Do not use direct mutation (`write`/`move`) unless the user explicitly requests manual override for that exact action.
12. Requests like “add this to the wiki” should route to `capture`/`ingest`, not direct mutation tools.

## Vault assumptions

- Vault contains `raw/`, `wiki/`, `manifest.json`, and root `CLAUDE.md`.
- `drafts/` and `manuscript/` are higher authority than `raw/` when facts conflict; automation still primarily ingests from **`raw/`** for pending detection unless the user targets a specific path via `ingest(filename=...)`.
- `verify` and `lint` are report-only unless the user explicitly asks for edits afterward.

## Guardrails

- Never write outside the vault root exposed as `VAULT_ROOT` on the server.
- Prefer wiki citations in answers; use raw/drafts/manuscript only when the user asks for verification or exact sourcing.
- Direct mutation guardrail: only use `direct_vault_op` mutation modes when the user explicitly requests manual override, and include `manual override requested` in `reason`.
