# Usage in Claude Chat

This guide shows how users can interact with Corpus Manager in natural language.

You do not need to speak in MCP command syntax. Talk to Claude naturally.

## Core mental model

Use a simple sequence:

1. Capture new thoughts or notes.
2. Process captures into the knowledge base.
3. Ask questions against the updated knowledge base.

If processing is already running, asking to process again returns status instead of starting a second overlapping run.

## Multi-vault routing

- If only one vault is configured on the server, Claude can call tools without `vault_id`.
- If multiple vaults are configured, Claude must include `vault_id` on vault-scoped tool calls.
- If vault choice is unclear, ask Claude to list vaults first:
  - "List configured vaults."
  - "Which vault ids are available?"
- You can set a project default in instructions, for example:
  - "Default vault id for Corpus Manager is `main`."

## Natural-language examples

### Capture / remember

- "Capture this thought: I feel most creative after walking."
- "Remember this for later: add a chapter about consent repair."
- "Store this in my notes."
- "Put this in my knowledge base."

### Process captures (ingest)

- "Process captured thoughts."
- "Update the wiki from my latest notes."
- "Sync unprocessed notes into the knowledge base."
- "Run ingest."
- "Initialize my wiki."
- "Initialize my wiki around relationships and consent."

### Check processing progress

- "Is ingest still running?"
- "Check ingest status."
- "Run ingest again and tell me progress."

### Process one specific source

- "Ingest this into the wiki: `raw/updates/neo.md`."
- "Process only `raw/misc-notes/She Can Help with Sunscreen.md`."

### Ask from your knowledge base

- "What do my notes say about Zipolite?"
- "Ask the wiki: what are my recurring themes about consent?"
- "Search my second brain for ideas about chapter ordering."

### Status and quality checks

- "What is the current wiki status?"
- "How many pending captures do I have?"
- "Run a lint check on my knowledge base."
- "Verify this page against sources: `wiki/chapters/what-did-you-say-to-her.md`."

### Retire outdated material

- "Deprecate `raw/old-outline.md` because it is obsolete."

### Direct file access and manual override

- "Read this file directly: `wiki/chapters/what-did-you-say-to-her.md`."
- "Manual override: directly update `wiki/concepts/consent-repair.md` with this exact content."
- "Manual override: move `wiki/concepts/old-name.md` to `wiki/concepts/new-name.md`."

Direct reads can be used when useful. Direct writes/moves are intentionally guarded and should be used only for explicit manual override requests. For write/move operations, the tool reason must include the exact phrase `manual override requested`.

## What Claude routes behind the scenes

- Capture intents -> `capture`
- Process/update intents -> `ingest`
- Status/pending intents -> `stats`
- Knowledge questions -> `query`
- Quality checks -> `lint` / `verify`
- Retirement intents -> `deprecate`
- Direct file reads and explicit manual direct-file mutations -> `direct_vault_op`
- Vault discovery in multi-vault deployments -> `list_corpora`

## First-time initialization behavior

If you ask Claude to run ingest on a brand-new vault with no captures yet, Corpus Manager can create an empty starter wiki scaffold so you can begin immediately.

You can optionally provide a topic in the request, for example:

- "Initialize my wiki around mycelial network research."

This starter structure is a bootstrap, not final content. After that, capture real notes in `raw/` and run ingest again to build substantive pages.

## Bootstrap a new vault (VPS)

Corpus Manager includes a helper script to add a new vault and register it in YAML.

Host invocation:

```bash
cd /opt/corpus-manager
python3 scripts/new-vault.py
```

Container invocation:

```bash
cd /opt/corpus-manager
docker compose run --rm corpus-manager-mcp python3 scripts/new-vault.py
```

The script will:

- Prompt for vault id and optional display label.
- Create `raw/`, `wiki/`, `manifest.json`, and `CLAUDE.md`.
- Add the vault entry to `CORPUS_REGISTRY_PATH` (default `/data/corpus-registry.yaml`).

After running it:

1. Add the new folder to Syncthing on all devices.
2. Restart `corpus-manager-mcp` so the updated registry loads.
3. Use the new `vault_id` in chat (or set a project default).
