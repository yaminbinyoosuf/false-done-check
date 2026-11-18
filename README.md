# False-Done Check

Your coding agent's PR says "all tests pass". Does CI agree?

AI coding agents (Claude Code, Cursor, Devin, Copilot and others) often run part of the test suite, see it pass, and write "tests pass" in the PR. CI then runs the rest and fails. Reviewers who read the description and not the checks merge broken changes. Across public agent PRs we check daily, about 1 in 25 PRs that claim tests pass had a failing test or build check, and more than half of those were merged anyway: [cogextai.com/honesty](https://cogextai.com/honesty).

This Action reads the PR description (and recent comments by bots), finds pass claims ("all tests pass", "CI is green", "build succeeds", a ticked `- [x] Tests pass`), and compares them with the check runs and commit statuses of the PR's head commit.

- **Claim, and a check failed:** one comment on the PR, e.g. "This PR says tests pass, but `test (3.12)` failed.", with a COGEXT MISMATCH receipt. Later runs edit that comment, so it is never posted twice.
- **Claim, and all checks green:** VERIFIED. Nothing is posted, and an earlier warning is edited to "Resolved".
- **No claim:** nothing is posted.
- **Every run** sets the commit status `cogext/false-done`.

It does not fail your workflow unless you set `fail-on-mismatch: true`. It only writes to the repository it is installed in, using that workflow's `GITHUB_TOKEN`. Nothing is sent to COGEXT or anywhere else.

## Install

Add `.github/workflows/false-done-check.yml`:

```yaml
name: false-done-check
on:
  pull_request:
    types: [opened, edited, synchronize, reopened]
  check_suite:
    types: [completed]
  workflow_run:
    workflows: ["CI"]          # the name(s) of your test/build workflows
    types: [completed]

permissions:
  contents: read
  checks: read
  statuses: write
  pull-requests: write

jobs:
  false-done-check:
    runs-on: ubuntu-latest
    steps:
      - uses: yaminbinyoosuf/false-done-check@v1
        # with:
        #   fail-on-mismatch: false      # true to fail this job on MISMATCH
        #   include-bot-comments: true   # also read claims in bot/agent comments
        #   ignore-checks: "(?i)deploy|preview"   # checks that aren't tests
```

`workflow_run` re-checks a PR after your CI finishes. It is also the event that can comment on PRs from forks, whose `pull_request` token is read-only. List your CI workflow names under `workflows:`. Keep the job id `false-done-check`, or pass `check-name:`, so the Action never judges itself.

## Inputs and output

| Input | Default | |
|---|---|---|
| `github-token` | `github.token` | Reads checks, writes the comment and status |
| `fail-on-mismatch` | `false` | Fail this job on MISMATCH |
| `include-bot-comments` | `true` | Also read claims in bot / agent comments |
| `check-name` | `false-done-check` | This job's name, excluded from evaluation |
| `ignore-checks` | deploy, preview, coverage, CLA bots | Regex of checks that are not tests or builds |

Output `verdict`: `MISMATCH`, `VERIFIED`, `PENDING`, `UNVERIFIABLE` (claim, but no checks ran) or `NO_CLAIM`.

Receipts come from [cogext-observe](https://pypi.org/project/cogext-observe/). They are unsigned unless you add a `COGEXT_RECEIPT_KEY` secret (32+ characters) and pass it as `env: COGEXT_RECEIPT_KEY: ${{ secrets.COGEXT_RECEIPT_KEY }}` on the step. Runs on Linux and macOS runners.

## Agents that act outside code

If your agents update CRMs, send email, move money or close tickets, the same "said done, wasn't" failure happens where no CI is watching. COGEXT runs a 48-hour audit of one such flow: [cogextai.com](https://cogextai.com) or hello@cogextai.com.

MIT licensed.
