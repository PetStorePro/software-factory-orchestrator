"""Local commands. Neither this CLI nor its GitHub adapter can integrate a PR."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal

from .agent_runner import AgentRunner
from .agents import create_provider
from .audit_logger import AuditLogger
from .config import Config, FactoryConfig, SANDBOXES
from .git_manager import GitManager
from .github_client import GitHubClient, LABELS
from .process import FactoryError, stop_agent
from .state_manager import StateManager, atomic_json, execution_lock
from .workflow import Workflow


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Serial Issue-to-reviewed-PR factory; human handoff only"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "once", "watch", "status", "stop"):
        command = commands.add_parser(name)
        if name in ("once", "watch"):
            command.add_argument("--dry-run", action="store_true")
    retry = commands.add_parser("retry")
    retry.add_argument("--issue", type=int, required=True)
    run = commands.add_parser("run", help="Standalone local agent task")
    run.add_argument("--role", choices=SANDBOXES, required=True)
    run.add_argument("--task", required=True)
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--timeout", type=float, default=300)
    run.add_argument("--audit-log", type=Path, default=Config().audit_path)
    run.add_argument("--ticket-id")
    args = parser.parse_args(argv)
    try:
        return standalone(args) if args.command == "run" else factory(args)
    except FactoryError, OSError, ValueError, KeyError, TypeError:
        print(
            "Operation blocked. Check configuration and audit records. Supported provider: codex."
        )
        return 2


def start(states):
    stop_agent(states.root / "agent-process.json")
    states.clear_stop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: states.request_stop())


def standalone(args):
    runtime = (
        Path(os.environ.get("FACTORY_RUNTIME", Path.home() / ".software-factory/m1"))
        .expanduser()
        .resolve()
    )
    states = StateManager(runtime, "")
    with execution_lock():
        start(states)
        result = AgentRunner(
            Config(args.timeout, args.audit_log),
            stop=states.stopped,
            pid_file=runtime / "agent-process.json",
        ).run(args.role, args.task, args.workspace, args.ticket_id)
    print(
        json.dumps(
            {
                key: getattr(result, key)
                for key in ("run_id", "status", "duration", "error", "task_verified")
            }
        )
    )
    return {"success": 0, "timeout": 124}.get(
        result.status, 127 if result.error == "executable_not_found" else 1
    )


def factory(args):
    config = FactoryConfig.from_env()
    states = StateManager(config.runtime, config.repository)
    audit = AuditLogger(config.runtime / "audit.jsonl")
    if args.command == "status":
        print(
            json.dumps(
                [
                    asdict(s) | {"issue_data": "[omitted]", "feedback": "[omitted]"}
                    for s in states.all()
                ],
                indent=2,
            )
        )
        return 0
    if args.command == "stop":
        states.request_stop()
        audit.write(actor="operator", action="stop_requested", status="requested")
        stop_agent(config.runtime / "agent-process.json")
        print("Stop requested; active agents also poll cancellation every second.")
        return 0
    with states.lock():
        if args.command == "retry":
            state = next(
                (
                    s
                    for s in states.all()
                    if s.issue == args.issue and s.status == "BLOCKED"
                ),
                None,
            )
            if (
                state is None
                or not state.resume_status
                or state.reason == "potential_secret_detected"
            ):
                raise FactoryError("issue_cannot_be_retried")
            state.status, state.reason = state.resume_status, None
            states.save(state)
            audit.write(
                actor="operator",
                action="retry_requested",
                issue=state.issue,
                run_id=state.run_id,
            )
            return 0
        live = args.command in ("once", "watch") and not args.dry_run
        if live:
            start(states)
        developer = create_provider(
            config.developer_provider, config, audit, states.stopped
        )
        reviewer = create_provider(
            config.reviewer_provider, config, audit, states.stopped
        )
        github = GitHubClient(
            config.repository, audit, states.stopped if live else lambda: False
        )
        info = github.repository_info()
        developer.check()  # Local help and existing login status only, no model call.
        missing = ((LABELS - {"ai-ready"}) | {config.issue_label}) - {
            label["name"] for label in github.listing("labels")
        }
        report = {
            "repository": config.repository,
            "main_is_default": info.get("default_branch") == "main",
            "missing_labels": sorted(missing),
            "live_allowed": not missing and info.get("default_branch") == "main",
        }
        atomic_json(config.runtime / "preflight.json", report)
        if not live or not report["live_allowed"]:
            if getattr(args, "dry_run", False):
                report["eligible_issues"] = [
                    issue["number"] for issue in github.issues(config.issue_label)
                ]
            print(json.dumps(report, indent=2))
            return 0 if report["live_allowed"] else 2
        workflow = Workflow(
            config,
            states,
            github,
            GitManager(config, audit, states.stopped),
            developer,
            reviewer,
            audit,
        )
        while not states.stopped():
            workflow.once()
            if args.command == "once":
                break
            workflow.pause()
        audit.write(actor="orchestrator", action="factory_stopped", status="stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
