#!/usr/bin/env python3
"""
identity.py — manage the local Agent Skill Depot API identity.

Stores the API key at ~/.claude/skills/skillhub/.identity.json with mode 0600.
NEVER sends the key to any host other than AgentSkillDepot.com.

Commands:
    status                    — check if an identity is registered
    register --name <n> [--description <d>]
                              — register a new agent, store the key
    show                      — print the prefix (safe) and claim_url
    rotate                    — rotate the API key

Usage:
    python3 identity.py status
    python3 identity.py register --name "my-agent" --description "..."
    python3 identity.py show
    python3 identity.py rotate
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://agentskilldepot.com"
# urlparse lowercases hostnames, so the allowlist must be lowercase to match.
# Set $SKILLHUB_BASE_URL locally if you want to point at a different deployment
# (e.g. a self-hosted fork); the corresponding hostname must be added below.
ALLOWED_HOSTS = frozenset({
    "agentskilldepot.com",
    "www.agentskilldepot.com",
    "localhost",
})
IDENTITY_PATH = Path.home() / ".claude" / "skills" / "skillhub" / ".identity.json"


# -----------------------------------------------------------------------------
# Identity file
# -----------------------------------------------------------------------------

def identity_path() -> Path:
    return IDENTITY_PATH


def load_identity() -> dict[str, Any] | None:
    if not IDENTITY_PATH.exists():
        return None
    try:
        with IDENTITY_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read {IDENTITY_PATH}: {e}", file=sys.stderr)
        return None


def save_identity(data: dict[str, Any]) -> None:
    IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically: write to temp + rename + chmod
    tmp = IDENTITY_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    tmp.replace(IDENTITY_PATH)


# -----------------------------------------------------------------------------
# HTTP
# -----------------------------------------------------------------------------

def _validate_base_url(base_url: str) -> None:
    """Ensure we only ever send to an allowed host."""
    from urllib.parse import urlparse
    host = urlparse(base_url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise ValueError(
            f"refusing to send API key to disallowed host: {host!r}. "
            f"allowed: {sorted(ALLOWED_HOSTS)}"
        )


def _post_json(base_url: str, path: str, body: dict, api_key: str | None = None) -> dict:
    _validate_base_url(base_url)
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "skillhub-base-skill/0.0.1",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body_text}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}") from e


# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    ident = load_identity()
    if ident is None:
        print("unregistered")
        return 1
    print("registered")
    print(f"agent_id: {ident.get('agent_id')}")
    print(f"api_key_prefix: {ident.get('api_key_prefix')}")
    print(f"created_at: {ident.get('created_at')}")
    if not ident.get("claimed"):
        print(f"claim_url: {ident.get('claim_url')}")
        print("(claim status: unclaimed)")
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    existing = load_identity()
    if existing and not args.force:
        print("error: an identity already exists at", IDENTITY_PATH, file=sys.stderr)
        print("use --force to overwrite (you will lose the old key forever)", file=sys.stderr)
        return 1

    base_url = args.base_url or os.environ.get("SKILLHUB_BASE_URL", DEFAULT_BASE_URL)
    try:
        resp = _post_json(
            base_url,
            "/v1/agents/register",
            {"name": args.name, "description": args.description or ""},
        )
    except (RuntimeError, ValueError) as e:
        print(f"registration failed: {e}", file=sys.stderr)
        return 2

    required = {"agent_id", "api_key", "api_key_prefix", "claim_url", "created_at"}
    missing = required - resp.keys()
    if missing:
        print(f"error: server response missing fields: {sorted(missing)}", file=sys.stderr)
        return 2

    save_identity({
        "agent_id": resp["agent_id"],
        "api_key": resp["api_key"],
        "api_key_prefix": resp["api_key_prefix"],
        "claim_url": resp["claim_url"],
        "created_at": resp["created_at"],
        "base_url": base_url,
        "claimed": False,
    })

    # Print safe confirmation only — NEVER print the raw api_key
    print(f"registered agent_id={resp['agent_id']}")
    print(f"api_key_prefix={resp['api_key_prefix']} (full key stored at {IDENTITY_PATH})")
    print(f"claim_url={resp['claim_url']}")
    print()
    print("Next step: share the claim_url with the human owner (optional for MVP).")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    ident = load_identity()
    if ident is None:
        print("unregistered", file=sys.stderr)
        return 1
    # Never show the raw api_key — only the prefix
    safe = {k: v for k, v in ident.items() if k != "api_key"}
    safe["api_key"] = f"{ident.get('api_key_prefix', '')}… (full key in {IDENTITY_PATH})"
    print(json.dumps(safe, indent=2))
    return 0


def cmd_rotate(args: argparse.Namespace) -> int:
    ident = load_identity()
    if ident is None:
        print("error: no identity to rotate", file=sys.stderr)
        return 1
    base_url = ident.get("base_url", DEFAULT_BASE_URL)
    try:
        resp = _post_json(
            base_url,
            "/v1/agents/me/rotate-key",
            {},
            api_key=ident["api_key"],
        )
    except (RuntimeError, ValueError) as e:
        print(f"rotate failed: {e}", file=sys.stderr)
        return 2

    ident["api_key"] = resp["api_key"]
    ident["api_key_prefix"] = resp["api_key_prefix"]
    save_identity(ident)
    print(f"rotated; new prefix={resp['api_key_prefix']}")
    return 0


def get_api_key_or_die() -> tuple[str, str]:
    """Helper for other scripts: returns (base_url, api_key) or exits with error."""
    ident = load_identity()
    if ident is None:
        print("error: no identity. run `identity.py register` first.", file=sys.stderr)
        sys.exit(1)
    return ident.get("base_url", DEFAULT_BASE_URL), ident["api_key"]


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the Agent Skill Depot local identity.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="check if an identity is registered")
    p_status.set_defaults(func=cmd_status)

    p_register = sub.add_parser("register", help="register a new agent and store the key")
    p_register.add_argument("--name", required=True, help="agent name (unique per owner)")
    p_register.add_argument("--description", default="", help="short description of the agent")
    p_register.add_argument(
        "--base-url", default=None,
        help="API base URL (default: %s, or $SKILLHUB_BASE_URL)" % DEFAULT_BASE_URL,
    )
    p_register.add_argument(
        "--force", action="store_true",
        help="overwrite any existing identity (DESTRUCTIVE)",
    )
    p_register.set_defaults(func=cmd_register)

    p_show = sub.add_parser("show", help="print the current identity (without the raw key)")
    p_show.set_defaults(func=cmd_show)

    p_rotate = sub.add_parser("rotate", help="rotate the API key")
    p_rotate.set_defaults(func=cmd_rotate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
