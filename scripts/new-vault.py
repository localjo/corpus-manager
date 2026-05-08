#!/usr/bin/env python3
"""Create and register a new Corpus Manager vault."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import yaml


def normalize_slug(value: str) -> str:
    lowered = value.strip().lower().replace(" ", "-")
    slug = re.sub(r"[^a-z0-9._-]+", "", lowered)
    return slug.strip(".-_")


def ensure_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def detect_defaults() -> tuple[Path, Path]:
    running_in_container = Path("/.dockerenv").exists()
    if running_in_container:
        return Path("/data/corpus-registry.yaml"), Path("/data/vaults")
    return Path("/opt/corpus-manager/corpus-registry.yaml"), Path("/srv/vaults")


def title_from_slug(slug: str) -> str:
    words = [w for w in re.split(r"[-_.]+", slug) if w]
    return " ".join(word.capitalize() for word in words) or slug


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create and register a new Corpus Manager vault.")
    parser.add_argument("vault_name", nargs="?", help="Vault name or id, e.g. 'Festivals and Retreats'")
    parser.add_argument("--vault-id", dest="vault_id", help="Explicit vault id/slug, e.g. festivals-and-retreats")
    parser.add_argument("--label", help="Display label, defaults to title-cased vault id")
    parser.add_argument("--registry-path", help="Override registry YAML path")
    parser.add_argument("--vaults-root", help="Override root directory for new vault folders")
    parser.add_argument("--claude-template", help="Path to CLAUDE.md template file")
    parser.add_argument(
        "--overwrite-claude-md",
        action="store_true",
        help="Overwrite CLAUDE.md for an existing vault entry using template",
    )
    return parser.parse_args()


def default_template_path() -> Path:
    return Path(__file__).resolve().parent.parent / "templates" / "vault-CLAUDE.md"


def render_claude_template(template_path: Path, vault_label: str) -> str:
    text = template_path.read_text(encoding="utf-8")
    rendered = text.replace("{{VAULT_LABEL}}", vault_label)
    if not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def main() -> None:
    args = parse_args()
    default_registry, default_vaults_root = detect_defaults()
    registry_path = Path(args.registry_path).expanduser() if args.registry_path else default_registry
    vaults_root = Path(args.vaults_root).expanduser() if args.vaults_root else default_vaults_root

    raw_input_name = (args.vault_id or args.vault_name or "").strip()
    if not raw_input_name:
        raw_input_name = input("Vault name (e.g. Festivals and Retreats): ").strip()
    slug = normalize_slug(raw_input_name)
    if not slug:
        raise SystemExit("Error: vault id must contain at least one alphanumeric character.")
    label = (args.label or "").strip() or title_from_slug(slug)

    template_path = Path(args.claude_template).expanduser() if args.claude_template else default_template_path()
    if not template_path.exists():
        raise SystemExit(f"Error: CLAUDE.md template not found at {template_path}")

    if registry_path.exists():
        registry_doc = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    else:
        registry_doc = {}
    if not isinstance(registry_doc, dict):
        registry_doc = {}
    corpora = registry_doc.get("corpora")
    if not isinstance(corpora, dict):
        corpora = {}
        registry_doc["corpora"] = corpora
    existing = corpora.get(slug) if isinstance(corpora.get(slug), dict) else None
    if existing and not args.overwrite_claude_md:
        raise SystemExit(
            f"Error: vault id '{slug}' already exists in {registry_path}. "
            "Re-run with --overwrite-claude-md to refresh CLAUDE.md from template."
        )

    if existing:
        vault_root = Path(str(existing.get("vault_root", vaults_root / slug))).expanduser()
        claude_md_path = Path(str(existing.get("claude_md", vault_root / "CLAUDE.md"))).expanduser()
        label = str(existing.get("label", label)).strip() or label
    else:
        vault_root = vaults_root / slug
        claude_md_path = vault_root / "CLAUDE.md"
        corpora[slug] = {
            "vault_root": str(vault_root),
            "claude_md": str(claude_md_path),
            "label": label,
        }

    manifest_path = vault_root / "manifest.json"
    vault_root.mkdir(parents=True, exist_ok=True)
    (vault_root / "raw").mkdir(parents=True, exist_ok=True)
    (vault_root / "wiki").mkdir(parents=True, exist_ok=True)

    template_text = render_claude_template(template_path, label)
    if args.overwrite_claude_md:
        claude_md_path.write_text(template_text, encoding="utf-8")
    else:
        ensure_file(claude_md_path, template_text)
    ensure_file(manifest_path, json.dumps({"sources": []}, indent=2) + "\n")
    ensure_file((vault_root / "wiki" / "index.md"), "# Wiki Index\n\n")
    ensure_file((vault_root / "wiki" / "log.md"), "# Wiki Log\n\n")

    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(yaml.safe_dump(registry_doc, sort_keys=True), encoding="utf-8")

    print("")
    action = "Updated" if existing else "Created"
    print(f"{action} vault '{slug}' ({label}) at: {vault_root}")
    print(f"Updated registry: {registry_path}")
    print("")
    print("Next steps:")
    print("- Add this folder in Syncthing on each device.")
    print("- Restart corpus-manager-mcp so it reloads the registry.")
    print("- In project instructions, set a default vault id when useful.")


if __name__ == "__main__":
    main()
