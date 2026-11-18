"""The false-done-check GitHub Action (action/check.py) against a mocked
GitHub REST API: no request leaves the process."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

import cogext_observe.receipt as receipt_mod

ACTION = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("false_done_check", ACTION / "check.py")
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

REPO = "acme/widgets"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


class FakeGitHub(check.GitHub):
    """Routes the client's REST calls to in-memory state."""

    def __init__(self, body, check_runs=(), statuses=(), comments=()):
        super().__init__("test-token", REPO)
        self.pr = {"number": 7, "body": body, "head": {"sha": SHA},
                   "html_url": f"https://github.com/{REPO}/pull/7"}
        self.check_runs_data = list(check_runs)
        self.statuses_data = list(statuses)
        self.comments_data = [dict(c) for c in comments]
        self.posted_statuses = []
        self.calls = []
        self._next_id = 1000

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        route = re.sub(r"\?.*$", "", path)
        if method == "GET" and route == f"/repos/{REPO}/pulls/7":
            return self.pr
        if method == "GET" and route == f"/repos/{REPO}/commits/{SHA}/check-runs":
            return {"total_count": len(self.check_runs_data), "check_runs": self.check_runs_data}
        if method == "GET" and route == f"/repos/{REPO}/commits/{SHA}/status":
            return {"state": "success", "statuses": self.statuses_data}
        if method == "GET" and route == f"/repos/{REPO}/commits/{SHA}/pulls":
            return [{"number": 7, "state": "open"}]
        if method == "GET" and route == f"/repos/{REPO}/issues/7/comments":
            return list(self.comments_data)
        if method == "POST" and route == f"/repos/{REPO}/issues/7/comments":
            self._next_id += 1
            comment = {"id": self._next_id, "body": body["body"],
                       "user": {"login": "github-actions[bot]", "type": "Bot"},
                       "html_url": f"https://github.com/{REPO}/pull/7#issuecomment-{self._next_id}"}
            self.comments_data.append(comment)
            return comment
        m = re.fullmatch(rf"/repos/{REPO}/issues/comments/(\d+)", route)
        if method == "PATCH" and m:
            comment = next(c for c in self.comments_data if c["id"] == int(m.group(1)))
            comment["body"] = body["body"]
            return comment
        if method == "POST" and route == f"/repos/{REPO}/statuses/{SHA}":
            self.posted_statuses.append(body)
            return body
        raise AssertionError(f"unexpected request {method} {path}")

    @property
    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PATCH") and "/statuses/" not in c[1]]

    @property
    def marker_comments(self):
        return [c for c in self.comments_data if check.MARKER in c["body"]]


def run(name, conclusion="success", status="completed", run_id=1):
    return {"name": name, "status": status, "conclusion": conclusion if status == "completed" else None,
            "html_url": f"https://github.com/{REPO}/runs/{run_id}",
            "details_url": f"https://github.com/{REPO}/actions/runs/{run_id}/job/{run_id}"}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("COGEXT_RECEIPT_KEY", "test-only-receipt-key-0123456789abcdef")
    monkeypatch.setattr(receipt_mod, "_warned_unsigned", False)
    monkeypatch.setenv("GITHUB_RUN_ID", "999")
    monkeypatch.setenv("GITHUB_JOB", "false-done-check")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7}}))
    out = tmp_path / "out.txt"
    out.write_text("")
    return {"GITHUB_REPOSITORY": REPO, "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_EVENT_PATH": str(event), "GITHUB_OUTPUT": str(out)}


def main(gh, env, **inputs):
    env = dict(env, **{f"INPUT_{k.upper()}": v for k, v in inputs.items()})
    code = check.main(env=env, gh=gh)
    outputs = dict(line.split("=", 1) for line in Path(env["GITHUB_OUTPUT"]).read_text().splitlines())
    return code, outputs


# ---------------------------------------------------------------- scenarios

def test_claim_with_failing_check_posts_mismatch_comment_and_receipt(_env):
    gh = FakeGitHub("Fixed the parser. All tests pass.",
                    check_runs=[run("lint", run_id=1), run("test (3.12)", "failure", run_id=2)])
    code, outputs = main(gh, _env)

    assert code == 0                                       # does not fail by default
    assert outputs["verdict"] == "MISMATCH"
    [comment] = gh.marker_comments
    assert "This PR says tests pass, but `test (3.12)` failed." in comment["body"]
    assert "> All tests pass." in comment["body"]

    receipt = json.loads(re.search(r"```json\n(.*?)\n```", comment["body"], re.S).group(1))
    assert receipt["verdict"] == "MISMATCH"
    assert receipt["agent_claim"] == "All tests pass."
    assert receipt["tool_response"]["checks"] == [{"name": "test (3.12)", "conclusion": "failure"}]
    assert receipt["tool_response"]["head_sha"] == SHA
    assert receipt_mod.verify_receipt(receipt)            # signed and intact

    [status] = gh.posted_statuses
    assert status["context"] == "cogext/false-done"
    assert status["state"] == "failure"
    assert "test (3.12)" in status["description"]


def test_timed_out_check_is_a_mismatch(_env):
    gh = FakeGitHub("CI is green.", check_runs=[run("build", "timed_out")])
    _, outputs = main(gh, _env)
    assert outputs["verdict"] == "MISMATCH"
    assert "`build` timed out" in gh.marker_comments[0]["body"]


def test_claim_with_all_green_checks_posts_nothing(_env):
    gh = FakeGitHub("All tests pass.", check_runs=[run("lint"), run("test")])
    code, outputs = main(gh, _env)
    assert code == 0
    assert outputs["verdict"] == "VERIFIED"
    assert gh.writes == []
    assert gh.posted_statuses[-1]["state"] == "success"
    assert gh.posted_statuses[-1]["description"].startswith("VERIFIED")


def test_no_claim_does_nothing_even_with_failing_check(_env):
    gh = FakeGitHub("Refactors the parser. Tests still need work, see TODO.",
                    check_runs=[run("test", "failure")])
    _, outputs = main(gh, _env)
    assert outputs["verdict"] == "NO_CLAIM"
    assert gh.writes == []
    assert not any("/check-runs" in path for _, path in gh.calls)


def test_repeated_run_updates_the_same_comment(_env):
    gh = FakeGitHub("All tests pass.", check_runs=[run("test", "failure")])
    main(gh, _env)
    main(gh, _env)                                         # identical: no rewrite
    gh.check_runs_data = [run("test", "failure"), run("e2e", "failure", run_id=3)]
    main(gh, _env)                                         # changed: edit in place

    assert len(gh.marker_comments) == 1
    assert sum(1 for m, p in gh.calls if m == "POST" and p.endswith("/issues/7/comments")) == 1
    assert sum(1 for m, _ in gh.calls if m == "PATCH") == 1
    assert "2 checks did not pass: `test`, `e2e`" in gh.marker_comments[0]["body"]


def test_existing_comment_is_resolved_when_checks_go_green(_env):
    gh = FakeGitHub("All tests pass.", check_runs=[run("test", "failure")])
    main(gh, _env)
    gh.check_runs_data = [run("test", "success")]
    _, outputs = main(gh, _env)
    assert outputs["verdict"] == "VERIFIED"
    [comment] = gh.marker_comments
    assert "Resolved" in comment["body"] and "VERIFIED" in comment["body"]


# --------------------------------------------------------------- edge cases

def test_own_check_is_excluded(_env):
    gh = FakeGitHub("All tests pass.", check_runs=[
        run("test"),
        run("false-done-check", "failure", run_id=5),       # by name
        run("other job, same run", "failure", run_id=999),  # by this run's id
    ])
    _, outputs = main(gh, _env)
    assert outputs["verdict"] == "VERIFIED"


def test_ignored_and_legacy_status_checks(_env):
    gh = FakeGitHub("All tests pass.", check_runs=[run("test"), run("Vercel Preview", "failure")],
                    statuses=[{"context": "cogext/false-done", "state": "failure"},
                              {"context": "ci/circleci: test", "state": "failure",
                               "target_url": "https://circleci.com/x"}])
    _, outputs = main(gh, _env)
    assert outputs["verdict"] == "MISMATCH"
    body = gh.marker_comments[0]["body"]
    assert "ci/circleci: test" in body and "Vercel" not in body


def test_pending_checks_wait(_env):
    gh = FakeGitHub("All tests pass.", check_runs=[run("test", status="in_progress")])
    _, outputs = main(gh, _env)
    assert outputs["verdict"] == "PENDING"
    assert gh.writes == []
    assert gh.posted_statuses[-1]["state"] == "pending"


def test_fail_on_mismatch_input(_env):
    gh = FakeGitHub("All tests pass.", check_runs=[run("test", "failure")])
    code, _ = main(gh, _env, fail_on_mismatch="true")
    assert code == 1


def test_bot_comment_claim_counts(_env):
    gh = FakeGitHub("Implements #12.", check_runs=[run("test", "failure")], comments=[
        {"id": 1, "body": "All tests pass, ready to merge.",
         "user": {"login": "some-human", "type": "User"}},
        {"id": 2, "body": "Done! All tests are passing now.",
         "user": {"login": "coding-agent[bot]", "type": "Bot"}},
    ])
    _, outputs = main(gh, _env)
    assert outputs["verdict"] == "MISMATCH"
    assert "comment by coding-agent[bot]" in gh.marker_comments[0]["body"]

    gh2 = FakeGitHub("Implements #12.", check_runs=[run("test", "failure")],
                     comments=gh.comments_data[:1])
    _, outputs = main(gh2, _env)
    assert outputs["verdict"] == "NO_CLAIM"                # human comments don't count


def test_workflow_run_event_finds_pr_by_commit(_env, tmp_path):
    event = tmp_path / "wr.json"
    event.write_text(json.dumps({"workflow_run": {"head_sha": SHA, "pull_requests": []}}))
    env = dict(_env, GITHUB_EVENT_NAME="workflow_run", GITHUB_EVENT_PATH=str(event))
    gh = FakeGitHub("All tests pass.", check_runs=[run("test", "failure")])
    _, outputs = main(gh, env)
    assert outputs["verdict"] == "MISMATCH"
    assert outputs["pull-requests"] == "7"


def test_read_only_token_warns_instead_of_crashing(_env, capsys):
    class ReadOnly(FakeGitHub):
        def request(self, method, path, body=None):
            if method in ("POST", "PATCH"):
                raise check.GitHubError(403, "Resource not accessible by integration")
            return super().request(method, path, body)

    gh = ReadOnly("All tests pass.", check_runs=[run("test", "failure")])
    code, outputs = main(gh, _env)
    assert code == 0 and outputs["verdict"] == "MISMATCH"
    assert "::warning::" in capsys.readouterr().out


# ------------------------------------------------------------ claim parsing

@pytest.mark.parametrize("text", [
    "All tests pass.",
    "Tests passing.",
    "CI is green",
    "The build succeeds.",
    "- [x] Tests pass",
    "## Test plan\n- [x] `pytest` passes",
    "✅ All checks passing",
    "42 tests passed, 0 failed",
])
def test_pass_claims_are_found(text):
    assert check.find_pass_claim(text)


@pytest.mark.parametrize("text", [
    "- [ ] Tests pass",                                    # unticked template box
    "<!-- Make sure all tests pass before opening -->",
    "Make sure CI is green before merging.",
    "Do the tests pass?",
    "Tests don't pass yet.",
    "Tests passed locally but CI failed.",                 # failure disclosed
    "All done.",                                           # not about tests
    "```\nAll tests pass\n```",
    "> All tests pass",
])
def test_non_claims_are_ignored(text):
    assert check.find_pass_claim(text) is None


def test_resolved_comment_is_not_rewritten_every_run(_env):
    gh = FakeGitHub("All tests pass.", check_runs=[run("test", "failure")])
    main(gh, _env)
    gh.check_runs_data = [run("test", "success")]
    main(gh, _env)
    main(gh, _env)
    assert sum(1 for m, _ in gh.calls if m == "PATCH") == 1
