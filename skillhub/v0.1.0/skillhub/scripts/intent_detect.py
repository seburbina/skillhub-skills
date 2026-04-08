#!/usr/bin/env python3
"""
intent_detect.py — zero-dependency task-intent detector for proactive skill discovery.

Scans a short piece of text (typically the user's most recent message) and decides
whether it looks like a task that a specialized Agent Skill Depot skill might
accelerate. Returns JSON so the agent can decide whether to ASK (never search
silently) whether the user wants to check the registry first.

Usage:
    echo "extract tables from this pdf" | python3 intent_detect.py
    python3 intent_detect.py "convert csv to xlsx"
    python3 intent_detect.py --json "redact all emails from these docs"

Output shape:
{
  "is_task": true,
  "verbs": ["extract"],
  "nouns": ["pdf", "tables"],
  "domains": [],
  "negative_signals": [],
  "confidence": 0.82,
  "topic_hash": "extract:pdf",
  "debounce": {
    "q_and_a": false,
    "mid_action": false,
    "kill_switch": false,
    "already_declined": false
  }
}

The agent reads this and decides whether to prompt:
"This sounds like something Agent Skill Depot might have a skill for — want me to check?"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# -----------------------------------------------------------------------------
# Lexicon — curated task verbs, format/domain nouns, negative signals
# -----------------------------------------------------------------------------

TASK_VERBS = {
    "extract", "parse", "convert", "transform", "analyze", "analyse",
    "summarize", "summarise", "generate", "refactor", "migrate", "scrape",
    "format", "validate", "debug", "review", "visualize", "visualise",
    "translate", "compare", "deduplicate", "dedupe", "redact", "transcribe",
    "classify", "cluster", "sort", "filter", "merge", "split", "join",
    "render", "build", "compile", "lint", "test", "benchmark", "profile",
    "optimize", "optimise", "minify", "obfuscate", "encrypt", "decrypt",
    "sign", "verify", "hash", "diff", "patch", "apply", "rollback",
    "deploy", "provision", "backup", "restore", "export", "import",
    "download", "upload", "fetch", "publish", "annotate", "highlight",
    "detect", "recognize", "recognise", "tag", "label", "caption",
    "crop", "resize", "compress", "decompress", "edit", "trim", "reformat",
    "scrub", "clean", "normalize", "normalise", "lemmatize", "tokenize",
    "embed", "index", "search",
}

FORMAT_NOUNS = {
    "pdf", "xlsx", "xls", "csv", "tsv", "docx", "doc", "pptx", "ppt",
    "txt", "json", "yaml", "yml", "toml", "xml", "html", "htm", "md",
    "markdown", "rst", "epub", "mobi", "png", "jpg", "jpeg", "svg",
    "gif", "webp", "tiff", "mp3", "mp4", "wav", "flac", "parquet",
    "avro", "orc", "arrow", "feather", "sqlite", "mbox", "eml", "ics",
    "vcf", "geojson", "shapefile", "kml", "gpx", "log", "zip", "tar",
    "gz", "7z", "rar", "iso",
}

DOMAIN_NOUNS = {
    "invoice", "receipt", "contract", "resume", "cv", "email", "emails",
    "commit", "commits", "diff", "pr", "issue", "ticket", "log", "logs",
    "metric", "metrics", "trace", "traces", "embedding", "embeddings",
    "dataset", "table", "column", "schema", "query", "sql", "dataframe",
    "stacktrace", "screenshot", "video", "audio", "transcript", "image",
    "images", "photo", "photos", "timeseries", "timeline", "notebook",
    "jupyter", "slide", "slides", "presentation",
}

# Stopwords / framing words we strip when normalizing for topic_hash
STOPWORDS = {
    "the", "a", "an", "this", "that", "these", "those", "my", "your",
    "our", "their", "from", "to", "into", "with", "for", "of", "in",
    "on", "at", "as", "by", "and", "or", "but", "can", "could", "would",
    "should", "please", "i", "me", "we", "you", "want", "need", "help",
    "all", "some", "any", "each", "every", "just",
}

# If any of these appear, it's a Q&A / explain request — don't prompt
NEGATIVE_QA_PATTERNS = [
    re.compile(r"\bexplain(?:\s+to\s+me)?\b", re.I),
    re.compile(r"\bwhat\s+(?:is|are|does|do)\b", re.I),
    re.compile(r"\bwhy\s+(?:is|are|does|do)\b", re.I),
    re.compile(r"\bhow\s+does\b", re.I),
    re.compile(r"\bhow\s+do\s+(?:i|you)\s+(?:know|tell)\b", re.I),
    re.compile(r"\btell\s+me\s+about\b", re.I),
    re.compile(r"\bhelp\s+me\s+understand\b", re.I),
    re.compile(r"\bwhat(?:'s|\s+is)\s+the\s+(?:difference|meaning)\b", re.I),
]

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


# -----------------------------------------------------------------------------
# Detection
# -----------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


def _detect(text: str) -> dict:
    tokens = _tokenize(text)
    token_set = set(tokens)

    verbs = sorted(token_set & TASK_VERBS)
    formats = sorted(token_set & FORMAT_NOUNS)
    domains = sorted(token_set & DOMAIN_NOUNS)

    qa_hits = []
    for p in NEGATIVE_QA_PATTERNS:
        if p.search(text):
            qa_hits.append(p.pattern)

    # Confidence heuristic: having any verb is the strongest signal;
    # a format or domain noun boosts further; Q&A cues nuke it.
    conf = 0.0
    if verbs:
        conf += 0.55
    if formats:
        conf += 0.25
    if domains:
        conf += 0.15
    # Multiple signals at once reinforce
    if len(verbs) >= 2:
        conf += 0.05
    if verbs and (formats or domains):
        conf += 0.05
    if qa_hits:
        conf = 0.0

    # Short "yes/no" / trivial messages should not trigger
    if len(tokens) < 3:
        conf = 0.0

    conf = round(min(conf, 1.0), 3)
    is_task = conf >= 0.5

    topic_hash = _topic_hash(verbs, formats + domains)

    return {
        "is_task": is_task,
        "verbs": verbs,
        "nouns": formats,
        "domains": domains,
        "negative_signals": qa_hits,
        "confidence": conf,
        "topic_hash": topic_hash,
    }


def _topic_hash(verbs: list[str], nouns: list[str]) -> str:
    """Stable short hash of (primary verb, primary noun) for session debounce."""
    if not verbs and not nouns:
        return ""
    primary_verb = verbs[0] if verbs else ""
    primary_noun = nouns[0] if nouns else ""
    key = f"{primary_verb}:{primary_noun}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return f"{key}#{digest}" if (primary_verb or primary_noun) else ""


# -----------------------------------------------------------------------------
# Debounce state (respects session_state + kill switch)
# -----------------------------------------------------------------------------

SKILL_ROOT = Path.home() / ".claude" / "skills" / "skillhub"
KILL_SWITCH = SKILL_ROOT / ".proactive_off"
SESSION_STATE = SKILL_ROOT / ".session_state.json"


def _session_state() -> dict:
    if not SESSION_STATE.exists():
        return {}
    try:
        with SESSION_STATE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _debounce_flags(topic_hash: str, mid_action: bool, qa_hits: list) -> dict:
    state = _session_state()
    declined = set(state.get("declined_topics", []))
    return {
        "q_and_a": bool(qa_hits),
        "mid_action": mid_action,
        "kill_switch": KILL_SWITCH.exists(),
        "already_declined": topic_hash in declined and bool(topic_hash),
    }


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect task intent for proactive skill discovery.")
    parser.add_argument(
        "text", nargs="?", default=None,
        help="Text to analyze (if omitted, reads from stdin).",
    )
    parser.add_argument("--mid-action", action="store_true",
                        help="Caller is mid-action; suppress prompts.")
    args = parser.parse_args(argv)

    text = args.text if args.text is not None else sys.stdin.read()
    text = text.strip()
    if not text:
        print(json.dumps({
            "is_task": False,
            "verbs": [], "nouns": [], "domains": [],
            "negative_signals": [], "confidence": 0.0,
            "topic_hash": "",
            "debounce": {"q_and_a": False, "mid_action": args.mid_action,
                         "kill_switch": KILL_SWITCH.exists(),
                         "already_declined": False},
        }))
        return 0

    result = _detect(text)
    result["debounce"] = _debounce_flags(
        result["topic_hash"], args.mid_action, result["negative_signals"]
    )

    # If any debounce flag is set, the agent should NOT prompt even if is_task is true
    if any(result["debounce"].values()):
        result["is_task"] = False

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
