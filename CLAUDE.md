# Corpus Manager — Developer Guide for AI Agents

This file is for AI coding agents (Claude, Cursor, etc.) acting as **developers
on the Corpus Manager codebase**. It is not for the runtime ingest agent that
runs on the VPS, and not for vault/wiki operational rules. Those live in:

- Runtime ingest agent rules: `OP_RULES` in `src/corpus_manager_mcp/server.py`
- Per-vault behavior: each vault's own `CLAUDE.md` (e.g. `The Jungle/CLAUDE.md`)
- Skill packaging for end-user Claude: `skill/SKILL.md`

If a request touches those, edit them in their own files. Do not embed
vault-specific or runtime-prompt content in this file.

## What this project is

Corpus Manager is a self-hosted MCP server that wraps an Obsidian-style vault
(`raw/`, `wiki/`, `manifest.json`) and exposes capture / ingest / query / lint
/ verify / deprecate tools to Claude.ai. It runs in Docker on a VPS alongside a
Syncthing container that keeps the vault synced to the user's other devices.

The repository is small and intentionally KISS:

- `src/corpus_manager_mcp/server.py` — FastMCP entrypoint, MCP tool surface,
  ingest job orchestration, system prompt (`OP_RULES`), `query`/`stats`/`lint`/
  `verify`/`capture`/`deprecate` tool implementations.
- `src/corpus_manager_mcp/agent.py` — Anthropic Messages tool-use loop
  (`run_tool_loop`) and tool definitions used by ingest/deprecate.
- `src/corpus_manager_mcp/vault_ops.py` — vault filesystem primitives:
  `vault_read`, `wiki_write`, manifest CRUD, frontmatter parsing, log
  rotation, traceability checks, deterministic index rebuild.
- `src/corpus_manager_mcp/deterministic.py` — pure-Python lint/verify helpers
  with no LLM calls.
- `docker-compose.yml`, `Dockerfile` — VPS deployment.
- `skill/SKILL.md` — Claude Skill bundle that teaches the end-user Claude how
  to call the MCP tools.
- `docs/setup-and-operations.md`, `docs/usage.md` — user-facing docs.

## Conventions for changes

- **Targeted edits over refactors.** Match the request scope; do not
  opportunistically rewrite adjacent code.
- **Simplest fix first.** Prefer adding one option / one branch over a
  redesign. Avoid speculative abstractions.
- **No new top-level dependencies** without explicit user agreement. The
  current dependency set in `pyproject.toml` is small on purpose.
- **Configuration goes through env vars.** New tunables should default to a
  sane value via `os.getenv("NAME", "default")` and be documented in
  `.env.example` and `docs/setup-and-operations.md`. Don't hardcode tunables.
- **Comments explain why, not what.** Avoid narrating code in comments.
- **Don't reach into the vault layout from new code.** Use `vault_ops`
  primitives. Anything touching the wiki tree should go through `vault_read`,
  `wiki_write`, or new helpers in `vault_ops.py`.

## What lives where (decision guide)

| Need to change… | Edit… |
| --- | --- |
| MCP tool surface or descriptions | `src/corpus_manager_mcp/server.py` (`@MCP.tool` definitions) |
| What the runtime ingest agent is told | `OP_RULES` in `server.py` |
| Anthropic API call shape, retries, turn loop | `src/corpus_manager_mcp/agent.py` |
| Vault filesystem semantics (read/write/manifest/log) | `src/corpus_manager_mcp/vault_ops.py` |
| Pure lint/verify rules without LLM calls | `src/corpus_manager_mcp/deterministic.py` |
| Per-call ingest-tool definitions exposed to the model | `INGEST_TOOLS` in `agent.py` |
| End-user-facing Claude routing rules | `skill/SKILL.md` |
| User setup/troubleshooting docs | `docs/setup-and-operations.md` |
| User usage examples | `docs/usage.md` |
| Defaults for new env vars | `.env.example` + `docs/setup-and-operations.md` |

## Runtime model (read this before changing ingest)

`ingest()` is non-blocking: it spawns a daemon thread (`_spawn_ingest_worker`)
that processes one `pending_sources` entry per loop iteration via
`_run_ingest_agent` -> `run_tool_loop`. Job state is persisted to
`wiki/.ingest-jobs.json` under `_INGEST_LOCK`. The status MCP tool is just
`ingest()` called again — it returns `_ingest_status_payload` for the current
job instead of starting a new one.

Important invariants:

- Only one job runs at a time. The lock + the `current_job_id` field enforce
  it. Do not introduce parallelism without coordinating job-state updates.
- The worker writes to the job state at source boundaries; per-turn updates
  come from the `on_turn_start` callback passed into `run_tool_loop`.
- `run_tool_loop` must fail closed: any unexpected stop condition (including
  `stop_reason == "max_tokens"`) returns `ok: False` *before* dispatching tool
  calls from a truncated turn. Do not relax this — a truncated `wiki_write`
  call can corrupt the wiki.
- Per-turn output budget is `INGEST_LOOP_MAX_TOKENS` (env-configurable).
  Increasing it is cheap. Lowering it has historically caused silent
  corruption; do not lower it without strong justification and matching
  failure-mode coverage.

## Testing and verification

There is no formal test suite yet. Use these as the standard checks after a
change:

- `python -m py_compile src/corpus_manager_mcp/*.py`
- `ReadLints` (or your editor's equivalent) on edited files; fix new lints,
  ignore pre-existing unless asked.
- For loop / agent changes, exercise `run_tool_loop` with a fake Anthropic
  client (see prior session notes — a `SimpleNamespace`-based fake works) to
  cover at minimum: success with one tool call, `stop_reason == "max_tokens"`,
  and a tool handler raising `KeyError`.
- For vault primitives, run a one-off Python repl against a throwaway vault
  dir before relying on it on a real vault.

When in doubt, prefer running things against `/tmp/test-vault` over the real
synced vault.

## Deployment notes

- Local iteration: `uv pip install -e .` (or `pip install -e .`) then
  `corpus-manager-mcp` for a stdio MCP run, or hit it via `fastmcp` / the
  configured HTTP transport. Most behavior is exercised through the Anthropic
  tool-use loop, which requires `ANTHROPIC_API_KEY` set in `.env`.
- VPS: `docker compose up -d --build --no-deps corpus-manager-mcp` rebuilds
  only the server container. Don't recreate the `syncthing` service casually
  — that has caused `syncthing-config/` data loss before. The fix is to
  bind-mount Syncthing state under `/srv/`, not under `/opt/corpus-manager/`.
- The vault path inside the `corpus-manager-mcp` container is `/data/vault`
  (`VAULT_ROOT`), not the host path. Don't hardcode host paths in code.

## Things to avoid

- Do not write to `manuscript/` or `drafts/` in vault primitives. `wiki_write`
  and `can_write_rel` enforce this; keep it that way.
- Do not bypass the `INGEST_LOOP_MAX_TOKENS` failure path with retries inside
  `run_tool_loop`. Surface the failure; let the caller decide.
- Do not add LLM calls to `deterministic.py`. Its job is to be deterministic.
- Do not silently auto-rebuild `wiki/index.md` on every ingest. The runtime
  agent owns that file. `rebuild_wiki_index` exists as an explicit recovery
  tool only.
- Do not edit `manuscript/` ever; do not edit `drafts/` unless explicitly
  asked. The wiki is fair game.

## When you are unsure

Ask. The codebase is small enough that a single clarifying question avoids
multi-file rework. Prefer one focused question over guessing across two or
three files.
