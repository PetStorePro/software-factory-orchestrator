"""Run a single sandboxed Codex process; output is untrusted data."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from uuid import UUID, uuid4

from .audit_logger import AuditLogger
from .config import Config, ROOT, SANDBOXES, validate_workspace


@dataclass
class AgentResult:
    run_id: str
    status: str
    duration: float
    returncode: int | None = None
    session_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    task_verified: bool = False


def parse_events(output: str) -> tuple[str | None, dict[str, int], bool, bool]:
    session, usage, completed, failed = None, {}, False, False
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started":
            try:
                session = str(UUID(event.get("thread_id")))
            except (ValueError, TypeError, AttributeError):
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


class AgentRunner:
    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.audit = AuditLogger(self.config.audit_path)

    def run(self, role: str, task: str, workspace: Path, ticket_id: str | None = None) -> AgentResult:
        if role not in SANDBOXES:
            raise ValueError("Unsupported role")
        if not task.strip():
            raise ValueError("Task must not be empty")
        if ticket_id is not None and (len(ticket_id) > 80 or not all(c.isalnum() or c in "-_." for c in ticket_id)):
            raise ValueError("Ticket ID must contain at most 80 letters, digits, dots, underscores or hyphens")
        workspace = validate_workspace(Path(workspace), self.audit.path)
        instructions = (ROOT / "prompts" / f"{role}.md").read_text(encoding="utf-8")
        command = ["codex", "--no-daemon", "--ask-for-approval", "never", "exec",
                   "--ignore-user-config", "--ignore-rules", "--ephemeral", "--json",
                   "--color", "never", "--sandbox", SANDBOXES[role],
                   "--cd", str(workspace), "--skip-git-repo-check"]
        for setting in ("sandbox_workspace_write.network_access=false",
                        "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                        "sandbox_workspace_write.exclude_slash_tmp=true",
                        'shell_environment_policy.inherit="none"',
                        'forced_login_method="chatgpt"'):
            command.extend(["-c", setting])
        command.append("-")
        # No inherited credentials, proxies, CODEX_HOME overrides, or shell startup variables.
        env = {key: os.environ[key] for key in ("HOME", "PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
        started = time.monotonic()
        result = AgentResult(str(uuid4()), "failed", 0)
        metadata = dict(run_id=result.run_id, actor=role, workspace=str(workspace), ticket_id=ticket_id)
        self.audit.write(**metadata, action="execution_start", status="started", duration=0,
                         session_id=None, error=None)
        try:
            with subprocess.Popen(command, cwd=workspace, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", errors="replace",
                                  start_new_session=True) as process:
                try:
                    result.stdout, result.stderr = process.communicate(
                        instructions + "\n\nTask:\n" + task, timeout=self.config.timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    result.stdout, result.stderr = process.communicate()
                    result.status, result.error = "timeout", "execution_timeout"
                result.returncode = process.returncode
        except FileNotFoundError:
            result.error = "executable_not_found"
        except OSError:
            result.error = "process_error"
        result.session_id, result.usage, completed, failed = parse_events(result.stdout)
        if result.error is None:
            if result.returncode != 0:
                result.error = "nonzero_exit"
            elif failed:
                result.error = "agent_reported_failure"
            elif not completed:
                result.error = "missing_completion_event"
            else:
                result.status = "success"
        result.duration = time.monotonic() - started
        self.audit.write(**metadata, action="execution_end", status=result.status,
                         duration=result.duration, session_id=result.session_id,
                         error=result.error, returncode=result.returncode, usage=result.usage,
                         task_verified=False)
        return result
