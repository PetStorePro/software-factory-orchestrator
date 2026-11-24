"""Save local audit events without raw prompts or agent responses."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path


class AuditLogger:
    """Append audit events to a local JSONL file. Each line is one event."""

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()

    def write(self, **event):
        """Append one event with a UTC timestamp and request that it be saved to disk."""

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        event["timestamp"] = datetime.now(timezone.utc).isoformat()
        data = (json.dumps(event, ensure_ascii=True) + "\n").encode()
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
