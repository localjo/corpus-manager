---
name: Corpus Manager Skill
description: Use one MCP connector to run second-brain operations on VPS.
---

# Corpus Manager Skill

Use this skill with the **single** remote MCP server connection for Corpus Manager.

## Commands

- `/ingest` -> call MCP tool `ingest`
- `/ingest <path>` -> call MCP tool `ingest_file` with `filename`
- `/query <question>` -> call MCP tool `query` with `question`
- `/capture <content>` -> call MCP tool `capture`
- `/verify <wiki-page>` -> call MCP tool `verify`
- `/lint` -> call MCP tool `lint`
- `/deprecate <path> --reason "..."` -> call MCP tool `deprecate`
- `/stats` -> call MCP tool `stats`

## Routing rules

1. Use the Corpus Manager MCP tools for all high-level operations.
2. Do not require the user to install a second MCP connector.
3. Keep one operation = one MCP tool call from Claude.ai; loops run on VPS.

## Vault assumptions

- Vault has `raw/`, `wiki/`, `manifest.json`, and root `CLAUDE.md`.
- `CLAUDE.md` is slim project context; operational behavior lives in server prompts and this skill.

## Guardrails

- Never write outside `VAULT_ROOT`.
- `query` reads wiki-first unless explicit raw verification is requested.
- `verify` and `lint` are report-only by default.
