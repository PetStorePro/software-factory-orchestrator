"""End-to-end scenarios with fake GitHub, Git and AI boundaries."""

import json
from pathlib import Path
import tempfile
import unittest

from orchestrator.agents.base import ProviderResult
from orchestrator.audit_logger import AuditLogger
from orchestrator.config import FactoryConfig
from orchestrator.state_manager import READY, StateManager
from orchestrator.workflow import Workflow


def issue(number):
    return dict(
        number=number,
        title="Add greeting",
        body="Acceptance: show Hello.",
        created_at=f"2026-01-{number:02}",
        html_url=f"https://github.com/test/app/issues/{number}",
    )


class FakeGitHub:
    def __init__(self):
        self.pulls, self.reviews, self.labels = {}, {}, []
        self.ci, self.crash, self.retry_flags = "passed", False, []

    def context(self, state):
        pass

    def issues(self, label):
        return [issue(2), issue(1)]

    def label(self, number, label, ready_label):
        self.labels.append((number, label))

    def find_pr(self, branch):
        return self.pulls.get(branch)

    def pr(self, number):
        return next(p for p in self.pulls.values() if p["number"] == number)

    def files(self, number):
        return [
            dict(filename="src/app.txt", patch="@@ -0,0 +1 @@\n+Hello", status="added")
        ]

    def diff(self, number, *, files=None):
        return files

    def checks(self, sha, required):
        return {name: {"state": self.ci} for name in required}

    def create_pr(self, state):
        if state.branch not in self.pulls:
            self.pulls[state.branch] = dict(
                number=len(self.pulls) + 1,
                state="open",
                head={"ref": state.branch, "sha": state.sha},
                base={"ref": "main"},
                html_url="https://github.com/test/app/pull/1",
            )
        return self.pulls[state.branch]

    def publish_review(self, pr, report, inline, *, retry=True):
        self.retry_flags.append(retry)
        self.reviews.setdefault(
            report["review_id"],
            dict(id=len(self.reviews) + 1, url="https://github.com/review"),
        )
        if self.crash:
            self.crash = False
            raise KeyboardInterrupt()  # Remote write succeeded; process died before its receipt.
        return self.reviews[report["review_id"]]


class FakeGit:
    def __init__(self, root, github):
        self.root, self.github, self.commits = root, github, {}

    def prepare(self, state):
        return "base-sha"

    def changes(self, state):
        return {}

    def export(self, state):
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def commit(self, state):
        return self.commits.setdefault(
            (state.run_id, state.iteration), f"sha-{len(self.commits) + 1}"
        )

    def push(self, state):
        if pr := self.github.find_pr(state.branch):
            pr["head"]["sha"] = state.sha


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.requests, self.verdicts = [], []

    def run(self, request):
        self.requests.append(request)
        if request.role == "developer":
            return ProviderResult("success", text="developer-private-context")
        verdict = self.verdicts.pop(0) if self.verdicts else "approved"
        findings = (
            []
            if verdict == "approved"
            else [
                dict(
                    file="src/app.txt",
                    line=1,
                    severity="medium",
                    message="Add a regression test.",
                )
            ]
        )
        return ProviderResult(
            "success",
            text=json.dumps(
                dict(
                    verdict=verdict,
                    summary="Independent review complete.",
                    findings=findings,
                )
            ),
        )


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = FactoryConfig(
            "test/app", runtime=self.root / "runtime", max_ci_fixes=1
        )
        self.states = StateManager(self.config.runtime, self.config.repository)
        self.github = FakeGitHub()
        self.git = FakeGit(self.root / "source", self.github)
        self.developer, self.reviewer = FakeProvider(), FakeProvider()
        self.audit = AuditLogger(self.config.runtime / "audit.jsonl")
        self.workflow = self.new_workflow()

    def new_workflow(self):
        return Workflow(
            self.config,
            self.states,
            self.github,
            self.git,
            self.developer,
            self.reviewer,
            self.audit,
        )

    def test_success_hands_off_then_processes_next_oldest_issue(self):
        with self.states.lock():
            state = self.workflow.once()
            self.assertEqual((state.issue, state.status), (1, READY))
            self.assertIn((1, READY), self.github.labels)
            self.assertEqual(self.github.retry_flags, [False])
            report = json.loads(Path(state.review_file).read_text())
            self.assertEqual(
                (report["verdict"], report["sha"]), ("approved", state.sha)
            )
            self.assertEqual(
                len(self.github.reviews), 1
            )  # Final positive COMMENT was published.
            self.assertEqual(self.workflow.once().issue, 2)
            self.assertIsNone(self.workflow.once())
        events = [json.loads(line) for line in self.audit.path.read_text().splitlines()]
        self.assertTrue(
            all(
                {"timestamp", "actor", "action", "issue", "pr"} <= event.keys()
                for event in events
            )
        )

    def test_review_repair_uses_saved_feedback_same_pr_and_fresh_context(self):
        self.reviewer.verdicts = ["changes_requested", "approved"]
        state = self.workflow.once()
        self.assertEqual((state.status, state.review_cycle), (READY, 2))
        self.assertEqual(len(self.github.pulls), 1)
        self.assertEqual(len(self.developer.requests), 2)
        self.assertIn("Add a regression test.", self.developer.requests[-1].task)
        self.assertTrue(
            all(
                "developer-private-context" not in r.task
                for r in self.reviewer.requests
            )
        )
        self.assertEqual(self.github.retry_flags, [False, False])

    def test_ci_failure_repairs_current_sha(self):
        self.github.checks = lambda sha, required: {
            name: {
                "state": "failed" if sha == "sha-1" else "passed",
                "summary": "Build failed: missing import",
            }
            for name in required
        }
        state = self.workflow.once()
        self.assertEqual((state.status, state.ci_fixes, state.sha), (READY, 1, "sha-2"))
        self.assertIn("missing import", self.developer.requests[-1].task)
        self.assertEqual(len(self.github.pulls), 1)

    def test_limits_and_missing_ci_block_without_starving_queue(self):
        self.github.ci = "failed"
        state = self.workflow.once()
        self.assertEqual(
            (state.status, state.reason), ("BLOCKED", "ci_repair_limit_reached")
        )
        self.assertEqual(len(self.developer.requests), 2)
        self.assertEqual(self.reviewer.requests, [])
        self.assertEqual(self.workflow.once().issue, 2)
        state.status, state.ci_started = "WAITING_FOR_CI", 1
        self.states.save(state)
        self.github.ci = "missing"
        self.assertEqual(self.workflow.once().reason, "ci_timeout_or_missing_checks")

    def test_interrupted_review_publication_recovers_without_duplicates(self):
        self.github.crash = True
        with self.assertRaises(KeyboardInterrupt):
            self.workflow.once()
        state = self.states.all()[0]
        original = Path(state.review_file).read_bytes()
        recovered = self.new_workflow().once()
        self.assertEqual(recovered.status, READY)
        self.assertEqual(Path(state.review_file).read_bytes(), original)
        self.assertEqual(
            (len(self.github.reviews), len(self.git.commits), len(self.github.pulls)),
            (1, 1, 1),
        )
        self.assertEqual(len(self.reviewer.requests), 1)
        self.assertEqual(self.github.retry_flags, [False, True])

    def test_changed_pr_during_review_is_not_handed_off(self):
        original = self.reviewer.run

        def changed(request):
            result = original(request)
            self.github.pr(1)["head"]["sha"] = "external-sha"
            return result

        self.reviewer.run = changed
        state = self.workflow.once()
        self.assertEqual(
            (state.status, state.reason), ("BLOCKED", "pr_changed_outside_factory")
        )
        self.assertEqual(self.github.reviews, {})
        self.assertNotIn((1, READY), self.github.labels)

    def test_review_limit_stops_and_keeps_reports(self):
        self.reviewer.verdicts = ["changes_requested"] * self.config.max_review_cycles
        state = self.workflow.once()
        self.assertEqual(
            (state.status, state.reason), ("BLOCKED", "review_cycle_limit_reached")
        )
        self.assertEqual(len(self.developer.requests), self.config.max_review_cycles)
        self.assertEqual(len(self.github.reviews), self.config.max_review_cycles)
        self.assertEqual(
            len(
                [
                    p
                    for p in Path(state.review_file).parent.glob("review-*.json")
                    if ".publication." not in p.name
                ]
            ),
            self.config.max_review_cycles,
        )
