"""Read local settings and check that work folders stay separate from control files."""

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
SANDBOXES = {"developer": "workspace-write", "reviewer": "read-only"}


@dataclass(frozen=True)
class Config:
    """Store the timeout and audit path for a local agent task."""

    timeout: float = 300
    audit_path: Path = field(
        default_factory=lambda: Path.home() / ".software-factory/audit.jsonl"
    )

    def __post_init__(self):
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Timeout must be finite and positive")


def overlaps(a, b):
    return a.is_relative_to(b) or b.is_relative_to(a)


def validate_workspace(workspace, audit_path):
    """Require an existing work folder separate from policies, login files and audit logs."""

    workspace = Path(workspace).expanduser().resolve(strict=True)
    auth = (
        Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        .expanduser()
        .resolve()
    )
    if (
        not workspace.is_dir()
        or overlaps(workspace, ROOT)
        or overlaps(workspace, auth)
        or audit_path.resolve().is_relative_to(workspace)
    ):
        raise ValueError(
            "Workspace must be a directory separate from policies, authentication and logs"
        )
    return workspace


@dataclass(frozen=True)
class FactoryConfig:
    """Store and validate settings for the Issue processing workflow."""

    repository: str
    base_branch: str = "main"
    runtime: Path = field(default_factory=lambda: Path.home() / ".software-factory/m1")
    issue_label: str = "ai-ready"
    developer_provider: str = "codex"
    reviewer_provider: str = "codex"
    required_checks: tuple[str, ...] = ("ci", "security")
    allow_missing_ci: bool = False
    poll_interval: int = 30
    agent_timeout: int = 600
    ci_timeout: int = 1800
    max_ci_fixes: int = 2
    max_review_cycles: int = 3

    def __post_init__(self):
        if (
            not re.fullmatch(
                r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*",
                self.base_branch,
            )
            or ".." in self.base_branch
            or any(
                part.endswith((".", ".lock")) for part in self.base_branch.split("/")
            )
        ):
            raise ValueError("BASE_BRANCH must be a valid branch name")
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", self.repository
        ):
            raise ValueError("GITHUB_REPOSITORY must be owner/repository")
        if self.repository.lower() == "petstorepro/software-factory-orchestrator":
            raise ValueError("The orchestrator cannot target itself")
        if (
            not self.required_checks
            or self.max_ci_fixes < 0
            or min(
                self.poll_interval,
                self.agent_timeout,
                self.ci_timeout,
                self.max_review_cycles,
            )
            <= 0
        ):
            raise ValueError(
                "Checks, positive timeouts and bounded retry settings are required"
            )
        runtime = self.runtime.expanduser().resolve()
        if overlaps(runtime, ROOT):
            raise ValueError("Runtime must be outside the orchestrator checkout")
        object.__setattr__(self, "runtime", runtime)

    @classmethod
    def from_env(cls):
        values = {
            name: os.environ[env]
            for name, env in (
                ("repository", "GITHUB_REPOSITORY"),
                ("base_branch", "BASE_BRANCH"),
                ("issue_label", "GITHUB_ISSUE_LABEL"),
                ("developer_provider", "AI_DEVELOPER_PROVIDER"),
                ("reviewer_provider", "AI_REVIEWER_PROVIDER"),
            )
            if env in os.environ
        }
        values["repository"] = values.get("repository", "")
        allow_missing_ci = os.environ.get("ALLOW_MISSING_CI", "false").lower()
        if allow_missing_ci not in ("true", "false"):
            raise ValueError("ALLOW_MISSING_CI must be true or false")
        values["allow_missing_ci"] = allow_missing_ci == "true"
        if "FACTORY_RUNTIME" in os.environ:
            values["runtime"] = Path(os.environ["FACTORY_RUNTIME"])
        if "REQUIRED_CHECKS" in os.environ:
            values["required_checks"] = tuple(
                s.strip() for s in os.environ["REQUIRED_CHECKS"].split(",") if s.strip()
            )
        for name, env in (
            ("poll_interval", "POLL_INTERVAL_SECONDS"),
            ("agent_timeout", "AGENT_TIMEOUT_SECONDS"),
            ("ci_timeout", "CI_TIMEOUT_SECONDS"),
            ("max_ci_fixes", "MAX_CI_FIX_ATTEMPTS"),
            ("max_review_cycles", "MAX_REVIEW_CYCLES"),
        ):
            if env in os.environ:
                values[name] = int(os.environ[env])
        return cls(**values)
