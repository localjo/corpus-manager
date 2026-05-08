"""Anthropic Messages API tool-use loop for ingest/deprecate."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)

from corpus_manager_mcp.vault_ops import (
    append_operation_log,
    infer_layer_book,
    manifest_deprecate_source,
    manifest_get_source,
    manifest_upsert_source,
    vault_read,
    wiki_write,
)

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


def _content_char_count(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_content_char_count(item) for item in value)
    if isinstance(value, dict):
        return sum(_content_char_count(item) for item in value.values())
    return len(str(value)) if value is not None else 0


def _request_summary(
    *,
    model: str,
    max_tokens: int,
    system: str,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    turn_index: int,
) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": max_tokens,
        "turn_index": turn_index,
        "message_count": len(messages),
        "system_chars": len(system),
        "message_content_chars": sum(_content_char_count(message.get("content")) for message in messages),
        "tool_names": [str(tool.get("name")) for tool in tools],
    }


def _error_detail(exc: Exception, request_summary: dict[str, Any]) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    request_id = getattr(exc, "request_id", None)
    if request_id is None and headers is not None:
        request_id = headers.get("request-id") or headers.get("x-request-id")

    detail: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc),
        "request": request_summary,
    }
    if isinstance(exc, APIStatusError):
        detail["status_code"] = exc.status_code
    if request_id:
        detail["request_id"] = request_id
    return detail

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
        "name": "manifest_get_source",
        "description": "Get one source entry from manifest.json by filename (compact fields only; avoids reading full manifest).",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Source path relative to vault root (e.g. raw/x.md)",
                },
            },
            "required": ["filename"],
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
        "name": "manifest_get_source",
        "description": "Get one source entry from manifest.json by filename.",
        "input_schema": INGEST_TOOLS[2]["input_schema"],
    },
    {
        "name": "append_operation_log",
        "description": "Append a structured entry to wiki/log.md.",
        "input_schema": INGEST_TOOLS[4]["input_schema"],
    },
]


def _build_dispatch(root: Path, manifest_path: Path) -> dict[str, ToolHandler]:
    def vault_read_tool(inp: dict[str, Any]) -> dict[str, Any]:
        path = str(inp["path"])
        if path.replace("\\", "/").lstrip("/") == "manifest.json":
            return {"ok": False, "error": "Reading full manifest.json is disabled in tool loops. Use manifest_get_source."}
        return vault_read(root, path, max_bytes=40_000)

    def wiki_write_tool(inp: dict[str, Any]) -> dict[str, Any]:
        return wiki_write(root, inp["path"], inp["content"])

    def manifest_get_source_tool(inp: dict[str, Any]) -> dict[str, Any]:
        return manifest_get_source(manifest_path, inp["filename"])

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
        "manifest_get_source": manifest_get_source_tool,
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
    max_tokens: int = 16_384,
    retry_attempts: int = 3,
    retry_wait_seconds: int = 12,
) -> dict[str, Any]:
    dispatch = _build_dispatch(root, manifest_path)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
    last_text = ""

    retryable_exc = (RateLimitError, InternalServerError, APIConnectionError, APITimeoutError)
    for turn_index in range(max_turns):
        last_exc: Exception | None = None
        resp = None
        for attempt in range(retry_attempts + 1):
            request_summary = _request_summary(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=tools,
                messages=messages,
                turn_index=turn_index,
            )
            try:
                resp = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    tools=tools,
                    messages=messages,
                )
                break
            except retryable_exc as exc:
                last_exc = exc
                if attempt >= retry_attempts:
                    return {
                        "ok": False,
                        "error": str(exc),
                        "error_detail": _error_detail(exc, request_summary),
                        "summary_text": last_text,
                    }
                time.sleep(retry_wait_seconds * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "error": str(exc),
                    "error_detail": _error_detail(exc, request_summary),
                    "summary_text": last_text,
                }
        if resp is None and last_exc is not None:
            return {
                "ok": False,
                "error": str(last_exc),
                "error_detail": _error_detail(last_exc, request_summary),
                "summary_text": last_text,
            }

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
