from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, asdict, fields as dataclass_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from jira_extractor.config import JiraConfig
from jira_extractor.formatter import adf_to_text


logger = logging.getLogger(__name__)

PAGE_SIZE = 100
API_VERSION = "2"
ACCOUNT_NAME_FIELD_ID = "customfield_23901"


@dataclass
class IssueRecord:
    key: str
    issue_title: str
    assignee: str
    account_name: str
    reporter: str
    created_datetime: str
    updated_datetime: str
    current_status: str
    issue_type: str
    priority: str
    resolution: str
    resolved_datetime: str
    description: str
    comments: str


# Default Jira issue fields to request. The account-name custom field id (if
# configured) is appended at request time.
_BASE_FIELDS: tuple[str, ...] = (
    "summary",
    "assignee",
    "reporter",
    "created",
    "updated",
    "status",
    "issuetype",
    "priority",
    "resolution",
    "resolutiondate",
    "description",
    "comment",
)


class JiraClient:
    def __init__(self, config: JiraConfig, timeout_seconds: int = 30) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

        retries = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST"]),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers["Authorization"] = f"Bearer {config.api_token}"
        self.session.headers["Accept"] = "application/json"

    # ---- HTTP helpers --------------------------------------------------

    def _api_url(self, path: str) -> str:
        return f"{self.config.base_url}/rest/api/{API_VERSION}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.request(
            method=method,
            url=self._api_url(path),
            timeout=self.timeout_seconds,
            **kwargs,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()

    # ---- Search --------------------------------------------------------

    def _request_fields(self) -> list[str]:
        fields = list(_BASE_FIELDS)
        fields.append(ACCOUNT_NAME_FIELD_ID)
        return fields

    def _iter_search_pages(self, jql: str) -> Iterator[dict[str, Any]]:
        """Jira Server / Data Center (API v2) pagination via POST /search."""
        start_at = 0
        fields = self._request_fields()
        while True:
            payload = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": PAGE_SIZE,
                "fields": fields,
            }
            data = self._request("POST", "search", json=payload)
            yield data
            issues = data.get("issues", []) or []
            fetched = len(issues)
            try:
                total = int(data.get("total", 0))
            except (TypeError, ValueError):
                total = 0
            start_at += fetched
            if fetched == 0 or start_at >= total:
                return

    def search_issues(self, jql: str) -> list[IssueRecord]:
        records: list[IssueRecord] = []
        for page in self._iter_search_pages(jql):
            issues = page.get("issues", []) or []
            for issue in issues:
                records.append(self._issue_to_record(issue))
            logger.info("Fetched page: %d issues (running total %d)", len(issues), len(records))
        return records

    # ---- Comments ------------------------------------------------------

    def fetch_all_comments(self, issue_key: str) -> list[dict[str, Any]]:
        all_comments: list[dict[str, Any]] = []
        start_at = 0

        while True:
            data = self._request(
                "GET",
                f"issue/{issue_key}/comment",
                params={"startAt": start_at, "maxResults": PAGE_SIZE},
            )
            comments = data.get("comments", []) or []
            all_comments.extend(comments)

            fetched = len(comments)
            total = int(data.get("total", 0))
            start_at += fetched

            if fetched == 0 or start_at >= total:
                break

        return all_comments

    # ---- Mapping -------------------------------------------------------

    def _extract_account_name(self, fields: dict[str, Any]) -> str:
        value = fields.get(ACCOUNT_NAME_FIELD_ID)
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            # Common custom-field shapes (select list, user, etc.).
            for key in ("value", "name", "displayName"):
                inner = value.get(key)
                if isinstance(inner, str) and inner:
                    return inner
            return ""
        if isinstance(value, list):
            names = []
            for item in value:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, dict):
                    for key in ("value", "name", "displayName"):
                        inner = item.get(key)
                        if isinstance(inner, str) and inner:
                            names.append(inner)
                            break
            return ", ".join(names)
        return str(value)

    @staticmethod
    def _named(value: Any, key: str = "displayName", default: str = "") -> str:
        if isinstance(value, dict):
            inner = value.get(key)
            if isinstance(inner, str):
                return inner
        return default

    def _issue_to_record(self, issue: dict[str, Any]) -> IssueRecord:
        fields = issue.get("fields", {}) or {}

        comment_payload = fields.get("comment") or {}
        comments_raw = comment_payload.get("comments", []) or []
        # Comments are truncated in search responses; fetch all if needed.
        try:
            comment_total = int(comment_payload.get("total", len(comments_raw)))
        except (TypeError, ValueError):
            comment_total = len(comments_raw)
        if comment_total > len(comments_raw):
            try:
                comments_raw = self.fetch_all_comments(str(issue.get("key", "")))
            except requests.RequestException as exc:
                # Don't fail the whole run for one issue's comments.
                logger.warning(
                    "Failed to fetch full comments for %s: %s", issue.get("key"), exc
                )

        comments_text: list[str] = []
        for comment in comments_raw:
            author = self._named(comment.get("author"), default="Unknown")
            body = adf_to_text(comment.get("body", "")).strip()
            created = str(comment.get("created", ""))
            comments_text.append(f"[{created}] {author}: {body}")

        return IssueRecord(
            key=str(issue.get("key", "")),
            issue_title=str(fields.get("summary", "") or ""),
            assignee=self._named(fields.get("assignee"), default="Unassigned"),
            account_name=self._extract_account_name(fields),
            reporter=self._named(fields.get("reporter"), default=""),
            created_datetime=str(fields.get("created", "") or ""),
            updated_datetime=str(fields.get("updated", "") or ""),
            current_status=self._named(fields.get("status"), key="name", default=""),
            issue_type=self._named(fields.get("issuetype"), key="name", default=""),
            priority=self._named(fields.get("priority"), key="name", default=""),
            resolution=self._named(fields.get("resolution"), key="name", default=""),
            resolved_datetime=str(fields.get("resolutiondate", "") or ""),
            description=adf_to_text(fields.get("description", "")).strip(),
            comments="\n\n".join(comments_text).strip(),
        )


def _record_fieldnames() -> list[str]:
    return [f.name for f in dataclass_fields(IssueRecord)]


def write_outputs(
    records: list[IssueRecord],
    output_dir: Path,
    base_name: str = "jira_issues",
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    csv_path = output_dir / f"{base_name}_{timestamp}.csv"

    serialized = [asdict(record) for record in records]

    fieldnames = _record_fieldnames()
    with csv_path.open("w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(serialized)

    return csv_path
