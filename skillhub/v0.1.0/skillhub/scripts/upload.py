#!/usr/bin/env python3
"""
upload.py — POST a packaged .skill to /v1/publish.

This is the FIRST moment any skill content leaves the user's machine. By the
time this runs, the local pipeline has already:
  1. Run the skill-creator quality gate
  2. Run scripts/sanitize.py (regex scrub)
  3. Done the agent-driven in-turn LLM review
  4. Had the user type "publish" verbatim
  5. Packaged the sanitized copy into a .skill ZIP

If any of those steps were skipped, this script must not be called.

Usage:
    python3 upload.py <file.skill> <manifest.json> <scrub_report.json> <skill_creator_report.json>

On network failure the action is queued at ~/.claude/skills/skillhub/.queue/
and retried by heartbeat.py.
"""
from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from identity import load_identity, _validate_base_url, DEFAULT_BASE_URL
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from identity import load_identity, _validate_base_url, DEFAULT_BASE_URL

VERSION = "0.0.1"
QUEUE_DIR = Path.home() / ".claude" / "skills" / "skillhub" / ".queue"


# -----------------------------------------------------------------------------
# Minimal multipart encoder (no requests dependency)
# -----------------------------------------------------------------------------

def _encode_multipart(fields: dict[str, Any], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """
    fields: name -> str
    files:  name -> (filename, content_bytes, content_type)
    Returns (body_bytes, content_type_header)
    """
    boundary = "----skillhub" + uuid.uuid4().hex
    buf = io.BytesIO()
    crlf = b"\r\n"

    for name, value in fields.items():
        buf.write(f"--{boundary}".encode("utf-8") + crlf)
        buf.write(f'Content-Disposition: form-data; name="{name}"'.encode("utf-8") + crlf)
        buf.write(crlf)
        if isinstance(value, (dict, list)):
            buf.write(json.dumps(value).encode("utf-8"))
        else:
            buf.write(str(value).encode("utf-8"))
        buf.write(crlf)

    for name, (filename, content, content_type) in files.items():
        buf.write(f"--{boundary}".encode("utf-8") + crlf)
        buf.write(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'
            .encode("utf-8") + crlf
        )
        buf.write(f"Content-Type: {content_type}".encode("utf-8") + crlf)
        buf.write(crlf)
        buf.write(content)
        buf.write(crlf)

    buf.write(f"--{boundary}--".encode("utf-8") + crlf)
    body = buf.getvalue()
    return body, f"multipart/form-data; boundary={boundary}"


# -----------------------------------------------------------------------------
# Queue (for retry on network failure)
# -----------------------------------------------------------------------------

def _enqueue_failure(payload: dict) -> Path:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = QUEUE_DIR / f"{ts}-publish-{uuid.uuid4().hex[:8]}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


# -----------------------------------------------------------------------------
# Publish
# -----------------------------------------------------------------------------

def publish(
    skill_path: Path,
    manifest: dict,
    scrub_report: dict,
    skill_creator_report: dict,
    base_url: str,
    api_key: str,
    max_retries: int = 2,
) -> dict:
    _validate_base_url(base_url)
    url = base_url.rstrip("/") + "/v1/publish"

    skill_bytes = skill_path.read_bytes()
    slug = manifest.get("slug") or skill_path.stem
    filename = f"{slug}.skill"

    fields: dict[str, Any] = {
        "manifest": manifest,
        "scrub_report": scrub_report,
        "skill_creator_report": skill_creator_report,
    }
    files = {
        "skill": (filename, skill_bytes, "application/zip"),
    }

    body, content_type = _encode_multipart(fields, files)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "User-Agent": f"skillhub-base-skill/{VERSION}",
    }

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            # 4xx errors should not retry — they indicate the client needs to fix something
            if 400 <= e.code < 500:
                try:
                    err_json = json.loads(err_body)
                except json.JSONDecodeError:
                    err_json = {"error": {"code": "unknown", "message": err_body}}
                raise RuntimeError(f"server rejected publish ({e.code}): {json.dumps(err_json, indent=2)}") from e
            last_error = e
        except urllib.error.URLError as e:
            last_error = e

        if attempt < max_retries:
            backoff = 2 ** attempt
            print(f"retry {attempt + 1}/{max_retries} after {backoff}s...", file=sys.stderr)
            time.sleep(backoff)

    # All retries exhausted — queue and raise
    queued_at = _enqueue_failure({
        "kind": "publish",
        "skill_filename": filename,
        "skill_path": str(skill_path),
        "manifest": manifest,
        "scrub_report_summary": {
            "overall_severity": scrub_report.get("overall_severity"),
            "total_findings": len(scrub_report.get("findings", [])),
        },
        "error": str(last_error),
    })
    raise RuntimeError(
        f"publish failed after {max_retries + 1} attempts. "
        f"Queued for retry at {queued_at}. Run heartbeat.py to retry."
    )


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upload a packaged .skill to Agent Skill Depot.")
    parser.add_argument("skill_file", type=Path, help="Path to the .skill ZIP archive")
    parser.add_argument("manifest_file", type=Path, help="Path to manifest.json")
    parser.add_argument("scrub_report_file", type=Path, help="Path to scrub_report.json")
    parser.add_argument(
        "skill_creator_report_file", type=Path,
        help="Path to skill_creator_report.json from the quality gate",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate inputs without uploading")
    args = parser.parse_args(argv)

    # Validate inputs
    for p in (args.skill_file, args.manifest_file, args.scrub_report_file, args.skill_creator_report_file):
        if not p.exists():
            print(f"error: {p} does not exist", file=sys.stderr)
            return 1

    try:
        manifest = json.loads(args.manifest_file.read_text(encoding="utf-8"))
        scrub_report = json.loads(args.scrub_report_file.read_text(encoding="utf-8"))
        skill_creator_report = json.loads(args.skill_creator_report_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error reading inputs: {e}", file=sys.stderr)
        return 1

    # Safety gate: the scrub report must not be overall_severity=block
    if scrub_report.get("overall_severity") == "block":
        print(
            "error: refusing to upload — scrub_report.overall_severity == 'block'. "
            "Re-run sanitize.py and the in-turn LLM review first.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print(f"[dry-run] would POST {args.skill_file.name} to /v1/publish")
        print(f"           manifest slug = {manifest.get('slug', '?')}")
        print(f"           scrub severity = {scrub_report.get('overall_severity', '?')}")
        print(f"           skill_creator status = {skill_creator_report.get('status', '?')}")
        return 0

    ident = load_identity()
    if ident is None:
        print("error: not registered. run `identity.py register` first.", file=sys.stderr)
        return 1
    base_url = ident.get("base_url", DEFAULT_BASE_URL)

    try:
        result = publish(
            skill_path=args.skill_file,
            manifest=manifest,
            scrub_report=scrub_report,
            skill_creator_report=skill_creator_report,
            base_url=base_url,
            api_key=ident["api_key"],
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2))
    if "public_url" in result:
        print()
        print(f"Published: {result['public_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
