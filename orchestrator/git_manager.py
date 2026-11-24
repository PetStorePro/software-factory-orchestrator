"""Manage Git copies and publish checked changes from agent work folders."""

import hashlib
import json
import os
from pathlib import Path
import shutil

from .process import execute, FactoryError
from .security import safe_path, scan_secret, validate_changes
from .state_manager import atomic_json

IGNORED_DIRS = {
    "node_modules",
    ".venv",
    "__pycache__",
    "dist",
    "build",
    ".next",
    "coverage",
    "target",
}
HIDDEN_CONTROL = {".codex", ".agents", "AGENTS.md"}


class GitManager:
    """Keep Git operations separate from agent edits and check changes before publishing."""

    def __init__(self, config, audit, stop=lambda: False):
        self.config, self.audit, self.stop = config, audit, stop
        # Disable user/system hooks, aliases, filters, SSH commands and signing.
        self.env = {
            k: os.environ[k]
            for k in ("HOME", "PATH", "GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR")
            if k in os.environ
        }
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_ATTR_NOSYSTEM="1",
        )

    def paths(self, state):
        return (
            self.config.runtime / "checkouts" / f"issue-{state.issue}",
            self.config.runtime / "workspaces" / f"issue-{state.issue}",
        )

    def git(self, state, *args, cwd=None):
        """Run a Git command with controlled settings and record its result in the audit log."""

        self.audit.write(
            run_id=state.run_id,
            ticket_id=f"issue-{state.issue}",
            actor="orchestrator",
            action="git_" + args[0],
            status="started",
        )
        result = execute(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=!gh auth git-credential",
                "-c",
                "protocol.file.allow=never",
                "-c",
                "protocol.ext.allow=never",
                *args,
            ],
            cwd=cwd or self.paths(state)[0],
            env=self.env,
            timeout=180,
            stop=self.stop,
        )
        self.audit.write(
            run_id=state.run_id,
            ticket_id=f"issue-{state.issue}",
            actor="orchestrator",
            action="git_" + args[0],
            status="success",
        )
        return result

    def prepare(self, state):
        """Fetch the base branch and create or reuse the feature branch owned by this Issue."""

        if (
            state.branch != f"ai/issue-{state.issue}"
            or state.branch == state.base_branch
        ):
            raise FactoryError("unsafe_feature_branch")
        checkout, _ = self.paths(state)
        checkout.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        url = f"https://github.com/{self.config.repository}.git"
        if not checkout.exists():
            self.git(
                state,
                "clone",
                "--no-checkout",
                "--no-tags",
                "--template=",
                "--",
                url,
                str(checkout),
                cwd=checkout.parent,
            )
        remote = self.git(state, "remote", "get-url", "origin").strip()
        if remote != url:
            raise FactoryError("unexpected_git_remote")
        self.git(
            state,
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/{state.base_branch}:refs/remotes/origin/{state.base_branch}",
        )
        # Reuse a local branch only as part of the saved Issue workflow.
        refs = self.git(
            state, "for-each-ref", "--format=%(refname)", f"refs/heads/{state.branch}"
        ).strip()
        if not refs:
            remote_branch = self.git(
                state, "ls-remote", "--heads", "origin", f"refs/heads/{state.branch}"
            ).strip()
            if remote_branch:
                # Do not take over a remote branch when this runtime has no local copy.
                raise FactoryError("feature_branch_already_exists_without_local_state")
            self.git(
                state, "checkout", "-b", state.branch, f"origin/{state.base_branch}"
            )
        else:
            self.git(state, "checkout", state.branch)
        return self.git(state, "rev-parse", "HEAD").strip()

    def head(self, state):
        return self.git(state, "rev-parse", "HEAD").strip()

    def _tracked(self, state):
        entries = self.git(state, "ls-files", "--stage", "-z").split("\0")
        result = {}
        checkout, _ = self.paths(state)
        for entry in entries:
            if not entry:
                continue
            details, name = entry.split("\t", 1)
            mode = details.split()[0]
            safe_path(name)
            if mode not in ("100644", "100755"):
                raise FactoryError("symlinks_or_submodules_not_supported")
            if any(part in HIDDEN_CONTROL for part in Path(name).parts):
                continue
            path = checkout / name
            if path.is_symlink() or not path.is_file():
                raise FactoryError("unsafe_checkout_file")
            content = path.read_bytes()
            if len(content) > 2_000_000:
                raise FactoryError("source_file_too_large")
            scan_secret(content)
            result[name] = content
        if sum(map(len, result.values())) > 32_000_000 or len(result) > 10000:
            raise FactoryError("source_export_too_large")
        return result

    def export(self, state):
        """Rebuild the agent folder from a clean commit and save content hashes for comparison."""

        _, source = self.paths(state)
        if (
            self.head(state) != state.sha
            or self.git(state, "status", "--porcelain", "--untracked-files=all").strip()
        ):
            raise FactoryError("export_requires_clean_expected_commit")
        if source.exists():
            shutil.rmtree(source)
        source.mkdir(parents=True, mode=0o700)
        tracked = self._tracked(state)
        for name, content in tracked.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            original = self.paths(state)[0] / name
            path.chmod(0o755 if original.stat().st_mode & 0o111 else 0o644)
            # No executable host permissions are imported back from the agent.
        atomic_json(
            self.config.runtime / "manifests" / f"issue-{state.issue}.json",
            {name: hashlib.sha256(data).hexdigest() for name, data in tracked.items()},
        )
        return source

    def workspace(self, state):
        return self.paths(state)[1]

    def changes(self, state):
        """Compare agent files with the saved hashes and validate additions, edits and deletions."""

        source = self.workspace(state)
        baseline = json.loads(
            (
                self.config.runtime / "manifests" / f"issue-{state.issue}.json"
            ).read_text()
        )
        current = {}
        total = 0
        for directory, folders, files in os.walk(source, followlinks=False):
            parent = Path(directory)
            for folder in folders:
                if (parent / folder).is_symlink():
                    raise FactoryError("agent_created_symlink")
            folders[:] = [folder for folder in folders if folder not in IGNORED_DIRS]
            for name in files:
                path = parent / name
                if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                    raise FactoryError("agent_created_unsafe_file")
                relative = path.relative_to(source).as_posix()
                safe_path(relative)
                if path.stat().st_size > 2_000_000:
                    raise FactoryError("agent_file_too_large")
                data = path.read_bytes()
                total += len(data)
                if total > 32_000_000 or len(current) >= 10000:
                    raise FactoryError("agent_output_too_large")
                current[relative] = data
        changes = {
            name: data
            for name, data in current.items()
            if hashlib.sha256(data).hexdigest() != baseline.get(name)
        }
        changes.update({name: None for name in baseline.keys() - current.keys()})
        validate_changes(changes)
        return changes

    def commit(self, state):
        """Import valid edits and create a commit, or reuse one already made for this operation."""

        operation = f"{state.run_id}-{state.iteration}"
        message = self.git(state, "log", "-1", "--format=%B")
        if f"Factory-Operation: {operation}" in message:
            return self.head(state)  # Crash after commit, before durable checkpoint.
        if self.head(state) != state.sha:
            raise FactoryError("unexpected_local_commit")
        changes = self.changes(state)
        if not changes:
            raise FactoryError("agent_produced_no_valid_changes")
        checkout, _ = self.paths(state)
        for name, content in sorted(
            changes.items(), key=lambda item: item[1] is not None
        ):
            path = checkout / name
            if path.is_symlink() or any(
                p.is_symlink() for p in path.parents if p != checkout.parent
            ):
                raise FactoryError("unsafe_import_path")
            if content is None:
                path.unlink(missing_ok=True)
            else:
                if path.is_dir():
                    path.rmdir()  # Only an empty directory may be replaced by a file.
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        staged = set(
            self.git(state, "diff", "--cached", "--name-only", "-z")
            .strip("\0")
            .split("\0")
        ) - {""}
        if not staged.issubset(changes):
            raise FactoryError("unexpected_staged_files")
        self.git(state, "add", "--all", "--", *changes.keys())
        self.git(
            state,
            "-c",
            "user.name=Software Factory",
            "-c",
            "user.email=software-factory@users.noreply.github.com",
            "commit",
            "-m",
            f"Implement issue #{state.issue}\n\nFactory-Operation: {operation}",
        )
        return self.head(state)

    def push(self, state):
        """Push only the expected feature branch without forcing remote changes."""

        branch = f"ai/issue-{state.issue}"
        if state.branch != branch or branch == state.base_branch:
            raise FactoryError("unsafe_push_target")
        # Push only this feature branch and never force changes on GitHub.
        self.git(state, "push", "origin", f"HEAD:refs/heads/{branch}")
