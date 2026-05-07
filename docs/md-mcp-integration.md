# markdown-vault-mcp Integration Decision

## Decision

Use **HTTP transport** for `markdown-vault-mcp` inside Docker Compose.

- Container: `markdown-vault-mcp`
- Command: `serve --transport http --host 0.0.0.0 --port 8000`
- Internal URL from orchestrator: `http://markdown-vault-mcp:8000`

## Why

1. Simpler in a multi-container network than wiring stdio pipes across containers.
2. Aligns with upstream Docker guidance for markdown-vault-mcp deployment.
3. Keeps orchestrator and md-mcp independently restartable and observable.

## Orchestrator contract

- Claude.ai sees only one external MCP endpoint: **Corpus Manager orchestrator**.
- Orchestrator delegates search/read primitives to md-mcp over HTTP where appropriate.
- Ingest/query loops stay on VPS in orchestrator.

## Future fallback

If upstream HTTP behavior changes, fallback option is same-container subprocess with stdio MCP. That is not the default path for this MVP.
