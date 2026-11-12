"""One local Codex execution path for both the provider and the standalone CLI."""

import json
import os
import time
from uuid import UUID, uuid4

from .agents.base import ProviderResult
from .audit_logger import AuditLogger
from .config import Config, ROOT, SANDBOXES, validate_workspace
from .process import execute, FactoryError
from .state_manager import atomic_json


def events(output):
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def parse_events(output):
    session, usage, completed, failed = None, {}, False, False
    for event in events(output):
        kind = event.get("type")
        if kind == "thread.started":
            try:
                session = str(UUID(event.get("thread_id")))
            except ValueError, TypeError, AttributeError:
                pass
        elif kind == "turn.completed":
            completed = True
            raw = event.get("usage")
            if isinstance(raw, dict):
                for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                    value = raw.get(key)
                    if type(value) is int and value >= 0:
                        usage[key] = usage.get(key, 0) + value
        elif kind in ("turn.failed", "error"):
            failed = True
    return session, usage, completed, failed


def final_message(output):
    text = ""
    for event in events(output):
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            text = item["text"]
    return text


class AgentRunner:
    def __init__(self, config=None, *, stop=lambda: False, pid_file=None):
        self.config = config or Config()
        self.audit = AuditLogger(self.config.audit_path)
        self.stop, self.pid_file = stop, pid_file

    def environment(self):
        # Reuse local ChatGPT authentication, without forwarding GitHub/API tokens or SSH agents.
        return {
            key: os.environ[key]
            for key in ("HOME", "PATH", "CODEX_HOME")
            if key in os.environ
        }

    def check(self):
        help_text = execute(["codex", "exec", "--help"], env=self.environment())
        if not all(
            flag in help_text
            for flag in ("--sandbox", "--json", "--ignore-user-config")
        ):
            raise FactoryError("codex_missing_required_flags")
        execute(
            ["codex", "-c", 'forced_login_method="chatgpt"', "login", "status"],
            env=self.environment(),
        )

    def run(
        self,
        role,
        task,
        workspace,
        ticket_id=None,
        *,
        run_id=None,
        timeout=None,
        output_schema=None,
    ):
        if role not in SANDBOXES or not task.strip():
            raise ValueError("Supported role and nonempty task required")
        if ticket_id is not None and (
            len(ticket_id) > 80 or not all(c.isalnum() or c in "-_." for c in ticket_id)
        ):
            raise ValueError("Invalid ticket ID")
        workspace = validate_workspace(workspace, self.audit.path)
        args = [
            "codex",
            "--no-daemon",
            "--ask-for-approval",
            "never",
            "exec",
            "--strict-config",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--json",
            "--color",
            "never",
            "--cd",
            str(workspace),
            "--skip-git-repo-check",
            "--sandbox",
            SANDBOXES[role],
        ]
        for setting in (
            'forced_login_method="chatgpt"',
            'shell_environment_policy.inherit="none"',
            "sandbox_workspace_write.network_access=false",
            "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            "sandbox_workspace_write.exclude_slash_tmp=true",
        ):
            args += ["-c", setting]
        if output_schema:
            schema = self.audit.path.parent / "schemas" / "review.json"
            atomic_json(schema, output_schema)
            args += ["--output-schema", str(schema)]
        args.append("-")
        prompt = (
            (ROOT / "prompts" / f"{role}.md").read_text()
            + "\n\nUntrusted task data:\n"
            + task
        )
        result = ProviderResult("failed", run_id=run_id or str(uuid4()))
        metadata = dict(
            run_id=result.run_id,
            actor=role,
            workspace=str(workspace),
            ticket_id=ticket_id,
        )
        started = time.monotonic()
        self.audit.write(
            **metadata, action="execution_start", status="started", duration=0
        )
        try:
            result.stdout = execute(
                args,
                cwd=workspace,
                env=self.environment(),
                data=prompt,
                timeout=timeout if timeout is not None else self.config.timeout,
                stop=self.stop,
                pid_file=self.pid_file,
            )
            result.returncode = 0
        except FactoryError as error:
            result.returncode = getattr(error, "returncode", None)
            result.stdout, result.stderr = getattr(error, "stdout", ""), getattr(
                error, "stderr", ""
            )
            result.error = {
                "command_failed": "nonzero_exit",
                "command_timeout": "execution_timeout",
            }.get(str(error), str(error))
            result.status = (
                "timeout" if result.error == "execution_timeout" else "failed"
            )
        result.session_id, result.usage, completed, failed = parse_events(result.stdout)
        if result.error is None:
            if failed:
                result.error = "agent_reported_failure"
            elif not completed:
                result.error = "missing_completion_event"
            else:
                result.status = "success"
        result.text = final_message(result.stdout)
        result.duration = time.monotonic() - started
        self.audit.write(
            **metadata,
            action="execution_end",
            status=result.status,
            error=result.error,
            session_id=result.session_id,
            duration=result.duration,
            usage=result.usage,
            returncode=result.returncode,
            task_verified=False,
        )
        return result
