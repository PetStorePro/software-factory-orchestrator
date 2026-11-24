"""Use GitHub CLI to read Issues and CI results and publish PRs and reviews."""

import json
from urllib.parse import quote, urlencode

from .process import execute, FactoryError
from .security import scan_secret


class GitHubError(FactoryError):
    """Report a GitHub request failure with its HTTP status when available."""

    def __init__(self, http_status=None):
        super().__init__("github_request_failed")
        self.http_status = http_status


LABELS = {
    "ai-ready",
    "ai-in-progress",
    "ai-review",
    "ai-blocked",
    "READY_FOR_HUMAN_REVIEW",
}


class GitHubClient:
    """Handle GitHub requests through the existing GitHub CLI login."""

    def __init__(self, repository, audit, stop=lambda: False):
        self.repository, self.audit, self.stop = repository, audit, stop
        self.prefix = f"repos/{repository}"
        self.run_id, self.ticket_id = "preflight", None

    def context(self, state):
        self.run_id, self.ticket_id = state.run_id, f"issue-{state.issue}"

    def api(self, endpoint, method="GET", payload=None, *, pages=False):
        """Run a GitHub API request and return parsed JSON while recording safe audit details."""

        args = [
            "gh",
            "api",
            "--hostname",
            "github.com",
            endpoint,
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
        ]
        if pages:
            args += ["--paginate", "--slurp"]
        if payload is not None:
            args += ["--input", "-"]
        self.audit.write(
            run_id=self.run_id,
            ticket_id=self.ticket_id,
            actor="orchestrator",
            action="github_request",
            method=method,
            endpoint=endpoint.split("?")[0],
            status="started",
        )
        try:
            output = execute(
                args,
                data=json.dumps(payload) if payload is not None else None,
                timeout=60,
                stop=self.stop,
            )
            result = json.loads(output) if output.strip() else None
        except (FactoryError, ValueError) as error:
            self.audit.write(
                run_id=self.run_id,
                ticket_id=self.ticket_id,
                actor="orchestrator",
                action="github_request",
                method=method,
                status="failed",
            )
            raise GitHubError(getattr(error, "http_status", None)) from None
        self.audit.write(
            run_id=self.run_id,
            ticket_id=self.ticket_id,
            actor="orchestrator",
            action="github_request",
            method=method,
            status="success",
        )
        return result

    def get(self, path, **kwargs):
        return self.api(f"{self.prefix}/{path}", **kwargs)

    def listing(self, path, key=None):
        """Read every result page and combine the items into one list."""

        pages = self.get(
            path + ("&" if "?" in path else "?") + "per_page=100", pages=True
        )
        return [item for page in pages for item in (page[key] if key else page)]

    def repository_info(self):
        self.api(
            "user"
        )  # Validate authentication without extracting or displaying tokens.
        return self.api(self.prefix)

    def issues(self, label):
        return [
            issue
            for issue in self.listing(
                "issues?"
                + urlencode(
                    dict(state="open", labels=label, sort="created", direction="asc")
                )
            )
            if "pull_request" not in issue
        ]

    def label(self, number, label, ready_label="ai-ready"):
        # Add the new stage first and keep labels that do not belong to the factory.
        """Set the factory stage label while keeping labels managed by other people."""

        existing = {item["name"] for item in self.get(f"issues/{number}/labels")}
        if label not in existing:
            self.api(
                f"{self.prefix}/issues/{number}/labels", "POST", {"labels": [label]}
            )
        for old in sorted((LABELS | {ready_label}) & existing - {label}):
            self.api(
                f'{self.prefix}/issues/{number}/labels/{quote(old, safe="")}', "DELETE"
            )

    def find_pr(self, branch):
        pulls = self.listing(
            "pulls?"
            + urlencode(
                dict(state="all", head=f'{self.repository.split("/")[0]}:{branch}')
            )
        )
        return pulls[0] if pulls else None

    def create_pr(self, state):
        found = self.find_pr(state.branch)
        if found:
            return found
        return self.api(
            f"{self.prefix}/pulls",
            "POST",
            {
                "title": f"Implement issue #{state.issue}",
                "head": state.branch,
                "base": state.base_branch,
                "body": f"Relates to #{state.issue}.\n\nSoftware Factory run `{state.run_id}`.\n"
                "AI-generated implementation; independent AI review is not human approval.",
                "maintainer_can_modify": False,
            },
        )

    def pr(self, number):
        return self.get(f"pulls/{number}")

    def files(self, number):
        return self.listing(f"pulls/{number}/files")

    def diff(self, number, *, files=None):
        files = self.files(number) if files is None else files
        if len(files) >= 3000 or any("patch" not in f for f in files):
            raise FactoryError("pr_diff_incomplete_or_binary")
        return [
            {"file": f["filename"], "patch": f["patch"], "status": f["status"]}
            for f in files
        ]

    def checks(self, sha, required):
        """Read required checks for this exact commit and collect safe failure details."""

        runs = self.listing(f"commits/{sha}/check-runs?filter=latest", "check_runs")
        results = {}
        for name in required:
            matches = [r for r in runs if r["name"] == name]
            if len(matches) != 1:
                results[name] = {
                    "state": "missing",
                    "reason": (
                        "no_check_runs"
                        if not runs
                        else "required_check_missing_or_ambiguous"
                    ),
                }  # Duplicate names are ambiguous too.
                continue
            run = matches[0]
            state = (
                "pending"
                if run["status"] != "completed"
                else "passed" if run.get("conclusion") == "success" else "failed"
            )
            summary = (run.get("output") or {}).get("summary") or ""
            if state == "failed" and (run.get("output") or {}).get(
                "annotations_count", 0
            ):
                notes = self.listing(f'check-runs/{run["id"]}/annotations')
                summary += "\n" + "\n".join(note["message"] for note in notes[:20])
            try:
                scan_secret(summary.encode())
                summary = summary[:8000]
            except FactoryError:
                summary = "[potential secret removed from CI diagnostics]"
            results[name] = {
                "state": state,
                "summary": summary,
                "url": run.get("html_url"),
            }
        return results

    def has_workflows(self, branch):
        """Check for workflow YAML files on the selected branch without hiding API errors."""
        try:
            entries = self.get(
                "contents/.github/workflows?" + urlencode({"ref": branch})
            )
        except GitHubError as error:
            if error.http_status == 404:
                return False
            raise
        return any(
            entry.get("type") == "file" and entry["name"].endswith((".yml", ".yaml"))
            for entry in entries
        )

    def publish_review(self, pr, report, inline, *, retry=True):
        """Publish a COMMENT review and look for an existing copy when retrying."""

        marker = f'<!-- software-factory-review:{report["review_id"]} -->'
        reviews = self.listing(f"pulls/{pr}/reviews") if retry else []
        for existing in reviews:
            if (
                marker in (existing.get("body") or "")
                and existing.get("commit_id") == report["sha"]
            ):
                return {"id": existing["id"], "url": existing.get("html_url")}
        findings = "\n".join(
            f'- {f["severity"]}: {f["file"]}:{f["line"]}: {f["message"]}'
            for f in report["findings"]
        )
        body = f'{marker}\nAI verdict: **{report["verdict"]}** (not human approval).\n\n{report["summary"]}\n\n{findings}'
        if report.get("ci_status") == "skipped":
            body += "\n\n**CI SKIPPED — explicit test mode. No automated CI checks were verified; this is not M1 completion.**"
        if len(body) > 60000:
            raise FactoryError("review_too_large_to_publish")
        try:
            result = self.api(
                f"{self.prefix}/pulls/{pr}/reviews",
                "POST",
                {
                    "commit_id": report["sha"],
                    "event": "COMMENT",
                    "body": body,
                    "comments": inline,
                },
            )
        except GitHubError as error:
            if inline and error.http_status == 422:
                # GitHub rejected inline attachment. All findings already exist in the body.
                # Check for an existing review before posting the summary without inline comments.
                return self.publish_review(pr, report, [])
            raise
        return {"id": result["id"], "url": result.get("html_url")}
