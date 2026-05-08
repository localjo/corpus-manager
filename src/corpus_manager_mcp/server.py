from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from anthropic import Anthropic
from dotenv import load_dotenv
from fastmcp import FastMCP

from corpus_manager_mcp.agent import DEPRECATE_TOOLS, INGEST_TOOLS, run_tool_loop
from corpus_manager_mcp.deterministic import (
    build_lint_payload,
    verify_bundle_for_page,
    _wiki_paths_index,
    resolve_wikilink_target,
)
from corpus_manager_mcp.vault_ops import (
    VaultPaths,
    append_operation_log,
    extract_wikilinks,
    rebuild_wiki_index,
    traceability_warnings,
    vault_read,
)

load_dotenv()


@dataclass(frozen=True)
class CorpusConfig:
    vault_id: str
    label: str
    paths: VaultPaths


def _build_single_corpus_default() -> dict[str, CorpusConfig]:
    vault_root = Path(os.getenv("VAULT_ROOT", "/data/vault")).expanduser().resolve()
    claude_md = Path(os.getenv("CLAUDE_MD_PATH", str(vault_root / "CLAUDE.md"))).expanduser().resolve()
    cfg = CorpusConfig(vault_id="main", label="Main", paths=VaultPaths.from_roots(vault_root, claude_md))
    return {cfg.vault_id: cfg}


def _load_corpus_registry() -> dict[str, CorpusConfig]:
    reg_path_raw = os.getenv("CORPUS_REGISTRY_PATH", "").strip()
    if not reg_path_raw:
        return _build_single_corpus_default()
    reg_path = Path(reg_path_raw).expanduser().resolve()
    if not reg_path.exists():
        return _build_single_corpus_default()
    raw = yaml.safe_load(reg_path.read_text(encoding="utf-8")) or {}
    corpora = raw.get("corpora") if isinstance(raw, dict) else None
    if not isinstance(corpora, dict) or not corpora:
        return _build_single_corpus_default()

    out: dict[str, CorpusConfig] = {}
    roots: set[str] = set()
    for key, value in corpora.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if not isinstance(value, dict):
            continue
        vid = key.strip()
        root_raw = str(value.get("vault_root", "")).strip()
        if not root_raw:
            continue
        root = Path(root_raw).expanduser().resolve()
        root_s = str(root)
        if root_s in roots:
            raise ValueError(f"Duplicate vault_root in CORPUS_REGISTRY_PATH: {root_s}")
        roots.add(root_s)
        claude_md_raw = str(value.get("claude_md", "")).strip()
        claude_md = Path(claude_md_raw).expanduser().resolve() if claude_md_raw else (root / "CLAUDE.md")
        label = str(value.get("label", vid)).strip() or vid
        out[vid] = CorpusConfig(vault_id=vid, label=label, paths=VaultPaths.from_roots(root, claude_md))
    if not out:
        return _build_single_corpus_default()
    return out


CORPORA = _load_corpus_registry()
_CURRENT_VAULT_ID: ContextVar[str] = ContextVar("current_vault_id", default=next(iter(CORPORA)))


class _PathProxy:
    def __init__(self, attr: str):
        self.attr = attr

    def _path(self) -> Path:
        return getattr(CORPORA[_CURRENT_VAULT_ID.get()].paths, self.attr)

    def __truediv__(self, other: str) -> Path:
        return self._path() / other

    def __fspath__(self) -> str:
        return str(self._path())

    def __str__(self) -> str:
        return str(self._path())

    def __repr__(self) -> str:
        return repr(self._path())

    def __getattr__(self, item: str) -> Any:
        return getattr(self._path(), item)


CFG_ROOT = _PathProxy("root")
CFG_WIKI = _PathProxy("wiki")
CFG_MANIFEST = _PathProxy("manifest")
CFG_CLAUDE_MD = _PathProxy("claude_md")

SERVER_INSTRUCTIONS = (
    "Corpus Manager is a VPS-hosted personal knowledge base manager. "
    "Users may say wiki, notes, knowledge base, second brain, memory, or vault. "
    "When multiple vaults are configured, require vault_id for vault-scoped operations; use list_corpora to discover available vaults. "
    "Use capture when users say capture/store/save/remember this, ingest when users say process/update/sync notes into the knowledge base, "
    "stats for status/what is pending, query for ask/find/what do my notes say, lint/verify for audits, and deprecate to retire a source. "
    "After starting ingest, do not repeatedly poll status unless the user explicitly asks for a status check. "
    "Acknowledge ingest runs in the background and may take a while. "
    "If users ask to initialize/start a brand-new wiki and there are no captures yet, ingest may create a starter scaffold (optionally topic-guided). "
    "Direct file reads are allowed when useful, but never perform direct file writes/moves unless the user explicitly requests a manual override for a specific file operation."
)

MCP = FastMCP("Corpus Manager", instructions=SERVER_INSTRUCTIONS)
CLIENT = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY")) if os.getenv("ANTHROPIC_API_KEY") else None

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
ANTHROPIC_QUERY_MODEL = os.getenv("ANTHROPIC_QUERY_MODEL", "claude-sonnet-4-5")
ANTHROPIC_INGEST_MODEL = os.getenv("ANTHROPIC_INGEST_MODEL", ANTHROPIC_MODEL)
INGEST_SOURCE_CHAR_LIMIT = int(os.getenv("INGEST_SOURCE_CHAR_LIMIT", "6000"))
INGEST_CLAUDE_MD_CHAR_LIMIT = int(os.getenv("INGEST_CLAUDE_MD_CHAR_LIMIT", "8000"))
INGEST_LOOP_MAX_TURNS = int(os.getenv("INGEST_LOOP_MAX_TURNS", "35"))
INGEST_LOOP_MAX_TOKENS = int(os.getenv("INGEST_LOOP_MAX_TOKENS", "8192"))
INGEST_RATE_RETRY_ATTEMPTS = int(os.getenv("INGEST_RATE_RETRY_ATTEMPTS", "3"))
INGEST_RATE_RETRY_WAIT_SECONDS = int(os.getenv("INGEST_RATE_RETRY_WAIT_SECONDS", "12"))
INGEST_SOURCE_DELAY_SECONDS = 5
_LOCK_GUARD = threading.Lock()
_INGEST_LOCKS: dict[str, threading.Lock] = {}
_INGEST_THREADS: dict[str, threading.Thread] = {}


def _ingest_lock(vault_id: str) -> threading.Lock:
    with _LOCK_GUARD:
        lock = _INGEST_LOCKS.get(vault_id)
        if lock is None:
            lock = threading.Lock()
            _INGEST_LOCKS[vault_id] = lock
        return lock


def _ingest_jobs_path(vault_id: str) -> Path:
    return CORPORA[vault_id].paths.wiki / ".ingest-jobs.json"


def _resolve_vault_id(vault_id: str) -> tuple[str | None, dict[str, Any] | None]:
    vid = vault_id.strip()
    if vid:
        if vid not in CORPORA:
            return None, {
                "status": "error",
                "message": f"Unknown vault_id '{vid}'.",
                "vault_ids": sorted(CORPORA.keys()),
            }
        return vid, None
    if len(CORPORA) == 1:
        return next(iter(CORPORA.keys())), None
    return None, {
        "status": "error",
        "message": "vault_id is required when multiple vaults are configured.",
        "vault_ids": sorted(CORPORA.keys()),
    }


@contextmanager
def _use_vault(vault_id: str):
    token = _CURRENT_VAULT_ID.set(vault_id)
    try:
        yield
    finally:
        _CURRENT_VAULT_ID.reset(token)


def _resolve(path: str) -> Path:
    p = (CFG_ROOT / path).resolve()
    if not str(p).startswith(str(CFG_ROOT)):
        raise ValueError("Path escapes VAULT_ROOT")
    return p


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _extract_text(resp: Any) -> str:
    parts = []
    for item in resp.content:
        if getattr(item, "type", "") == "text":
            parts.append(item.text)
    return "\n".join(parts).strip()


def _run_model(prompt: str, model: str) -> str:
    if CLIENT is None:
        raise RuntimeError("ANTHROPIC_API_KEY is required for this tool")
    resp = CLIENT.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_text(resp)


OP_RULES = """
Karpathy-style wiki pattern (VPS + MCP adaptation):
- This server maintains a persistent wiki artifact under the selected vault, not one-off RAG answers.
- Source layers:
  - raw/ is source material and read-only for automation.
  - wiki/ is compiled, cross-linked knowledge maintained by tools.
  - manifest.json tracks provenance and ingest/deprecation state.
- Default writable scope is wiki/, manifest.json, and CLAUDE.md only.
- Keep manual wiki corrections unless source truth clearly supersedes them.
- Use concise, factual language by default.
- Use YAML frontmatter on wiki pages with at least: type, sources, date_updated, tags.
- Default page categories are entity, concept, and synthesis unless vault-specific rules define additional types.
- Use wikilinks aggressively with human-readable aliases: [[path/to/file.md|Readable Title]].
- Inside markdown tables, escape the alias pipe to avoid breaking table columns: [[path/to/file.md\\|Readable Title]].
- Surface unresolved source contradictions explicitly; never silently merge conflicting claims.
- Reconcile in place; do not duplicate pages.
- After wiki edits, call manifest_upsert_source with full wiki_pages (paths relative to wiki/, no wiki/ prefix).
- Update wiki/index.md when adding navigable pages.
- Finish by append_operation_log with bullets listing wiki paths touched and manifest updates.

Tool-use efficiency (does not affect output quality):
- When you need several independent reads, issue them as parallel tool_use blocks in a single message (e.g. multiple vault_reads at once, or vault_read + manifest_get_source together) rather than one tool call per turn.
- Read each wiki page at most once per ingest. If you've already seen a page in this session, work from that content; do not re-read.
- Trust tool success responses. Do not call vault_read after a successful wiki_write to verify; the write succeeded if the result is ok.
- Plan the full set of wiki_write calls before issuing them. Avoid speculative writes that you then revise.

wiki_pages in manifest use paths relative to wiki/ (e.g. concepts/foo.md), NOT prefixed with wiki/.
"""


def build_system_prompt() -> str:
    claude = _read_text(CFG_CLAUDE_MD)[:INGEST_CLAUDE_MD_CHAR_LIMIT]
    return f"{OP_RULES}\n\n--- Vault CLAUDE.md ---\n{claude}"


def _list_raw_files() -> list[Path]:
    raw_root = CFG_ROOT / "raw"
    if not raw_root.exists():
        return []
    return [p for p in raw_root.rglob("*") if p.is_file() and not _is_ignored_source(p)]


def _is_ignored_source(path: Path) -> bool:
    name = path.name
    if name.startswith("."):
        return True
    if name.startswith("._"):
        return True
    if name == ".DS_Store":
        return True
    if path.suffix.lower() == ".plist":
        return True
    return False


def _manifest_sources() -> list[dict[str, Any]]:
    doc = _read_json(CFG_MANIFEST)
    sources = doc.get("sources")
    return sources if isinstance(sources, list) else []


def _pending_sources() -> list[str]:
    manifest = _manifest_sources()
    by_filename = {entry.get("filename"): entry for entry in manifest if isinstance(entry, dict)}
    pending: list[str] = []
    for f in _list_raw_files():
        rel = str(f.relative_to(CFG_ROOT))
        entry = by_filename.get(rel) or by_filename.get(f.name)
        if not entry:
            pending.append(rel)
            continue
        if entry.get("status") == "deprecated":
            continue
        ingested_at = entry.get("ingested_at")
        if not ingested_at:
            pending.append(rel)
            continue
        try:
            ingested_dt = datetime.fromisoformat(str(ingested_at).replace("Z", "+00:00"))
        except ValueError:
            pending.append(rel)
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime > ingested_dt:
            pending.append(rel)
    return sorted(set(pending))


def _wiki_is_effectively_empty() -> bool:
    if not CFG_WIKI.exists():
        return True
    for p in CFG_WIKI.rglob("*.md"):
        if p.name == ".ingest-jobs.json":
            continue
        return False
    return True


def _initialize_empty_wiki(topic: str = "") -> dict[str, Any]:
    """Create a starter wiki scaffold when no sources exist yet."""
    today = datetime.now().strftime("%Y-%m-%d")
    topic_line = topic.strip() or "General"
    created: list[str] = []

    files_to_write = {
        "wiki/index.md": (
            "# Wiki Index\n\n"
            "This wiki was initialized by Corpus Manager.\n\n"
            "## Getting started\n\n"
            f"- Topic focus: **{topic_line}**\n"
            "- Add captures, then run ingest to compile pages.\n"
        ),
        "wiki/log.md": (
            "# Wiki Log\n\n"
            "Operational log for write actions.\n"
        ),
        "wiki/synthesis/getting-started.md": (
            "---\n"
            "type: synthesis\n"
            "book: shared\n"
            "sources: []\n"
            f"date_updated: {today}\n"
            "tags:\n"
            "  - bootstrap\n"
            "  - overview\n"
            "---\n\n"
            f"# Getting Started ({topic_line})\n\n"
            "This starter page was created because ingest was requested before any captures existed.\n"
            "Capture notes in `raw/`, then run ingest to expand this wiki.\n"
        ),
    }

    for rel, content in files_to_write.items():
        path = _resolve(rel)
        if path.exists():
            continue
        _write_text(path, content)
        created.append(rel)

    manifest_doc = _read_json(CFG_MANIFEST)
    if not isinstance(manifest_doc, dict):
        manifest_doc = {}
    if "sources" not in manifest_doc or not isinstance(manifest_doc["sources"], list):
        manifest_doc["sources"] = []
        _write_text(CFG_MANIFEST, json.dumps(manifest_doc, indent=2, ensure_ascii=False) + "\n")
        created.append("manifest.json")

    append_operation_log(
        CFG_ROOT,
        "initialize",
        "empty-wiki",
        [
            f"created starter wiki scaffold ({topic_line})",
            *(f"created `{p}`" for p in created),
        ],
    )
    return {"created": created, "topic": topic_line}


def _search_wiki_keywords(question: str, limit: int = 8) -> list[str]:
    tokens = [t for t in re.split(r"\W+", question.lower()) if len(t) >= 4]
    hits: list[tuple[int, Path]] = []
    for p in CFG_WIKI.rglob("*.md"):
        text = _read_text(p).lower()
        score = sum(tok in text for tok in tokens)
        if score:
            hits.append((score, p))
    hits.sort(key=lambda x: x[0], reverse=True)
    return [str(p.relative_to(CFG_ROOT)) for _, p in hits[:limit]]


def _query_context(question: str, allow_raw: bool) -> str:
    chunks: list[str] = []
    index_path = CFG_WIKI / "index.md"
    index_text = _read_text(index_path)[:12000]
    chunks.append(f"## wiki/index.md\n{index_text}")

    idx = _wiki_paths_index(CFG_ROOT)
    neighbor_rels: list[str] = []
    for link in extract_wikilinks(index_text)[:24]:
        resolved = resolve_wikilink_target(CFG_ROOT, link, idx)
        if resolved:
            neighbor_rels.append(str(resolved.relative_to(CFG_ROOT)))

    seen: set[str] = set()
    for rel in neighbor_rels:
        if rel in seen:
            continue
        seen.add(rel)
        chunks.append(f"## {rel}\n{_read_text(_resolve(rel))[:4500]}")

    for rel in _search_wiki_keywords(question, limit=6):
        if rel not in seen:
            seen.add(rel)
            chunks.append(f"## {rel}\n{_read_text(_resolve(rel))[:4500]}")

    if allow_raw:
        raw_hint = _search_wiki_keywords(question, limit=3)
        for r in raw_hint:
            if r.startswith("raw/"):
                chunks.append(f"## {r}\n{_read_text(_resolve(r))[:6000]}")

    return "\n\n".join(chunks)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_ingest_jobs(vault_id: str) -> dict[str, Any]:
    jobs_path = _ingest_jobs_path(vault_id)
    if not jobs_path.exists():
        return {"current_job_id": None, "jobs": {}}
    try:
        doc = json.loads(jobs_path.read_text(encoding="utf-8"))
    except Exception:
        return {"current_job_id": None, "jobs": {}}
    if not isinstance(doc, dict):
        return {"current_job_id": None, "jobs": {}}
    if "jobs" not in doc or not isinstance(doc["jobs"], dict):
        doc["jobs"] = {}
    if "current_job_id" not in doc:
        doc["current_job_id"] = None
    return doc


def _save_ingest_jobs(doc: dict[str, Any], vault_id: str) -> None:
    jobs_path = _ingest_jobs_path(vault_id)
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _get_job(doc: dict[str, Any], job_id: str | None = None) -> tuple[str | None, dict[str, Any] | None]:
    jobs = doc.get("jobs", {})
    if not isinstance(jobs, dict):
        return None, None
    jid = job_id or doc.get("current_job_id")
    if not jid:
        return None, None
    job = jobs.get(jid)
    if not isinstance(job, dict):
        return None, None
    return jid, job


def _spawn_ingest_worker(job_id: str, vault_id: str) -> None:
    def _worker() -> None:
        token = _CURRENT_VAULT_ID.set(vault_id)
        try:
            while True:
                with _ingest_lock(vault_id):
                    doc = _load_ingest_jobs(vault_id)
                    job = doc.get("jobs", {}).get(job_id)
                    if not isinstance(job, dict):
                        return
                    pending = list(job.get("pending_sources") or [])
                    if not pending:
                        job["status"] = "failed" if job.get("errors") else "completed"
                        job["finished_at"] = _utc_now()
                        job["updated_at"] = _utc_now()
                        job["current_source"] = None
                        job["current_turn"] = None
                        _save_ingest_jobs(doc, vault_id)
                        return
                    rel = pending[0]
                    job_model = job.get("model") or ANTHROPIC_INGEST_MODEL
                    job_max_turns = int(job.get("max_turns") or INGEST_LOOP_MAX_TURNS)
                    job_max_tokens = int(job.get("max_tokens") or INGEST_LOOP_MAX_TOKENS)
                    job["status"] = "running"
                    job["current_source"] = rel
                    job["current_turn"] = 0
                    job["updated_at"] = _utc_now()
                    _save_ingest_jobs(doc, vault_id)

                def _on_turn_start(turn_index: int, _job_id: str = job_id) -> None:
                    with _ingest_lock(vault_id):
                        doc_inner = _load_ingest_jobs(vault_id)
                        job_inner = doc_inner.get("jobs", {}).get(_job_id)
                        if isinstance(job_inner, dict):
                            job_inner["current_turn"] = turn_index + 1
                            job_inner["updated_at"] = _utc_now()
                            _save_ingest_jobs(doc_inner, vault_id)

                result = _run_ingest_agent(
                    rel,
                    model=job_model,
                    max_turns=job_max_turns,
                    max_tokens=job_max_tokens,
                    on_turn_start=_on_turn_start,
                )

                with _ingest_lock(vault_id):
                    doc = _load_ingest_jobs(vault_id)
                    job = doc.get("jobs", {}).get(job_id)
                    if not isinstance(job, dict):
                        return
                    if not result.get("ok"):
                        job["status"] = "failed"
                        job["finished_at"] = _utc_now()
                        job["current_source"] = None
                        job["failed_sources"] = sorted(set(list(job.get("failed_sources") or []) + [rel]))
                        error_entry: dict[str, Any] = {
                            "source": rel,
                            "error": result.get("error", "unknown"),
                            "error_detail": result.get("error_detail"),
                        }
                        if result.get("error") == "max_turns_exceeded":
                            error_entry["last_summary_text"] = result.get("summary_text") or ""
                            error_entry["remedy"] = (
                                f"Hit per-source turn budget of {job_max_turns}. "
                                "Retry with a higher budget: ingest(retry=True, max_turns=N) "
                                "where N is e.g. 40 for complex multi-page reconciliations."
                            )
                        if result.get("error") == "output_token_limit_exceeded":
                            error_entry["last_summary_text"] = result.get("summary_text") or ""
                            error_entry["remedy"] = (
                                f"Hit per-turn output token budget of {job_max_tokens}. "
                                "Increase INGEST_LOOP_MAX_TOKENS and retry the ingest."
                            )
                        job["errors"] = list(job.get("errors") or []) + [error_entry]
                        job["results"] = list(job.get("results") or []) + [{"source": rel, **result}]
                        job["updated_at"] = _utc_now()
                        _save_ingest_jobs(doc, vault_id)
                        return
                    pending = list(job.get("pending_sources") or [])
                    if pending and pending[0] == rel:
                        pending = pending[1:]
                    elif rel in pending:
                        pending.remove(rel)
                    job["pending_sources"] = pending
                    job["processed_sources"] = list(job.get("processed_sources") or []) + [rel]
                    job["results"] = list(job.get("results") or []) + [{"source": rel, **result}]
                    job["updated_at"] = _utc_now()
                    _save_ingest_jobs(doc, vault_id)

                if INGEST_SOURCE_DELAY_SECONDS > 0:
                    time.sleep(INGEST_SOURCE_DELAY_SECONDS)

            # unreachable
        except Exception as exc:  # noqa: BLE001
            with _ingest_lock(vault_id):
                doc = _load_ingest_jobs(vault_id)
                job = doc.get("jobs", {}).get(job_id)
                if isinstance(job, dict):
                    job["status"] = "failed"
                    job["updated_at"] = _utc_now()
                    job["finished_at"] = _utc_now()
                    job["errors"] = list(job.get("errors") or []) + [
                        {"source": job.get("current_source"), "error": str(exc), "traceback": traceback.format_exc()}
                    ]
                    _save_ingest_jobs(doc, vault_id)
        finally:
            _CURRENT_VAULT_ID.reset(token)

    t = threading.Thread(target=_worker, name=f"ingest-job-{job_id[:8]}", daemon=True)
    with _LOCK_GUARD:
        _INGEST_THREADS[vault_id] = t
    t.start()


def _ingest_status_payload(doc: dict[str, Any], job_id: str | None = None) -> dict[str, Any]:
    jid, job = _get_job(doc, job_id)
    if not jid or not job:
        return {"status": "not_found", "message": "No ingest job found."}
    pending = list(job.get("pending_sources") or [])
    processed = list(job.get("processed_sources") or [])
    errors = list(job.get("errors") or [])
    error_sources = [str(error.get("source")) for error in errors if isinstance(error, dict) and error.get("source")]
    failed = sorted(set(list(job.get("failed_sources") or []) + error_sources))
    processed = [source for source in processed if source not in failed]
    if job.get("status") == "failed":
        pending = sorted(set(pending + failed))
    return {
        "job_id": jid,
        "status": job.get("status"),
        "model": job.get("model"),
        "max_turns": job.get("max_turns"),
        "max_tokens": job.get("max_tokens"),
        "started_at": job.get("started_at"),
        "updated_at": job.get("updated_at"),
        "finished_at": job.get("finished_at"),
        "current_source": job.get("current_source"),
        "current_turn": job.get("current_turn"),
        "pending_count": len(pending),
        "processed_count": len(processed),
        "failed_count": len(failed),
        "pending_sources_preview": pending[:20],
        "processed_sources_preview": processed[-20:],
        "failed_sources_preview": failed[-20:],
        "error_count": len(errors),
        "errors_preview": errors[-5:],
    }


@MCP.tool(description="List configured vaults (vault_id values) for multi-vault routing.")
def list_corpora() -> dict[str, Any]:
    return {
        "count": len(CORPORA),
        "vaults": [
            {
                "vault_id": cfg.vault_id,
                "label": cfg.label,
                "vault_root": str(cfg.paths.root),
            }
            for cfg in CORPORA.values()
        ],
    }


@MCP.tool(
    description="Show knowledge-base status for a vault. Pass vault_id when multiple vaults are configured."
)
def stats(vault_id: str = "") -> dict[str, Any]:
    resolved_id, err = _resolve_vault_id(vault_id)
    if err:
        return err
    assert resolved_id is not None
    with _use_vault(resolved_id):
        manifest = _manifest_sources()
        active = [s for s in manifest if s.get("status") == "active"]
        deprecated = [s for s in manifest if s.get("status") == "deprecated"]
        wiki_pages = list(CFG_WIKI.rglob("*.md")) if CFG_WIKI.exists() else []
        ingested = [s.get("ingested_at") for s in manifest if s.get("ingested_at")]
        pending = _pending_sources()
        return {
            "vault_id": resolved_id,
            "vault_root": str(CFG_ROOT),
            "ingest_model": ANTHROPIC_INGEST_MODEL,
            "total_sources": len(manifest),
            "active_sources": len(active),
            "deprecated_sources": len(deprecated),
            "pending_sources": len(pending),
            "pending_source_paths": pending[:50],
            "wiki_page_count": len(wiki_pages),
            "last_ingested_at": max(ingested) if ingested else None,
        }


@MCP.tool(description="Regenerate wiki/index.md deterministically for a vault. Pass vault_id when needed.")
def rebuild_index(vault_id: str = "") -> dict[str, Any]:
    resolved_id, err = _resolve_vault_id(vault_id)
    if err:
        return err
    assert resolved_id is not None
    with _use_vault(resolved_id):
        result = rebuild_wiki_index(CFG_ROOT)
        append_operation_log(
            CFG_ROOT,
            "rebuild_index",
            "wiki/index.md",
            [f"rebuilt deterministic index with {result.get('page_count', 0)} pages"],
        )
        return {"vault_id": resolved_id, **result}


@MCP.tool(
    description=(
        "Capture/store/remember a note for later processing into the knowledge base. Pass vault_id when multiple vaults are configured."
    )
)
def capture(
    content: str,
    filename: str = "",
    frontmatter: dict[str, Any] | None = None,
    vault_id: str = "",
) -> dict[str, str]:
    resolved_id, err = _resolve_vault_id(vault_id)
    if err:
        return {"written": "", **err}
    assert resolved_id is not None
    with _use_vault(resolved_id):
        slug = filename.strip() or datetime.now().strftime("capture-%Y%m%d-%H%M%S")
        if not slug.endswith(".md"):
            slug += ".md"
        rel = f"raw/{slug}"
        path = _resolve(rel)

        body = content.strip() + "\n"
        if frontmatter:
            fm = yaml.safe_dump(frontmatter, sort_keys=False).strip()
            body = f"---\n{fm}\n---\n\n{body}"

        _write_text(path, body)
        append_operation_log(
            CFG_ROOT,
            "capture",
            rel,
            [f"wrote `{rel}`"],
        )
        return {"vault_id": resolved_id, "written": rel}


@MCP.tool(
    description="Answer questions from a vault's notes/knowledge base (wiki-first), with optional source grounding (raw) when requested."
)
def query(question: str, allow_raw: bool = False, vault_id: str = "") -> dict[str, str]:
    resolved_id, err = _resolve_vault_id(vault_id)
    if err:
        return {"answer": "", **err}
    assert resolved_id is not None
    with _use_vault(resolved_id):
        wiki_context = _query_context(question, allow_raw)
        raw_hint = "Allowed" if allow_raw else "Use raw/drafts/manuscript only if the question asks for verification or quotes."
        prompt = (
            "Answer from the wiki-first context below. Cite wiki paths in backticks.\n"
            "If the user might want a reusable synthesis page, mention it once at the end as an optional follow-up — do not create pages.\n\n"
            f"Question: {question}\n\n"
            f"Source layers policy: {raw_hint}\n\n"
            f"Vault CLAUDE.md:\n{_read_text(CFG_CLAUDE_MD)[:12000]}\n\n"
            f"Context:\n{wiki_context}"
        )
        answer = _run_model(prompt, ANTHROPIC_QUERY_MODEL)
        return {"vault_id": resolved_id, "answer": answer}


@MCP.tool(
    description="Read-only source audit for one page/topic in a vault: returns confirmed claims, mismatches, and untraceable statements."
)
def verify(wiki_page: str, vault_id: str = "") -> dict[str, str]:
    resolved_id, err = _resolve_vault_id(vault_id)
    if err:
        return {"report": "", **err}
    assert resolved_id is not None
    with _use_vault(resolved_id):
        rel = wiki_page if wiki_page.startswith("wiki/") else f"wiki/{wiki_page}"
        bundle = verify_bundle_for_page(CFG_ROOT, rel)
        payload = json.dumps(bundle, indent=2, ensure_ascii=False)[:90000]
        prompt = (
            "Read-only audit. Classify claims into: confirmed (supported by cited sources), "
            "mismatched (quote what source says vs wiki claim), untraceable (no cited support).\n"
            "Do not suggest edits.\n\n"
            f"{payload}"
        )
        report = _run_model(prompt, ANTHROPIC_QUERY_MODEL)
        return {"vault_id": resolved_id, "report": report}


@MCP.tool(
    description="Read-only quality check for a vault's knowledge base: broken links, traceability gaps, missing/deprecated references, and coverage issues."
)
def lint(vault_id: str = "") -> dict[str, Any]:
    resolved_id, err = _resolve_vault_id(vault_id)
    if err:
        return err
    assert resolved_id is not None
    with _use_vault(resolved_id):
        det = build_lint_payload(CFG_ROOT)
        det_json = json.dumps(det, indent=2, ensure_ascii=False)[:70000]
        prompt = (
            "You are linting a markdown wiki vault. Deterministic findings are already listed.\n"
            "Add qualitative findings only: suspected unstated contradictions, privacy/pseudonym risks, stale narrative — "
            "as findings with suggested fixes. Do not claim files were auto-fixed.\n\n"
            f"DETERMINISTIC_JSON:\n{det_json}"
        )
        narrative = _run_model(prompt, ANTHROPIC_QUERY_MODEL)
        return {"vault_id": resolved_id, "deterministic": det, "narrative_report": narrative}


def _run_ingest_agent(
    source_rel: str,
    *,
    model: str | None = None,
    max_turns: int | None = None,
    max_tokens: int | None = None,
    on_turn_start: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    if CLIENT is None:
        raise RuntimeError("ANTHROPIC_API_KEY is required for ingest")
    read_out = vault_read(CFG_ROOT, source_rel)
    if not read_out.get("ok"):
        return {"ok": False, "error": read_out.get("error"), "source": source_rel}
    src_text = read_out.get("content") or ""
    system = build_system_prompt()
    user_msg = (
        f"Ingest this source into the wiki (reconcile in place). Source path: `{source_rel}`.\n\n"
        f"--- SOURCE BEGIN ---\n{src_text[:INGEST_SOURCE_CHAR_LIMIT]}\n--- SOURCE END ---\n\n"
        "Use tools until manifest is updated, wiki pages written/updated, index updated if needed, and log appended."
        "Use manifest_get_source for source metadata; do not read full manifest.json."
    )
    return run_tool_loop(
        CLIENT,
        model or ANTHROPIC_INGEST_MODEL,
        system,
        user_msg,
        CFG_ROOT,
        CFG_MANIFEST,
        INGEST_TOOLS,
        max_turns=max_turns or INGEST_LOOP_MAX_TURNS,
        max_tokens=max_tokens or INGEST_LOOP_MAX_TOKENS,
        retry_attempts=INGEST_RATE_RETRY_ATTEMPTS,
        retry_wait_seconds=INGEST_RATE_RETRY_WAIT_SECONDS,
        on_turn_start=on_turn_start,
    )


@MCP.tool(
    description=(
        "Process captured notes into the knowledge base in the background (start-or-report). "
        "Do not auto-poll this tool after starting a job; only call again when the user explicitly requests status. "
        "Inform users that ingest runs in the background and may take 10-20 minutes for larger vaults. "
        "If already running, returns progress. If the most recent run failed, returns the "
        "failure details so the caller can explain them; pass retry=True to start a fresh attempt. "
        "Optional filename targets a specific source. "
        "Optional model overrides ANTHROPIC_INGEST_MODEL for this job (e.g. 'claude-sonnet-4-6' "
        "to fall back when the default ingest model is having issues). "
        "Optional max_turns sets the per-source turn budget for the agent loop "
        "(default from INGEST_LOOP_MAX_TURNS env, currently 35). Bump it (e.g. max_turns=40) "
        "when a source needs extensive cross-page reconciliation and previously failed with "
        "error 'max_turns_exceeded'. "
        "INGEST_LOOP_MAX_TOKENS controls the per-turn output budget; if the model hits it, "
        "the job fails with error 'output_token_limit_exceeded' rather than applying a truncated turn. "
        "If no captures exist yet, this can initialize a starter wiki scaffold (optional topic). "
        "Pass vault_id when multiple vaults are configured."
    )
)
def ingest(
    filename: str = "",
    topic: str = "",
    retry: bool = False,
    model: str = "",
    max_turns: int = 0,
    vault_id: str = "",
) -> dict[str, Any]:
    resolved_id, err = _resolve_vault_id(vault_id)
    if err:
        return err
    assert resolved_id is not None
    if CLIENT is None:
        raise RuntimeError("ANTHROPIC_API_KEY is required for ingest")
    with _use_vault(resolved_id):
        requested_filename = filename.strip()
        requested_model = model.strip() or ANTHROPIC_INGEST_MODEL
        requested_max_turns = max_turns if max_turns > 0 else INGEST_LOOP_MAX_TURNS
        lock = _ingest_lock(resolved_id)
        with lock:
            doc = _load_ingest_jobs(resolved_id)
            current_id, current_job = _get_job(doc)
            if current_id and current_job:
                current_status = current_job.get("status")
                if current_status in {"queued", "running"}:
                    payload = _ingest_status_payload(doc, current_id)
                    payload["message"] = "Ingest job already running."
                    payload["vault_id"] = resolved_id
                    return payload
                if current_status == "failed" and not retry and not requested_filename:
                    payload = _ingest_status_payload(doc, current_id)
                    payload["message"] = (
                        "Previous ingest job failed. See errors_preview for details. "
                        "Pass retry=True to start a new attempt, or pass filename=... to ingest a specific source."
                    )
                    payload["vault_id"] = resolved_id
                    return payload

            requested = requested_filename
            if requested:
                rel = requested if requested.startswith(("raw/", "drafts/", "manuscript/")) else f"raw/{requested}"
                read_out = vault_read(CFG_ROOT, rel, max_bytes=2_000)
                if not read_out.get("ok"):
                    return {
                        "status": "error",
                        "message": read_out.get("error", "invalid source path"),
                        "filename": rel,
                        "vault_id": resolved_id,
                    }
                if read_out.get("missing"):
                    return {"status": "error", "message": f"Source file not found: {rel}", "filename": rel, "vault_id": resolved_id}
                pending = [rel]
                mode = "single"
            else:
                pending = _pending_sources()
                mode = "full"
            if not pending:
                if mode == "full" and _wiki_is_effectively_empty():
                    init = _initialize_empty_wiki(topic=topic)
                    return {
                        "status": "initialized",
                        "message": "No captures found; created starter wiki structure.",
                        "topic": init.get("topic"),
                        "created": init.get("created", []),
                        "pending_count": 0,
                        "vault_id": resolved_id,
                    }
                payload = _ingest_status_payload(doc, current_id) if current_id else {"status": "completed"}
                payload["message"] = "No pending sources."
                payload["pending_count"] = 0
                payload["traceability_warnings"] = traceability_warnings(CFG_ROOT)
                payload["vault_id"] = resolved_id
                return payload

            job_id = f"ingest-{uuid.uuid4().hex[:12]}"
            started = _utc_now()
            doc["current_job_id"] = job_id
            doc["jobs"][job_id] = {
                "job_id": job_id,
                "status": "queued",
                "mode": mode,
                "model": requested_model,
                "max_turns": requested_max_turns,
                "max_tokens": INGEST_LOOP_MAX_TOKENS,
                "started_at": started,
                "updated_at": started,
                "finished_at": None,
                "current_source": None,
                "pending_sources": pending,
                "processed_sources": [],
                "failed_sources": [],
                "results": [],
                "errors": [],
                "pending_at_start": len(pending),
            }
            _save_ingest_jobs(doc, resolved_id)

        _spawn_ingest_worker(job_id, resolved_id)
        with lock:
            payload = _ingest_status_payload(_load_ingest_jobs(resolved_id), job_id)
        payload["message"] = "Ingest job started in background."
        payload["mode"] = mode
        payload["vault_id"] = resolved_id
        if mode == "single":
            payload["filename"] = pending[0]
        return payload


# NOTE: targeted ingest is now handled by ingest(filename=...).


@MCP.tool(
    description="Retire/remove a source from active knowledge-base use for a vault, update provenance status, and reconcile affected pages/citations."
)
def deprecate(filename: str, reason: str, vault_id: str = "") -> dict[str, Any]:
    resolved_id, err = _resolve_vault_id(vault_id)
    if err:
        return err
    assert resolved_id is not None
    if CLIENT is None:
        raise RuntimeError("ANTHROPIC_API_KEY is required for deprecate")
    with _use_vault(resolved_id):
        rel = filename if filename.startswith(("raw/", "drafts/", "manuscript/")) else f"raw/{filename}"
        system = build_system_prompt() + (
            "\nDeprecation mode: mark manifest deprecated, remove or rewrite claims supported only by this source, "
            "update wiki frontmatter sources lists, adjust index if pages removed, note partial excision if cleanup incomplete. "
            "Use manifest_deprecate_source and wiki_write; finish with append_operation_log."
        )
        user_msg = (
            f"Deprecate source `{rel}`.\nReason: {reason}\n"
            "Look up manifest entry and affected wiki pages via vault_read, edit pages, then deprecate in manifest."
        )
        out = run_tool_loop(
            CLIENT,
            ANTHROPIC_INGEST_MODEL,
            system,
            user_msg,
            CFG_ROOT,
            CFG_MANIFEST,
            DEPRECATE_TOOLS,
            max_turns=INGEST_LOOP_MAX_TURNS,
            max_tokens=INGEST_LOOP_MAX_TOKENS,
            retry_attempts=INGEST_RATE_RETRY_ATTEMPTS,
            retry_wait_seconds=INGEST_RATE_RETRY_WAIT_SECONDS,
        )
        warnings = traceability_warnings(CFG_ROOT)
        return {"vault_id": resolved_id, "source": rel, **out, "traceability_warnings": warnings}


@MCP.tool(
    description=(
        "Direct vault file operations for exceptional/manual tasks. "
        "read is allowed without override. "
        "write/move are manual-override operations and require explicit_request=True, "
        "a non-empty reason, and reason text containing 'manual override requested'. "
        "Pass vault_id when multiple vaults are configured."
    )
)
def direct_vault_op(
    operation: str,
    path: str,
    explicit_request: bool = False,
    reason: str = "",
    content: str = "",
    destination_path: str = "",
    vault_id: str = "",
) -> dict[str, Any]:
    resolved_id, err = _resolve_vault_id(vault_id)
    if err:
        return {"ok": False, **err}
    assert resolved_id is not None
    with _use_vault(resolved_id):
        op = operation.strip().lower()
        rel = path.replace("\\", "/").lstrip("/")
        why = reason.strip()
        if op not in {"read", "write", "move"}:
            return {"ok": False, "error": "operation must be 'read', 'write', or 'move'"}
        if op == "read":
            read_out = vault_read(CFG_ROOT, rel)
            return {"ok": bool(read_out.get("ok")), "vault_id": resolved_id, "operation": op, **read_out}

    # Mutation modes are intentionally narrow and explicit.
        if not explicit_request:
            return {"ok": False, "error": f"direct {op} denied: explicit_request must be true"}
        if not why:
            return {"ok": False, "error": f"direct {op} denied: reason is required"}
        if "manual override requested" not in why.lower():
            return {
                "ok": False,
                "error": (
                    f"direct {op} denied: reason must include "
                    "'manual override requested'"
                ),
            }
        if not rel.startswith("wiki/"):
            return {
                "ok": False,
                "error": f"direct {op} is only allowed under wiki/",
                "path": rel,
            }

        if op == "write":
            if not content.strip():
                return {"ok": False, "error": "content is required for write operations", "path": rel}
            write_out = wiki_write(CFG_ROOT, rel, content)
            if not write_out.get("ok"):
                return {"ok": False, "operation": op, "reason": why, **write_out}
            append_operation_log(
                CFG_ROOT,
                "manual_override",
                rel,
                [f"direct_vault_op write requested explicitly: {why}"],
            )
            return {"ok": True, "vault_id": resolved_id, "operation": op, "path": rel, "bytes": len(content.encode("utf-8"))}

        dest_rel = destination_path.replace("\\", "/").lstrip("/")
        if not dest_rel:
            return {"ok": False, "error": "destination_path is required for move", "path": rel}
        if not dest_rel.startswith("wiki/"):
            return {
                "ok": False,
                "error": "direct move destination must be under wiki/",
                "destination_path": dest_rel,
            }
        try:
            src = _resolve(rel)
            dst = _resolve(dest_rel)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "path": rel, "destination_path": dest_rel}
        if not src.exists():
            return {"ok": False, "error": "source file not found", "path": rel}
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        append_operation_log(
            CFG_ROOT,
            "manual_override",
            rel,
            [f"direct_vault_op move requested explicitly: {why}", f"moved to `{dest_rel}`"],
        )
        return {"ok": True, "vault_id": resolved_id, "operation": op, "from": rel, "to": dest_rel}


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8765"))
    path = os.getenv("MCP_PATH", "/mcp")
    MCP.run(transport="streamable-http", host=host, port=port, path=path)


if __name__ == "__main__":
    main()
