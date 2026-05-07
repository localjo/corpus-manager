# Corpus Manager smoke test

Quick checks before or after deploy (run on a machine with Python 3.11+ and optional Anthropic API key).

## 1) Import / syntax

From the `Corpus Manager` repo root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -c "from corpus_manager_mcp import vault_ops, agent, deterministic; print('ok')"
```

## 2) Point at a vault copy

Use a **copy** of your vault, not the live Obsidian folder:

```bash
export VAULT_ROOT=/path/to/copy/of/vault
export CLAUDE_MD_PATH="$VAULT_ROOT/CLAUDE.md"
export ANTHROPIC_API_KEY=sk-ant-...
```

## 3) Deterministic lint only (no API)

```bash
PYTHONPATH=src python3 -c "
from pathlib import Path
from corpus_manager_mcp.deterministic import build_lint_payload
import json, os
root = Path(os.environ['VAULT_ROOT'])
print(json.dumps(build_lint_payload(root), indent=2)[:4000])
"
```

## 4) Full ingest (costs API tokens)

Run the MCP server locally or call `ingest` / `ingest_file` through your MCP client. Confirm:

- New or updated files under `wiki/`
- `manifest.json` updated with `ingested_at` and `wiki_pages`
- `wiki/log.md` has a new `## [timestamp] …` section

## 5) Docker

```bash
docker compose build corpus-manager-mcp
docker compose run --rm corpus-manager-mcp corpus-manager-mcp --help 2>/dev/null || true
```

If the image starts, check logs for bind-mount path to `VAULT_ROOT`.
