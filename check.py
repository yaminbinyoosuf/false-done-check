"""false-done-check: flag pull requests that claim tests pass while CI failed.

Runs inside a GitHub Actions job of the repository that installed the Action.
It reads the PR description (and, optionally, recent comments by bots), looks
for a pass claim ("all tests pass", "CI is green", "build succeeds"), then
reads the check runs and commit statuses of the PR head commit. When a claim
exists and a check concluded failure or timed_out, it posts one comment --
updated in place on later runs, found by a hidden marker -- with a COGEXT
MISMATCH receipt, and sets the commit status ``cogext/false-done``.

It only ever writes to the repository it runs in, with that workflow's
GITHUB_TOKEN. Nothing is sent anywhere else.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Optional

from cogext_observe.detector import find_claim
from cogext_observe.receipt import generate_receipt

MARKER = "<!-- cogext:false-done-check -->"
STATUS_CONTEXT = "cogext/false-done"
DEFAULT_CHECK_NAME = "false-done-check"
FAILED_CONCLUSIONS = {"failure", "timed_out"}
FAILED_STATUS_STATES = {"failure", "error"}
PENDING_CHECK_STATES = {"queued", "in_progress", "waiting", "requested", "pending"}
# Checks that report on something other than tests or builds. A failing
# preview deploy does not contradict "all tests pass".
DEFAULT_IGNORE = r"(?i)deploy|preview|vercel|netlify|codecov|coverage|\bcla\b|license/cla|labeler|stale"

# The detector's "call" for a CI run: claims must be about these nouns.
CI_CALL = {
    "tool": "ci_checks",
    "claim_terms": ("tests", "test", "suite", "suites", "checks", "check", "ci",
                    "build", "builds", "specs", "spec", "pipeline"),
}
_TEST_WORD = re.compile(r"(?i)\b(tests?|test suite|suites?|checks?|ci|builds?|specs?|pipeline)\b")
_TEST_RUNNERS = re.compile(
    r"(?i)\b(pytest|unittest|jest|vitest|mocha|rspec|phpunit|cargo test|go test|npm test|"
    r"yarn test|pnpm test|tox|nox|ctest|gradle test|mvn test|make test)\b")

# Rewrites applied before the detector reads a sentence, so phrasings it does
# not know ("CI is green", "build succeeds", "tests passing") reach it as ones
# it does ("CI passed"). Its mood, modality and negation rules still apply.
_NORMALIZE = [
    (re.compile(r"(?i)\b(is|are|was|were|now|all)?\s*green\b"), r" \1 passed"),
    (re.compile(r"(?i)\bsucceeds\b"), "succeeded"),
    (re.compile(r"(?i)\bpassing\b"), "passed"),
    (re.compile(r"(?i)\bbuilds successfully\b"), "build succeeded"),
    (re.compile(r"(?i)\bcompiles( cleanly| successfully)?\b"), "build succeeded"),
    (re.compile(r"(?i),?\s*\(?\b(0|zero|no) (failures?|failed|failing|errors?)\)?"), ""),
    (re.compile(r":\s*"), " "),
]
# The PR itself admits a failure: no false-done, the author disclosed it.
_ADMISSION = re.compile(
    r"(?i)\b(tests?|ci|builds?|checks?|specs?|suite|pipeline)\b[^.\n]{0,40}?"
    r"\b(fail|fails|failing|failed|broken|red|flaky)\b"
    r"|\b(failing|failed|broken|flaky)\s+(tests?|ci|builds?|checks?|specs?)\b")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


# --------------------------------------------------------------------------
# Claim detection
# --------------------------------------------------------------------------

def _clean_markdown(text: str) -> str:
    """Drop what is not the author's own assertion: HTML comments (PR
    templates), code blocks, quotes, unticked checkboxes, our own comment."""
    text = re.sub(r"<!--.*?-->", " ", text or "", flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"^[-*+]\s*\[ \]", stripped):          # unticked: not a claim
            continue
        stripped = re.sub(r"^[-*+]\s*\[[xX]\]\s*", "", stripped)
        stripped = re.sub(r"^(#+|[-*+]|\d+[.)])\s+", "", stripped)
        stripped = re.sub(r"[*_`~]|[✅✔☑\U0001F7E2]️?", " ", stripped)
        lines.append(stripped)
    return "\n".join(lines)


def find_pass_claim(text: str) -> Optional[str]:
    """The first sentence of ``text`` claiming tests/CI/build pass, or None."""
    cleaned = _clean_markdown(text)
    if _ADMISSION.search(_strip_zero_failures(cleaned)):
        return None
    for sentence in _SENTENCE.split(cleaned):
        sentence = " ".join(sentence.split())
        if not sentence:
            continue
        normalized = sentence
        for pattern, repl in _NORMALIZE:
            normalized = pattern.sub(repl, normalized)
        normalized = _TEST_RUNNERS.sub("tests", normalized)
        normalized = " ".join(normalized.split())
        clause = find_claim(normalized, CI_CALL)
        if clause and _TEST_WORD.search(clause):
            return sentence[:300]
    return None


def _strip_zero_failures(text: str) -> str:
    return re.sub(r"(?i)\b(0|zero|no) (failures?|failed|failing|errors?)\b", " ", text)


# --------------------------------------------------------------------------
# GitHub API
# --------------------------------------------------------------------------

class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


class GitHub:
    """Minimal GitHub REST client (stdlib only)."""

    def __init__(self, token: str, repo: str, api_url: str = "https://api.github.com"):
        self.token = token
        self.repo = repo
        self.api_url = api_url.rstrip("/")

    def request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "false-done-check")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise GitHubError(exc.code, exc.read().decode(errors="replace")[:300]) from None
        return json.loads(raw) if raw else None

    def paginate(self, path: str, key: Optional[str] = None, limit: int = 1000) -> list:
        items: list = []
        page = 1
        sep = "&" if "?" in path else "?"
        while len(items) < limit:
            data = self.request("GET", f"{path}{sep}per_page=100&page={page}")
            batch = data.get(key, []) if key else data
            items.extend(batch or [])
            if not batch or len(batch) < 100:
                break
            page += 1
        return items

    # -- typed helpers ----------------------------------------------------
    def pull(self, number: int) -> dict:
        return self.request("GET", f"/repos/{self.repo}/pulls/{number}")

    def pulls_for_commit(self, sha: str) -> list:
        return self.request("GET", f"/repos/{self.repo}/commits/{sha}/pulls") or []

    def check_runs(self, sha: str) -> list:
        return self.paginate(f"/repos/{self.repo}/commits/{sha}/check-runs?filter=latest",
                             key="check_runs")

    def statuses(self, sha: str) -> list:
        data = self.request("GET", f"/repos/{self.repo}/commits/{sha}/status?per_page=100")
        return (data or {}).get("statuses", [])

    def comments(self, number: int) -> list:
        return self.paginate(f"/repos/{self.repo}/issues/{number}/comments")

    def create_comment(self, number: int, body: str) -> dict:
        return self.request("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})

    def update_comment(self, comment_id: int, body: str) -> dict:
        return self.request("PATCH", f"/repos/{self.repo}/issues/comments/{comment_id}",
                            {"body": body})

    def set_status(self, sha: str, state: str, description: str,
                   target_url: Optional[str] = None) -> None:
        body = {"state": state, "context": STATUS_CONTEXT, "description": description[:140]}
        if target_url:
            body["target_url"] = target_url
        self.request("POST", f"/repos/{self.repo}/statuses/{sha}", body)


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def _is_bot(user: Optional[dict]) -> bool:
    user = user or {}
    return user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]")


def collect_claim(pr: dict, comments: list, include_bot_comments: bool,
                  max_comments: int = 5) -> Optional[dict]:
    """The pass claim and where it was made, or None."""
    claim = find_pass_claim(pr.get("body") or "")
    if claim:
        return {"text": claim, "source": "PR description", "url": pr.get("html_url")}
    if not include_bot_comments:
        return None
    bot_comments = [c for c in comments
                    if _is_bot(c.get("user")) and MARKER not in (c.get("body") or "")]
    for comment in reversed(bot_comments[-max_comments:]):     # newest first
        claim = find_pass_claim(comment.get("body") or "")
        if claim:
            login = (comment.get("user") or {}).get("login", "a bot")
            return {"text": claim, "source": f"comment by {login}", "url": comment.get("html_url")}
    return None


def evaluate_checks(check_runs: list, statuses: list, *, exclude_names: Iterable[str],
                    ignore_pattern: Optional[str], run_id: Optional[str]) -> dict:
    """Split the head commit's checks into failed / pending / passed."""
    exclude = {n.strip().lower() for n in exclude_names if n and n.strip()}
    ignore = re.compile(ignore_pattern) if ignore_pattern else None

    def skipped(name: str, details_url: str = "") -> bool:
        if name.strip().lower() in exclude:
            return True
        if run_id and f"/actions/runs/{run_id}/" in (details_url or ""):
            return True                              # this very workflow run
        return bool(ignore and ignore.search(name))

    failed, pending, passed = [], [], []
    for run in check_runs:
        name = run.get("name") or ""
        if skipped(name, run.get("details_url") or ""):
            continue
        entry = {"name": name, "conclusion": run.get("conclusion"),
                 "url": run.get("html_url") or run.get("details_url")}
        if run.get("status") != "completed":
            entry["conclusion"] = run.get("status")
            pending.append(entry)
        elif run.get("conclusion") in FAILED_CONCLUSIONS:
            failed.append(entry)
        else:
            passed.append(entry)
    for status in statuses:
        name = status.get("context") or ""
        if name == STATUS_CONTEXT or skipped(name):
            continue
        entry = {"name": name, "conclusion": status.get("state"), "url": status.get("target_url")}
        if status.get("state") in FAILED_STATUS_STATES:
            failed.append(entry)
        elif status.get("state") == "pending":
            pending.append(entry)
        else:
            passed.append(entry)
    return {"failed": failed, "pending": pending, "passed": passed}


def _describe(conclusion: Optional[str]) -> str:
    return {"timed_out": "timed out", "error": "errored"}.get(conclusion or "", "failed")


def build_receipt(repo: str, pr_number: int, sha: str, claim: dict, failed: list) -> dict:
    mismatch = {
        "tool": "github_checks",
        "tool_response": {
            "head_sha": sha,
            "checks": [{"name": f["name"], "conclusion": f["conclusion"]} for f in failed],
        },
        "agent_claim": claim["text"][:200],
        "verdict": "MISMATCH",
        "reason": "PR claims tests pass; a CI check on the head commit did not pass",
    }
    receipt = generate_receipt(mismatch, session_id=f"github:{repo}#{pr_number}")
    # The receipt is not uploaded to cogextai.com, so a verify link would 404.
    receipt.pop("verify_url", None)
    return receipt


def _link(text: str, url: Optional[str]) -> str:
    return f"[{text}]({url})" if url else text


def mismatch_comment(claim: dict, failed: list, receipt: dict, sha: str) -> str:
    first = failed[0]
    names = ", ".join(f"`{f['name']}`" for f in failed[:5])
    more = f" (and {len(failed) - 5} more)" if len(failed) > 5 else ""
    if len(failed) == 1:
        headline = f"`{first['name']}` {_describe(first['conclusion'])}"
    else:
        headline = f"{len(failed)} checks did not pass: {names}{more}"
    source = f"[{claim['source']}]({claim['url']})" if claim.get("url") else claim["source"]
    rows = "\n".join(
        f"| {_link(f['name'], f.get('url'))} | `{f['conclusion']}` |" for f in failed[:20])
    signed = ("signed (HMAC-SHA256)" if receipt.get("signature")
              else "unsigned (set `COGEXT_RECEIPT_KEY` to sign)")
    return (
        f"{MARKER}\n"
        f"### ⚠️ This PR says tests pass, but {headline}.\n\n"
        f"> {claim['text']}\n\n"
        f"— {source}, checked against head commit `{sha[:7]}`\n\n"
        f"| Check | Conclusion |\n|---|---|\n{rows}\n\n"
        f"<details><summary>COGEXT receipt: <b>MISMATCH</b> ({signed})</summary>\n\n"
        f"```json\n{json.dumps(receipt, indent=2, ensure_ascii=False)}\n```\n</details>\n\n"
        f"<sub>Posted by [false-done-check](https://github.com/yaminbinyoosuf/cogext-observe/tree/main/action). "
        f"This comment updates in place; it never posts twice.</sub>"
    )


def resolved_comment(sha: str, reason: str) -> str:
    return (
        f"{MARKER}\n"
        f"### ✅ Resolved: {reason}\n\n"
        f"Checked head commit `{sha[:7]}`. Verdict: **VERIFIED**.\n\n"
        f"<sub>Posted by [false-done-check](https://github.com/yaminbinyoosuf/cogext-observe/tree/main/action).</sub>"
    )


def _is_resolved(comment: dict) -> bool:
    return f"{MARKER}\n### \u2705 Resolved" in (comment.get("body") or "")


def _fingerprint(sha: str, claim: dict, failed: list) -> str:
    key = json.dumps([sha, claim["text"], sorted((f["name"], f["conclusion"]) for f in failed)])
    return "cogext:fingerprint=" + hashlib.sha256(key.encode()).hexdigest()[:16]


def check_pull_request(gh: GitHub, pr_number: int, cfg: dict) -> dict:
    """Evaluate one PR, write its comment and status. Returns the outcome."""
    pr = gh.pull(pr_number)
    sha = pr["head"]["sha"]
    comments = gh.comments(pr_number)
    existing = next((c for c in comments if MARKER in (c.get("body") or "")), None)
    claim = collect_claim(pr, comments, cfg["include_bot_comments"])

    result = {"pr": pr_number, "sha": sha, "claim": claim and claim["text"],
              "verdict": None, "comment": None}

    if claim is None:
        result["verdict"] = "NO_CLAIM"
        if existing and not _is_resolved(existing):
            _safe(lambda: gh.update_comment(existing["id"], resolved_comment(
                sha, "the PR no longer claims tests pass.")), cfg)
            result["comment"] = "resolved"
        _safe(lambda: gh.set_status(sha, "success", "No pass claim found"), cfg)
        return result

    checks = evaluate_checks(
        gh.check_runs(sha), gh.statuses(sha),
        exclude_names=[cfg["check_name"], os.environ.get("GITHUB_JOB", "")],
        ignore_pattern=cfg["ignore_checks"], run_id=os.environ.get("GITHUB_RUN_ID"))

    if checks["failed"]:
        result["verdict"] = "MISMATCH"
        receipt = build_receipt(gh.repo, pr_number, sha, claim, checks["failed"])
        stamp = _fingerprint(sha, claim, checks["failed"])
        body = mismatch_comment(claim, checks["failed"], receipt, sha) + f"\n<!-- {stamp} -->"
        if existing:
            # Same claim, commit and failures: keep the receipt already posted.
            if stamp in (existing.get("body") or ""):
                result["comment"] = "unchanged"
            else:
                _safe(lambda: gh.update_comment(existing["id"], body), cfg)
                result["comment"] = "updated"
        else:
            _safe(lambda: gh.create_comment(pr_number, body), cfg)
            result["comment"] = "created"
        first = checks["failed"][0]
        _safe(lambda: gh.set_status(
            sha, "failure", f"MISMATCH: claims tests pass, {first['name']} "
                            f"{_describe(first['conclusion'])}",
            target_url=(existing or {}).get("html_url") or pr.get("html_url")), cfg)
        result["receipt"] = receipt
        return result

    if checks["pending"]:
        result["verdict"] = "PENDING"
        _safe(lambda: gh.set_status(sha, "pending",
                                    f"Pass claim found; waiting for {len(checks['pending'])} check(s)"),
              cfg)
        return result

    if not checks["passed"]:
        result["verdict"] = "UNVERIFIABLE"
        _safe(lambda: gh.set_status(sha, "success",
                                    "UNVERIFIABLE: pass claim, but no checks ran"), cfg)
        return result

    result["verdict"] = "VERIFIED"
    if existing and not _is_resolved(existing):
        _safe(lambda: gh.update_comment(existing["id"], resolved_comment(
            sha, "all checks on the head commit now pass.")), cfg)
        result["comment"] = "resolved"
    _safe(lambda: gh.set_status(sha, "success",
                                f"VERIFIED: pass claim matches {len(checks['passed'])} passing check(s)"),
          cfg)
    return result


def _safe(fn, cfg):
    """Writes can fail (fork PRs get a read-only token). Warn, don't crash."""
    try:
        return fn()
    except GitHubError as exc:
        if exc.status in (403, 404):
            print(f"::warning::false-done-check could not write to the PR ({exc}). "
                  "Fork PRs get a read-only token; run on workflow_run to cover them, "
                  "and grant pull-requests: write and statuses: write.")
            cfg.setdefault("write_errors", []).append(str(exc))
            return None
        raise


# --------------------------------------------------------------------------
# Event plumbing
# --------------------------------------------------------------------------

def pr_numbers_from_event(gh: GitHub, event_name: str, event: dict) -> list:
    if event_name in ("pull_request", "pull_request_target"):
        return [event["pull_request"]["number"]]
    if event_name == "issue_comment":
        issue = event.get("issue") or {}
        return [issue["number"]] if issue.get("pull_request") else []
    if event_name == "check_suite":
        suite = event.get("check_suite") or {}
        prs, sha = suite.get("pull_requests") or [], suite.get("head_sha")
    elif event_name == "workflow_run":
        run = event.get("workflow_run") or {}
        prs, sha = run.get("pull_requests") or [], run.get("head_sha")
    else:
        return []
    numbers = [p["number"] for p in prs if p.get("number")]
    if not numbers and sha:
        # Fork PRs are not listed on the event; look them up by commit.
        numbers = [p["number"] for p in gh.pulls_for_commit(sha)
                   if p.get("state") == "open"]
    return sorted(set(numbers))


def _bool(value: Optional[str], default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_config(env: dict) -> dict:
    return {
        "check_name": env.get("INPUT_CHECK_NAME") or DEFAULT_CHECK_NAME,
        "ignore_checks": env.get("INPUT_IGNORE_CHECKS", DEFAULT_IGNORE),
        "include_bot_comments": _bool(env.get("INPUT_INCLUDE_BOT_COMMENTS"), True),
        "fail_on_mismatch": _bool(env.get("INPUT_FAIL_ON_MISMATCH"), False),
    }


def main(env: Optional[dict] = None, gh: Optional[GitHub] = None) -> int:
    env = dict(os.environ if env is None else env)
    cfg = load_config(env)
    repo = env.get("GITHUB_REPOSITORY", "")
    if gh is None:
        token = env.get("INPUT_GITHUB_TOKEN") or env.get("GITHUB_TOKEN", "")
        gh = GitHub(token, repo, env.get("GITHUB_API_URL", "https://api.github.com"))
    event_name = env.get("GITHUB_EVENT_NAME", "")
    event_path = env.get("GITHUB_EVENT_PATH")
    event = {}
    if event_path and os.path.exists(event_path):
        with open(event_path, encoding="utf-8") as fh:
            event = json.load(fh)

    numbers = pr_numbers_from_event(gh, event_name, event)
    if not numbers:
        print(f"false-done-check: no pull request for event '{event_name}'; nothing to do.")
        return 0

    results = [check_pull_request(gh, n, cfg) for n in numbers]
    for r in results:
        print(f"false-done-check: PR #{r['pr']} @ {r['sha'][:7]}: {r['verdict']}"
              + (f" (comment {r['comment']})" if r["comment"] else "")
              + (f" -- claim: {r['claim']!r}" if r["claim"] else ""))
    mismatches = [r for r in results if r["verdict"] == "MISMATCH"]
    out = env.get("GITHUB_OUTPUT")
    if out:
        verdict = "MISMATCH" if mismatches else results[-1]["verdict"]
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"verdict={verdict}\n")
            fh.write(f"pull-requests={','.join(str(r['pr']) for r in results)}\n")
    if mismatches and cfg["fail_on_mismatch"]:
        print("::error::false-done-check: a PR claims tests pass while CI failed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
