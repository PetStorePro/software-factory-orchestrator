"""Shared local execution: restrictions, output, failures and CLI compatibility."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.agent_runner import AgentRunner, final_message, parse_events
from orchestrator.config import Config
from orchestrator.main import main
from orchestrator.process import CommandError, FactoryError

SESSION = "550e8400-e29b-41d4-a716-446655440000"
OUTPUT = "\n".join(
    json.dumps(event)
    for event in [
        {"type": "thread.started", "thread_id": SESSION},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Finished."},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3}},
    ]
)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.audit = self.root / "logs" / "audit.jsonl"
        self.runner = AgentRunner(Config(2, self.audit))

    def test_event_output_is_untrusted_and_completion_is_not_verification(self):
        session, usage, completed, failed = parse_events(OUTPUT + "\nnull\nnot JSON")
        self.assertEqual(session, SESSION)
        self.assertEqual(usage, {"input_tokens": 12, "output_tokens": 3})
        self.assertTrue(completed)
        self.assertFalse(failed)
        self.assertEqual(final_message(OUTPUT), "Finished.")
        self.assertEqual(
            parse_events('{"type":"turn.completed","usage":{"input_tokens":true}}')[1],
            {},
        )
        for output, error in [
            ("[]", "missing_completion_event"),
            (OUTPUT + '\n{"type":"turn.failed"}', "agent_reported_failure"),
        ]:
            with patch("orchestrator.agent_runner.execute", return_value=output):
                result = self.runner.run("developer", "task", self.workspace)
            self.assertEqual(result.error, error)
            self.assertFalse(result.task_verified)

    @patch("orchestrator.agent_runner.execute", return_value=OUTPUT)
    def test_local_roles_environment_schema_and_audit(self, execute):
        for role, sandbox in [
            ("developer", "workspace-write"),
            ("reviewer", "read-only"),
        ]:
            with patch.dict(
                os.environ,
                {
                    "GITHUB_TOKEN": "dummy",
                    "SSH_AUTH_SOCK": "/dummy",
                    "OPENAI_API_KEY": "dummy",
                },
            ):
                result = self.runner.run(
                    role,
                    "private task text",
                    self.workspace,
                    "SSDLC-1",
                    output_schema={"type": "object"},
                )
            self.assertEqual(result.status, "success")
            args, kwargs = execute.call_args
            command = args[0]
            self.assertEqual(command[0], "codex")
            self.assertEqual(command[command.index("--sandbox") + 1], sandbox)
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--output-schema", command)
            self.assertEqual(kwargs["env"]["HOME"], os.environ["HOME"])
            self.assertFalse(
                {"GITHUB_TOKEN", "SSH_AUTH_SOCK", "OPENAI_API_KEY"}
                & kwargs["env"].keys()
            )
            self.assertEqual(kwargs["timeout"], 2)
        records = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertEqual(
            [r["status"] for r in records], ["started", "success", "started", "success"]
        )
        self.assertEqual(records[-1]["session_id"], SESSION)
        self.assertNotIn("private task text", self.audit.read_text())
        with self.assertRaises(ValueError):
            self.runner.run(
                "developer", "task", self.root
            )  # Would include audit storage.

    def test_process_failures_are_structured_and_do_not_leak_diagnostics(self):
        for exception, status, error in [
            (FactoryError("executable_not_found"), "failed", "executable_not_found"),
            (FactoryError("command_timeout"), "timeout", "execution_timeout"),
            (CommandError(7, "sensitive diagnostic", OUTPUT), "failed", "nonzero_exit"),
        ]:
            with patch("orchestrator.agent_runner.execute", side_effect=exception):
                result = self.runner.run("developer", "task", self.workspace)
            self.assertEqual((result.status, result.error), (status, error))
        self.assertEqual(result.stdout, OUTPUT)
        self.assertEqual(result.session_id, SESSION)
        self.assertNotIn("sensitive diagnostic", self.audit.read_text())

    @patch("orchestrator.main.start")
    @patch("builtins.print")
    def test_original_run_cli_exit_codes(self, output, signals):
        args = [
            "run",
            "--role",
            "reviewer",
            "--task",
            "review",
            "--workspace",
            str(self.workspace),
            "--audit-log",
            str(self.audit),
        ]
        with patch.dict(os.environ, {"FACTORY_RUNTIME": str(self.root / "runtime")}):
            with patch("orchestrator.agent_runner.execute", return_value=OUTPUT):
                self.assertEqual(main(args), 0)
            with patch(
                "orchestrator.agent_runner.execute",
                side_effect=FactoryError("executable_not_found"),
            ):
                self.assertEqual(main(args), 127)
            self.assertEqual(main(args + ["--timeout", "-1"]), 2)
