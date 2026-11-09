"""Command line entry point. Never render untrusted model text."""
import argparse
import json
from pathlib import Path

from .agent_runner import AgentRunner
from .config import Config, SANDBOXES


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Local Software Factory agent runner")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--role", choices=SANDBOXES, required=True)
    run.add_argument("--task", required=True)
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--timeout", type=float, default=300)
    run.add_argument("--audit-log", type=Path, default=Config().audit_path)
    run.add_argument("--ticket-id")
    args = parser.parse_args(argv)
    try:
        result = AgentRunner(Config(args.timeout, args.audit_log)).run(
            args.role, args.task, args.workspace, args.ticket_id)
    except (ValueError, OSError, RuntimeError):
        # Exception messages may contain sensitive paths or arbitrary filesystem data.
        print("Configuration, workspace, prompt, or audit storage error; execution cannot be trusted.")
        return 2
    print(json.dumps(dict(run_id=result.run_id, status=result.status,
                          duration=round(result.duration, 3), session_id=result.session_id,
                          error=result.error, task_verified=False)))
    return {"success": 0, "timeout": 124}.get(result.status, 127 if result.error == "executable_not_found" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
