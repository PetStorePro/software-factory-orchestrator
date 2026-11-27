# The Software Factory

It runs locally on macOS, using Python and Codex CLI with your existing ChatGPT login. No Docker, OpenAI API key or extra paid service is needed.

**The factory never merges or deploys. A human takes over after AI review.**

## How it works

```text
Oldest ai-ready Issue
        ↓
Feature branch → Developer → Commit, push and PR
                                 ↓
                             CI checks
                                 ↓
                         Independent Reviewer
                                 ↓
                       READY_FOR_HUMAN_REVIEW
                                 ↓
                Human review → Merge → Approved deployment
```

If CI fails or the Reviewer asks for changes, the Developer repairs the code in the same PR. CI and review run again, within configured limits.

Only one Issue and one agent run at a time. Reviews are posted as COMMENT reviews, with inline comments where possible. AI approval does not replace human approval.

The target repository owns the application, CI/CD, branch protection, human reviews and production approval. The factory coordinates the work and reads CI results for the current commit.

## Setup

Install Python 3.14, Git, GitHub CLI and Codex CLI. Authenticate GitHub CLI for the target repository and Codex through ChatGPT.

From this repository:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
gh auth status
codex login status
```

Copy the example **only if you do not already have `.env`**:

```bash
cp .env.example .env
```

Edit it for your repository, branch and CI check names. The example targets `PetStorePro/pet-store-web-app` on `main`. Its required checks are `backend`, `frontend`, `dependency-review`, `backend-image` and `frontend-image`. CI skipping is disabled.

Create these labels in the target repository:

```text
ai-ready · ai-in-progress · ai-review · ai-blocked · READY_FOR_HUMAN_REVIEW
```

Load your trusted local configuration in each terminal you use:

```bash
set -a
source .env
set +a
```

This exports the settings so Python can read them. The factory does not load `.env` automatically. Keep credentials out of it.

## Run

Check the setup and see eligible Issues without running agents or changing GitHub:

```bash
python -m orchestrator.main preflight
python -m orchestrator.main once --dry-run
```

These checks do not verify branch protection or production approval.

Add `ai-ready` to a small, clear Issue, then start real work:

```bash
python -m orchestrator.main once
```

**This can create commits, push a feature branch, open a PR and publish reviews.** It prints short progress messages in English.

| Command after `python -m orchestrator.main` | Purpose |
| --- | --- |
| `watch` | Keep processing Issues, one at a time. |
| `status` | Show saved progress and results. |
| `stop` | Request shutdown and stop the managed agent. |
| `retry --issue 1` | Reactivate eligible blocked work; then run `once`. |
| `run --role developer --task "..." --workspace /path` | Run a separate local agent task; also supports `reviewer`. |

A command finishing normally does not mean the Issue succeeded. Check `status`: the expected final state is `READY_FOR_HUMAN_REVIEW`. A problem leaves it `BLOCKED`, with a reason.

Status returns `stop_requested`, `agent_running` and an `issues` list. Stopping leaves the Issue at its saved stage so it can resume. `agent_running` is `true` for a verified recorded agent, `false` when no agent record exists, and `null` when the record cannot be verified. These fields do not indicate whether the factory itself is running.

If GitHub shows a different PR commit after a push, the factory checks again after 5 seconds and once more after another 5 seconds. A mismatch that remains blocks the Issue. Closed PRs or changed branch names block immediately.

## Settings and saved data

[.env.example](.env.example) lists the settings: target repository, base branch, separate agent providers, required CI checks, timeouts and retry limits. Only the `codex` provider is implemented.

For a temporary demo, `ALLOW_MISSING_CI=true` skips CI only when there are no check runs and no workflow YAML files on the base branch. It does not ignore failed or pending checks. Reviews carry a **CI SKIPPED** warning. Restore `false` when CI is ready.

`FACTORY_RUNTIME` holds saved state, working copies, `audit.jsonl` and reports such as `runs/issue-1/pr-2/review-1.json`. Its default is `~/.software-factory/m1`.

Keep the same runtime to resume interrupted work and avoid duplicate operations. Do not delete state to force a restart. Back it up before upgrades.

## Safety and limits

Developer uses `workspace-write`; Reviewer starts fresh with `read-only` access. The orchestrator handles GitHub authentication and does not forward token variables to agents. Sensitive file changes, suspected secrets and declared safety refusals block publication.

**Local sandboxing does not fully isolate credentials.** Agents may read files accessible to the same macOS user. Prompts and simple secret checks cannot catch every attack. Use controlled Issues and limited GitHub permissions.

## Tests and code

```bash
python -m unittest discover -v
```

Tests use fakes and mocks: no real model calls or GitHub writes.

`workflow.py` coordinates the steps; `agents/` runs providers; `git_manager.py` and `github_client.py` handle repositories; review, state, audit and security modules keep results and enforce local checks.
