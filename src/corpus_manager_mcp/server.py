from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
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
    traceability_warnings,
    vault_read,
)

load_dotenv()


def _build_config() -> VaultPaths:
    vault_root = Path(os.getenv("VAULT_ROOT", "/data/vault")).expanduser().resolve()
    claude_md = Path(os.getenv("CLAUDE_MD_PATH", str(vault_root / "CLAUDE.md"))).expanduser().resolve()
    return VaultPaths.from_roots(vault_root, claude_md)


VP = _build_config()
CFG_ROOT = VP.root
CFG_WIKI = VP.wiki
CFG_MANIFEST = VP.manifest
CFG_CLAUDE_MD = VP.claude_md

SERVER_INSTRUCTIONS = (
    "Corpus Manager is a VPS-hosted personal knowledge base manager. "
    "Users may say wiki, notes, knowledge base, second brain, memory, or vault. "
    "Use capture when users say capture/store/save/remember this, ingest when users say process/update/sync notes into the knowledge base, "
    "stats for status/what is pending, query for ask/find/what do my notes say, lint/verify for audits, and deprecate to retire a source. "
    "If users ask to initialize/start a brand-new wiki and there are no captures yet, ingest may create a starter scaffold (optionally topic-guided)."
)

MCP = FastMCP("Corpus Manager", instructions=SERVER_INSTRUCTIONS)
CLIENT = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY")) if os.getenv("ANTHROPIC_API_KEY") else None

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
ANTHROPIC_QUERY_MODEL = os.getenv("ANTHROPIC_QUERY_MODEL", "claude-sonnet-4-5")
# Use Opus for ingest operations to benefit from higher ingest TPM limits.
ANTHROPIC_INGEST_MODEL = "claude-opus-4-1-20250805"
MD_MCP_HTTP_URL = os.getenv("MD_MCP_HTTP_URL", "").rstrip("/")
INGEST_SOURCE_CHAR_LIMIT = int(os.getenv("INGEST_SOURCE_CHAR_LIMIT", "6000"))
INGEST_CLAUDE_MD_CHAR_LIMIT = int(os.getenv("INGEST_CLAUDE_MD_CHAR_LIMIT", "8000"))
INGEST_LOOP_MAX_TURNS = 10
INGEST_LOOP_MAX_TOKENS = 2500
INGEST_RATE_RETRY_ATTEMPTS = int(os.getenv("INGEST_RATE_RETRY_ATTEMPTS", "3"))
INGEST_RATE_RETRY_WAIT_SECONDS = int(os.getenv("INGEST_RATE_RETRY_WAIT_SECONDS", "12"))
INGEST_SOURCE_DELAY_SECONDS = 5
INGEST_JOBS_PATH = CFG_WIKI / ".ingest-jobs.json"
_INGEST_LOCK = threading.Lock()
_INGEST_THREAD: threading.Thread | None = None


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
Karpathy-style wiki (operational layer):
- raw/ is read-only for automation; wiki/ and manifest.json are maintained by ingest tools.
- Never write to manuscript/ or drafts/ unless the user explicitly requested it (tools do not allow it).
- manuscript/ and drafts/ outrank raw/ when both address the same factual claims; surface conflicts with warning callouts, never silent merges.
- Use YAML frontmatter on wiki pages: type, book, sources, date_updated, tags as appropriate.
- Wikilinks [[like/this]] aggressively; first mention should link.
- Reconcile in place; do not duplicate pages.
- After editing wiki pages, call manifest_upsert_source with the source filename and full wiki_pages list (paths relative to wiki/, no wiki/ prefix).
- Update wiki/index.md when adding navigable pages.
- Finish by append_operation_log with bullets listing wiki paths touched and manifest updated.

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


def _md_mcp_ping() -> str:
    if not MD_MCP_HTTP_URL:
        return "md-mcp HTTP URL not configured"
    try:
        with httpx.Client(timeout=8.0) as client:
            r = client.get(MD_MCP_HTTP_URL)
            return f"md-mcp reachable: {r.status_code}"
    except Exception as exc:  # noqa: BLE001
        return f"md-mcp health check failed: {exc}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_ingest_jobs() -> dict[str, Any]:
    if not INGEST_JOBS_PATH.exists():
        return {"current_job_id": None, "jobs": {}}
    try:
        doc = json.loads(INGEST_JOBS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"current_job_id": None, "jobs": {}}
    if not isinstance(doc, dict):
        return {"current_job_id": None, "jobs": {}}
    if "jobs" not in doc or not isinstance(doc["jobs"], dict):
        doc["jobs"] = {}
    if "current_job_id" not in doc:
        doc["current_job_id"] = None
    return doc


def _save_ingest_jobs(doc: dict[str, Any]) -> None:
    INGEST_JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    INGEST_JOBS_PATH.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


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


def _spawn_ingest_worker(job_id: str) -> None:
    def _worker() -> None:
        try:
            while True:
                with _INGEST_LOCK:
                    doc = _load_ingest_jobs()
                    job = doc.get("jobs", {}).get(job_id)
                    if not isinstance(job, dict):
                        return
                    pending = list(job.get("pending_sources") or [])
                    if not pending:
                        job["status"] = "completed"
                        job["finished_at"] = _utc_now()
                        job["updated_at"] = _utc_now()
                        job["current_source"] = None
                        _save_ingest_jobs(doc)
                        return
                    rel = pending[0]
                    job["status"] = "running"
                    job["current_source"] = rel
                    job["updated_at"] = _utc_now()
                    _save_ingest_jobs(doc)

                result = _run_ingest_agent(rel)

                with _INGEST_LOCK:
                    doc = _load_ingest_jobs()
                    job = doc.get("jobs", {}).get(job_id)
                    if not isinstance(job, dict):
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
                    if not result.get("ok"):
                        job["errors"] = list(job.get("errors") or []) + [
                            {"source": rel, "error": result.get("error", "unknown")}
                        ]
                # keep going even if one source fails; run through full queue
                    _save_ingest_jobs(doc)

                if INGEST_SOURCE_DELAY_SECONDS > 0:
                    time.sleep(INGEST_SOURCE_DELAY_SECONDS)

            # unreachable
        except Exception as exc:  # noqa: BLE001
            with _INGEST_LOCK:
                doc = _load_ingest_jobs()
                job = doc.get("jobs", {}).get(job_id)
                if isinstance(job, dict):
                    job["status"] = "failed"
                    job["updated_at"] = _utc_now()
                    job["finished_at"] = _utc_now()
                    job["errors"] = list(job.get("errors") or []) + [
                        {"source": job.get("current_source"), "error": str(exc), "traceback": traceback.format_exc()}
                    ]
                    _save_ingest_jobs(doc)

    global _INGEST_THREAD
    t = threading.Thread(target=_worker, name=f"ingest-job-{job_id[:8]}", daemon=True)
    _INGEST_THREAD = t
    t.start()


def _ingest_status_payload(doc: dict[str, Any], job_id: str | None = None) -> dict[str, Any]:
    jid, job = _get_job(doc, job_id)
    if not jid or not job:
        return {"status": "not_found", "message": "No ingest job found."}
    pending = list(job.get("pending_sources") or [])
    processed = list(job.get("processed_sources") or [])
    errors = list(job.get("errors") or [])
    return {
        "job_id": jid,
        "status": job.get("status"),
        "started_at": job.get("started_at"),
        "updated_at": job.get("updated_at"),
        "finished_at": job.get("finished_at"),
        "current_source": job.get("current_source"),
        "pending_count": len(pending),
        "processed_count": len(processed),
        "pending_sources_preview": pending[:20],
        "processed_sources_preview": processed[-20:],
        "error_count": len(errors),
        "errors_preview": errors[-5:],
    }


@MCP.tool(
    description="Show knowledge-base status: how many sources/pages exist, how many are pending processing, and overall connector health."
)
def stats() -> dict[str, Any]:
    manifest = _manifest_sources()
    active = [s for s in manifest if s.get("status") == "active"]
    deprecated = [s for s in manifest if s.get("status") == "deprecated"]
    wiki_pages = list(CFG_WIKI.rglob("*.md")) if CFG_WIKI.exists() else []
    ingested = [s.get("ingested_at") for s in manifest if s.get("ingested_at")]
    pending = _pending_sources()
    return {
        "vault_root": str(CFG_ROOT),
        "ingest_model": ANTHROPIC_INGEST_MODEL,
        "total_sources": len(manifest),
        "active_sources": len(active),
        "deprecated_sources": len(deprecated),
        "pending_sources": len(pending),
        "pending_source_paths": pending[:50],
        "wiki_page_count": len(wiki_pages),
        "last_ingested_at": max(ingested) if ingested else None,
        "md_mcp": _md_mcp_ping(),
    }


@MCP.tool(
    description=(
        "Capture/store/remember a note, thought, idea, or fragment for later processing into the knowledge base."
    )
)
def capture(content: str, filename: str = "", frontmatter: dict[str, Any] | None = None) -> dict[str, str]:
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
    return {"written": rel}


@MCP.tool(
    description="Answer questions from the user's notes/knowledge base (wiki-first), with optional source grounding (raw) when requested."
)
def query(question: str, allow_raw: bool = False) -> dict[str, str]:
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
    return {"answer": answer}


@MCP.tool(
    description="Read-only source audit for one page/topic: returns confirmed claims, mismatches, and untraceable statements."
)
def verify(wiki_page: str) -> dict[str, str]:
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
    return {"report": report}


@MCP.tool(
    description="Read-only quality check for the knowledge base: broken links, traceability gaps, missing/deprecated references, and coverage issues."
)
def lint() -> dict[str, Any]:
    det = build_lint_payload(CFG_ROOT)
    det_json = json.dumps(det, indent=2, ensure_ascii=False)[:70000]
    prompt = (
        "You are linting a markdown wiki vault. Deterministic findings are already listed.\n"
        "Add qualitative findings only: suspected unstated contradictions, privacy/pseudonym risks, stale narrative — "
        "as findings with suggested fixes. Do not claim files were auto-fixed.\n\n"
        f"DETERMINISTIC_JSON:\n{det_json}"
    )
    narrative = _run_model(prompt, ANTHROPIC_QUERY_MODEL)
    return {"deterministic": det, "narrative_report": narrative}


def _run_ingest_agent(source_rel: str) -> dict[str, Any]:
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
        ANTHROPIC_INGEST_MODEL,
        system,
        user_msg,
        CFG_ROOT,
        CFG_MANIFEST,
        INGEST_TOOLS,
        max_turns=INGEST_LOOP_MAX_TURNS,
        max_tokens=INGEST_LOOP_MAX_TOKENS,
        retry_attempts=INGEST_RATE_RETRY_ATTEMPTS,
        retry_wait_seconds=INGEST_RATE_RETRY_WAIT_SECONDS,
    )


@MCP.tool(
    description=(
        "Process captured notes into the knowledge base in the background (start-or-report). "
        "If already running, returns progress. Optional filename targets a specific source. "
        "If no captures exist yet, this can initialize a starter wiki scaffold (optional topic)."
    )
)
def ingest(filename: str = "", topic: str = "") -> dict[str, Any]:
    if CLIENT is None:
        raise RuntimeError("ANTHROPIC_API_KEY is required for ingest")
    with _INGEST_LOCK:
        doc = _load_ingest_jobs()
        current_id, current_job = _get_job(doc)
        if current_id and current_job and current_job.get("status") in {"queued", "running"}:
            payload = _ingest_status_payload(doc, current_id)
            payload["message"] = "Ingest job already running."
            return payload

        requested = filename.strip()
        if requested:
            rel = requested if requested.startswith(("raw/", "drafts/", "manuscript/")) else f"raw/{requested}"
            # Validate source path exists before creating a job.
            read_out = vault_read(CFG_ROOT, rel, max_bytes=2_000)
            if not read_out.get("ok"):
                return {"status": "error", "message": read_out.get("error", "invalid source path"), "filename": rel}
            if read_out.get("missing"):
                return {"status": "error", "message": f"Source file not found: {rel}", "filename": rel}
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
                }
            payload = _ingest_status_payload(doc, current_id) if current_id else {"status": "completed"}
            payload["message"] = "No pending sources."
            payload["pending_count"] = 0
            payload["traceability_warnings"] = traceability_warnings(CFG_ROOT)
            return payload

        job_id = f"ingest-{uuid.uuid4().hex[:12]}"
        started = _utc_now()
        doc["current_job_id"] = job_id
        doc["jobs"][job_id] = {
            "job_id": job_id,
            "status": "queued",
            "mode": mode,
            "started_at": started,
            "updated_at": started,
            "finished_at": None,
            "current_source": None,
            "pending_sources": pending,
            "processed_sources": [],
            "results": [],
            "errors": [],
            "pending_at_start": len(pending),
        }
        _save_ingest_jobs(doc)

    _spawn_ingest_worker(job_id)
    with _INGEST_LOCK:
        payload = _ingest_status_payload(_load_ingest_jobs(), job_id)
    payload["message"] = "Ingest job started in background."
    payload["mode"] = mode
    if mode == "single":
        payload["filename"] = pending[0]
    return payload


# NOTE: targeted ingest is now handled by ingest(filename=...).


@MCP.tool(
    description="Retire/remove a source from active knowledge-base use, update provenance status, and reconcile affected pages/citations."
)
def deprecate(filename: str, reason: str) -> dict[str, Any]:
    if CLIENT is None:
        raise RuntimeError("ANTHROPIC_API_KEY is required for deprecate")
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
    return {"source": rel, **out, "traceability_warnings": warnings}


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8765"))
    path = os.getenv("MCP_PATH", "/mcp")
    MCP.run(transport="streamable-http", host=host, port=port, path=path)


if __name__ == "__main__":
    main()
