"""Bounded host commands; diagnostics never include credentials or raw output."""

import os
import re
import signal
import subprocess
import time


class FactoryError(RuntimeError):
    """A safe, fixed diagnostic suitable for audit records."""


class CommandError(FactoryError):
    def __init__(self, returncode, stderr, stdout=""):
        super().__init__("command_failed")
        self.returncode = returncode
        self.stdout, self.stderr = stdout, stderr
        match = re.search(r"HTTP (\d{3})", stderr)
        self.http_status = int(match[1]) if match else None


class Stopped(FactoryError):
    pass


def execute(
    args, *, cwd=None, data=None, timeout=60, env=None, stop=None, pid_file=None
):
    started = time.monotonic()
    try:
        with subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        ) as process:
            first = True
            try:
                if pid_file:
                    from .state_manager import atomic_json

                    identity = process_identity(process.pid)
                    if not identity:
                        raise FactoryError("agent_identity_cannot_be_recorded")
                    atomic_json(pid_file, {"pid": process.pid, "identity": identity})
                while True:
                    if stop and stop():
                        raise Stopped("shutdown_requested")
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise FactoryError("command_timeout")
                    try:
                        stdout, stderr = process.communicate(
                            data if first else None, timeout=min(1, remaining)
                        )
                        if process.returncode:
                            raise CommandError(process.returncode, stderr, stdout)
                        return stdout
                    except subprocess.TimeoutExpired:
                        first = False
            finally:
                if pid_file or process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                if pid_file:
                    pid_file.unlink(missing_ok=True)
    except FileNotFoundError:
        raise FactoryError("executable_not_found") from None


def process_identity(pid):
    """Start time, group and UID only; never command arguments or environment."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "pgid=", "-o", "uid="],
            capture_output=True,
            text=True,
            timeout=2,
            env={"LC_ALL": "C", "PATH": os.defpath},
        )
    except subprocess.TimeoutExpired:
        raise FactoryError("agent_identity_cannot_be_verified") from None
    fields = result.stdout.split()
    return (
        " ".join(fields)
        if result.returncode == 0 and fields[-2:] == [str(pid), str(os.getuid())]
        else ""
    )


def stop_agent(pid_file):
    """Stop only the recorded process group with the same birth time and owner."""
    import json

    if not pid_file.exists():
        return
    record = json.loads(pid_file.read_text())
    pid, identity = record["pid"], record["identity"]
    if type(pid) is not int or pid <= 1 or not identity:
        raise FactoryError("invalid_agent_process_record")
    current = process_identity(pid)
    if current == identity:
        try:
            os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + 3
            while process_identity(pid) == identity and time.monotonic() < deadline:
                time.sleep(0.05)
            if process_identity(pid) == identity:
                os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif not current:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise FactoryError("agent_group_has_no_verified_leader_inspect_manually")
    pid_file.unlink(missing_ok=True)
