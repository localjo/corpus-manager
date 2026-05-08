"""Vault filesystem, manifest, frontmatter, logging — Corpus Manager primitives."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# Log rotation thresholds (implementation guide §14)
LOG_MAX_ENTRIES = 300
LOG_MAX_BYTES = 512 * 1024


@dataclass(frozen=True)
class VaultPaths:
    root: Path
    wiki: Path
    manifest: Path
    claude_md: Path

    @classmethod
    def from_roots(cls, vault_root: Path, claude_md: Path | None = None) -> VaultPaths:
        root = vault_root.expanduser().resolve()
        return cls(
            root=root,
            wiki=root / "wiki",
            manifest=root / "manifest.json",
            claude_md=(claude_md or root / "CLAUDE.md").expanduser().resolve(),
        )


def resolve_safe(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if not str(p).startswith(str(root)):
        raise ValueError("Path escapes vault root")
    return p


def can_read_rel(root: Path, rel: str) -> bool:
    rel_n = rel.replace("\\", "/").lstrip("/")
    if rel_n == "manifest.json" or rel_n.endswith("/manifest.json"):
        return True
    if rel_n == "CLAUDE.md" or rel_n.endswith("/CLAUDE.md"):
        return True
    for prefix in ("raw/", "drafts/", "manuscript/", "wiki/"):
        if rel_n.startswith(prefix):
            return True
    return False


def can_write_rel(root: Path, rel: str) -> bool:
    rel_n = rel.replace("\\", "/").lstrip("/")
    if rel_n == "manifest.json":
        return True
    if rel_n.startswith("wiki/"):
        return True
    return False


def vault_read(root: Path, rel: str, max_bytes: int = 500_000) -> dict[str, Any]:
    rel = rel.replace("\\", "/").lstrip("/")
    if not can_read_rel(root, rel):
        return {"ok": False, "error": f"read not allowed for path: {rel}"}
    path = resolve_safe(root, rel)
    if not path.exists():
        return {"ok": True, "path": rel, "content": "", "missing": True}
    data = path.read_bytes()
    if len(data) > max_bytes:
        text = data[:max_bytes].decode("utf-8", errors="replace") + "\n\n… [truncated]"
    else:
        text = data.decode("utf-8", errors="replace")
    return {"ok": True, "path": rel, "content": text, "missing": False}


def wiki_write(root: Path, rel: str, content: str) -> dict[str, Any]:
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel.startswith("wiki/"):
        rel = f"wiki/{rel}"
    if not can_write_rel(root, rel):
        return {"ok": False, "error": f"write not allowed for path: {rel}"}
    path = resolve_safe(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": rel, "bytes": len(content.encode("utf-8"))}


def _wiki_page_title(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    _, body = split_frontmatter(text)
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return path.stem.replace("-", " ").title()


def rebuild_wiki_index(root: Path) -> dict[str, Any]:
    """Regenerate wiki/index.md from the current wiki tree."""
    wiki = root / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)

    section_labels = {
        "chapters": "Chapters",
        "concepts": "Concepts",
        "frameworks": "Frameworks",
        "entities": "Entities",
        "synthesis": "Synthesis",
    }
    section_order = ["chapters", "concepts", "frameworks", "entities", "synthesis"]
    grouped: dict[str, list[tuple[str, str]]] = {section: [] for section in section_order}
    grouped["other"] = []

    for path in wiki.rglob("*.md"):
        rel = str(path.relative_to(wiki)).replace("\\", "/")
        if rel in {"index.md", "log.md"} or rel.startswith("log-archive/"):
            continue
        section = rel.split("/", 1)[0] if "/" in rel else "other"
        if section not in grouped:
            section = "other"
        title = _wiki_page_title(path)
        link = rel[:-3] if rel.endswith(".md") else rel
        grouped[section].append((title, link))

    lines = ["# Wiki Index", ""]
    total_pages = 0
    for section in [*section_order, "other"]:
        entries = sorted(grouped[section], key=lambda item: item[0].lower())
        if not entries:
            continue
        label = section_labels.get(section, "Other")
        lines.extend([f"## {label}", ""])
        for title, link in entries:
            lines.append(f"- [[{link}|{title}]]")
        lines.append("")
        total_pages += len(entries)

    index_path = wiki / "index.md"
    content = "\n".join(lines).rstrip() + "\n"
    index_path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": "wiki/index.md", "page_count": total_pages, "bytes": len(content.encode("utf-8"))}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"sources": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def manifest_load(manifest_path: Path) -> dict[str, Any]:
    doc = _read_json(manifest_path)
    if "sources" not in doc or not isinstance(doc["sources"], list):
        doc["sources"] = []
    return doc


def manifest_save(manifest_path: Path, doc: dict[str, Any]) -> None:
    _write_json(manifest_path, doc)


def _default_source_entry(
    filename: str,
    *,
    layer: str,
    book: str | None,
    wiki_pages: list[str],
    status: str = "active",
) -> dict[str, Any]:
    return {
        "filename": filename,
        "layer": layer,
        "book": book,
        "ingested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wiki_pages": wiki_pages,
        "status": status,
        "deprecation_reason": None,
        "deprecated_at": None,
    }


def manifest_upsert_source(
    manifest_path: Path,
    filename: str,
    layer: str,
    book: str | None,
    wiki_pages: list[str],
) -> dict[str, Any]:
    doc = manifest_load(manifest_path)
    sources: list[dict[str, Any]] = doc["sources"]
    found = False
    iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i, entry in enumerate(sources):
        if entry.get("filename") == filename:
            sources[i] = {
                **entry,
                "layer": layer,
                "book": book,
                "ingested_at": iso_now,
                "wiki_pages": wiki_pages,
                "status": entry.get("status") or "active",
                "deprecation_reason": entry.get("deprecation_reason"),
                "deprecated_at": entry.get("deprecated_at"),
            }
            found = True
            break
    if not found:
        sources.append(_default_source_entry(filename, layer=layer, book=book, wiki_pages=wiki_pages))
    manifest_save(manifest_path, doc)
    return {"ok": True, "filename": filename, "wiki_pages": wiki_pages, "ingested_at": iso_now}


def manifest_get_source(manifest_path: Path, filename: str) -> dict[str, Any]:
    doc = manifest_load(manifest_path)
    sources: list[dict[str, Any]] = doc["sources"]
    for entry in sources:
        if entry.get("filename") == filename:
            return {
                "ok": True,
                "source": {
                    "filename": entry.get("filename"),
                    "layer": entry.get("layer"),
                    "book": entry.get("book"),
                    "ingested_at": entry.get("ingested_at"),
                    "wiki_pages": entry.get("wiki_pages") or [],
                    "status": entry.get("status"),
                    "deprecation_reason": entry.get("deprecation_reason"),
                    "deprecated_at": entry.get("deprecated_at"),
                },
            }
    return {"ok": False, "error": f"source not found in manifest: {filename}"}


def manifest_deprecate_source(
    manifest_path: Path,
    filename: str,
    reason: str,
) -> dict[str, Any]:
    doc = manifest_load(manifest_path)
    sources: list[dict[str, Any]] = doc["sources"]
    iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i, entry in enumerate(sources):
        if entry.get("filename") == filename:
            sources[i] = {
                **entry,
                "status": "deprecated",
                "deprecation_reason": reason,
                "deprecated_at": iso_now,
            }
            manifest_save(manifest_path, doc)
            return {"ok": True, "filename": filename, "deprecated_at": iso_now}
    return {"ok": False, "error": f"source not found in manifest: {filename}"}


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    if not text.startswith("---"):
        return None, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*", text, re.DOTALL)
    if not m:
        return None, text
    body = text[m.end() :]
    try:
        meta = yaml.safe_load(m.group(1)) or {}
        if not isinstance(meta, dict):
            return None, text
        return meta, body.lstrip("\n")
    except yaml.YAMLError:
        return None, text


def merge_frontmatter_body(meta: dict[str, Any], body: str) -> str:
    dumped = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{dumped}\n---\n\n{body}"


def parse_frontmatter_sources(wiki_path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    text = wiki_path.read_text(encoding="utf-8") if wiki_path.exists() else ""
    meta, body = split_frontmatter(text)
    sources: list[str] = []
    if meta and isinstance(meta.get("sources"), list):
        sources = [str(s) for s in meta["sources"]]
    return meta, sources


def infer_layer_book(rel: str) -> tuple[str, str | None]:
    r = rel.replace("\\", "/")
    if r.startswith("raw/"):
        return "raw", None
    if r.startswith("drafts/memoir/") or r.startswith("drafts/guide/"):
        book = "memoir" if "/memoir/" in r else "guide"
        return "drafts", book
    if r.startswith("drafts/"):
        return "drafts", None
    if r.startswith("manuscript/"):
        return "manuscript", None
    return "raw", None


_LOG_HEADING_LINE = re.compile(r"(?m)^## \[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]")


def append_operation_log(
    root: Path,
    operation: str,
    subject: str,
    bullets: list[str],
) -> dict[str, Any]:
    log_path = root / "wiki" / "log.md"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"\n## [{ts}] {operation} | {subject}\n"]
    for b in bullets:
        lines.append(f"- {b}\n")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    prev = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    log_path.write_text(prev + "".join(lines), encoding="utf-8")
    rotate_result = maybe_rotate_log(root)
    return {"ok": True, "log_path": "wiki/log.md", "rotation": rotate_result}


def maybe_rotate_log(root: Path) -> dict[str, Any]:
    log_path = root / "wiki" / "log.md"
    if not log_path.exists():
        return {"rotated": False}
    content = log_path.read_text(encoding="utf-8")
    matches = list(_LOG_HEADING_LINE.finditer(content))
    if not matches:
        return {"rotated": False}

    blocks: list[str] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        blocks.append(content[start:end])
    preamble = content[: matches[0].start()]
    entry_count = len(blocks)
    size = len(content.encode("utf-8"))
    if entry_count <= LOG_MAX_ENTRIES and size <= LOG_MAX_BYTES:
        return {"rotated": False}

    archive_dir = root / "wiki" / "log-archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived_names: list[str] = []

    while blocks and (
        len(blocks) > LOG_MAX_ENTRIES
        or len((preamble + "".join(blocks)).encode("utf-8")) > LOG_MAX_BYTES
    ):
        oldest = blocks.pop(0)
        mdate = re.match(r"(?m)^## \[(\d{4}-\d{2})", oldest)
        ym = mdate.group(1) if mdate else datetime.now().strftime("%Y-%m")
        arch_file = archive_dir / f"{ym}.md"
        existing = arch_file.read_text(encoding="utf-8") if arch_file.exists() else ""
        arch_file.write_text(existing + "\n" + oldest, encoding="utf-8")
        archived_names.append(str(arch_file.relative_to(root)))

    new_content = preamble + "".join(blocks)
    log_path.write_text(new_content, encoding="utf-8")
    return {"rotated": True, "archived_to": archived_names[-5:], "remaining_entries": len(blocks)}


def normalize_wiki_page_path(wp: str) -> str:
    w = wp.replace("\\", "/").lstrip("/")
    if w.startswith("wiki/"):
        w = w[5:]
    return w


def wiki_rel_from_page(wp: str) -> str:
    w = normalize_wiki_page_path(wp)
    return f"wiki/{w}"


def traceability_warnings(root: Path) -> list[str]:
    manifest_path = root / "manifest.json"
    doc = manifest_load(manifest_path)
    warnings: list[str] = []
    manifest_by_file = {e.get("filename"): e for e in doc.get("sources", []) if isinstance(e, dict)}

    for fn, entry in manifest_by_file.items():
        wps = entry.get("wiki_pages") or []
        if not isinstance(wps, list):
            continue
        for wp in wps:
            rel = wiki_rel_from_page(str(wp))
            try:
                path = resolve_safe(root, rel)
            except ValueError:
                warnings.append(f"manifest lists invalid wiki path {wp} for {fn}")
                continue
            if not path.exists():
                warnings.append(f"manifest wiki_pages missing file: {rel} (source {fn})")
                continue
            meta, fm_sources = parse_frontmatter_sources(path)
            if meta is None:
                continue
            src_list = fm_sources if fm_sources else []
            if fn not in src_list:
                warnings.append(f"page {rel} frontmatter sources missing manifest filename {fn}")

    # Reverse: wiki sources should appear in manifest wiki_pages for that file
    wiki_root = root / "wiki"
    if wiki_root.exists():
        for p in wiki_root.rglob("*.md"):
            if p.name in ("log.md", "index.md"):
                continue
            rel = str(p.relative_to(root))
            meta, fm_sources = parse_frontmatter_sources(p)
            if not meta or not fm_sources:
                continue
            for fn in fm_sources:
                ent = manifest_by_file.get(fn)
                if not ent:
                    warnings.append(f"{rel} cites source not in manifest: {fn}")
                    continue
                wps = ent.get("wiki_pages") or []
                norm = normalize_wiki_page_path(str(p.relative_to(root / "wiki")))
                if isinstance(wps, list):
                    n2 = [normalize_wiki_page_path(str(x)) for x in wps]
                    if norm not in n2:
                        warnings.append(
                            f"manifest for {fn} does not list wiki page {norm} but page cites source"
                        )

    return warnings


def list_wiki_md_paths(root: Path) -> set[str]:
    out: set[str] = set()
    wiki = root / "wiki"
    if not wiki.exists():
        return out
    for p in wiki.rglob("*.md"):
        rel = p.relative_to(root / "wiki")
        out.add(str(rel).replace("\\", "/"))
    return out


def extract_wikilinks(body: str) -> list[str]:
    links: list[str] = []
    for m in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", body):
        links.append(m.group(1).strip())
    return links
