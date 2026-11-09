"""Trusted configuration, never loaded from agent workspaces."""
from dataclasses import dataclass, field
from pathlib import Path
import math

ROOT = Path(__file__).resolve().parent.parent
SANDBOXES = {"developer": "workspace-write", "reviewer": "read-only"}


@dataclass(frozen=True)
class Config:
    timeout: float = 300
    audit_path: Path = field(default_factory=lambda: Path.home() / ".software-factory" / "audit.jsonl")

    def __post_init__(self):
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Timeout must be finite and positive")


def overlaps(a: Path, b: Path) -> bool:
    return a.is_relative_to(b) or b.is_relative_to(a)


def validate_workspace(workspace: Path, audit_path: Path) -> Path:
    workspace = workspace.expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("Workspace must be an existing directory")
    protected = (ROOT, Path.home() / ".codex", audit_path.expanduser().resolve().parent)
    if any(overlaps(workspace, path.resolve()) for path in protected):
        raise ValueError("Workspace overlaps orchestrator, authentication storage, or audit directory")
    return workspace
