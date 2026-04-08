#!/usr/bin/env python3
"""
sanitize.py — local regex scrubber for the Agent Skill Depot publish pipeline.

Reads every text file in a skill directory, applies the rules defined in
references/scrubbing.md, writes a sanitized copy to <skill-dir>.sanitized/, and
produces scrub_report.regex.json.

This is stage 1 of the 3-stage scrub pipeline. Stage 2 is the agent's in-turn LLM
review; stage 3 is the user's "publish" confirmation. All three happen locally —
content never leaves the machine before the user approves.

Exit codes:
    0 — clean or warn-only
    1 — block finding(s), must not publish
    2 — error (I/O, invalid skill dir, etc.)

Usage:
    python3 sanitize.py <skill-dir> [--output-dir <path>] [--json-only]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# -----------------------------------------------------------------------------
# Rule definitions
# -----------------------------------------------------------------------------

TEXT_EXTENSIONS = {
    ".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml",
    ".yml", ".toml", ".sh", ".bash", ".zsh", ".rb", ".go", ".rs", ".sql",
    ".html", ".htm", ".css", ".scss", ".env.example", ".ini", ".conf",
    ".cfg", ".xml", ".csv", ".tsv",
}

MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MiB

# File-level exclusions (excluded from sanitized output entirely, reported as block)
EXCLUDED_FILENAMES = {
    ".env", ".envrc", ".netrc", "id_rsa", "id_rsa.pub", "id_ed25519",
    "id_ed25519.pub", "credentials", "credentials.json", "credentials.yaml",
    "credentials.yml",
}
EXCLUDED_PATTERNS = [
    re.compile(r"^\.env(\.|$)"),              # .env, .<INTERNAL_HOST_REDACTED>, .env.production
    re.compile(r"^secrets?(\.|$)", re.I),     # secrets, secret.json, etc.
    re.compile(r".*\.pem$", re.I),
    re.compile(r".*\.key$", re.I),
    re.compile(r".*\.pfx$", re.I),
    re.compile(r".*\.p12$", re.I),
]
EXCLUDED_DIRS = {".aws", ".ssh", "__pycache__", "node_modules", ".git"}


@dataclass
class Rule:
    name: str
    severity: str                            # block | warn | info
    pattern: re.Pattern
    replacement: str | Callable[[re.Match], str]
    description: str = ""


def _make_rules() -> list[Rule]:
    """Build the regex rule set. Order matters — apply in this order."""
    rules: list[Rule] = []

    # --- block severity: credentials ---
    rules += [
        Rule("aws_access_key", "block",
             re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
             "<AWS_ACCESS_KEY_REDACTED>",
             "AWS access key ID"),
        Rule("aws_secret", "block",
             re.compile(r"""(?ix)
                 aws.{0,20}?(?:secret|access).{0,20}?
                 ['"]([A-Za-z0-9/+=]{40})['"]
             """),
             lambda m: m.group(0).replace(m.group(1), "<AWS_SECRET_REDACTED>"),
             "AWS secret access key (heuristic)"),
        Rule("github_pat", "block",
             re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
             "<GITHUB_TOKEN_REDACTED>",
             "GitHub personal access token"),
        Rule("github_oauth", "block",
             re.compile(r"\bgho_[A-Za-z0-9]{36}\b"),
             "<GITHUB_TOKEN_REDACTED>",
             "GitHub OAuth token"),
        Rule("github_app", "block",
             re.compile(r"\bghs_[A-Za-z0-9]{36}\b"),
             "<GITHUB_TOKEN_REDACTED>",
             "GitHub app installation token"),
        Rule("github_refresh", "block",
             re.compile(r"\bghr_[A-Za-z0-9]{36}\b"),
             "<GITHUB_TOKEN_REDACTED>",
             "GitHub refresh token"),
        Rule("anthropic_key", "block",
             re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
             "<ANTHROPIC_KEY_REDACTED>",
             "Anthropic API key"),
        Rule("openai_key", "block",
             # Excludes sk-ant (handled above), requires non-hyphen after sk-
             re.compile(r"\bsk-(?!ant-)[A-Za-z0-9]{20,}\b"),
             "<OPENAI_KEY_REDACTED>",
             "OpenAI API key"),
        Rule("stripe_key", "block",
             re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{24,}\b"),
             "<STRIPE_KEY_REDACTED>",
             "Stripe API key"),
        Rule("google_api", "block",
             re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
             "<GOOGLE_API_KEY_REDACTED>",
             "Google API key"),
        Rule("slack_token", "block",
             re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
             "<SLACK_TOKEN_REDACTED>",
             "Slack token"),
        Rule("twilio_key", "block",
             re.compile(r"\bSK[0-9a-fA-F]{32}\b"),
             "<TWILIO_KEY_REDACTED>",
             "Twilio API key"),
        Rule("sendgrid_key", "block",
             re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b"),
             "<SENDGRID_KEY_REDACTED>",
             "SendGrid API key"),
        Rule("private_key_pem", "block",
             re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
             "<PRIVATE_KEY_REDACTED>",
             "PEM private key header"),
        Rule("jwt", "block",
             re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
             "<JWT_REDACTED>",
             "JSON Web Token"),
    ]

    # --- warn severity: personal data ---
    rules += [
        Rule("ssn_us", "warn",
             re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
             "<SSN_REDACTED>",
             "US Social Security Number"),
        Rule("email", "warn",
             re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
             "<EMAIL_REDACTED>",
             "email address"),
        Rule("phone_intl", "warn",
             re.compile(r"\+\d{1,3}[ .-]?\d{6,14}\b"),
             "<PHONE_REDACTED>",
             "international phone number"),
        Rule("phone_us", "warn",
             re.compile(r"\b(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b"),
             "<PHONE_REDACTED>",
             "US phone number"),
        Rule("ipv4_private", "warn",
             re.compile(
                 r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
                 r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
                 r"|192\.168\.\d{1,3}\.\d{1,3})\b"
             ),
             "<PRIVATE_IP_REDACTED>",
             "RFC1918 private IPv4"),
        Rule("ipv4_public", "warn",
             re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"),
             "<IP_REDACTED>",
             "public IPv4"),
        Rule("mac_address", "warn",
             re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"),
             "<MAC_REDACTED>",
             "MAC address"),
        Rule("absolute_user_path", "warn",
             re.compile(r"(/Users|/home)/[^/\s\"']+"),
             lambda m: "~",
             "absolute user home path (canonicalized to ~)"),
        Rule("internal_dns", "warn",
             re.compile(r"\b[a-z0-9][a-z0-9-]*\.(?:internal|corp|local|lan)\b"),
             "<INTERNAL_HOST_REDACTED>",
             "internal hostname"),
    ]

    # --- info severity: heuristics ---
    rules += [
        Rule("windows_user_path", "info",
             re.compile(r"C:\\Users\\[^\\]+"),
             "C:\\Users\\<user>",
             "Windows user path"),
    ]

    return rules


RULES = _make_rules()


# -----------------------------------------------------------------------------
# Credit-card detector (Luhn-validated, standalone because it needs validation)
# -----------------------------------------------------------------------------

_CC_RE = re.compile(r"\b(?:\d[ -]*?){13,16}\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0 and len(digits) >= 13


def _scrub_credit_cards(text: str) -> tuple[str, list[tuple[int, int, str]]]:
    """Find Luhn-validated credit card numbers. Returns (new_text, [(start, end, snippet)])."""
    findings: list[tuple[int, int, str]] = []
    out_parts: list[str] = []
    last_end = 0
    for m in _CC_RE.finditer(text):
        digits = re.sub(r"[ -]", "", m.group(0))
        if len(digits) < 13 or len(digits) > 16:
            continue
        if not _luhn_ok(digits):
            continue
        out_parts.append(text[last_end:m.start()])
        out_parts.append("<CARD_REDACTED>")
        findings.append((m.start(), m.end(), m.group(0)))
        last_end = m.end()
    out_parts.append(text[last_end:])
    return "".join(out_parts), findings


# -----------------------------------------------------------------------------
# Data classes for the report
# -----------------------------------------------------------------------------

@dataclass
class Finding:
    file: str
    line: int
    column: int
    rule: str
    severity: str
    snippet: str
    replacement: str


@dataclass
class ScanStats:
    files_scanned: int = 0
    files_skipped_binary: int = 0
    files_skipped_size: int = 0
    files_excluded: int = 0
    total_findings: int = 0


@dataclass
class Report:
    version: int
    skill_dir: str
    sanitized_dir: str
    generated_at: str
    scan_stats: ScanStats
    findings: list[Finding]
    excluded_files: list[str]
    overall_severity: str


# -----------------------------------------------------------------------------
# Scanning
# -----------------------------------------------------------------------------

def _is_text_file(path: Path) -> bool:
    """Heuristic: extension-based + binary sniff."""
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    # Fall back: sniff first 512 bytes for null bytes
    try:
        with path.open("rb") as f:
            chunk = f.read(512)
        return b"\x00" not in chunk
    except OSError:
        return False


def _should_exclude_file(rel_path: Path) -> bool:
    name = rel_path.name
    if name in EXCLUDED_FILENAMES:
        return True
    for pattern in EXCLUDED_PATTERNS:
        if pattern.match(name):
            return True
    # Any ancestor directory excluded?
    for part in rel_path.parts[:-1]:
        if part in EXCLUDED_DIRS:
            return True
    return False


def _line_column(text: str, offset: int) -> tuple[int, int]:
    """Convert a byte offset to (line, column), 1-indexed."""
    line = text.count("\n", 0, offset) + 1
    last_newline = text.rfind("\n", 0, offset)
    column = offset - last_newline if last_newline >= 0 else offset + 1
    return line, column


def _apply_rules(
    text: str,
    rel_path: str,
    findings: list[Finding],
) -> str:
    """Apply all rules to `text`, append findings, return sanitized text."""
    # 1) Luhn-validated credit cards (standalone)
    text, cc_matches = _scrub_credit_cards(text)
    for start, end, snippet in cc_matches:
        line, col = _line_column(text, start)
        findings.append(Finding(
            file=rel_path, line=line, column=col,
            rule="credit_card", severity="warn",
            snippet=_truncate(snippet),
            replacement="<CARD_REDACTED>",
        ))

    # 2) All other rules, in order
    for rule in RULES:
        def _sub(m: re.Match, rule=rule, rel_path=rel_path) -> str:
            # Compute the replacement (may be callable)
            if callable(rule.replacement):
                repl = rule.replacement(m)
            else:
                repl = rule.replacement
            line, col = _line_column(text, m.start())
            findings.append(Finding(
                file=rel_path, line=line, column=col,
                rule=rule.name, severity=rule.severity,
                snippet=_truncate(m.group(0)),
                replacement=repl,
            ))
            return repl
        text = rule.pattern.sub(_sub, text)

    return text


def _truncate(s: str, limit: int = 120) -> str:
    s = s.replace("\n", "\\n")
    if len(s) <= limit:
        return s
    return s[:limit] + "…"


def scan_skill(
    skill_dir: Path,
    sanitized_dir: Path,
) -> Report:
    """Walk `skill_dir`, apply rules, write sanitized copy, return Report."""
    if sanitized_dir.exists():
        shutil.rmtree(sanitized_dir)
    sanitized_dir.mkdir(parents=True, exist_ok=True)

    stats = ScanStats()
    findings: list[Finding] = []
    excluded_files: list[str] = []

    for root, dirs, files in os.walk(skill_dir):
        root_path = Path(root)
        # Prune excluded dirs
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]

        rel_root = root_path.relative_to(skill_dir)
        (sanitized_dir / rel_root).mkdir(parents=True, exist_ok=True)

        for fname in files:
            src = root_path / fname
            rel = (rel_root / fname).as_posix()

            if _should_exclude_file(Path(rel)):
                stats.files_excluded += 1
                excluded_files.append(rel)
                findings.append(Finding(
                    file=rel, line=0, column=0,
                    rule="file_excluded",
                    severity="block",
                    snippet=f"file excluded from package: {rel}",
                    replacement="(removed)",
                ))
                continue

            try:
                size = src.stat().st_size
            except OSError:
                stats.files_skipped_binary += 1
                continue

            if size > MAX_FILE_SIZE:
                stats.files_skipped_size += 1
                # Still copy verbatim (do not scan, but include in package)
                shutil.copy2(src, sanitized_dir / rel)
                continue

            if not _is_text_file(src):
                stats.files_skipped_binary += 1
                shutil.copy2(src, sanitized_dir / rel)
                continue

            try:
                text = src.read_text(encoding="utf-8", errors="replace")
            except OSError:
                stats.files_skipped_binary += 1
                continue

            stats.files_scanned += 1
            sanitized_text = _apply_rules(text, rel, findings)
            (sanitized_dir / rel).write_text(sanitized_text, encoding="utf-8")

    stats.total_findings = len(findings)
    overall = _overall_severity(findings)

    report = Report(
        version=1,
        skill_dir=str(skill_dir.resolve()),
        sanitized_dir=str(sanitized_dir.resolve()),
        generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        scan_stats=stats,
        findings=findings,
        excluded_files=excluded_files,
        overall_severity=overall,
    )
    return report


def _overall_severity(findings: list[Finding]) -> str:
    if any(f.severity == "block" for f in findings):
        return "block"
    if any(f.severity == "warn" for f in findings):
        return "warn"
    return "clean"


def report_to_json(report: Report) -> dict:
    return {
        "version": report.version,
        "skill_dir": report.skill_dir,
        "sanitized_dir": report.sanitized_dir,
        "generated_at": report.generated_at,
        "scan_stats": asdict(report.scan_stats),
        "findings": [asdict(f) for f in report.findings],
        "excluded_files": report.excluded_files,
        "overall_severity": report.overall_severity,
    }


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Agent Skill Depot — local regex scrubber.",
    )
    parser.add_argument("skill_dir", type=Path, help="Path to the skill directory to scan.")
    parser.add_argument(
        "--output-dir", type=Path, default=None,
        help="Where to write the sanitized copy (default: <skill-dir>.sanitized/).",
    )
    parser.add_argument(
        "--json-only", action="store_true",
        help="Only print the JSON report to stdout; do not print a summary.",
    )
    args = parser.parse_args(argv)

    skill_dir: Path = args.skill_dir.resolve()
    if not skill_dir.is_dir():
        print(f"error: {skill_dir} is not a directory", file=sys.stderr)
        return 2

    sanitized_dir: Path = (
        args.output_dir.resolve() if args.output_dir
        else skill_dir.parent / f"{skill_dir.name}.sanitized"
    )

    try:
        report = scan_skill(skill_dir, sanitized_dir)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # Write report
    report_path = sanitized_dir / "scrub_report.regex.json"
    report_path.write_text(
        json.dumps(report_to_json(report), indent=2),
        encoding="utf-8",
    )

    if args.json_only:
        print(json.dumps(report_to_json(report), indent=2))
    else:
        _print_summary(report, report_path)

    if report.overall_severity == "block":
        return 1
    return 0


def _print_summary(report: Report, report_path: Path) -> None:
    print(f"Scanned {report.scan_stats.files_scanned} files")
    print(f"Sanitized copy: {report.sanitized_dir}")
    print(f"Report: {report_path}")
    print(f"Overall severity: {report.overall_severity.upper()}")
    print()
    by_severity: dict[str, list[Finding]] = {"block": [], "warn": [], "info": []}
    for f in report.findings:
        by_severity.setdefault(f.severity, []).append(f)
    for sev in ("block", "warn", "info"):
        items = by_severity.get(sev, [])
        if not items:
            continue
        print(f"{sev.upper()} ({len(items)}):")
        for f in items[:10]:
            print(f"  {f.file}:{f.line}  [{f.rule}]  {f.snippet}")
        if len(items) > 10:
            print(f"  ... and {len(items) - 10} more")
        print()
    if report.excluded_files:
        print(f"Excluded files (NOT packaged): {', '.join(report.excluded_files)}")
        print()


if __name__ == "__main__":
    sys.exit(main())
