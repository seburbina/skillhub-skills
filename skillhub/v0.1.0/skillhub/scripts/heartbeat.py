#!/usr/bin/env python3
"""
heartbeat.py — periodic sync with Agent Skill Depot.

POSTs to /v1/agents/me/heartbeat, surfaces notifications + updates, and
processes any queued-on-failure actions in ~/.claude/skills/skillhub/.queue/.

Call at session start, then at most once per 30 minutes. The response tells us
the next allowed heartbeat time.

Usage:
    python3 heartbeat.py           # run once, print JSON summary
    python3 heartbeat.py --force   # ignore the local throttle and sync now
    python3 heartbeat.py --quiet   # machine-readable output only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from identity import load_identity, _validate_base_url, DEFAULT_BASE_URL
except ImportError:  # when run from a different cwd
    sys.path.insert(0, str(Path(__file__).parent))
    from identity import load_identity, _validate_base_url, DEFAULT_BASE_URL

SKILL_ROOT = Path.home() / ".claude" / "skills" / "skillhub"
INSTALLED_SKILLS_PATH = SKILL_ROOT / ".installed.json"
STATE_PATH = SKILL_ROOT / ".session_state.json"
QUEUE_DIR = SKILL_ROOT / ".queue"
LAST_HEARTBEAT_PATH = SKILL_ROOT / ".last_heartbeat.json"

VERSION = "0.0.1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def _post(base_url: str, path: str, api_key: str, body: dict) -> dict:
    _validate_base_url(base_url)
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": f"skillhub-base-skill/{VERSION}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _installed_skills() -> list[dict]:
    data = _read_json(INSTALLED_SKILLS_PATH, {})
    return [{"slug": k, **v} for k, v in data.items()]


def _record_heartbeat_result(result: dict) -> None:
    _write_json(LAST_HEARTBEAT_PATH, {
        "at": _now_iso(),
        "next_heartbeat_in_seconds": result.get("next_heartbeat_in_seconds", 1800),
        "updates_available_count": len(result.get("updates_available", [])),
        "notifications_count": len(result.get("notifications", [])),
    })


def _should_throttle(force: bool) -> tuple[bool, int]:
    """Return (should_throttle, seconds_remaining)."""
    if force:
        return False, 0
    last = _read_json(LAST_HEARTBEAT_PATH, None)
    if last is None:
        return False, 0
    try:
        last_at = datetime.fromisoformat(last["at"].replace("Z", "+00:00"))
    except (ValueError, KeyError):
        return False, 0
    min_interval = int(last.get("next_heartbeat_in_seconds", 1800))
    elapsed = (datetime.now(timezone.utc) - last_at).total_seconds()
    if elapsed < min_interval:
        return True, int(min_interval - elapsed)
    return False, 0


def _process_queue(base_url: str, api_key: str, log: list[str]) -> None:
    """Retry any queued actions from previous failures."""
    if not QUEUE_DIR.exists():
        return
    for entry in sorted(QUEUE_DIR.iterdir()):
        if entry.is_file() and entry.suffix == ".json":
            log.append(f"queued action pending: {entry.name} (retry handled by upload.py)")


def run_heartbeat(force: bool, quiet: bool) -> int:
    ident = load_identity()
    if ident is None:
        if not quiet:
            print("error: not registered. run `identity.py register` first.", file=sys.stderr)
        return 1

    throttled, seconds_left = _should_throttle(force)
    if throttled:
        if not quiet:
            print(f"throttled: next heartbeat in {seconds_left}s (use --force to override)")
        return 0

    base_url = ident.get("base_url", DEFAULT_BASE_URL)
    body = {
        "installed_skills": _installed_skills(),
        "client_meta": {
            "base_skill_version": VERSION,
            "os": sys.platform,
        },
    }

    try:
        result = _post(base_url, "/v1/agents/me/heartbeat", ident["api_key"], body)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        if not quiet:
            print(f"heartbeat HTTP {e.code}: {err_body}", file=sys.stderr)
        return 2
    except urllib.error.URLError as e:
        if not quiet:
            print(f"heartbeat network error: {e.reason}", file=sys.stderr)
        return 2
    except ValueError as e:
        if not quiet:
            print(f"heartbeat config error: {e}", file=sys.stderr)
        return 2

    _record_heartbeat_result(result)

    # Print for the agent's in-turn consumption
    log: list[str] = []
    updates = result.get("updates_available", [])
    notifs = result.get("notifications", [])
    if updates:
        log.append(f"{len(updates)} skill update(s) available")
        for u in updates[:5]:
            auto = " (auto-update eligible)" if u.get("auto_update_eligible") else ""
            log.append(
                f"  - {u['slug']}: {u.get('installed_version', '?')} -> "
                f"{u['latest_version']}{auto}"
            )
    if notifs:
        log.append(f"{len(notifs)} new notification(s)")
        for n in notifs[:5]:
            log.append(f"  - {n.get('type', 'unknown')}: {json.dumps(n, separators=(',', ':'))}")

    _process_queue(base_url, ident["api_key"], log)

    if quiet:
        print(json.dumps(result))
    else:
        print(f"heartbeat ok @ {result.get('now', _now_iso())}")
        for line in log:
            print(line)
        if not log:
            print("nothing new")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Skill Depot heartbeat.")
    parser.add_argument("--force", action="store_true", help="bypass local throttle")
    parser.add_argument("--quiet", action="store_true", help="machine-readable JSON only")
    args = parser.parse_args(argv)
    return run_heartbeat(force=args.force, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
