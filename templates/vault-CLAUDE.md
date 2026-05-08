# {{VAULT_LABEL}} Vault

This vault is a personal knowledge workspace following the LLM wiki pattern:
raw notes are captured, then compiled into wiki pages for durable retrieval and synthesis.

## Layers

- `raw/` — user-owned source material.
- `wiki/` — compiled knowledge layer consumed by queries and synthesis.
- `manifest.json` — source-to-wiki provenance.

## Boundaries

- Treat `raw/` as source authority; captures are new files added there.
- Default writable scope is `wiki/`, `manifest.json`, and this file.

## Voice and project context

- Entities are factual and third-person.
- Narrator experience is first-person across chapter, concept, framework, and synthesis pages.
- Do not refer to the narrator as "the narrator" when presenting narrator-owned experience, interpretation, desire, memory, or meaning; use first person.
- Entity pages are factual and third-person, but references to the narrator's relationship or experience should still use first person where natural.
- Track naming/pseudonym consistency when relevant.
- Prefer concise, concrete language.

## Wiki page types

- `entity`
- `concept`
- `synthesis`

Each wiki page should include frontmatter with `type`, `sources`, `date_updated`, and tags.

## Routing note

Operational behavior for ingest/query/deprecate/verify/lint and MCP tool usage is defined in Corpus Manager server prompts and Skill docs.
On first-time setup with no captures in `raw/`, an ingest request may initialize a minimal starter wiki scaffold (optionally topic-guided) before normal ingest workflows begin.
