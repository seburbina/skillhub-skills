#!/usr/bin/env python3
"""
package.py — thin wrapper around skill-creator's package_skill.py.

Delegates to `~/.claude/skills/skill-creator/scripts/package_skill.py` to
produce a `.skill` ZIP archive. skill-creator is a HARD prerequisite for
Agent Skill Depot publishing — if it isn't installed, this script refuses
to proceed and tells the user to install it.

Usage:
    python3 package.py <skill-dir> [output-file.skill]
    python3 package.py <skill-dir> --output-dir <dir>

If `output-file.skill` is provided and differs from the default
`<output-dir>/<skill-dir-name>.skill`, the file is renamed to match.

Exit codes:
    0 on success, 1 on config/user error, 2 on packager failure.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_CREATOR_DIR = Path.home() / ".claude" / "skills" / "skill-creator"
PACKAGER_SCRIPT = SKILL_CREATOR_DIR / "scripts" / "package_skill.py"


def _ensure_skill_creator() -> None:
    if not SKILL_CREATOR_DIR.is_dir():
        sys.stderr.write(
            f"error: skill-creator is not installed at {SKILL_CREATOR_DIR}.\n"
            "Agent Skill Depot requires skill-creator for the publish quality\n"
            "gate AND for packaging. skill-creator ships with Claude Code — it's\n"
            "usually already present. If it isn't, install it before publishing.\n"
        )
        sys.exit(1)
    if not PACKAGER_SCRIPT.exists():
        sys.stderr.write(
            f"error: skill-creator is installed but its packager is missing at\n"
            f"{PACKAGER_SCRIPT}. Your skill-creator install may be corrupted.\n"
        )
        sys.exit(1)


def _run_packager(skill_dir: Path, output_dir: Path) -> Path:
    """Run skill-creator's packager as a subprocess.

    skill-creator's packager does `from scripts.quick_validate import ...`,
    which requires the skill-creator root (not its scripts/ dir) to be on
    PYTHONPATH. Running via `python3 -m scripts.package_skill` from the
    skill-creator root makes both cwd and module resolution work together.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Prepend skill-creator root to PYTHONPATH so `scripts.quick_validate` resolves
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(SKILL_CREATOR_DIR) + (os.pathsep + existing_pp if existing_pp else "")
    )

    cmd = [
        sys.executable,
        "-m", "scripts.package_skill",
        str(skill_dir.resolve()),
        str(output_dir.resolve()),
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(SKILL_CREATOR_DIR),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write("error: skill-creator packager timed out after 60s\n")
        sys.exit(2)
    except OSError as e:
        sys.stderr.write(f"error: failed to invoke skill-creator packager: {e}\n")
        sys.exit(2)

    # Always surface packager output so the user sees validation messages
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)

    if result.returncode != 0:
        sys.stderr.write(f"error: skill-creator packager exited with {result.returncode}\n")
        sys.exit(2)

    produced = output_dir / f"{skill_dir.name}.skill"
    if not produced.exists():
        sys.stderr.write(f"error: packager reported success but {produced} does not exist\n")
        sys.exit(2)
    return produced


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Package a skill directory into a .skill archive via skill-creator.",
    )
    parser.add_argument("skill_dir", type=Path, help="Skill directory to package")
    parser.add_argument(
        "output_file", nargs="?", default=None, type=Path,
        help="Destination .skill file (default: ./<skill-name>.skill)",
    )
    parser.add_argument(
        "--output-dir", default=None, type=Path,
        help="Output directory (incompatible with output_file)",
    )
    args = parser.parse_args(argv)

    if args.output_file and args.output_dir:
        parser.error("provide either output_file OR --output-dir, not both")

    skill_dir = args.skill_dir.resolve()
    if not skill_dir.is_dir():
        sys.stderr.write(f"error: {skill_dir} is not a directory\n")
        return 1
    if not (skill_dir / "SKILL.md").exists():
        sys.stderr.write(f"error: {skill_dir}/SKILL.md not found\n")
        return 1

    _ensure_skill_creator()

    # Determine target path
    if args.output_file:
        target = args.output_file.resolve()
        output_dir = target.parent
    elif args.output_dir:
        output_dir = args.output_dir.resolve()
        target = output_dir / f"{skill_dir.name}.skill"
    else:
        output_dir = Path.cwd()
        target = output_dir / f"{skill_dir.name}.skill"

    produced = _run_packager(skill_dir, output_dir)

    # Rename if the caller requested a different filename
    if produced != target:
        if target.exists():
            target.unlink()
        shutil.move(str(produced), str(target))

    print(f"packaged: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
