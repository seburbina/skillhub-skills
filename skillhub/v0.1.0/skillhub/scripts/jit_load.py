#!/usr/bin/env python3
"""
jit_load.py — just-in-time skill loader.

Downloads a .skill by slug + semver, unzips into
~/.claude/skills/skillhub-installed/<slug>/ (so Claude picks it up at next
session start), and prints the SKILL.md + referenced files so the agent can
"load" the skill inline into the CURRENT session without waiting for a restart.

This is the bridge between "searched and picked a skill" and "use it right now".

Usage:
    python3 jit_load.py <slug> [--version <semver>] [--no-download] [--no-print]

Exit codes:
    0 on success, 1 on user/config error, 2 on network/IO error.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

try:
    from identity import load_identity, _validate_base_url, DEFAULT_BASE_URL
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from identity import load_identity, _validate_base_url, DEFAULT_BASE_URL

INSTALLED_ROOT = Path.home() / ".claude" / "skills" / "skillhub-installed"
INSTALLED_INDEX = Path.home() / ".claude" / "skills" / "skillhub" / ".installed.json"
VERSION = "0.0.1"

# Limit on files we inline into the conversation to avoid blowing the context
MAX_INLINE_FILES = 8
MAX_INLINE_BYTES = 120 * 1024  # 120 KiB total across all inlined files
PRINT_SEPARATOR = "\n" + ("─" * 72) + "\n"


def _get(base_url: str, path: str, api_key: str, follow_redirects: bool = True) -> bytes:
    _validate_base_url(base_url)
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": f"skillhub-base-skill/{VERSION}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _resolve_and_download(base_url: str, api_key: str, slug: str, version: str | None) -> tuple[Path, str, str]:
    """Resolve the skill version and download the .skill ZIP.

    Returns (zip_path, skill_id, resolved_semver).
    """
    # Resolve latest version if not specified
    if version is None:
        meta = _get(base_url, f"/v1/skills/{slug}", api_key)
        try:
            parsed = json.loads(meta.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"bad metadata response for {slug}: {e}") from e
        version = parsed.get("latest_version") or parsed.get("current_version")
        skill_id = parsed.get("skill_id")
        if not version or not skill_id:
            raise RuntimeError(f"could not resolve latest version for {slug}")
    else:
        skill_id = None  # will be derived from download response headers if needed

    # Download the .skill
    path = f"/v1/skills/by-slug/{slug}/versions/{version}/download"
    content = _get(base_url, path, api_key)
    tmp = tempfile.NamedTemporaryFile(suffix=".skill", delete=False)
    tmp.write(content)
    tmp.close()
    return Path(tmp.name), (skill_id or slug), version


def _unzip_skill(zip_path: Path, target_dir: Path) -> None:
    if target_dir.exists():
        # Rollback target for safety
        rollback = target_dir.with_name(target_dir.name + ".previous")
        if rollback.exists():
            shutil.rmtree(rollback)
        target_dir.rename(rollback)

    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        # Validate no path traversal
        for name in zf.namelist():
            if name.startswith("/") or ".." in Path(name).parts:
                raise RuntimeError(f"refusing to extract suspicious path: {name}")
        zf.extractall(target_dir)


def _update_installed_index(slug: str, version: str, skill_id: str) -> None:
    INSTALLED_INDEX.parent.mkdir(parents=True, exist_ok=True)
    if INSTALLED_INDEX.exists():
        try:
            with INSTALLED_INDEX.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}
    else:
        data = {}
    data[slug] = {
        "version": version,
        "skill_id": skill_id,
        "auto_update_consent": data.get(slug, {}).get("auto_update_consent", False),
    }
    tmp = INSTALLED_INDEX.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(INSTALLED_INDEX)


def _inline_print(skill_dir: Path) -> None:
    """Print SKILL.md + small referenced files so the agent can act on them immediately."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        print(f"warning: {skill_dir} has no SKILL.md", file=sys.stderr)
        return

    print("=" * 72)
    print(f"INLINED SKILL: {skill_dir.name}")
    print("The following content was just installed. Treat it as if it were a")
    print("loaded skill in the current session. Follow its instructions when")
    print("the user's request matches its triggers.")
    print("=" * 72)
    print()
    print(f"--- {skill_md.relative_to(skill_dir)} ---")
    print(skill_md.read_text(encoding="utf-8", errors="replace"))

    total_bytes = skill_md.stat().st_size
    files_printed = 1

    # Also inline any referenced references/*.md files up to the budget
    references_dir = skill_dir / "references"
    if references_dir.is_dir():
        for ref in sorted(references_dir.rglob("*.md")):
            if files_printed >= MAX_INLINE_FILES:
                break
            size = ref.stat().st_size
            if total_bytes + size > MAX_INLINE_BYTES:
                print(f"\n(skipped {ref.relative_to(skill_dir)}: would exceed inline budget)")
                continue
            print(PRINT_SEPARATOR)
            print(f"--- {ref.relative_to(skill_dir)} ---")
            print(ref.read_text(encoding="utf-8", errors="replace"))
            total_bytes += size
            files_printed += 1

    print()
    print("=" * 72)
    print(f"END INLINED SKILL ({files_printed} files, {total_bytes} bytes)")
    print(f"Persisted at: {skill_dir}")
    print(f"Next session will auto-discover this skill.")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Just-in-time skill loader.")
    parser.add_argument("slug", help="Skill slug to install.")
    parser.add_argument("--version", default=None, help="Specific semver (default: latest)")
    parser.add_argument(
        "--no-download", action="store_true",
        help="Skip download (assumes skill already at install location).",
    )
    parser.add_argument(
        "--no-print", action="store_true",
        help="Do not inline the SKILL.md into stdout (useful for scripted installs).",
    )
    args = parser.parse_args(argv)

    target_dir = INSTALLED_ROOT / args.slug

    if not args.no_download:
        ident = load_identity()
        if ident is None:
            print("error: not registered. run `identity.py register` first.", file=sys.stderr)
            return 1
        base_url = ident.get("base_url", DEFAULT_BASE_URL)

        try:
            zip_path, skill_id, resolved_version = _resolve_and_download(
                base_url, ident["api_key"], args.slug, args.version
            )
        except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError) as e:
            print(f"download failed: {e}", file=sys.stderr)
            return 2

        try:
            _unzip_skill(zip_path, target_dir)
        except (RuntimeError, zipfile.BadZipFile, OSError) as e:
            print(f"unzip failed: {e}", file=sys.stderr)
            return 2
        finally:
            try:
                zip_path.unlink()
            except OSError:
                pass

        _update_installed_index(args.slug, resolved_version, skill_id)
    else:
        if not target_dir.exists():
            print(f"error: {target_dir} does not exist", file=sys.stderr)
            return 1

    if not args.no_print:
        _inline_print(target_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
