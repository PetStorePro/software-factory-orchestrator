"""Check review reports, save them locally and publish comments on GitHub."""

import json
import re
from pathlib import Path
from uuid import uuid4

from .process import FactoryError
from .security import SAFETY_REFUSAL, safe_path, scan_secret
from .state_manager import utcnow, atomic_json

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "findings"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["approved", "changes_requested", "failed"],
        },
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file", "line", "severity", "message"],
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer", "minimum": 1},
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                    },
                    "message": {"type": "string"},
                },
            },
        },
    },
}


def validate_report(text):
    """Accept only a valid review with safe paths and no detected secrets."""

    if len(text) > 100000:
        raise FactoryError("review_report_too_large")
    scan_secret(text.encode())
    try:
        data = json.loads(text)
        if set(data) != {"verdict", "summary", "findings"}:
            raise ValueError()
        if data["verdict"] not in ("approved", "changes_requested", "failed"):
            raise ValueError()
        if (
            not isinstance(data["summary"], str)
            or not data["summary"].strip()
            or len(data["summary"]) > 8000
        ):
            raise ValueError()
        if not isinstance(data["findings"], list) or len(data["findings"]) > 50:
            raise ValueError()
        if data["summary"].lstrip().startswith(SAFETY_REFUSAL):
            raise FactoryError("security_policy_violation")
        for finding in data["findings"]:
            if set(finding) != {"file", "line", "severity", "message"}:
                raise ValueError()
            safe_path(finding["file"])
            if type(finding["line"]) is not int or finding["line"] < 1:
                raise ValueError()
            if finding["severity"] not in ("low", "medium", "high", "critical"):
                raise ValueError()
            if (
                not isinstance(finding["message"], str)
                or not finding["message"].strip()
                or len(finding["message"]) > 4000
            ):
                raise ValueError()
        if data["verdict"] == "approved" and data["findings"]:
            raise ValueError()  # Approval with unresolved findings is ambiguous.
        return data
    except ValueError, TypeError, KeyError, AttributeError:
        raise FactoryError("invalid_review_report") from None


def changed_lines(files):
    """Find added lines in the new file version that can receive inline comments."""

    valid = set()
    for file in files:
        name, patch = file["filename"], file.get("patch", "")
        line = None
        for row in patch.splitlines():
            match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", row)
            if match:
                line = int(match[1])
            elif line is not None and row.startswith("+"):
                valid.add((name, line))
                line += 1
            elif line is not None and row.startswith(" "):
                line += 1
    return valid


class ReviewManager:
    """Keep review reports and publication records tied to the commit that was reviewed."""

    def __init__(self, root, github):
        self.root, self.github = root, github

    def path(self, state, cycle):
        directory = self.root / "runs" / f"issue-{state.issue}" / f"pr-{state.pr}"
        legacy = directory / f"review-{cycle:03}.json"
        return legacy if legacy.exists() else directory / f"review-{cycle}.json"

    def save(self, state, result, provider):
        """Validate the response and save a new report without replacing previous reports."""

        data = validate_report(result.text)
        path = self.path(state, state.review_cycle)
        if path.exists():
            raise FactoryError("review_artifact_already_exists")
        report = dict(
            data,
            review_id=str(uuid4()),
            run_id=state.run_id,
            issue=state.issue,
            pr=state.pr,
            cycle=state.review_cycle,
            provider=provider,
            session_id=result.session_id,
            sha=state.sha,
            ci_status=state.ci_status,
            timestamp=utcnow(),
            publication={"status": "pending"},
        )
        atomic_json(path, report)
        return str(path)

    def load(self, path):
        resolved = Path(path).resolve()
        if not resolved.is_relative_to((self.root / "runs").resolve()):
            raise FactoryError("unsafe_review_artifact_path")
        return json.loads(resolved.read_text())

    def publish(self, state):
        """Attach valid inline comments and save a separate record of GitHub publication."""

        report = self.load(state.review_file)
        publication_path = Path(state.review_file).with_suffix(".publication.json")
        if (
            report["sha"] != state.sha
            or report["run_id"] != state.run_id
            or report["pr"] != state.pr
        ):
            raise FactoryError("review_artifact_state_mismatch")
        retry = publication_path.exists()
        if retry and json.loads(publication_path.read_text())["status"] == "published":
            return report
        valid = (
            changed_lines(self.github.files(state.pr)) if report["findings"] else set()
        )
        inline = [
            {
                "path": f["file"],
                "line": f["line"],
                "side": "RIGHT",
                "body": f'{f["severity"]}: {f["message"]}',
            }
            for f in report["findings"]
            if (f["file"], f["line"]) in valid
        ]
        atomic_json(publication_path, {"status": "publishing"})
        result = self.github.publish_review(state.pr, report, inline, retry=retry)
        # Keep the original report unchanged and save publication details in a separate file.
        atomic_json(publication_path, dict(status="published", **result))
        return report
