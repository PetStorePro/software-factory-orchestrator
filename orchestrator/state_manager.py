"""Save Issue progress and prevent local factory processes from running together."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from .process import FactoryError

READY = "READY_FOR_HUMAN_REVIEW"
INACTIVE = {"BLOCKED", READY}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value):
    """Write a complete temporary JSON file before replacing the saved version."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def execution_lock():
    """Allow only one factory or local agent command per user on this machine."""
    path = Path(tempfile.gettempdir()) / f"software-factory-{os.getuid()}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as stream:
        if os.fstat(stream.fileno()).st_uid != os.getuid():
            raise FactoryError("untrusted_factory_lock")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FactoryError("another_factory_is_running") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@dataclass
class WorkflowState:
    """Hold the saved progress and identifiers for one Issue."""

    issue: int
    base_branch: str = "main"
    run_id: str = field(default_factory=lambda: str(uuid4()))
    status: str = "CLAIMED"
    branch: str = ""
    pr: int | None = None
    sha: str = ""
    review_cycle: int = 0
    ci_fixes: int = 0
    iteration: int = 0
    feedback: dict = field(default_factory=dict)
    review_file: str = ""
    ci_started: float = 0
    ci_status: str = "not_checked"
    issue_data: dict = field(default_factory=dict)
    urls: dict = field(default_factory=dict)
    reason: str | None = None
    resume_status: str | None = None
    updated_at: str = field(default_factory=utcnow)


class StateManager:
    """Read and save Issue progress and manage the local stop request."""

    def __init__(self, root: Path, repository: str):
        self.root = root
        self.repository = repository
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stop_file = root / "STOP"
        owner = root / "repository.json"
        if (
            repository
            and owner.exists()
            and json.loads(owner.read_text())["repository"] != repository
        ):
            raise FactoryError("runtime_belongs_to_another_repository")

    @contextmanager
    def lock(self):
        """Lock local execution and check that this runtime belongs to the selected repository."""

        with execution_lock():
            owner = self.root / "repository.json"
            if (
                owner.exists()
                and json.loads(owner.read_text())["repository"] != self.repository
            ):
                raise FactoryError("runtime_belongs_to_another_repository")
            if not owner.exists():
                atomic_json(owner, {"repository": self.repository})
            yield

    def save(self, state):
        state.updated_at = utcnow()
        atomic_json(self.root / "states" / f"issue-{state.issue}.json", asdict(state))

    def all(self):
        """Read saved Issue files and build states using the fields this version understands."""

        known = {f.name for f in fields(WorkflowState)}
        return [
            WorkflowState(
                **{k: v for k, v in json.loads(path.read_text()).items() if k in known}
            )
            for path in sorted((self.root / "states").glob("issue-*.json"))
        ]

    def stopped(self):
        return self.stop_file.exists()

    def request_stop(self):
        self.stop_file.touch(mode=0o600)

    def clear_stop(self):
        self.stop_file.unlink(missing_ok=True)
