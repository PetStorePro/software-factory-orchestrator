"""Publication policy enforced on byte content, never on agent assertions."""

import fnmatch
from pathlib import PurePosixPath
import re

from .process import FactoryError

PROTECTED = (
    ".github/**",
    ".git/**",
    ".git",
    ".codex/**",
    ".agents/**",
    "AGENTS.md",
    "**/AGENTS.md",
    "**/.codex/**",
    "**/.agents/**",
    "SECURITY.md",
    "**/SECURITY.md",
    "CODEOWNERS",
    "**/CODEOWNERS",
    "orchestrator/**",
    "prompts/**",
    "infra/**",
    "terraform/**",
    "deploy/**",
    "deployment/**",
    "Dockerfile*",
    "docker-compose*",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    ".gitmodules",
    ".gitattributes",
)
SECRET = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    rb"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    rb"AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{24,})\b|"
    rb'(?i:(?:password|secret|api[_-]?key|token)\s*[:=]\s*["\x27][^"\x27\s]{12,}["\x27])'
)


def safe_path(value):
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or any(ord(c) < 32 for c in value)
    ):
        raise FactoryError("unsafe_repository_path")
    return value


def scan_secret(data):
    if SECRET.search(data):
        raise FactoryError("potential_secret_detected")


def validate_changes(changes, extra=()):
    for name, content in changes.items():
        safe_path(name)
        if any(
            fnmatch.fnmatchcase(name.lower(), pattern.lower())
            for pattern in (*PROTECTED, *extra)
        ):
            raise FactoryError("protected_path_changed")
        if content is not None:
            scan_secret(content)
