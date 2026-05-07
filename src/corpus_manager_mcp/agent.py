"""Anthropic Messages API tool-use loop for ingest/deprecate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from anthropic import Anthropic

from corpus_manager_mcp.vault_ops import (
    append_operation_log,
    infer_layer_book,
    manifest_deprecate_source,
    manifest_upsert_source,
    vault_read,
    wiki_write,
)

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]

INGEST_TOOLS: list[dict[str, Any]] = [
    {
        "name": "vault_read",
        "description": "Read a file under the vault. Allowed: raw/, drafts/, manuscript/, wiki/, manifest.json, CLAUDE.md at vault root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Project-relative path from vault root (e.g. raw/notes/x.md, wiki/entities/a.md, manifest.json)",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "wiki_write",
        "description": "Create or replace a markdown file under wiki/ only. Pass path as wiki/... or pages relative to wiki/.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path under wiki (e.g. concepts/foo.md or wiki/concepts/foo.md)",
                },
                "content": {"type": "string", "description": "Full file contents including YAML frontmatter"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "manifest_upsert_source",
        "description": "Update manifest.json entry for a source file after ingesting. Sets ingested_at, wiki_pages, layer, book.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Source path relative to vault root (e.g. raw/x.md)",
                },
                "wiki_pages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Wiki page paths relative to wiki/ (e.g. concepts/foo.md), NOT prefixed with wiki/",
                },
            },
            "required": ["filename", "wiki_pages"],
        },
    },
    {
        "name": "append_operation_log",
        "description": "Append a structured entry to wiki/log.md (rotation applied automatically).",
        "input_schema": {
            "type": "object",
            "properties": {
                "operation": {"type": "string"},
                "subject": {"type": "string"},
                "bullets": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["operation", "subject", "bullets"],
        },
    },
]

DEPRECATE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "vault_read",
        "description": "Read a file under the vault.",
        "input_schema": INGEST_TOOLS[0]["input_schema"],
    },
    {
        "name": "wiki_write",
        "description": "Create or replace a markdown file under wiki/ only.",
        "input_schema": INGEST_TOOLS[1]["input_schema"],
    },
    {
        "name": "manifest_deprecate_source",
        "description": "Mark a manifest source as deprecated with reason and timestamps.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["filename", "reason"],
        },
    },
    {
        "name": "append_operation_log",
        "description": "Append a structured entry to wiki/log.md.",
        "input_schema": INGEST_TOOLS[3]["input_schema"],
    },
]


def _build_dispatch(root: Path, manifest_path: Path) -> dict[str, ToolHandler]:
    def vault_read_tool(inp: dict[str, Any]) -> dict[str, Any]:
        return vault_read(root, inp["path"])

    def wiki_write_tool(inp: dict[str, Any]) -> dict[str, Any]:
        return wiki_write(root, inp["path"], inp["content"])

    def manifest_upsert_tool(inp: dict[str, Any]) -> dict[str, Any]:
        fn = inp["filename"]
        layer, book = infer_layer_book(fn)
        return manifest_upsert_source(
            manifest_path,
            filename=fn,
            layer=layer,
            book=book,
            wiki_pages=list(inp.get("wiki_pages") or []),
        )

    def manifest_deprecate_tool(inp: dict[str, Any]) -> dict[str, Any]:
        return manifest_deprecate_source(manifest_path, inp["filename"], inp["reason"])

    def append_log_tool(inp: dict[str, Any]) -> dict[str, Any]:
        return append_operation_log(
            root,
            inp["operation"],
            inp["subject"],
            list(inp.get("bullets") or []),
        )

    return {
        "vault_read": vault_read_tool,
        "wiki_write": wiki_write_tool,
        "manifest_upsert_source": manifest_upsert_tool,
        "manifest_deprecate_source": manifest_deprecate_tool,
        "append_operation_log": append_log_tool,
    }


def run_tool_loop(
    client: Anthropic,
    model: str,
    system: str,
    user_message: str,
    root: Path,
    manifest_path: Path,
    tools: list[dict[str, Any]],
    *,
    max_turns: int = 28,
) -> dict[str, Any]:
    dispatch = _build_dispatch(root, manifest_path)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
    last_text = ""

    for _ in range(max_turns):
        resp = client.messages.create(
            model=model,
            max_tokens=16_384,
            system=system,
            tools=tools,
            messages=messages,
        )

        text_parts: list[str] = []
        tool_uses: list[Any] = []
        for block in resp.content:
            btype = getattr(block, "type", "")
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_uses.append(block)

        if text_parts:
            last_text = "\n".join(text_parts).strip()

        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "end_turn" and not tool_uses:
            return {"ok": True, "summary_text": last_text, "stop_reason": resp.stop_reason}

        if not tool_uses:
            return {"ok": True, "summary_text": last_text, "stop_reason": resp.stop_reason}

        results: list[dict[str, Any]] = []
        for tu in tool_uses:
            name = getattr(tu, "name", "")
            tid = getattr(tu, "id", "")
            inp = getattr(tu, "input", {}) or {}
            if not isinstance(inp, dict):
                inp = {}
            handler = dispatch.get(name)
            if handler is None:
                out: dict[str, Any] = {"ok": False, "error": f"unknown tool {name}"}
            else:
                try:
                    out = handler(inp)
                except Exception as exc:  # noqa: BLE001
                    out = {"ok": False, "error": str(exc)}
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": json.dumps(out, ensure_ascii=False),
                }
            )

        messages.append({"role": "user", "content": results})

    return {"ok": False, "error": "max_turns_exceeded", "summary_text": last_text}
