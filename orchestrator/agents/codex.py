"""Connect the shared agent interface to the local Codex runner."""

from ..agent_runner import AgentRunner
from ..config import Config


class CodexProvider:
    """Pass agent requests to the shared Codex runner."""

    name = "codex"

    def __init__(self, config, audit, stop):
        self.runner = AgentRunner(
            Config(config.agent_timeout, audit.path),
            stop=stop,
            pid_file=config.runtime / "agent-process.json",
        )

    def check(self):
        self.runner.check()

    def run(self, request):
        return self.runner.run(
            request.role,
            request.task,
            request.workspace,
            request.ticket_id,
            run_id=request.run_id,
            timeout=request.timeout,
            output_schema=request.output_schema,
        )
