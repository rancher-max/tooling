from __future__ import annotations

import json
import logging
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from jira_extractor.jira_client import IssueRecord


logger = logging.getLogger(__name__)

# Conservative per-issue character caps so the prompt fits within one batch.
_DESCRIPTION_CHAR_LIMIT = 2000
_COMMENTS_CHAR_LIMIT = 3000
# Hard ceiling on the serialized issue payload sent in a single batch prompt.
# Multiple batches are used when the full issue set exceeds this limit.
_BATCH_PAYLOAD_CHAR_LIMIT = 50_000


class OllamaUnavailableError(RuntimeError):
    """Raised when Ollama is unreachable or not healthy."""


@dataclass(frozen=True)
class IssueStats:
    """Deterministic statistics computed directly from issue records."""

    total_issues: int
    closed_or_resolved_issues: int
    open_issues: int
    avg_resolution_days: float | None
    median_resolution_days: float | None
    # Time from issue creation to first comment by the assignee.
    avg_initial_response_days: float | None
    median_initial_response_days: float | None
    # Time the issue was open and NOT in "Waiting for Reporter" status.
    avg_time_actively_worked_days: float | None
    median_time_actively_worked_days: float | None
    # Time the issue spent in "Waiting for Reporter" status.
    avg_time_waiting_days: float | None
    median_time_waiting_days: float | None
    issues_per_customer: dict[str, int]
    issues_per_team: dict[str, int]


@dataclass(frozen=True)
class ThemeAnalysisResult:
    model: str
    source_jql: str
    prompt_issue_count: int
    structured: dict[str, Any]
    raw_response: str
    stats: IssueStats


class OllamaAnalyzer:
    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434",
        timeout_seconds: int = 900,
        max_retries: int = 2,
        retry_backoff_seconds: float = 5.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(max_retries, 0)
        self.retry_backoff_seconds = max(retry_backoff_seconds, 0.0)
        self.session = requests.Session()

    # ---- HTTP ----------------------------------------------------------

    @staticmethod
    def _is_retryable_request_error(exc: requests.RequestException) -> bool:
        if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
            return True
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            return exc.response.status_code in {408, 429, 500, 502, 503, 504}
        return False

    def _chat(self, prompt: str) -> str:
        request_payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a precise issue triage analyst. "
                        "Return only valid JSON. No markdown fences, no prose "
                        "outside the JSON object."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            # Force JSON-only output from Ollama (supported since 0.1.30+).
            "format": "json",
            "options": {"temperature": 0.1},
        }

        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(
                    f"{self.base_url}/api/chat",
                    json=request_payload,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
                return str(payload.get("message", {}).get("content", "")).strip()
            except requests.RequestException as exc:
                is_last_attempt = attempt >= self.max_retries
                if is_last_attempt or not self._is_retryable_request_error(exc):
                    raise

                backoff = self.retry_backoff_seconds * (2**attempt)
                logger.warning(
                    "Ollama request failed (%s). Retrying in %.1fs (%d/%d)...",
                    exc,
                    backoff,
                    attempt + 1,
                    self.max_retries,
                )
                if backoff > 0:
                    time.sleep(backoff)

        raise RuntimeError("unreachable")

    def ensure_available(self) -> None:
        try:
            response = self.session.get(
                f"{self.base_url}/api/tags",
                timeout=min(self.timeout_seconds, 10),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise OllamaUnavailableError(
                f"Unable to reach Ollama at {self.base_url}. "
                "Ensure Ollama is running and reachable."
            ) from exc

        # Best-effort verification that the requested model is pulled.
        try:
            tags = response.json().get("models", []) or []
            available = {str(m.get("name", "")) for m in tags}
            if available and self.model not in available:
                logger.warning(
                    "Ollama model %r not present locally. Available: %s. "
                    "Continuing; Ollama may pull on demand.",
                    self.model,
                    sorted(available),
                )
        except (ValueError, AttributeError):
            pass

    # ---- Prompt --------------------------------------------------------

    @staticmethod
    def _normalize(issues: list[IssueRecord]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for issue in issues:
            normalized.append(
                {
                    "key": issue.key,
                    "title": issue.issue_title,
                    "type": issue.issue_type,
                    "priority": issue.priority,
                    "status": issue.current_status,
                    "assignee": issue.assignee,
                    "account_name": issue.account_name,
                    "rancher_team": issue.rancher_team,
                    "resolution": issue.resolution,
                    "resolved_datetime": issue.resolved_datetime,
                    # description = problem statement; comments = resolution details
                    "problem_statement": (issue.description or "")[:_DESCRIPTION_CHAR_LIMIT],
                    "resolution_details": (issue.comments or "")[:_COMMENTS_CHAR_LIMIT],
                }
            )
        return normalized

    @staticmethod
    def _theme_schema() -> dict[str, Any]:
        return {
            "theme": "string — concise label for this cluster of issues",
            "problem_patterns": ["string — recurring problem descriptions inferred from problem_statement"],
            "observations": ["string — what the resolution_details reveal about how these were resolved or what was learned"],
            "affected_components": ["string"],
            "issue_keys": ["JIRA-123"],
            "confidence": "high|medium|low",
        }

    @staticmethod
    def _team_theme_schema() -> dict[str, Any]:
        return {
            "team": "string — Rancher team name, use '(no team)' when missing",
            "themes": ["string — concise team-specific themes based on recurring problem_statement patterns"],
            "potential_action_items": [
                "string — concrete actions this team can take to reduce recurrence or improve resolution"
            ],
            "representative_issue_keys": ["JIRA-123"],
        }

    @classmethod
    def _build_batch_prompt(cls, issues: list[IssueRecord], jql: str) -> str:
        normalized = cls._normalize(issues)
        serialized = json.dumps(normalized, indent=2)

        schema = {
            "source_jql": jql,
            "issue_count": len(issues),
            "themes": [cls._theme_schema()],
            "team_themes": [cls._team_theme_schema()],
            "cross_cutting_observations": [
                "string — patterns that cut across multiple themes or issues; "
                "focus on similarities and recurring problem areas"
            ],
        }

        return (
            "Analyze these Jira issues and identify recurring themes. "
            "Each issue has a 'problem_statement' (description field = the reported problem) "
            "and 'resolution_details' (comments = how the issue was resolved, root causes, and troubleshooting steps). "
            "IMPORTANT: Prioritize resolution_details (comments) heavily over problem_statement when extracting themes and root causes. "
            "The comments contain the actual diagnostic insights and solutions; use them as the primary source for themes and especially for actionable improvements. "
            "Problem patterns can draw from both, but action items MUST be derived primarily from resolution_details. "
            "Focus on recurring patterns, root causes, and solutions rather than just problem restatement. "
            "Also produce team-specific analysis using the 'rancher_team' field: for each team, extract themes and concrete, actionable improvements based on the resolution patterns observed in their issues. "
            "Return strictly valid JSON matching this schema shape and key names exactly:\n"
            f"{json.dumps(schema, indent=2)}\n\n"
            "If information is missing, use empty strings or empty arrays, not null.\n"
            f"Source JQL:\n{jql}\n\n"
            "Issue data:\n"
            f"{serialized}"
        )

    @classmethod
    def _build_synthesis_prompt(
        cls, batch_analyses: list[dict[str, Any]], jql: str, total_issues: int
    ) -> str:
        schema = {
            "source_jql": jql,
            "issue_count": total_issues,
            "themes": [cls._theme_schema()],
            "team_themes": [cls._team_theme_schema()],
            "cross_cutting_observations": [
                "string — patterns that cut across multiple themes or issues; "
                "focus on similarities and recurring problem areas"
            ],
        }

        serialized = json.dumps(batch_analyses, indent=2)
        return (
            "You have analyzed a large set of Jira issues in multiple batches. "
            "Below are the theme analyses from each batch. "
            "Synthesize them into a single coherent analysis: merge similar themes, eliminate duplicates, "
            "and surface the most important cross-cutting patterns shared across batches. "
            "Prioritize observations that recur across multiple batches — these are the most significant signals. "
            "When synthesizing team-specific themes and action items, emphasize patterns from resolution_details (comments) "
            "to ensure action items are concrete and grounded in real troubleshooting experience. "
            "Return strictly valid JSON matching this schema shape and key names exactly:\n"
            f"{json.dumps(schema, indent=2)}\n\n"
            "If information is missing, use empty strings or empty arrays, not null.\n"
            f"Batch analyses:\n{serialized}"
        )

    def _split_into_batches(self, issues: list[IssueRecord]) -> list[list[IssueRecord]]:
        """Split issues into batches whose serialized payload each fits within the limit."""
        if not issues:
            return []

        normalized = self._normalize(issues)
        batches: list[list[IssueRecord]] = []
        current_issues: list[IssueRecord] = []
        current_size = 0

        for issue, norm in zip(issues, normalized):
            item_size = len(json.dumps(norm))
            if current_issues and current_size + item_size > _BATCH_PAYLOAD_CHAR_LIMIT:
                batches.append(current_issues)
                current_issues = [issue]
                current_size = item_size
            else:
                current_issues.append(issue)
                current_size += item_size

        if current_issues:
            batches.append(current_issues)

        return batches

    # ---- JSON extraction -----------------------------------------------

    @staticmethod
    def _extract_json(content: str) -> dict[str, Any]:
        if not content:
            raise json.JSONDecodeError("empty response", content, 0)

        stripped = content.strip()

        # Strip ```json ... ``` or ``` ... ``` fences if a model ignored
        # format=json and wrapped its output anyway.
        fence_match = re.match(
            r"^```(?:json)?\s*(.*?)\s*```$", stripped, flags=re.DOTALL | re.IGNORECASE
        )
        if fence_match:
            stripped = fence_match.group(1).strip()

        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

        # Last-resort: pull the first balanced JSON object out of the text.
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = stripped[start : end + 1]
            return json.loads(candidate)

        raise json.JSONDecodeError("no JSON object found in response", content, 0)

    # ---- Public --------------------------------------------------------

    def analyze(self, issues: list[IssueRecord], jql: str) -> ThemeAnalysisResult:
        stats = _compute_stats(issues)
        batches = self._split_into_batches(issues)

        if not batches:
            return ThemeAnalysisResult(
                model=self.model,
                source_jql=jql,
                prompt_issue_count=0,
                structured={},
                raw_response="",
                stats=stats,
            )

        if len(batches) == 1:
            logger.info("Single batch (%d issues) — sending direct analysis prompt.", len(issues))
            prompt = self._build_batch_prompt(batches[0], jql)
            raw = self._chat(prompt)
            structured = self._extract_json(raw)
        else:
            logger.info(
                "Paginating AI analysis: %d issues split into %d batches.",
                len(issues),
                len(batches),
            )
            batch_analyses: list[dict[str, Any]] = []
            for idx, batch in enumerate(batches, start=1):
                logger.info("Analyzing batch %d/%d (%d issues)…", idx, len(batches), len(batch))
                prompt = self._build_batch_prompt(batch, jql)
                raw = self._chat(prompt)
                batch_analyses.append(self._extract_json(raw))

            logger.info("Synthesizing %d batch analyses…", len(batch_analyses))
            synthesis_prompt = self._build_synthesis_prompt(batch_analyses, jql, len(issues))
            raw = self._chat(synthesis_prompt)
            structured = self._extract_json(raw)

        return ThemeAnalysisResult(
            model=self.model,
            source_jql=jql,
            prompt_issue_count=len(issues),
            structured=structured,
            raw_response=raw,
            stats=stats,
        )


_WAITING_STATUS = "waiting for reporter"


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO 8601 timestamp, returning None on failure."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


def _compute_status_durations(
    created_dt: datetime,
    end_dt: datetime,
    transitions: list[dict[str, Any]],
) -> tuple[float, float]:
    """Return (time_waiting_days, time_actively_worked_days) for one issue.

    *time_waiting_days* is time spent in "Waiting for Reporter" status.
    *time_actively_worked_days* is total open time minus waiting time.
    """
    if not transitions:
        # No status transitions recorded; treat entire open period as active work.
        total = max((end_dt - created_dt).total_seconds() / 86400.0, 0.0)
        return 0.0, total

    # Reconstruct the status at creation from the first transition's from_status.
    initial_status = transitions[0].get("from_status", "")

    # Build a timeline of (datetime, status).
    timeline: list[tuple[datetime, str]] = [(created_dt, initial_status)]
    for t in transitions:
        ts = _parse_iso(t.get("timestamp", ""))
        if ts is None:
            continue
        timeline.append((ts, t.get("to_status", "")))
    timeline.append((end_dt, ""))  # sentinel

    # Ensure timeline is sorted in case changelog timestamps are out of order.
    timeline.sort(key=lambda x: x[0])

    waiting = 0.0
    active = 0.0
    for i in range(len(timeline) - 1):
        seg_start, status = timeline[i]
        seg_end, _ = timeline[i + 1]
        duration = max((seg_end - seg_start).total_seconds() / 86400.0, 0.0)
        if status.strip().casefold() == _WAITING_STATUS:
            waiting += duration
        else:
            active += duration

    return waiting, active


def _compute_stats(issues: list[IssueRecord]) -> IssueStats:
    """Compute deterministic statistics directly from issue records."""
    resolution_days: list[float] = []
    initial_response_days: list[float] = []
    time_actively_worked_days: list[float] = []
    time_waiting_days: list[float] = []

    closed_statuses = {
        "resolved",
        "closed",
        "done",
        "completed",
        "rejected",
    }
    closed_or_resolved_count = 0
    now = datetime.now(timezone.utc)

    for issue in issues:
        created_dt = _parse_iso(issue.created_datetime) if issue.created_datetime else None

        resolved_dt: datetime | None = None
        if issue.resolved_datetime:
            resolved_dt = _parse_iso(issue.resolved_datetime)

        if created_dt and resolved_dt:
            delta = (resolved_dt - created_dt).total_seconds() / 86400.0
            if delta >= 0:
                resolution_days.append(delta)

        is_closed_or_resolved = (
            bool((issue.resolved_datetime or "").strip())
            or bool((issue.resolution or "").strip())
            or (issue.current_status or "").strip().casefold() in closed_statuses
        )
        if is_closed_or_resolved:
            closed_or_resolved_count += 1

        # --- Time to initial response (first assignee comment) ---
        if created_dt and issue.assignee and issue.assignee != "Unassigned":
            try:
                comment_entries: list[dict[str, Any]] = json.loads(
                    issue.comment_authors_dates or "[]"
                )
                assignee_cf = issue.assignee.strip().casefold()
                for entry in comment_entries:
                    author = (entry.get("author") or "").strip().casefold()
                    if author == assignee_cf:
                        response_dt = _parse_iso(entry.get("created", ""))
                        if response_dt:
                            delta = (response_dt - created_dt).total_seconds() / 86400.0
                            if delta >= 0:
                                initial_response_days.append(delta)
                        break
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

        # --- Time actively worked / time waiting ---
        if created_dt:
            end_dt = resolved_dt if resolved_dt else now
            try:
                transitions: list[dict[str, Any]] = json.loads(
                    issue.status_history or "[]"
                )
            except (json.JSONDecodeError, TypeError, AttributeError):
                transitions = []
            waiting, active = _compute_status_durations(created_dt, end_dt, transitions)
            time_waiting_days.append(waiting)
            time_actively_worked_days.append(active)

    def _avg(vals: list[float]) -> float | None:
        return round(statistics.mean(vals), 2) if vals else None

    def _median(vals: list[float]) -> float | None:
        return round(statistics.median(vals), 2) if vals else None

    total_issues = len(issues)
    open_count = max(total_issues - closed_or_resolved_count, 0)

    customer_counts: Counter[str] = Counter()
    team_counts: Counter[str] = Counter()
    for issue in issues:
        name = (issue.account_name or "").strip()
        customer_counts[name if name else "(no account)"] += 1

        team = (issue.rancher_team or "").strip()
        team_counts[team if team else "(no team)"] += 1

    return IssueStats(
        total_issues=total_issues,
        closed_or_resolved_issues=closed_or_resolved_count,
        open_issues=open_count,
        avg_resolution_days=_avg(resolution_days),
        median_resolution_days=_median(resolution_days),
        avg_initial_response_days=_avg(initial_response_days),
        median_initial_response_days=_median(initial_response_days),
        avg_time_actively_worked_days=_avg(time_actively_worked_days),
        median_time_actively_worked_days=_median(time_actively_worked_days),
        avg_time_waiting_days=_avg(time_waiting_days),
        median_time_waiting_days=_median(time_waiting_days),
        issues_per_customer=dict(customer_counts.most_common()),
        issues_per_team=dict(team_counts.most_common()),
    )


def _jql_mentions_resolution_scope(jql: str) -> bool:
    """Return whether resolution-time metrics are relevant for this query."""
    normalized = jql.casefold()
    if any(token in normalized for token in (" resolved ", "resolutiondate", "resolution ")):
        return True

    resolved_statuses = (
        "resolved",
        "closed",
        "done",
        "completed",
        "rejected",
    )
    status_pattern = re.compile(r"status\s+(?:=|in)\s*(.+)")
    match = status_pattern.search(normalized)
    if not match:
        return False

    status_clause = match.group(1)
    return any(status in status_clause for status in resolved_statuses)


def _extract_scoped_customers_from_jql(jql: str) -> list[str]:
    """Best-effort parse for account/customer filters in JQL."""
    # Covers common field names and the known custom field id.
    field_pattern = r'(?:"?account\s*name"?|account|customfield_23901)'

    # Example: account in ("Acme", "Beta Corp")
    in_match = re.search(
        rf'{field_pattern}\s+in\s*\(([^\)]*)\)',
        jql,
        flags=re.IGNORECASE,
    )
    if in_match:
        raw_values = in_match.group(1)
        candidates = [part.strip() for part in raw_values.split(",")]
        cleaned = [item.strip('"\' ') for item in candidates if item.strip('"\' ')]
        return cleaned

    # Example: account = "Acme"
    eq_match = re.search(
        rf'{field_pattern}\s*=\s*("[^"]+"|\'[^\']+\'|[^\s\)]+)',
        jql,
        flags=re.IGNORECASE,
    )
    if eq_match:
        value = eq_match.group(1).strip().strip('"\'')
        return [value] if value else []

    return []


def write_theme_outputs(
    result: ThemeAnalysisResult,
    output_dir: Path,
    page_title: str = "Jira Issue Theme Analysis",
    base_name: str = "jira_issue_themes",
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    md_path = output_dir / f"{base_name}_{timestamp}.md"

    stats = result.stats
    # --- Markdown header ---
    lines = [
        f"# {page_title}",
        "",
        f"- Generated at (UTC): {timestamp}",
        f"- Model: {result.model}",
        f"- Total issues: {stats.total_issues}",
        "",
    ]

    lines.append("## JQL")
    lines.append("")
    lines.append("```jql")
    lines.append(result.source_jql)
    lines.append("```")
    lines.append("")

    lines.append("## Status Breakdown")
    lines.append("")
    lines.append(f"- Closed/Resolved issues: {stats.closed_or_resolved_issues}")
    lines.append(f"- Still open issues: {stats.open_issues}")
    lines.append("")

    # --- Statistics section ---
    if _jql_mentions_resolution_scope(result.source_jql):
        lines.append("## Statistics")
        lines.append("")
        if stats.avg_resolution_days is not None:
            lines.append(f"- Average time to resolution: **{stats.avg_resolution_days} days**")
        else:
            lines.append("- Average time to resolution: N/A")
        if stats.median_resolution_days is not None:
            lines.append(f"- Median time to resolution: **{stats.median_resolution_days} days**")
        else:
            lines.append("- Median time to resolution: N/A")
        lines.append("")

    # --- Time metrics (always shown) ---
    lines.append("## Time Metrics")
    lines.append("")
    lines.append("### Time to Initial Response")
    lines.append(
        "> Time from issue creation to the first comment posted by the assignee."
    )
    lines.append("")
    if stats.avg_initial_response_days is not None:
        lines.append(
            f"- Average time to initial response: **{stats.avg_initial_response_days} days**"
        )
    else:
        lines.append("- Average time to initial response: N/A")
    if stats.median_initial_response_days is not None:
        lines.append(
            f"- Median time to initial response: **{stats.median_initial_response_days} days**"
        )
    else:
        lines.append("- Median time to initial response: N/A")
    lines.append("")
    lines.append("### Time Actively Worked")
    lines.append(
        "> Time the issue was open and **not** in *Waiting for Reporter* status."
    )
    lines.append("")
    if stats.avg_time_actively_worked_days is not None:
        lines.append(
            f"- Average time actively worked: **{stats.avg_time_actively_worked_days} days**"
        )
    else:
        lines.append("- Average time actively worked: N/A")
    if stats.median_time_actively_worked_days is not None:
        lines.append(
            f"- Median time actively worked: **{stats.median_time_actively_worked_days} days**"
        )
    else:
        lines.append("- Median time actively worked: N/A")
    lines.append("")
    lines.append("### Time Waiting for Reporter")
    lines.append(
        "> Time the issue spent in *Waiting for Reporter* status."
    )
    lines.append("")
    if stats.avg_time_waiting_days is not None:
        lines.append(
            f"- Average time waiting for reporter: **{stats.avg_time_waiting_days} days**"
        )
    else:
        lines.append("- Average time waiting for reporter: N/A")
    if stats.median_time_waiting_days is not None:
        lines.append(
            f"- Median time waiting for reporter: **{stats.median_time_waiting_days} days**"
        )
    else:
        lines.append("- Median time waiting for reporter: N/A")
    lines.append("")

    # --- Issues by customer ---
    scoped_customers = _extract_scoped_customers_from_jql(result.source_jql)
    if len(scoped_customers) == 1:
        scoped = scoped_customers[0]
        lines.append("## Customer Scope")
        lines.append("")
        lines.append(f"- Query is scoped to a single customer: **{scoped}**")
        lines.append(
            f"- Issues in scope: {stats.issues_per_customer.get(scoped, 0)}"
        )
        lines.append("")
    else:
        lines.append("## Issues by Customer")
        lines.append("")
        if stats.issues_per_customer:
            for customer, count in stats.issues_per_customer.items():
                lines.append(f"- {customer}: {count}")
        else:
            lines.append("- No customer data available.")
        lines.append("")

    lines.append("## Issues by Rancher Team")
    lines.append("")
    if stats.issues_per_team:
        for team, count in stats.issues_per_team.items():
            lines.append(f"- {team}: {count}")
    else:
        lines.append("- No team data available.")
    lines.append("")

    # --- AI analysis ---
    analysis = result.structured
    themes = analysis.get("themes", []) if isinstance(analysis, dict) else []
    team_themes = analysis.get("team_themes", []) if isinstance(analysis, dict) else []
    observations = (
        analysis.get("cross_cutting_observations", []) if isinstance(analysis, dict) else []
    )

    # Cross-cutting observations come first — they are the primary insight.
    lines.append("## Cross-Cutting Observations")
    lines.append("")
    if observations:
        for item in observations:
            lines.append(f"- {item}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Themes")
    lines.append("")
    if not themes:
        lines.append("No themes identified.")
    else:
        for idx, theme in enumerate(themes, start=1):
            if not isinstance(theme, dict):
                continue
            lines.append(f"### {idx}. {theme.get('theme', 'Unnamed theme')}")
            lines.append(f"- Confidence: {theme.get('confidence', '')}")
            issue_keys = theme.get("issue_keys", [])
            if isinstance(issue_keys, list) and issue_keys:
                lines.append(f"- Issue keys: {', '.join(issue_keys)}")
            lines.append("- Problem patterns:")
            for item in theme.get("problem_patterns", []):
                lines.append(f"  - {item}")
            observations_theme = theme.get("observations", [])
            if observations_theme:
                lines.append("- Observations:")
                for item in observations_theme:
                    lines.append(f"  - {item}")
            components = theme.get("affected_components", [])
            if components:
                lines.append("- Affected components:")
                for item in components:
                    lines.append(f"  - {item}")
            lines.append("")

    lines.append("## Themes by Team")
    lines.append("")
    if not team_themes:
        lines.append("No team-specific themes identified.")
    else:
        for item in team_themes:
            if not isinstance(item, dict):
                continue
            team_name = item.get("team", "(no team)")
            lines.append(f"### {team_name}")

            themes_for_team = item.get("themes", [])
            lines.append("- Themes:")
            if isinstance(themes_for_team, list) and themes_for_team:
                for theme in themes_for_team:
                    lines.append(f"  - {theme}")
            else:
                lines.append("  - None")

            action_items = item.get("potential_action_items", [])
            lines.append("- Potential action items:")
            if isinstance(action_items, list) and action_items:
                for action in action_items:
                    lines.append(f"  - {action}")
            else:
                lines.append("  - None")

            representative_keys = item.get("representative_issue_keys", [])
            if isinstance(representative_keys, list) and representative_keys:
                lines.append(f"- Representative issue keys: {', '.join(representative_keys)}")
            lines.append("")

    md_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return md_path