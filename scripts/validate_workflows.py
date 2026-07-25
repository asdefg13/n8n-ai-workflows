#!/usr/bin/env python3
"""Validate every workflow export in ``workflows/``.

A broken export is worse than no export: it wastes the reader's time and looks
careless. This runs in CI on every push and checks that each file is

* valid JSON with the keys n8n needs to import it;
* internally consistent — unique node names, connections that point at nodes
  that actually exist, no orphaned nodes;
* free of credentials, API keys, chat IDs and spreadsheet IDs.

Standard library only, so CI needs no install step.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / "workflows"

REQUIRED_TOP_LEVEL = ("name", "nodes", "connections")
REQUIRED_NODE_KEYS = ("id", "name", "type", "typeVersion", "position", "parameters")

TRIGGER_MARKERS = ("trigger", "webhook")
DOCUMENTATION_NODES = ("n8n-nodes-base.stickyNote",)

# Anything that must be filled in by the person importing the workflow.
ALLOWED_PLACEHOLDERS = {
    "REPLACE_WITH_CREDENTIAL_ID",
    "REPLACE_WITH_GOOGLE_SHEET_ID",
    "REPLACE_WITH_TELEGRAM_CHAT_ID",
}

# Patterns that must never appear in a committed export.
SECRET_PATTERNS = {
    "OpenAI API key": re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    "Telegram bot token": re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{30,}\b"),
    "JWT / Supabase key": re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"),
    "Google OAuth client id": re.compile(r"\b\d{12}-[a-z0-9]{32}\.apps\.googleusercontent\.com"),
    "Slack token": re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    "Bare Google Sheet id": re.compile(r"\b1[A-Za-z0-9_\-]{42,}\b"),
}


class Failure(Exception):
    """A validation error with a human-readable message."""


def _iter_strings(value):
    """Yield every string anywhere inside a nested JSON structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def check_structure(workflow: dict) -> None:
    for key in REQUIRED_TOP_LEVEL:
        if key not in workflow:
            raise Failure(f"missing top-level key {key!r}")

    if not isinstance(workflow["nodes"], list) or not workflow["nodes"]:
        raise Failure("'nodes' must be a non-empty list")
    if not isinstance(workflow["connections"], dict):
        raise Failure("'connections' must be an object")

    for index, node in enumerate(workflow["nodes"]):
        for key in REQUIRED_NODE_KEYS:
            if key not in node:
                raise Failure(f"node #{index} ({node.get('name', '?')}) is missing {key!r}")
        if not isinstance(node["position"], list) or len(node["position"]) != 2:
            raise Failure(f"node {node['name']!r} has an invalid position")


def check_uniqueness(workflow: dict) -> None:
    names, ids = [], []
    for node in workflow["nodes"]:
        names.append(node["name"])
        ids.append(node["id"])

    for label, values in (("name", names), ("id", ids)):
        duplicates = {value for value in values if values.count(value) > 1}
        if duplicates:
            raise Failure(f"duplicate node {label}(s): {', '.join(sorted(duplicates))}")


def check_connections(workflow: dict) -> set[str]:
    """Verify every connection endpoint exists; return the set of connected nodes."""
    known = {node["name"] for node in workflow["nodes"]}
    connected: set[str] = set()

    for source, outputs in workflow["connections"].items():
        if source not in known:
            raise Failure(f"connection source {source!r} is not a node in this workflow")
        connected.add(source)

        for branch in outputs.get("main", []):
            for link in branch or []:
                target = link.get("node")
                if target not in known:
                    raise Failure(f"{source!r} points at unknown node {target!r}")
                connected.add(target)

    return connected


def check_reachability(workflow: dict, connected: set[str]) -> None:
    triggers = [
        node["name"]
        for node in workflow["nodes"]
        if any(marker in node["type"].lower() for marker in TRIGGER_MARKERS)
    ]
    if not triggers:
        raise Failure("workflow has no trigger node")

    orphans = [
        node["name"]
        for node in workflow["nodes"]
        if node["type"] not in DOCUMENTATION_NODES
        and node["name"] not in connected
        and node["name"] not in triggers
    ]
    if orphans:
        raise Failure(f"node(s) not wired into the graph: {', '.join(orphans)}")


def check_no_secrets(workflow: dict) -> None:
    for text in _iter_strings(workflow):
        for label, pattern in SECRET_PATTERNS.items():
            match = pattern.search(text)
            if match:
                raise Failure(f"possible {label} committed: {match.group()[:12]}…")

    for node in workflow["nodes"]:
        for credential in (node.get("credentials") or {}).values():
            if credential.get("id") != "REPLACE_WITH_CREDENTIAL_ID":
                raise Failure(
                    f"node {node['name']!r} carries a real credential id "
                    f"({credential.get('id')!r})"
                )


def check_placeholders(workflow: dict) -> int:
    found = set()
    for text in _iter_strings(workflow):
        for placeholder in re.findall(r"REPLACE_WITH_[A-Z_]+", text):
            if placeholder not in ALLOWED_PLACEHOLDERS:
                raise Failure(f"undocumented placeholder {placeholder!r}")
            found.add(placeholder)
    return len(found)


def check_documented(workflow: dict) -> None:
    has_sticky = any(node["type"] in DOCUMENTATION_NODES for node in workflow["nodes"])
    if not has_sticky:
        raise Failure("workflow has no sticky note explaining what it does")


def validate(path: Path) -> str:
    try:
        workflow = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise Failure(f"invalid JSON: {exc}") from exc

    check_structure(workflow)
    check_uniqueness(workflow)
    connected = check_connections(workflow)
    check_reachability(workflow, connected)
    check_no_secrets(workflow)
    check_documented(workflow)
    placeholders = check_placeholders(workflow)

    nodes = len(workflow["nodes"])
    return f"{nodes:>2} nodes · {placeholders} placeholder kind(s) · {workflow['name']}"


def main() -> int:
    files = sorted(WORKFLOW_DIR.glob("*.json"))
    if not files:
        print(f"No workflow exports found in {WORKFLOW_DIR}", file=sys.stderr)
        return 1

    failures = 0
    for path in files:
        try:
            summary = validate(path)
        except Failure as exc:
            failures += 1
            print(f"FAIL  {path.name}: {exc}", file=sys.stderr)
        else:
            print(f"ok    {path.name}  —  {summary}")

    print(f"\n{len(files) - failures}/{len(files)} workflow(s) valid")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
