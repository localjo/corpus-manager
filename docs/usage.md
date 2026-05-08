# Usage in Claude Chat

This guide shows how users can interact with Corpus Manager in natural language.

You do not need to speak in MCP command syntax. Talk to Claude naturally.

## Core mental model

Use a simple sequence:

1. Capture new thoughts or notes.
2. Process captures into the knowledge base.
3. Ask questions against the updated knowledge base.

If processing is already running, asking to process again returns status instead of starting a second overlapping run.

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

## What Claude routes behind the scenes

- Capture intents -> `capture`
- Process/update intents -> `ingest`
- Status/pending intents -> `stats`
- Knowledge questions -> `query`
- Quality checks -> `lint` / `verify`
- Retirement intents -> `deprecate`

## First-time initialization behavior

If you ask Claude to run ingest on a brand-new vault with no captures yet, Corpus Manager can create an empty starter wiki scaffold so you can begin immediately.

You can optionally provide a topic in the request, for example:

- "Initialize my wiki around mycelial network research."

This starter structure is a bootstrap, not final content. After that, capture real notes in `raw/` and run ingest again to build substantive pages.
