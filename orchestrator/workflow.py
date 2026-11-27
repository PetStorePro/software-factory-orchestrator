"""Process one Issue through development, CI and AI review before handing it to a human."""

import json
import time

from .agents.base import AgentRequest
from .process import FactoryError, Stopped
from .review_manager import REVIEW_SCHEMA, ReviewManager
from .security import scan_secret, validate_changes
from .state_manager import INACTIVE, READY, WorkflowState


class Workflow:
    """Coordinate agents, Git, CI and reviews for one Issue at a time."""

    def __init__(
        self, config, states, github, git, developer, reviewer, audit, *, progress=None
    ):
        self.config, self.states, self.github, self.git = config, states, github, git
        self.developer, self.reviewer, self.audit = developer, reviewer, audit
        self.reviews = ReviewManager(config.runtime, github)
        self.progress = progress or (lambda message: None)

    def say(self, state, message):
        # Callers supply fixed messages and numeric IDs, never Issue/agent text.
        self.progress(f"Issue #{state.issue}: {message}")

    def checkpoint(self, state, status, action):
        """Save the new stage and record the transition in the audit log."""

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
            ci_status=state.ci_status,
            error=state.reason,
            evidence=state.urls,
        )

    def label(self, state, label):
        self.github.label(state.issue, label, self.config.issue_label)

    def verify_pr(self, state):
        """Allow a short delay for GitHub to show a pushed commit before rejecting a mismatch."""

        for attempt in range(3):
            if self.states.stopped():
                raise Stopped("shutdown_requested")
            pr = self.github.pr(state.pr)
            if (
                pr["state"] != "open"
                or pr["head"]["ref"] != state.branch
                or pr["base"]["ref"] != state.base_branch
            ):
                raise FactoryError("pr_changed_outside_factory")
            if pr["head"]["sha"] == state.sha:
                return
            if attempt < 2:
                self.say(state, "Waiting 5 seconds for GitHub to show the expected commit.")
                self.pause(seconds=5)
        raise FactoryError("pr_changed_outside_factory")

    def once(self):
        """Resume saved active work or choose the oldest new Issue and record any blocking error."""

        self.progress("Looking for a ready issue or saved work...")
        saved = self.states.all()
        active = [s for s in saved if s.status not in INACTIVE]
        if len(active) > 1:
            raise FactoryError("multiple_active_issues")
        if active:
            state = active[0]
            if state.base_branch != self.config.base_branch:
                raise FactoryError("active_issue_uses_different_base_branch")
            self.say(state, "Resuming saved work.")
        else:
            known = {s.issue for s in saved}
            issues = sorted(
                self.github.issues(self.config.issue_label),
                key=lambda i: (i["created_at"], i["number"]),
            )
            issue = next((i for i in issues if i["number"] not in known), None)
            if issue is None or self.states.stopped():
                self.progress("No ready issue was found, or a stop was requested.")
                return None
            state = WorkflowState(
                issue["number"],
                base_branch=self.config.base_branch,
                branch=f'ai/issue-{issue["number"]}',
                issue_data={"title": issue["title"], "body": issue.get("body") or ""},
                urls={"issue": issue["html_url"]},
            )
            self.say(state, "Starting this issue.")
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
                message = {
                    "security_policy_violation": "Blocked: the agent rejected an unsafe request.",
                    "potential_secret_detected": "Blocked: a possible secret was found.",
                    "protected_path_changed": "Blocked: changes touch protected files.",
                    "review_cycle_limit_reached": "Stopped: the review limit was reached.",
                    "ci_repair_limit_reached": "Stopped: the CI repair limit was reached.",
                }.get(
                    state.reason, "Work is blocked. Use the status command for details."
                )
                self.say(state, message)
                self.label(state, "ai-blocked")
        if self.states.stopped():
            self.say(state, "Stop requested. Saved work can be resumed.")
        return state

    def run(self, state):
        # Checkpoints retain only operations that can be interrupted independently
        """Follow saved stages until human handoff, a blocking error or a stop request."""

        while not self.states.stopped() and state.status not in INACTIVE:
            if state.status == "CLAIMED":
                self.say(state, "Preparing the work folder and feature branch.")
                if self.github.find_pr(state.branch):
                    raise FactoryError("existing_pr_without_owned_state")
                state.sha = self.git.prepare(state)
                self.checkpoint(state, "IMPLEMENTING", "workspace_prepared")
            elif state.status == "IMPLEMENTING":
                self.say(
                    state,
                    f"Starting the Developer agent (attempt {state.iteration + 1}).",
                )
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
                self.say(state, "The Developer agent has finished.")
                self.checkpoint(state, "PUBLISHING", "developer_completed")
            elif state.status == "PUBLISHING":
                self.say(state, "Checking changed files and saving the commit.")
                state.sha = self.git.commit(
                    state
                )  # Validates paths/secrets and reuses a prior operation marker.
                self.checkpoint(state, "PUSHING", "commit_confirmed")
            elif state.status == "PUSHING":
                self.say(state, "Pushing the feature branch.")
                self.git.push(state)
                self.say(state, "Opening or updating the pull request.")
                pr = self.github.create_pr(state)
                state.pr, state.urls["pr"] = pr["number"], pr["html_url"]
                state.ci_started = time.time()
                state.ci_status = "not_checked"
                self.checkpoint(state, "WAITING_FOR_CI", "pr_confirmed")
                self.say(state, f"PR #{state.pr} is open.")
            elif state.status == "WAITING_FOR_CI":
                self.say(state, "Checking CI results...")
                self.verify_pr(state)
                checks = self.github.checks(state.sha, self.config.required_checks)
                values = [
                    checks.get(name, {}).get("state", "missing")
                    for name in self.config.required_checks
                ]
                passed = all(value == "passed" for value in values)
                if passed or self.can_skip_ci(state, checks):
                    self.say(
                        state,
                        (
                            "CI checks passed."
                            if passed
                            else "CI was skipped for this test. No CI checks were verified."
                        ),
                    )
                    state.ci_status = "passed" if passed else "skipped"
                    state.feedback = {"ci": checks, "ci_status": state.ci_status}
                    self.label(state, "ai-review")
                    self.checkpoint(
                        state, "REVIEWING", "ci_passed" if passed else "ci_skipped"
                    )
                elif any(value == "failed" for value in values):
                    self.say(
                        state, "CI failed. Sending the errors to the Developer agent."
                    )
                    if state.ci_fixes >= self.config.max_ci_fixes:
                        raise FactoryError("ci_repair_limit_reached")
                    state.ci_fixes += 1
                    self.repair(state, {"ci_errors": checks})
                elif time.time() - state.ci_started >= self.config.ci_timeout:
                    raise FactoryError("ci_timeout_or_missing_checks")
                else:
                    self.say(state, "Waiting for CI checks.")
                    self.pause()
            elif state.status == "REVIEWING":
                self.review(state)
            elif state.status == "PUBLISHING_REVIEW":
                self.say(state, "Checking the PR before posting the review.")
                self.verify_pr(state)
                report = self.reviews.load(state.review_file)
                if report["verdict"] == "approved":
                    checks = self.github.checks(state.sha, self.config.required_checks)
                    if all(
                        checks.get(name, {}).get("state") == "passed"
                        for name in self.config.required_checks
                    ):
                        state.ci_status = "passed"
                    elif state.ci_status != "skipped" or not self.can_skip_ci(
                        state, checks
                    ):
                        raise FactoryError("ci_changed_during_review")
                self.say(state, "Posting the review on GitHub.")
                report = self.reviews.publish(
                    state
                )  # Post the final positive comment without granting GitHub approval.
                if report["verdict"] == "approved":
                    self.label(state, READY)
                    self.checkpoint(state, READY, "human_handoff")
                    self.say(state, f"PR #{state.pr} is ready for human review.")
                elif report["verdict"] == "failed":
                    self.say(state, "The Reviewer could not finish the review.")
                    raise FactoryError("independent_review_failed")
                elif state.review_cycle >= self.config.max_review_cycles:
                    raise FactoryError("review_cycle_limit_reached")
                else:
                    self.say(
                        state,
                        "The Reviewer asked for changes. Sending the saved feedback to the Developer agent.",
                    )
                    self.repair(state, {"saved_review": report})
            else:
                raise FactoryError("unsupported_workflow_checkpoint")

    def repair(self, state, feedback):
        """Save feedback and return to development without creating another PR."""

        state.iteration += 1
        state.feedback = feedback
        self.checkpoint(state, "IMPLEMENTING", "repair_requested")

    def can_skip_ci(self, state, checks):
        """Allow the explicit demo exception only when checks and workflow files are absent."""

        return (
            self.config.allow_missing_ci
            and all(
                checks.get(name, {}).get("state") == "missing"
                and checks[name].get("reason") == "no_check_runs"
                for name in self.config.required_checks
            )
            and not self.github.has_workflows(state.base_branch)
        )

    def review(self, state):
        """Run a fresh Reviewer or recover a saved report for the current commit."""

        self.verify_pr(state)
        if state.review_cycle >= self.config.max_review_cycles:
            raise FactoryError("review_cycle_limit_reached")
        path = self.reviews.path(state, state.review_cycle + 1)
        if not path.exists():
            self.say(
                state,
                f"Starting a fresh, read-only Reviewer agent (review {state.review_cycle + 1} of {self.config.max_review_cycles}).",
            )
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
            self.say(state, "Using the saved review report.")
            report = self.reviews.load(path)
            if report["sha"] != state.sha or report["run_id"] != state.run_id:
                raise FactoryError("review_artifact_state_mismatch")
            state.review_cycle += 1
            state.review_file = str(path)
        self.checkpoint(state, "PUBLISHING_REVIEW", "review_saved")
        self.say(state, "The review report is saved.")

    def pause(self, seconds=None):
        duration = self.config.poll_interval if seconds is None else seconds
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline and not self.states.stopped():
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
