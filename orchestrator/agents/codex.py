"""Thin local Codex adapter; all subprocess behavior lives in AgentRunner."""

from ..agent_runner import AgentRunner
from ..config import Config


class CodexProvider:
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
