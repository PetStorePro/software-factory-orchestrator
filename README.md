# The Software Factory — Milestone 0

A standard library Python 3.14 orchestrator for local Codex CLI developer and
independent reviewer runs. It uses existing ChatGPT authentication; this project
introduces no API keys, API SDK, GitHub integration, or paid services. Codex still
contacts OpenAI and uses your account's existing access and limits.

## Setup

Install Python 3.14 and authenticate Codex CLI with ChatGPT separately. From this
repository root:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python --version
codex --version
codex exec --help
python -m unittest discover -v
```

## Run

Use a separate disposable workspace outside this checkout:

```bash
mkdir -p /tmp/software-factory-demo
python -m orchestrator.main run --role developer \
  --task "Create a simple README file describing this demo" \
  --workspace /tmp/software-factory-demo --timeout 120 --ticket-id SSDLC-0
python -m orchestrator.main run --role reviewer \
  --task "Review README.md for clarity and report issues without editing files" \
  --workspace /tmp/software-factory-demo --timeout 120
```

Inspect the workspace and diff yourself after execution. A successful execution
**does not certify correctness**. The CLI prints a concise JSON summary with
`task_verified: false`. The Python `AgentResult` also provides captured stdout,
stderr, return code, session ID, usage and duration; callers must treat this
output as untrusted and potentially sensitive. It is never automatically
executed, rendered in the terminal, or written to audit logs.

Options: `--timeout` is a positive finite number of seconds (default 300);
`--ticket-id` is optional metadata; `--audit-log` overrides
`~/.software-factory/audit.jsonl` and must remain outside the workspace.
Exit codes: 0 execution completed, 1 process/event failure, 2 invalid input or
configuration/prompt/audit failure, 124 timeout, 127 missing executable.
Do not include secrets in tasks, paths or ticket IDs. CLI task arguments are
visible in shell history/process listings; the child receives its prompt on stdin.

## Architecture

- `config.py`: fixed role/sandbox mapping, timeout and canonical workspace checks.
- `agent_runner.py`: trusted prompts, subprocess lifecycle, POSIX process-group
  timeout cleanup and JSON event parsing into an `AgentResult`.
- `audit_logger.py`: metadata-only JSONL with UTC timestamp, shared run ID,
  actor, action, status, workspace, duration, ticket, session, usage and error code.
- `main.py`: CLI validation, safe execution summaries and meaningful exit codes.
- `prompts/`: developer implementation and independent reviewer instructions.
- `tests/`: mocked subprocess tests, without real model calls or credential access.

Developer uses `workspace-write`; reviewer uses `read-only`. Both disable
approval escalation (`never`), shared daemon use, user configuration and external
execpolicy rules. They use ephemeral sessions and force ChatGPT login. Developer
network access and extra temporary-directory write access are explicitly disabled.
The runner requires completion metadata and treats failure events as failures even
if the process exits zero. Unknown/malformed JSON events are ignored. Session IDs
must be UUIDs, and token counts must be nonnegative integers.

## Security boundaries and limitations

A prompt is **not a security boundary**. Codex's operating-system sandbox controls
local tool writes. The reviewer cannot modify source through sandboxed commands.
The orchestrator never executes output, invokes Git operations, commits, pushes,
merges or deploys. Prompts also prohibit those actions, but prompts alone cannot
prohibit every action a developer could attempt inside its writable workspace.
Human inspection remains required.

Existing workspaces are canonicalized (including symlinks and `..`). Overlap with
this orchestrator checkout, its prompts/policies, default Codex authentication
storage or audit directory is rejected. The checkout cannot be used as a target.
Keep the checkout, interpreter, executable PATH and audit storage trusted. Path
validation does not defend against concurrent hostile symlink/mount changes or
privileged local users.

The child environment allowlists only HOME, PATH and Windows system directory
variables. Arbitrary inherited GitHub tokens, API keys, proxies and CODEX_HOME
settings are not forwarded. This code never reads authentication files; Codex
itself uses its already authenticated ChatGPT installation.
**Sandbox write restrictions are not secret isolation**: reads outside the
workspace may be allowed, including user files. Never place secrets in demo
workspaces. Use a dedicated OS account or externally isolated environment when
strong confidentiality or hostile repository isolation is needed.

User configuration is ignored, but project configuration, hooks, plugins and MCP
integrations can have independent privileges. Do not enable integrations or trust
project configuration in untrusted repositories. This MVP is for controlled
university demo workspaces, not adversarial repository execution. The installed
CLI and authentication backend remain trusted. Investigate sandbox failures;
never bypass them. Read-only applies to source/tool writes, not the CLI's own
trusted runtime/authentication bookkeeping. Blocking tool network access does not
block the model connection.

Audit logs contain fixed error codes rather than exception text, prompts, agent
output or environment dumps. Files are created with restrictive POSIX permissions.
A failed start log prevents execution; a failed end log produces a non-success CLI
exit even if work already occurred. Logs are outside the writable workspace but
are not cryptographically tamper-proof. Paths/ticket metadata may be sensitive.
Output is buffered in memory, so extremely noisy processes can consume memory.
Timeout kills the POSIX process group but cannot undo changes or kill processes
that deliberately escaped the group. No sandbox integration tests or real model
calls are made by the automated suite.

See official [non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode)
and [Codex security](https://learn.chatgpt.com/docs/security) documentation.

## Implemented and planned

Implemented: local role-based execution, sandbox selection, validated inputs,
timeouts, structured results, safe summaries, audit logs and mocked tests.

Next milestone: GitHub Issues integration. The eventual workflow is Issue →
Developer → Pull Request → GitHub Actions CI → independent Reviewer → Human
Approval → Production Deployment. GitHub access, automatic PR creation, CI handoff,
human approval enforcement and deployment are all outside Milestone 0.
