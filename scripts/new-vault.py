#!/usr/bin/env python3
"""Create and register a new Corpus Manager vault."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import yaml


def prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{text}{suffix}: ").strip()
    return value or default


def normalize_slug(value: str) -> str:
    lowered = value.strip().lower().replace(" ", "-")
    slug = re.sub(r"[^a-z0-9._-]+", "", lowered)
    return slug.strip(".-_")


def ensure_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def main() -> None:
    default_registry = Path("/data/corpus-registry.yaml")
    registry_path = Path(prompt("Registry path", str(default_registry))).expanduser()
    vaults_root = Path(prompt("Vaults root directory", "/data/vaults")).expanduser()

    raw_slug = prompt("New vault id (slug)")
    slug = normalize_slug(raw_slug)
    if not slug:
        raise SystemExit("Error: vault id must contain at least one alphanumeric character.")
    label = prompt("Display label", slug)

    vault_root = vaults_root / slug
    claude_md_path = vault_root / "CLAUDE.md"
    manifest_path = vault_root / "manifest.json"

    vault_root.mkdir(parents=True, exist_ok=True)
    (vault_root / "raw").mkdir(parents=True, exist_ok=True)
    (vault_root / "wiki").mkdir(parents=True, exist_ok=True)

    today = datetime.utcnow().strftime("%Y-%m-%d")
    ensure_file(
        claude_md_path,
        (
            "# Vault Rules\n\n"
            "This vault follows the Corpus Manager wiki workflow.\n\n"
            "## Layers\n\n"
            "- `raw/` source material\n"
            "- `wiki/` compiled knowledge\n"
            "- `manifest.json` source tracking\n"
        ),
    )
    ensure_file(manifest_path, json.dumps({"sources": []}, indent=2) + "\n")
    ensure_file((vault_root / "wiki" / "index.md"), "# Wiki Index\n\n")
    ensure_file((vault_root / "wiki" / "log.md"), "# Wiki Log\n\n")

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
    if slug in corpora:
        raise SystemExit(f"Error: vault id '{slug}' already exists in {registry_path}")
    corpora[slug] = {
        "vault_root": str(vault_root),
        "claude_md": str(claude_md_path),
        "label": label,
    }

    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(yaml.safe_dump(registry_doc, sort_keys=True), encoding="utf-8")

    print("")
    print(f"Created vault '{slug}' at: {vault_root}")
    print(f"Updated registry: {registry_path}")
    print("")
    print("Next steps:")
    print("- Add this folder in Syncthing on each device.")
    print("- Restart corpus-manager-mcp so it reloads the registry.")
    print("- In project instructions, set a default vault id when useful.")


if __name__ == "__main__":
    main()
