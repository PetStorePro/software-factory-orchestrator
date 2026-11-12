# The Software Factory

A project that turns
GitHub Issues into tested, independently AI-reviewed Pull Requests. A local Python
process on macOS coordinates Developer and Reviewer agents through the installed
Codex CLI, using existing ChatGPT authentication.

**The factory stops at `READY_FOR_HUMAN_REVIEW`. It never merges or deploys.**
Human review, merge, production approval and deployment belong to the target
Petstore repository and GitHub. A real human-approved production deployment is
still required to complete M1; a reviewed PR alone does not complete it.

## Workflow and responsibilities

```text
Oldest ai-ready Issue → feature branch from latest main → Developer → commit/PR
       → GitHub Actions CI → fresh, read-only Reviewer → human handoff
              ↑ errors          │ findings
              └──── Developer repair in the same PR ──┘
```

The orchestrator processes one Issue and runs one agent at a time. It handles
branches, commits, pushes, PRs, CI results, local feedback and COMMENT reviews.
CI failures go back to the Developer with available error summaries/annotations.
Review findings are saved locally and drive the repair loop without fetching PR
comments. Each fix is checked by CI and a fresh independent reviewer.

Only current-commit CI success plus an `approved` review allows the final positive
COMMENT and the `READY_FOR_HUMAN_REVIEW` label/state. AI approval is advisory; it is
never a formal GitHub APPROVE. The Issue stays open, the factory moves to the next
Issue, and a human takes over. Blocked Issues also release the queue slot.

| Orchestrator | Petstore / GitHub |
| --- | --- |
| Issue queue, agents, feature commits/PRs, repair loops | Application and automated tests |
| Observe configured CI checks on the current SHA | CI/security workflows, specialized secret scanning, dependency checks |
| Independent review comments and human handoff | Branch protection, required human PR reviews and merge |
| Local audit and safe recovery | Protected production Environment, secrets, human approval and deployment |

## Architecture

- `workflow.py`: the serial workflow and bounded CI/review repair loops.
- `agents/` and `agent_runner.py`: small provider contract and shared Codex runner.
  Developer/reviewer providers are selected independently; only Codex is implemented.
- `git_manager.py`: host-owned Git operations and clean source exports without `.git`.
- `github_client.py`, `review_manager.py`: GitHub CLI/REST, validated JSON reviews,
  inline comments on changed lines, and publication receipts.
- `state_manager.py`, `audit_logger.py`, `process.py`: atomic JSON checkpoints,
  local execution lock, audit records, timeouts and process-group cancellation.
- `security.py`: local protected-path and heuristic secret checks before publication.

## Setup and configuration

Install Python 3.14, Git, `gh` and a Codex CLI supporting the documented execution
flags. Authenticate `gh` for the target repository and Codex with ChatGPT normally;
your existing local login is reused. Do not put tokens in configuration or prompts.

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m unittest discover -v
gh auth status
codex login status
cp .env.example .env
# Edit the trusted local .env, then export it:
set -a
source .env
set +a
```

`.env` is ignored by Git and is not loaded automatically. Never source a target
repository's configuration. Run commands from this repository root.

| Variable | Default / purpose |
| --- | --- |
| `GITHUB_REPOSITORY` | Required `owner/repository`, such as `PetStorePro/petstore` |
| `GITHUB_ISSUE_LABEL` | `ai-ready` |
| `AI_DEVELOPER_PROVIDER`, `AI_REVIEWER_PROVIDER` | Independently selected; `codex` only for now |
| `REQUIRED_CHECKS` | `ci,security`; actual, unique GitHub Actions check names |
| `FACTORY_RUNTIME` | `~/.software-factory/m1`, outside this checkout |
| `POLL_INTERVAL_SECONDS` | `30` |
| `AGENT_TIMEOUT_SECONDS`, `CI_TIMEOUT_SECONDS` | `600`, `1800` |
| `MAX_CI_FIX_ATTEMPTS`, `MAX_REVIEW_CYCLES` | `2`, `3` total per Issue |

The target must have `main` and these labels: `ai-ready` (or the configured
eligibility label), `ai-in-progress`, `ai-review`, `ai-blocked`, and
`READY_FOR_HUMAN_REVIEW`. The factory does not configure GitHub protections.

## Commands

```bash
python -m orchestrator.main preflight
python -m orchestrator.main once --dry-run
python -m orchestrator.main once
python -m orchestrator.main watch
python -m orchestrator.main status
python -m orchestrator.main stop
python -m orchestrator.main retry --issue 12
```

Preflight checks repository access, default `main`, labels, local Codex flags and
login status. Dry-run also lists eligible Issues. Both perform read-only GitHub
requests and no model calls; neither certifies GitHub branch/environment security.
`once` resumes one interrupted active workflow before claiming new work; `watch`
polls continuously. Use the same persistent runtime directory for the queue.
`stop` requests cancellation and stops the recorded agent process group; Ctrl-C
also requests shutdown. A local lock prevents concurrent factory/agent CLI runs.

`retry` resumes a blocked checkpoint after investigation, without resetting
limits or bypassing checks. Potential-secret rejections require a new sanitized
Issue. Unsupported older checkpoints block rather than guessing how to resume;
back up runtime data before upgrading. CLI exit 0 means the attempt/control command
finished (possibly blocked); inspect `status` for the Issue result. Exit 2 indicates
configuration/control failure.

A standalone task remains available:

```bash
mkdir -p /tmp/factory-demo
python -m orchestrator.main run --role developer \
  --task 'Create a README file' --workspace /tmp/factory-demo
```

Use `--role reviewer` for read-only access. Its exit codes are 0 completed, 1
agent failure, 2 invalid input, 124 timeout, and 127 missing executable. A successful
process alone does not verify correctness (`task_verified=false`).

## Feedback, recovery and security

Runtime holds `states/`, host-only `checkouts/`, agent `workspaces/`, and
`audit.jsonl`. Reviews are immutable files such as
`runs/issue-12/pr-34/review-1.json`, tied to the exact commit SHA. Publication receipts
and stable review IDs prevent routine duplicate comments after interruption.
Commit markers and branch/PR lookup prevent duplicate commits/PRs. Interrupted
agent work restarts from committed source; do not erase state to reclaim a branch.

Developer uses `workspace-write`; Reviewer uses `read-only`, fresh context and
validated JSON output. Agents receive no forwarded GitHub/API tokens or SSH agent
variables. The orchestrator performs authenticated Git operations; source exports
exclude Git metadata and authorization instructions. Protected paths and suspected
secrets block publication. All Issue/CI/review content remains untrusted input.

**Local sandboxing is not credential isolation.** Standard Codex sandboxes may
read files outside the workspace, including credential stores belonging to the
same macOS user. Filtering environment variables does not prevent that. Use only
controlled tasks and least-privilege target credentials; stronger isolation needs
a separate execution identity/machine that this project does not provision.
