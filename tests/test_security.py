import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.audit_logger import AuditLogger
from orchestrator.config import FactoryConfig
from orchestrator.git_manager import GitManager
from orchestrator.github_client import GitHubClient
from orchestrator.process import FactoryError
from orchestrator.review_manager import validate_report, changed_lines
from orchestrator.state_manager import WorkflowState
from orchestrator.security import validate_changes


class SecurityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = FactoryConfig("test/app", runtime=self.root / "runtime")
        self.audit = AuditLogger(self.config.runtime / "audit.jsonl")

    def test_protected_paths_secrets_and_traversal(self):
        for changes in (
            {".github/workflows/ci.yml": b"changed"},
            {"../escape": b"no"},
            {"src/key.txt": b"-----BEGIN PRIVATE KEY-----"},
            {".codex/config.toml": b"data"},
        ):
            with self.subTest(paths=list(changes)), self.assertRaises(FactoryError):
                validate_changes(changes)
        validate_changes({"src/app.jsx": b"export default 42;"})

    def test_strict_review_validation_and_inline_positions(self):
        good = {
            "verdict": "approved",
            "summary": "Checked acceptance and tests.",
            "findings": [],
        }
        self.assertEqual(validate_report(json.dumps(good)), good)
        for text in (
            "{}",
            "not JSON",
            json.dumps(good | {"extra": True}),
            json.dumps(good | {"verdict": "APPROVE"}),
            json.dumps(
                good
                | {
                    "findings": [
                        {
                            "file": "../bad",
                            "line": True,
                            "severity": "high",
                            "message": "bad",
                        }
                    ]
                }
            ),
        ):
            with self.subTest(text=text), self.assertRaises(FactoryError):
                validate_report(text)
        valid = changed_lines(
            [
                {
                    "filename": "a.txt",
                    "patch": "@@ -1,2 +1,3 @@\n same\n-old\n+new\n+extra",
                }
            ]
        )
        self.assertEqual(valid, {("a.txt", 2), ("a.txt", 3)})

    def test_commit_marker_recovery_does_not_commit_again(self):
        manager = GitManager(self.config, self.audit)
        state = WorkflowState(1, branch="ai/issue-1")
        with patch.object(
            manager,
            "git",
            side_effect=[f"Factory-Operation: {state.run_id}-0", "saved-sha"],
        ) as git:
            self.assertEqual(manager.commit(state), "saved-sha")
        self.assertEqual(
            [call.args[1] for call in git.call_args_list], ["log", "rev-parse"]
        )

    @patch("orchestrator.github_client.execute")
    def test_checks_missing_stale_failures_and_review_retry_marker(self, execute):
        github = GitHubClient("test/app", self.audit)
        execute.return_value = json.dumps(
            [
                {
                    "check_runs": [
                        {
                            "name": "ci",
                            "status": "completed",
                            "conclusion": "success",
                            "app": {"id": 1},
                        },
                        {
                            "name": "security",
                            "status": "completed",
                            "conclusion": "cancelled",
                        },
                    ]
                }
            ]
        )
        checks = github.checks("current-sha", ("ci", "security", "absent"))
        self.assertEqual(
            [checks[k]["state"] for k in ("ci", "security", "absent")],
            ["passed", "failed", "missing"],
        )
        self.assertIn(
            "commits/current-sha/check-runs", execute.call_args_list[0].args[0][4]
        )
        report = {"review_id": "stable-id", "sha": "current-sha"}
        execute.side_effect = None
        execute.return_value = json.dumps(
            [
                [
                    {
                        "body": "<!-- software-factory-review:stable-id -->",
                        "commit_id": "current-sha",
                        "id": 10,
                        "html_url": "https://github.com/review",
                    }
                ]
            ]
        )
        receipt = github.publish_review(1, report, [])
        self.assertEqual(receipt["id"], 10)
        self.assertEqual(
            execute.call_args.args[0][execute.call_args.args[0].index("--method") + 1],
            "GET",
        )

    def test_rejected_inline_attachment_falls_back_to_comment(self):
        from orchestrator.github_client import GitHubError

        github = GitHubClient("test/app", self.audit)
        report = {
            "review_id": "stable",
            "sha": "current",
            "verdict": "changes_requested",
            "summary": "Missing test.",
            "findings": [
                {
                    "file": "a.txt",
                    "line": 1,
                    "severity": "medium",
                    "message": "Add test.",
                }
            ],
        }
        with (
            patch.object(github, "listing", return_value=[]),
            patch.object(
                github,
                "api",
                side_effect=[
                    GitHubError(422),
                    {"id": 11, "html_url": "https://github.com/review"},
                ],
            ) as api,
        ):
            github.publish_review(
                1,
                report,
                [{"path": "a.txt", "line": 1, "side": "RIGHT", "body": "Add test."}],
            )
            final_payload = api.call_args.args[2]
            self.assertEqual(final_payload["event"], "COMMENT")
            self.assertEqual(final_payload["comments"], [])
            self.assertIn("Add test.", final_payload["body"])

    def test_single_process_lock_and_unsupported_provider(self):
        from orchestrator.agents import create_provider
        from orchestrator.state_manager import StateManager

        states = StateManager(self.config.runtime, self.config.repository)
        other = StateManager(self.root / "other-runtime", self.config.repository)
        with states.lock(), self.assertRaises(FactoryError):
            with other.lock():
                pass
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            create_provider("unsupported", self.config, self.audit, lambda: False)

    def test_cancellation_and_timeout_kill_only_the_child_group(self):
        from unittest.mock import MagicMock
        from orchestrator.process import execute
        import signal

        for timeout, stop in ((30, lambda: True), (0, lambda: False)):
            process = MagicMock(pid=987654321)
            process.poll.return_value = None
            with (
                patch("orchestrator.process.subprocess.Popen") as popen,
                patch("orchestrator.process.os.killpg") as kill,
            ):
                popen.return_value.__enter__.return_value = process
                with self.assertRaises(FactoryError):
                    execute(["codex", "exec"], timeout=timeout, stop=stop)
                kill.assert_called_once_with(process.pid, signal.SIGKILL)
