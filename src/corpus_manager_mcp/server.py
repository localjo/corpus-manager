from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from anthropic import Anthropic
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()


@dataclass
class Config:
    vault_root: Path
    claude_md_path: Path
    wiki_root: Path
    raw_root: Path
    manifest_path: Path
    anthropic_model: str
    anthropic_query_model: str
    md_mcp_http_url: str


def _build_config() -> Config:
    vault_root = Path(os.getenv("VAULT_ROOT", "/data/vault")).expanduser().resolve()
    return Config(
        vault_root=vault_root,
        claude_md_path=Path(os.getenv("CLAUDE_MD_PATH", str(vault_root / "CLAUDE.md"))).expanduser().resolve(),
        wiki_root=vault_root / "wiki",
        raw_root=vault_root / "raw",
        manifest_path=vault_root / "manifest.json",
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        anthropic_query_model=os.getenv("ANTHROPIC_QUERY_MODEL", "claude-sonnet-4-5"),
        md_mcp_http_url=os.getenv("MD_MCP_HTTP_URL", "").rstrip("/"),
    )


CFG = _build_config()
MCP = FastMCP("Corpus Manager")
CLIENT = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY")) if os.getenv("ANTHROPIC_API_KEY") else None


def _resolve(path: str) -> Path:
    p = (CFG.vault_root / path).resolve()
    if not str(p).startswith(str(CFG.vault_root)):
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


def _append_log(line: str) -> None:
    log_path = CFG.wiki_root / "log.md"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n## [{ts}] mcp-operation | {line}\n\n- via Corpus Manager MCP\n"
    _write_text(log_path, _read_text(log_path) + entry)


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


def _list_raw_files() -> list[Path]:
    if not CFG.raw_root.exists():
        return []
    return [p for p in CFG.raw_root.rglob("*") if p.is_file() and not _is_ignored_source(p)]


def _is_ignored_source(path: Path) -> bool:
    name = path.name
    # Ignore hidden/system/editor metadata files during ingest scans.
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
    doc = _read_json(CFG.manifest_path)
    sources = doc.get("sources")
    return sources if isinstance(sources, list) else []


def _pending_sources() -> list[str]:
    manifest = _manifest_sources()
    by_filename = {entry.get("filename"): entry for entry in manifest if isinstance(entry, dict)}
    pending: list[str] = []
    for f in _list_raw_files():
        rel = str(f.relative_to(CFG.vault_root))
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


def _search_wiki(question: str) -> str:
    # KISS retrieval: cheap keyword scan before model synthesis.
    tokens = [t for t in re.split(r"\W+", question.lower()) if len(t) >= 4]
    hits: list[tuple[int, Path]] = []
    for p in CFG.wiki_root.rglob("*.md"):
        text = _read_text(p).lower()
        score = sum(tok in text for tok in tokens)
        if score:
            hits.append((score, p))
    hits.sort(key=lambda x: x[0], reverse=True)
    top = [str(p.relative_to(CFG.vault_root)) for _, p in hits[:8]]
    chunks = []
    for rel in top:
        chunks.append(f"## {rel}\n{_read_text(_resolve(rel))[:4000]}")
    return "\n\n".join(chunks)


def _md_mcp_ping() -> str:
    if not CFG.md_mcp_http_url:
        return "md-mcp HTTP URL not configured"
    try:
        with httpx.Client(timeout=8.0) as client:
            r = client.get(CFG.md_mcp_http_url)
            return f"md-mcp reachable: {r.status_code}"
    except Exception as exc:  # noqa: BLE001
        return f"md-mcp health check failed: {exc}"


@MCP.tool()
def stats() -> dict[str, Any]:
    manifest = _manifest_sources()
    active = [s for s in manifest if s.get("status") == "active"]
    deprecated = [s for s in manifest if s.get("status") == "deprecated"]
    wiki_pages = list(CFG.wiki_root.rglob("*.md")) if CFG.wiki_root.exists() else []
    ingested = [s.get("ingested_at") for s in manifest if s.get("ingested_at")]
    pending = _pending_sources()
    return {
        "vault_root": str(CFG.vault_root),
        "total_sources": len(manifest),
        "active_sources": len(active),
        "deprecated_sources": len(deprecated),
        "pending_sources": len(pending),
        "pending_source_paths": pending[:50],
        "wiki_page_count": len(wiki_pages),
        "last_ingested_at": max(ingested) if ingested else None,
        "md_mcp": _md_mcp_ping(),
    }


@MCP.tool()
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
    _append_log(f"capture | {rel}")
    return {"written": rel}


@MCP.tool()
def query(question: str, allow_raw: bool = False) -> dict[str, str]:
    wiki_context = _search_wiki(question)
    raw_hint = "Allowed" if allow_raw else "Not allowed unless explicitly required."
    prompt = (
        f"Use the vault context to answer. Cite wiki paths in backticks.\\n"
        f"Question: {question}\\n"
        f"Raw usage: {raw_hint}\\n\\n"
        f"Vault CLAUDE.md:\\n{_read_text(CFG.claude_md_path)[:12000]}\\n\\n"
        f"Wiki context:\\n{wiki_context}"
    )
    answer = _run_model(prompt, CFG.anthropic_query_model)
    _append_log("query")
    return {"answer": answer}


@MCP.tool()
def verify(wiki_page: str) -> dict[str, str]:
    rel = wiki_page if wiki_page.startswith("wiki/") else f"wiki/{wiki_page}"
    page = _read_text(_resolve(rel))
    prompt = (
        "Audit this page and return: confirmed claims, mismatches, missing sources.\\n"
        f"Page path: {rel}\\n\\n"
        f"{page}"
    )
    report = _run_model(prompt, CFG.anthropic_model)
    _append_log(f"verify | {rel}")
    return {"report": report}


@MCP.tool()
def lint() -> dict[str, str]:
    index = _read_text(CFG.wiki_root / "index.md")
    manifest = json.dumps(_read_json(CFG.manifest_path), indent=2)[:50000]
    prompt = (
        "Lint this vault and report prioritized issues: broken links, orphan pages, stale or contradictory claims.\\n"
        f"index.md:\\n{index}\\n\\nmanifest.json:\\n{manifest}"
    )
    report = _run_model(prompt, CFG.anthropic_model)
    _append_log("lint")
    return {"report": report}


@MCP.tool()
def ingest() -> dict[str, Any]:
    pending = _pending_sources()
    touched: list[str] = []
    summaries: list[str] = []

    # KISS: per-source VPS loop. One outer MCP call; repeated inner model calls.
    for rel in pending:
        source = _read_text(_resolve(rel))[:14000]
        prompt = (
            "Given source text and existing vault rules, propose concise wiki updates. "
            "Return plain markdown bullets with page paths and required edits.\\n"
            f"Source: {rel}\\n\\n{source}\\n\\n"
            f"Vault rules:\\n{_read_text(CFG.claude_md_path)[:8000]}"
        )
        summaries.append(f"### {rel}\\n" + _run_model(prompt, CFG.anthropic_model))
        touched.append(rel)

    _append_log(f"ingest | {len(touched)} source(s)")
    return {
        "pending_count": len(pending),
        "sources": touched,
        "plan": "\\n\\n".join(summaries) if summaries else "No pending sources.",
    }


@MCP.tool()
def ingest_file(filename: str) -> dict[str, str]:
    rel = filename if filename.startswith(("raw/", "drafts/", "manuscript/")) else f"raw/{filename}"
    text = _read_text(_resolve(rel))[:20000]
    prompt = (
        "Reconcile this source with the wiki and return concrete page edits as markdown bullets.\\n"
        f"Source path: {rel}\\n\\n{text}\\n\\n"
        f"Vault rules:\\n{_read_text(CFG.claude_md_path)[:9000]}"
    )
    plan = _run_model(prompt, CFG.anthropic_model)
    _append_log(f"ingest_file | {rel}")
    return {"source": rel, "plan": plan}


@MCP.tool()
def deprecate(filename: str, reason: str) -> dict[str, str]:
    rel = filename if filename.startswith(("raw/", "drafts/", "manuscript/")) else f"raw/{filename}"
    prompt = (
        "Generate a deprecation action plan for this source, including manifest fields and affected wiki pages.\\n"
        f"Source: {rel}\\nReason: {reason}\\n"
    )
    result = _run_model(prompt, CFG.anthropic_model)
    _append_log(f"deprecate | {rel}")
    return {"source": rel, "result": result}


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8765"))
    path = os.getenv("MCP_PATH", "/mcp")
    MCP.run(transport="streamable-http", host=host, port=port, path=path)


if __name__ == "__main__":
    main()
