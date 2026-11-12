"""Serial Issue → implementation → CI → independent review → human handoff."""

import json
import time

from .agents.base import AgentRequest
from .process import FactoryError
from .review_manager import REVIEW_SCHEMA, ReviewManager
from .security import scan_secret, validate_changes
from .state_manager import INACTIVE, READY, WorkflowState


class Workflow:
    def __init__(self, config, states, github, git, developer, reviewer, audit):
        self.config, self.states, self.github, self.git = config, states, github, git
        self.developer, self.reviewer, self.audit = developer, reviewer, audit
        self.reviews = ReviewManager(config.runtime, github)

    def checkpoint(self, state, status, action):
        state.status = status
        self.states.save(state)
        self.audit.write(
            run_id=state.run_id,
            ticket_id=f"issue-{state.issue}",
            issue=state.issue,
            pr=state.pr,
            sha=state.sha,
            actor="orchestrator",
            action=action,
            status=status,
            error=state.reason,
            evidence=state.urls,
        )

    def label(self, state, label):
        self.github.label(state.issue, label, self.config.issue_label)

    def verify_pr(self, state):
        pr = self.github.pr(state.pr)
        if (
            pr["state"] != "open"
            or pr["head"]["sha"] != state.sha
            or pr["head"]["ref"] != state.branch
            or pr["base"]["ref"] != "main"
        ):
            raise FactoryError("pr_changed_outside_factory")

    def once(self):
        saved = self.states.all()
        active = [s for s in saved if s.status not in INACTIVE]
        if len(active) > 1:
            raise FactoryError("multiple_active_issues")
        if active:
            state = active[0]
        else:
            known = {s.issue for s in saved}
            issues = sorted(
                self.github.issues(self.config.issue_label),
                key=lambda i: (i["created_at"], i["number"]),
            )
            issue = next((i for i in issues if i["number"] not in known), None)
            if issue is None or self.states.stopped():
                return None
            state = WorkflowState(
                issue["number"],
                branch=f'ai/issue-{issue["number"]}',
                issue_data={"title": issue["title"], "body": issue.get("body") or ""},
                urls={"issue": issue["html_url"]},
            )
        self.github.context(state)
        try:
            scan_secret(json.dumps(state.issue_data).encode())
            self.states.save(state)  # Durable ownership before GitHub writes.
            self.run(state)
        except (FactoryError, OSError, ValueError, KeyError, TypeError) as error:
            if not self.states.stopped():
                state.resume_status = state.status
                state.reason = (
                    str(error)
                    if isinstance(error, FactoryError)
                    else "invalid_local_or_remote_state"
                )
                # Never persist rejected Issue text or arbitrary exception diagnostics.
                if state.reason == "potential_secret_detected":
                    state.issue_data = {"body": "[potential secret rejected]"}
                self.checkpoint(state, "BLOCKED", "workflow_blocked")
                self.label(state, "ai-blocked")
        return state

    def run(self, state):
        # Checkpoints retain only operations that can be interrupted independently.
        while not self.states.stopped() and state.status not in INACTIVE:
            if state.status == "CLAIMED":
                if self.github.find_pr(state.branch):
                    raise FactoryError("existing_pr_without_owned_state")
                state.sha = self.git.prepare(state)
                self.checkpoint(state, "IMPLEMENTING", "workspace_prepared")
            elif state.status == "IMPLEMENTING":
                self.label(state, "ai-in-progress")
                if state.pr:
                    self.verify_pr(state)
                task = json.dumps(
                    {"issue": state.issue_data, "feedback": state.feedback}
                )
                scan_secret(task.encode())
                result = self.developer.run(
                    AgentRequest(
                        "developer",
                        task,
                        self.git.export(state),
                        self.config.agent_timeout,
                        state.run_id,
                        f"issue-{state.issue}",
                    )
                )
                if result.status != "success":
                    raise FactoryError(result.error or "developer_failed")
                self.checkpoint(state, "PUBLISHING", "developer_completed")
            elif state.status == "PUBLISHING":
                state.sha = self.git.commit(
                    state
                )  # Validates paths/secrets and reuses a prior operation marker.
                self.checkpoint(state, "PUSHING", "commit_confirmed")
            elif state.status == "PUSHING":
                self.git.push(state)
                pr = self.github.create_pr(state)
                state.pr, state.urls["pr"] = pr["number"], pr["html_url"]
                state.ci_started = time.time()
                self.checkpoint(state, "WAITING_FOR_CI", "pr_confirmed")
            elif state.status == "WAITING_FOR_CI":
                self.verify_pr(state)
                checks = self.github.checks(state.sha, self.config.required_checks)
                values = [
                    checks.get(name, {}).get("state", "missing")
                    for name in self.config.required_checks
                ]
                if all(value == "passed" for value in values):
                    state.feedback = {"ci": checks}
                    self.label(state, "ai-review")
                    self.checkpoint(state, "REVIEWING", "ci_passed")
                elif any(value == "failed" for value in values):
                    if state.ci_fixes >= self.config.max_ci_fixes:
                        raise FactoryError("ci_repair_limit_reached")
                    state.ci_fixes += 1
                    self.repair(state, {"ci_errors": checks})
                elif time.time() - state.ci_started >= self.config.ci_timeout:
                    raise FactoryError("ci_timeout_or_missing_checks")
                else:
                    self.pause()
            elif state.status == "REVIEWING":
                self.review(state)
            elif state.status == "PUBLISHING_REVIEW":
                self.verify_pr(state)
                report = self.reviews.load(state.review_file)
                if report["verdict"] == "approved":
                    checks = self.github.checks(state.sha, self.config.required_checks)
                    if any(
                        checks.get(name, {}).get("state") != "passed"
                        for name in self.config.required_checks
                    ):
                        raise FactoryError("ci_changed_during_review")
                report = self.reviews.publish(
                    state
                )  # Includes the final positive COMMENT; never a GitHub approval.
                if report["verdict"] == "approved":
                    self.label(state, READY)
                    self.checkpoint(state, READY, "human_handoff")
                elif report["verdict"] == "failed":
                    raise FactoryError("independent_review_failed")
                elif state.review_cycle >= self.config.max_review_cycles:
                    raise FactoryError("review_cycle_limit_reached")
                else:
                    self.repair(state, {"saved_review": report})
            else:
                raise FactoryError("unsupported_workflow_checkpoint")

    def repair(self, state, feedback):
        state.iteration += 1
        state.feedback = feedback
        self.checkpoint(state, "IMPLEMENTING", "repair_requested")

    def review(self, state):
        self.verify_pr(state)
        if state.review_cycle >= self.config.max_review_cycles:
            raise FactoryError("review_cycle_limit_reached")
        path = self.reviews.path(state, state.review_cycle + 1)
        if not path.exists():
            files = self.github.files(state.pr)
            changed = {f["filename"]: None for f in files}
            changed.update(
                {
                    f["previous_filename"]: None
                    for f in files
                    if "previous_filename" in f
                }
            )
            validate_changes(changed)
            task = json.dumps(
                {
                    "issue": state.issue_data,
                    "sha": state.sha,
                    "ci": state.feedback,
                    "diff": self.github.diff(state.pr, files=files),
                }
            )
            scan_secret(task.encode())
            result = self.reviewer.run(
                AgentRequest(
                    "reviewer",
                    task,
                    self.git.export(state),
                    self.config.agent_timeout,
                    state.run_id,
                    f"issue-{state.issue}",
                    output_schema=REVIEW_SCHEMA,
                )
            )
            if result.status != "success" or self.git.changes(state):
                raise FactoryError("reviewer_failed_or_modified_source")
            state.review_cycle += 1
            state.review_file = self.reviews.save(state, result, self.reviewer.name)
        else:
            report = self.reviews.load(path)
            if report["sha"] != state.sha or report["run_id"] != state.run_id:
                raise FactoryError("review_artifact_state_mismatch")
            state.review_cycle += 1
            state.review_file = str(path)
        self.checkpoint(state, "PUBLISHING_REVIEW", "review_saved")

    def pause(self):
        deadline = time.monotonic() + self.config.poll_interval
        while time.monotonic() < deadline and not self.states.stopped():
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
