"""Deterministic vault checks for lint and verify."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from corpus_manager_mcp.vault_ops import (
    extract_wikilinks,
    list_wiki_md_paths,
    manifest_load,
    parse_frontmatter_sources,
    resolve_safe,
    split_frontmatter,
)


def _wiki_paths_index(root: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    wiki = root / "wiki"
    if not wiki.exists():
        return out
    for p in wiki.rglob("*.md"):
        rel = str(p.relative_to(wiki)).replace("\\", "/")
        out[rel.lower()] = p
    return out


def resolve_wikilink_target(root: Path, link: str, index: dict[str, Path]) -> Path | None:
    """Resolve [[page]] or [[path/to/page]] to a wiki file path."""
    link = link.strip()
    if not link:
        return None
    base = link.replace("\\", "/").strip("/")
    candidates = [
        base + ".md",
        base if base.endswith(".md") else base + ".md",
    ]
    for c in candidates:
        key = c.lower()
        if key in index:
            return index[key]
        # try without leading path segments mapping
        parts = c.split("/")
        if parts:
            tail = parts[-1]
            if not tail.endswith(".md"):
                tail += ".md"
            k2 = tail.lower()
            if k2 in index:
                return index[k2]
    return None


def broken_wikilinks_report(root: Path) -> list[str]:
    idx = _wiki_paths_index(root)
    issues: list[str] = []
    wiki = root / "wiki"
    if not wiki.exists():
        return issues
    for p in wiki.rglob("*.md"):
        text = p.read_text(encoding="utf-8")
        _, body = split_frontmatter(text)
        if body == text:
            body = text
        for link in extract_wikilinks(body):
            if link.startswith("http") or "://" in link:
                continue
            resolved = resolve_wikilink_target(root, link, idx)
            if resolved is None:
                rel = str(p.relative_to(root))
                issues.append(f"broken wikilink [[{link}]] in `{rel}`")
    return issues


def manifest_missing_files_report(root: Path) -> list[str]:
    doc = manifest_load(root / "manifest.json")
    issues: list[str] = []
    for entry in doc.get("sources", []):
        if not isinstance(entry, dict):
            continue
        fn = entry.get("filename")
        if not fn:
            continue
        try:
            path = resolve_safe(root, fn)
        except ValueError:
            issues.append(f"manifest filename invalid: {fn}")
            continue
        if not path.exists():
            issues.append(f"manifest source missing on disk: `{fn}`")
    return issues


def wiki_sources_broken_report(root: Path) -> list[str]:
    doc = manifest_load(root / "manifest.json")
    valid_sources = {e.get("filename") for e in doc.get("sources", []) if isinstance(e, dict)}
    issues: list[str] = []
    wiki = root / "wiki"
    if not wiki.exists():
        return issues
    for p in wiki.rglob("*.md"):
        _, srcs = parse_frontmatter_sources(p)
        for fn in srcs:
            if fn not in valid_sources:
                issues.append(f"`{p.relative_to(root)}` cites unknown manifest source `{fn}`")
                continue
            try:
                sp = resolve_safe(root, fn)
            except ValueError:
                issues.append(f"`{p.relative_to(root)}` cites invalid path `{fn}`")
                continue
            if not sp.exists():
                issues.append(f"`{p.relative_to(root)}` cites missing file `{fn}`")
    return issues


def deprecated_sources_still_linked(root: Path) -> list[str]:
    doc = manifest_load(root / "manifest.json")
    deprecated = {
        e.get("filename")
        for e in doc.get("sources", [])
        if isinstance(e, dict) and e.get("status") == "deprecated"
    }
    issues: list[str] = []
    wiki = root / "wiki"
    if not wiki.exists():
        return issues
    for p in wiki.rglob("*.md"):
        _, srcs = parse_frontmatter_sources(p)
        for fn in srcs:
            if fn in deprecated:
                issues.append(f"`{p.relative_to(root)}` still cites deprecated source `{fn}`")
    return issues


def index_coverage_hints(root: Path) -> list[str]:
    """Pages under wiki/ not obviously mentioned in index.md (heuristic)."""
    index_path = root / "wiki" / "index.md"
    if not index_path.exists():
        return ["wiki/index.md missing"]
    index_text = index_path.read_text(encoding="utf-8").lower()
    all_pages = list_wiki_md_paths(root)
    hints: list[str] = []
    for rel in sorted(all_pages):
        if rel in ("index.md", "log.md"):
            continue
        stem = Path(rel).stem.lower()
        check = stem.replace("-", " ")
        if stem not in index_text and check not in index_text and rel.lower() not in index_text:
            hints.append(f"page possibly orphan vs index: `wiki/{rel}`")
    return hints[:80]


def build_lint_payload(root: Path) -> dict[str, Any]:
    return {
        "broken_wikilinks": broken_wikilinks_report(root)[:200],
        "manifest_missing_files": manifest_missing_files_report(root)[:200],
        "wiki_source_issues": wiki_sources_broken_report(root)[:200],
        "deprecated_source_refs": deprecated_sources_still_linked(root)[:200],
        "index_coverage_hints": index_coverage_hints(root)[:120],
    }


def verify_bundle_for_page(root: Path, wiki_rel: str) -> dict[str, Any]:
    """wiki_rel like wiki/entities/foo.md"""
    path = resolve_safe(root, wiki_rel)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    meta, body = split_frontmatter(text)
    sources = []
    if isinstance(meta, dict) and isinstance(meta.get("sources"), list):
        sources = [str(s) for s in meta["sources"]]
    snippets: dict[str, str] = {}
    for fn in sources[:30]:
        try:
            sp = resolve_safe(root, fn)
        except ValueError:
            snippets[fn] = ""
            continue
        if sp.exists():
            raw_t = sp.read_text(encoding="utf-8")[:8000]
            snippets[fn] = raw_t
        else:
            snippets[fn] = ""
    return {
        "wiki_page": wiki_rel,
        "frontmatter": meta,
        "body_preview": body[:12000],
        "source_snippets": snippets,
    }
