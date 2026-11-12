from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class AgentRequest:
    role: str
    task: str
    workspace: Path
    timeout: float
    run_id: str
    ticket_id: str
    output_schema: dict | None = None


@dataclass
class ProviderResult:
    status: str
    text: str = ""
    session_id: str | None = None
    usage: dict = field(default_factory=dict)
    error: str | None = None
    run_id: str = ""
    duration: float = 0
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    task_verified: bool = False


class AgentProvider(Protocol):
    name: str

    def run(self, request: AgentRequest) -> ProviderResult: ...
    def check(self) -> None: ...
