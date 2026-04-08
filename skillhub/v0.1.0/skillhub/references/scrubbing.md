# Scrubbing contract

This file is THE contract for stripping personal / confidential / credential data from skills
before they are published to Agent Skill Depot. It governs both the automated regex pass
(`scripts/sanitize.py`) and the agent-driven LLM review (which you, the running Claude session,
perform in-turn during publish step 4).

**This file is load-bearing.** The PII/secret stripping pipeline fails closed if anything in
this contract is ambiguous or out of date.

---

## Stage 1 — Regex pass (automated, executed by `scripts/sanitize.py`)

Apply rules in order. Each rule has a severity:

- `block` — refuses to continue without explicit user override
- `warn`  — flagged, user confirms or edits
- `info`  — silently replaced or queued for LLM review

File types scanned: `.md`, `.txt`, `.py`, `.js`, `.ts`, `.tsx`, `.jsx`, `.json`, `.yaml`,
`.yml`, `.toml`, `.sh`, `.rb`, `.go`, `.rs`, `.sql`, `.html`, `.css`. Binary files and files
>1 MiB are skipped (but their presence is logged).

### block severity — credentials and private keys

| Rule | Pattern | Replacement |
|---|---|---|
| aws_access_key       | `\bAKIA[0-9A-Z]{16}\b` | `<AWS_ACCESS_KEY_REDACTED>` |
| aws_secret           | `(?i)aws.{0,20}?(secret|access).{0,20}?['"]([A-Za-z0-9/+=]{40})['"]` | `<AWS_SECRET_REDACTED>` |
| github_pat           | `\bghp_[A-Za-z0-9]{36}\b` | `<GITHUB_TOKEN_REDACTED>` |
| github_oauth         | `\bgho_[A-Za-z0-9]{36}\b` | `<GITHUB_TOKEN_REDACTED>` |
| github_app           | `\bghs_[A-Za-z0-9]{36}\b` | `<GITHUB_TOKEN_REDACTED>` |
| github_refresh       | `\bghr_[A-Za-z0-9]{36}\b` | `<GITHUB_TOKEN_REDACTED>` |
| anthropic_key        | `\bsk-ant-[A-Za-z0-9_-]{20,}\b` | `<ANTHROPIC_KEY_REDACTED>` |
| openai_key           | `\bsk-[A-Za-z0-9]{20,}\b` | `<OPENAI_KEY_REDACTED>` |
| stripe_key           | `\b(sk|rk)_(live|test)_[A-Za-z0-9]{24,}\b` | `<STRIPE_KEY_REDACTED>` |
| google_api           | `\bAIza[0-9A-Za-z_-]{35}\b` | `<GOOGLE_API_KEY_REDACTED>` |
| slack_token          | `\bxox[baprs]-[A-Za-z0-9-]{10,}\b` | `<SLACK_TOKEN_REDACTED>` |
| twilio_key           | `\bSK[0-9a-fA-F]{32}\b` | `<TWILIO_KEY_REDACTED>` |
| sendgrid_key         | `\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b` | `<SENDGRID_KEY_REDACTED>` |
| private_key_pem      | `-----BEGIN [A-Z ]*PRIVATE KEY-----` | `<PRIVATE_KEY_REDACTED>` |
| jwt                  | `\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b` | `<JWT_REDACTED>` |

### block severity — file-level exclusions

The following files are excluded from the package entirely and reported as `block` findings:

- `.env`, `.env.*`
- `.envrc`
- `id_rsa`, `id_rsa.pub`, `id_ed25519`, `id_ed25519.pub`
- `*.pem`, `*.key`, `*.pfx`, `*.p12`
- `credentials`, `credentials.json`, `credentials.yaml`
- `secrets`, `secrets.*`
- `.aws/`, `.ssh/` (any files under these directories)
- `.netrc`

### warn severity — personal data

| Rule | Pattern | Replacement |
|---|---|---|
| credit_card          | Luhn-validated `\b(?:\d[ -]*?){13,16}\b` | `<CARD_REDACTED>` |
| email                | `\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b` (case-insensitive) | `<EMAIL_REDACTED>` |
| phone_us             | `\b(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b` | `<PHONE_REDACTED>` |
| phone_international  | `\+\d{1,3}[ .-]?\d{6,14}\b` | `<PHONE_REDACTED>` |
| ssn_us               | `\b\d{3}-\d{2}-\d{4}\b` | `<SSN_REDACTED>` |
| ipv4_private         | `\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b` | `<PRIVATE_IP_REDACTED>` |
| ipv4_public          | any IPv4 not matched above | `<IP_REDACTED>` |
| ipv6                 | common IPv6 patterns (non-loopback, non-link-local) | `<IP_REDACTED>` |
| mac_address          | `\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b` | `<MAC_REDACTED>` |
| absolute_user_path   | `(/Users|/home)/[^/\s]+(?=/)` | `~` |
| internal_dns         | `\b[a-z0-9-]+\.(?:internal|corp|local|lan)\b` | `<INTERNAL_HOST_REDACTED>` |
| uuid_in_url          | `https?://[^/\s]+/[^?\s]*\b[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}\b` | URL with UUID redacted |

### info severity — heuristics and path canonicalization

| Rule | Pattern | Replacement |
|---|---|---|
| windows_user_path    | `C:\\Users\\[^\\]+` | `C:\\Users\\<user>` |
| base64_long          | base64-looking strings >100 chars not matched above | flagged for LLM review |
| credential_hint      | comment lines containing `password`, `token`, `secret`, `apikey`, `api_key`, `passwd` | flagged for LLM review |

### Output

`sanitize.py` writes:

- Sanitized copy of the skill at `<skill-dir>.sanitized/`
- `<skill-dir>.sanitized/scrub_report.regex.json` with this schema:

```json
{
  "version": 1,
  "skill_dir": "/abs/path/to/skill",
  "sanitized_dir": "/abs/path/to/skill.sanitized",
  "scan_stats": {
    "files_scanned": 42,
    "files_skipped_binary": 3,
    "files_skipped_size": 0,
    "files_excluded": 1,
    "total_findings": 11
  },
  "findings": [
    {
      "file": "scripts/foo.py",
      "line": 42,
      "column": 8,
      "rule": "anthropic_key",
      "severity": "block",
      "snippet": "key = 'sk-ant-<REDACTED>'",
      "replacement": "key = '<ANTHROPIC_KEY_REDACTED>'"
    }
  ],
  "excluded_files": [".env"],
  "overall_severity": "block"
}
```

Exit code: `0` on clean or warn-only, `1` on block, `2` on error.

---

## Stage 2 — LLM review (you, in-turn, during publish step 4)

**No external API call. No script. This is the running Claude session doing its own review.**

You are reviewing a skill that the user wants to publish. The regex pass has already run. Your
job is to find subtler leaks the regex cannot catch.

### Review procedure

1. Read every file in `<skill-dir>.sanitized/` (the regex has already canonicalized paths and
   redacted known credential shapes — focus on what regex cannot see).
2. For each file, scan for the seven categories below.
3. Write findings to `<skill-dir>.sanitized/scrub_report.llm.json` using the schema at the
   bottom of this file.
4. Determine overall status: `block` if any finding is high-confidence leak, `warn` if there
   are uncertain candidates, `clean` if nothing found.
5. Merge regex + LLM reports into `scrub_report.json`. Worst severity wins.

### Categories (what to look for)

**1. Internal company / project / client / codename nouns**
Proper nouns that look like internal names not in widespread public use. "Microsoft" and "AWS"
are fine. "AcmeCorp" in a code comment or test fixture is not — it's either a placeholder
(replace with `ExampleCo`) or a real client (ask the user to confirm before keeping).

**2. Dataset / table / schema names**
Names that suggest internal databases. `customer_v2`, `users_prod_2024`, `internal_ml_features`
are suspicious. Ask: "does this name reveal anything about the author's employer's data
architecture?" If yes, suggest a generic replacement.

**3. Paths revealing organizational structure**
Regex catches `~/`. It does not catch things like `projects/acme-migration/phase-3`
embedded in a script comment. Look for directory fragments that look like org slugs, project
codenames, or sprint identifiers.

**4. People's names in comments / examples / commit-like strings**
"Reviewed by Alice", "Per Bob's request", "# TODO: ask Carol" — these identify specific people.
Replace with "the reviewer", "per request", "# TODO: confirm".

**5. Internal URLs not matched by the regex set**
The regex catches `*.internal`, `*.corp`, `*.local`, `*.lan`. Look for other internal URLs:
`wiki.company.example.com`, `<INTERNAL_HOST_REDACTED>.example`, `jenkins-prod-01.example.io`. Err on the
side of redacting.

**6. Cross-field re-identification risk**
Values that, individually, are innocuous, but together identify the author or employer.
Example: a script comment mentions "Zurich office" + "24-person ML team" + "Q3 2025 initiative"
— together these three fragments could identify a specific company. Flag as `warn`.

**7. Credential formats the regex set does not recognize**
New SaaS vendors, internal service tokens, OAuth refresh tokens with non-standard prefixes. If a
string looks like a credential (high-entropy, fixed length, base64-ish) but does not match any
known prefix, flag it as `warn` with category `unknown_credential`.

### Non-goals (do NOT flag)

- Example keys that are obviously placeholders: `sk-ant-example`, `your-api-key-here`,
  `XXXXXX`, `<REPLACE_ME>`
- Documentation that TALKS about API keys without containing one: "set your `ANTHROPIC_API_KEY`
  environment variable"
- Public domain names: `google.com`, `github.com`, `anthropic.com`
- Open-source project names: `react`, `numpy`, `pandas`
- Markdown code blocks that demonstrate format with clearly-fake values

### Decision rules

- **Prefer `warn` over `clean` when uncertain.** `block` requires high confidence that the
  content reveals sensitive information.
- If you cannot produce well-formed JSON after two tries, treat the review as `block`.
- If the regex pass and the LLM pass disagree, the **worst severity wins**.
- Any disagreement between the LLM pass and the server's re-scan at publish time → server
  wins, publish is rejected, user sees the server finding verbatim.

### JSON schema (what to write)

```json
{
  "version": 1,
  "skill_dir": "/abs/path/to/skill.sanitized",
  "reviewed_at": "2026-04-07T10:23:00Z",
  "reviewer_model": "claude",
  "findings": [
    {
      "file": "scripts/foo.py",
      "line": 42,
      "snippet": "# TODO: ask Alice about the Acme migration",
      "category": "person_name | internal_name | dataset_name | org_path | internal_url | cross_field_reid | unknown_credential",
      "reason": "short explanation (one sentence)",
      "suggested_replacement": "# TODO: confirm migration approach",
      "confidence": "low | medium | high"
    }
  ],
  "status": "clean | warn | block",
  "summary": "one-paragraph summary of what was reviewed and any concerns"
}
```

---

## Stage 3 — User approval

The base skill presents the merged report + a diff to the user. The user must type `publish`
verbatim to continue. See `SKILL.md` step 5 for the exact prompt format.

---

## Fail-safe rules (MUST be enforced)

- `sanitize.py` returns exit code ≥ 1 → STOP. Do not offer to override without explicit user
  request.
- LLM review JSON is malformed after two tries → STOP. Treat as `block`.
- User types anything other than `publish` → STOP. Save sanitized copy. Exit cleanly.
- Server-side re-scan finds a leak the client missed → STOP. Show the server finding verbatim.
- Any disagreement between layers → worst severity wins.
- Every `block` decision is recorded in the server's `scrub_reports` table for audit, even if
  the user never completes the publish.
