from __future__ import annotations

import argparse
import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from jira_extractor.config import load_config
from jira_extractor.jira_client import IssueRecord, JiraClient


DEFAULT_PROJECTS = ["SURE", "NVSHAS"]
DEFAULT_TRIAGE_STATUSES = ["In Triage"]
DEFAULT_CODE_FIX_STATUSES = [
    "In Development",
    "ENG PR Review",
    "Eng PR Review",
    "To Test",
    "QA Working",
]
DEFAULT_WAITING_FOR_REPORTER_STATUSES = ["Waiting for Reporter"]
DEFAULT_NEW_STATUSES = ["New"]
BLOCKER_CRITICAL_PRIORITIES = {"blocker", "critical"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate Jira SLA and workflow metrics for a fixed assignee/project scope."
        )
    )
    parser.add_argument(
        "--weeks",
        type=int,
        default=12,
        help="Rolling week window used for new and resolved totals (default: 12).",
    )
    parser.add_argument(
        "--projects",
        default=",".join(DEFAULT_PROJECTS),
        help="Comma-separated Jira project keys.",
    )
    parser.add_argument(
        "--debug-acceptance",
        action="store_true",
        help=(
            "Print per-issue acceptance debug details, including assignee "
            "history and computed acceptance timestamp."
        ),
    )
    return parser.parse_args()


def _jql_quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def _in_clause(values: list[str]) -> str:
    return ", ".join(_jql_quote(v) for v in values)


def _parse_csv_values(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _scope_clause(projects: list[str], assignees: list[str]) -> str:
    return (
        f"project in ({_in_clause(projects)}) "
        f"AND assignee in ({_in_clause(assignees)})"
    )


def _assignee_was_clause(assignees: list[str]) -> str:
    """Build 'assignee was (user1) OR assignee was (user2)...' clause."""
    parts = [f"assignee was {_jql_quote(user)}" for user in assignees]
    return " OR ".join(parts)


def build_jqls(projects: list[str], assignees: list[str], weeks: int) -> dict[str, str]:
    scope = _scope_clause(projects, assignees)
    triage_statuses = _in_clause(DEFAULT_TRIAGE_STATUSES)
    code_fix_statuses = _in_clause(DEFAULT_CODE_FIX_STATUSES)
    waiting_statuses = _in_clause(DEFAULT_WAITING_FOR_REPORTER_STATUSES)
    historical_assignee_clause = _assignee_was_clause(assignees)

    return {
        "current_triage": (
            f"{scope} AND status in ({triage_statuses}) "
            "ORDER BY priority DESC, updated DESC"
        ),
        "current_code_fix": (
            f"{scope} AND status in ({code_fix_statuses}) "
            "ORDER BY priority DESC, updated DESC"
        ),
        "current_waiting_for_reporter": (
            f"{scope} AND status in ({waiting_statuses}) "
            "ORDER BY priority DESC, updated DESC"
        ),
        "current_open": (
            f"{scope} AND statusCategory != Done "
            "ORDER BY priority DESC, updated DESC"
        ),
        "new_created_window": (
            f"project in ({_in_clause(projects)}) AND created >= startOfWeek(\"-{weeks}w\") "
            f"AND ({historical_assignee_clause}) "
            "ORDER BY created DESC"
        ),
        "resolved_window": (
            f"project in ({_in_clause(projects)}) AND statusCategory = Done "
            f"AND resolved >= startOfWeek(\"-{weeks}w\") "
            f"AND ({historical_assignee_clause}) "
            "ORDER BY resolved DESC"
        ),
    }


def _parse_jira_datetime(value: str) -> datetime | None:
    if not value:
        return None

    for layout in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, layout)
        except ValueError:
            continue
    return None


def _status_cf(value: str) -> str:
    return (value or "").strip().casefold()


def _statuses_cf(values: list[str]) -> set[str]:
    return {_status_cf(value) for value in values}


def _assignee_tokens(value: str) -> set[str]:
    """Build a tolerant token set for assignee matching.

    Handles display names, usernames in brackets, and email-like forms.
    """
    raw = (value or "").strip()
    if not raw:
        return set()

    tokens: set[str] = set()
    base = raw.casefold()
    tokens.add(base)

    bracket_matches = re.findall(r"\[([^\]]+)\]", raw)
    for match in bracket_matches:
        token = match.strip().casefold()
        if token:
            tokens.add(token)

    for part in re.split(r"[\s,;]+", raw):
        part_cf = part.strip().casefold()
        if part_cf:
            tokens.add(part_cf)
        if "@" in part_cf:
            local = part_cf.split("@", 1)[0].strip()
            if local:
                tokens.add(local)

    return tokens


def _assignee_token_union(values: list[str]) -> set[str]:
    union: set[str] = set()
    for value in values:
        union.update(_assignee_tokens(value))
    return union


def _parse_status_history(issue: IssueRecord) -> list[dict[str, str]]:
    try:
        raw = json.loads(issue.status_history or "[]")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return []

    transitions: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        timestamp = str(entry.get("timestamp", ""))
        if _parse_jira_datetime(timestamp) is None:
            continue
        transitions.append(
            {
                "timestamp": timestamp,
                "from_status": str(entry.get("from_status", "")),
                "to_status": str(entry.get("to_status", "")),
            }
        )

    return sorted(transitions, key=lambda item: item["timestamp"])


def _parse_assignee_history(issue: IssueRecord) -> list[dict[str, str]]:
    try:
        raw = json.loads(issue.assignee_history or "[]")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return []

    transitions: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        timestamp = str(entry.get("timestamp", ""))
        if _parse_jira_datetime(timestamp) is None:
            continue
        transitions.append(
            {
                "timestamp": timestamp,
                "from_assignee": str(entry.get("from_assignee", "")),
                "to_assignee": str(entry.get("to_assignee", "")),
                "from_assignee_id": str(entry.get("from_assignee_id", "")),
                "to_assignee_id": str(entry.get("to_assignee_id", "")),
            }
        )

    return sorted(transitions, key=lambda item: item["timestamp"])


def _parse_priority_history(issue: IssueRecord) -> list[dict[str, str]]:
    try:
        raw = json.loads(issue.priority_history or "[]")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return []

    transitions: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        timestamp = str(entry.get("timestamp", ""))
        if _parse_jira_datetime(timestamp) is None:
            continue
        transitions.append(
            {
                "timestamp": timestamp,
                "from_priority": str(entry.get("from_priority", "")),
                "to_priority": str(entry.get("to_priority", "")),
                "from_priority_id": str(entry.get("from_priority_id", "")),
                "to_priority_id": str(entry.get("to_priority_id", "")),
            }
        )

    return sorted(transitions, key=lambda item: item["timestamp"])


def _priority_at_datetime(issue: IssueRecord, point_dt: datetime) -> str:
    transitions = _parse_priority_history(issue)

    if transitions:
        initial_priority = transitions[0].get("from_priority", "")
        current_priority = initial_priority or issue.priority
        for transition in transitions:
            ts = _parse_jira_datetime(transition.get("timestamp", ""))
            if ts is None:
                continue
            if ts > point_dt:
                break
            to_priority = transition.get("to_priority", "")
            if to_priority:
                current_priority = to_priority
        return current_priority

    return issue.priority


def _business_days_between(start: datetime, end: datetime) -> float:
    if end <= start:
        return 0.0

    current = start
    total_seconds = 0.0
    while current < end:
        next_midnight = (current + timedelta(days=1)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        segment_end = min(next_midnight, end)
        if current.weekday() < 5:
            total_seconds += max((segment_end - current).total_seconds(), 0.0)
        current = segment_end

    return total_seconds / 86400.0


def _find_first_assignee_comment_datetime(
    issue: IssueRecord,
    *,
    not_before: datetime | None = None,
) -> datetime | None:
    if not issue.assignee or issue.assignee == "Unassigned":
        return None

    try:
        entries = json.loads(issue.comment_authors_dates or "[]")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None

    assignee_cf = _status_cf(issue.assignee)
    times: list[datetime] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        author_cf = _status_cf(str(entry.get("author", "")))
        if author_cf != assignee_cf:
            continue
        comment_dt = _parse_jira_datetime(str(entry.get("created", "")))
        if comment_dt is None:
            continue
        if not_before is not None and comment_dt < not_before:
            continue
        times.append(comment_dt)

    return min(times) if times else None


def _find_acceptance_details(
    issue: IssueRecord,
    tracked_assignee_tokens: set[str],
) -> tuple[datetime | None, str, dict[str, Any]]:
    created_dt = _parse_jira_datetime(issue.created_datetime)
    if created_dt is None:
        return None, "created_datetime_unparseable", {}

    transitions = _parse_assignee_history(issue)

    for transition in transitions:
        transition_dt = _parse_jira_datetime(transition["timestamp"])
        if transition_dt is None or transition_dt < created_dt:
            continue

        to_assignee = transition.get("to_assignee", "")
        to_assignee_id = transition.get("to_assignee_id", "")
        transition_tokens = _assignee_tokens(to_assignee) | _assignee_tokens(to_assignee_id)

        if transition_tokens & tracked_assignee_tokens:
            return transition_dt, "assignee_transition_to_tracked", {
                "to_assignee": to_assignee,
                "to_assignee_id": to_assignee_id,
                "timestamp": transition.get("timestamp", ""),
                "matched_tokens": sorted(transition_tokens & tracked_assignee_tokens),
            }

    # Fallback for Jira instances where changelog assignee identity formats
    # don't match configured values (e.g., display name only, missing user key).
    # Since the source issue set is already scoped by "assignee was (...)",
    # use the first real assignment event instead of measuring until now.
    for transition in transitions:
        transition_dt = _parse_jira_datetime(transition["timestamp"])
        if transition_dt is None or transition_dt < created_dt:
            continue

        to_assignee = (transition.get("to_assignee", "") or "").strip()
        to_assignee_id = (transition.get("to_assignee_id", "") or "").strip()
        if to_assignee or to_assignee_id:
            return transition_dt, "fallback_first_assignment_in_scoped_dataset", {
                "to_assignee": to_assignee,
                "to_assignee_id": to_assignee_id,
                "timestamp": transition.get("timestamp", ""),
            }

    # Fallback when changelog has no assignee transitions but issue is currently
    # assigned to a tracked assignee: assume acceptance at creation.
    current_assignee_tokens = _assignee_tokens(issue.assignee)
    if current_assignee_tokens & tracked_assignee_tokens:
        return created_dt, "fallback_current_assignee_matches_tracked", {
            "current_assignee": issue.assignee,
            "matched_tokens": sorted(current_assignee_tokens & tracked_assignee_tokens),
        }

    return None, "never_assigned_to_tracked_assignees", {}

def _format_assignee_transition(transition: dict[str, str]) -> str:
    from_assignee = transition.get("from_assignee") or transition.get("from_assignee_id") or "(unassigned)"
    to_assignee = transition.get("to_assignee") or transition.get("to_assignee_id") or "(unassigned)"
    ts = transition.get("timestamp", "")
    to_id = transition.get("to_assignee_id", "")
    from_id = transition.get("from_assignee_id", "")
    return f"{ts} | {from_assignee} [{from_id}] -> {to_assignee} [{to_id}]"


def _duration_in_statuses_days(
    issue: IssueRecord,
    target_statuses_cf: set[str],
    now_dt: datetime,
) -> float | None:
    created_dt = _parse_jira_datetime(issue.created_datetime)
    if created_dt is None:
        return None

    resolved_dt = _parse_jira_datetime(issue.resolved_datetime)
    end_dt = resolved_dt if resolved_dt is not None else now_dt
    if end_dt < created_dt:
        return None

    transitions = _parse_status_history(issue)
    if not transitions:
        current_status_cf = _status_cf(issue.current_status)
        total_days = max((end_dt - created_dt).total_seconds() / 86400.0, 0.0)
        return total_days if current_status_cf in target_statuses_cf else 0.0

    initial_status = transitions[0].get("from_status", "")
    timeline: list[tuple[datetime, str]] = [(created_dt, initial_status)]
    for transition in transitions:
        ts = _parse_jira_datetime(transition["timestamp"])
        if ts is None:
            continue
        timeline.append((ts, transition["to_status"]))
    timeline.append((end_dt, ""))
    timeline.sort(key=lambda item: item[0])

    total_days = 0.0
    for idx in range(len(timeline) - 1):
        seg_start, status = timeline[idx]
        seg_end, _ = timeline[idx + 1]
        if seg_end <= seg_start:
            continue
        if _status_cf(status) not in target_statuses_cf:
            continue
        total_days += (seg_end - seg_start).total_seconds() / 86400.0

    return max(total_days, 0.0)


def _format_issue(issue: IssueRecord) -> str:
    assignee = issue.assignee or "Unassigned"
    summary = issue.issue_title.strip() or "(no summary)"
    return f"- {issue.key} | {assignee} | {issue.current_status} | {summary}"


def _print_section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def _extract_http_error_message(error: requests.HTTPError) -> str:
    response = error.response
    if response is None:
        return str(error)
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or str(error)

    if isinstance(payload, dict):
        details: list[str] = []
        messages = payload.get("errorMessages")
        if isinstance(messages, list):
            details.extend(str(msg) for msg in messages)
        errors = payload.get("errors")
        if isinstance(errors, dict):
            details.extend(f"{field}: {message}" for field, message in errors.items())
        if details:
            return " | ".join(details)

    return str(payload)


def _print_issue_list(title: str, issues: list[IssueRecord]) -> None:
    _print_section(f"{title}: {len(issues)}")
    if not issues:
        print("- none")
        return
    for issue in issues:
        print(_format_issue(issue))


def _average(values: list[float]) -> float | None:
    return round(statistics.mean(values), 2) if values else None


def _fmt_days(value: float | None) -> str:
    return f"{value:.2f} days" if value is not None else "N/A"


def _fmt_pct(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "N/A"
    pct = (numerator / denominator) * 100.0
    return f"{pct:.2f}%"


def _print_conditional_issue_list(title: str, issues: list[IssueRecord]) -> None:
    if not issues:
        return
    print(f"{title}:")
    for issue in issues:
        print(_format_issue(issue))


def main() -> None:
    args = _parse_args()

    if args.weeks <= 0:
        raise SystemExit("--weeks must be greater than 0")

    # load_config also loads .env, so JIRA_ASSIGNEES may come from either shell env or .env.
    config = load_config()

    assignees_raw = os.getenv("JIRA_ASSIGNEES", "")
    assignees = _parse_csv_values(assignees_raw)
    projects = _parse_csv_values(args.projects)

    if not assignees:
        raise SystemExit("JIRA_ASSIGNEES must include at least one comma-separated value")
    if not projects:
        raise SystemExit("--projects must include at least one value")

    jqls = build_jqls(projects, assignees, args.weeks)

    client = JiraClient(config, fetch_changelog=True)

    try:
        print("Starting Jira metrics collection...", flush=True)
        print("[1/6] Fetching current triage issues...", flush=True)
        triage_issues = client.search_issues(jqls["current_triage"])
        print(f"[1/6] Retrieved {len(triage_issues)} triage issues.", flush=True)

        print("[2/6] Fetching current code-fix issues...", flush=True)
        code_fix_issues = client.search_issues(jqls["current_code_fix"])
        print(f"[2/6] Retrieved {len(code_fix_issues)} code-fix issues.", flush=True)

        print("[3/6] Fetching current waiting-for-reporter issues...", flush=True)
        waiting_issues = client.search_issues(jqls["current_waiting_for_reporter"])
        print(
            f"[3/6] Retrieved {len(waiting_issues)} waiting-for-reporter issues.",
            flush=True,
        )

        print("[4/6] Fetching current open issues for stale check...", flush=True)
        open_issues = client.search_issues(jqls["current_open"])
        print(f"[4/6] Retrieved {len(open_issues)} open issues.", flush=True)

        print(f"[5/6] Fetching new issues in last {args.weeks} weeks...", flush=True)
        new_issues_window = client.search_issues(jqls["new_created_window"])
        print(f"[5/6] Retrieved {len(new_issues_window)} new issues.", flush=True)

        print(f"[6/6] Fetching resolved issues in last {args.weeks} weeks...", flush=True)
        resolved_issues_window = client.search_issues(jqls["resolved_window"])
        print(f"[6/6] Retrieved {len(resolved_issues_window)} resolved issues.", flush=True)
    except requests.HTTPError as error:
        print(f"Jira query failed: {_extract_http_error_message(error)}")
        raise SystemExit(1) from error

    print("Computing derived metrics...", flush=True)

    # Output header confirming scope
    print("\n" + "=" * 60)
    print(f"Metrics Report - Scoped to {len(assignees)} assignee(s):")
    print(f"  {', '.join(assignees)}")
    print(f"  Projects: {', '.join(projects)}")
    print(f"  Rolling window: {args.weeks} weeks")
    if args.debug_acceptance:
        print(f"  Tracked assignee tokens: {', '.join(sorted(_assignee_token_union(assignees)))}")
    print("=" * 60)

    now_dt = datetime.now(timezone.utc)
    tracked_assignee_tokens = _assignee_token_union(assignees)
    triage_statuses_cf = _statuses_cf(DEFAULT_TRIAGE_STATUSES)
    code_fix_statuses_cf = _statuses_cf(DEFAULT_CODE_FIX_STATUSES)
    waiting_statuses_cf = _statuses_cf(DEFAULT_WAITING_FOR_REPORTER_STATUSES)

    initial_response_days: list[float] = []
    triage_days: list[float] = []
    code_fix_days: list[float] = []
    waiting_days: list[float] = []
    resolution_days: list[float] = []

    blocker_critical_acceptance_total = 0
    blocker_critical_acceptance_over_1d = 0
    non_blocker_critical_acceptance_total = 0
    non_blocker_critical_acceptance_within_1d = 0
    non_blocker_critical_acceptance_within_2d = 0
    non_blocker_critical_acceptance_within_3d = 0
    non_blocker_critical_acceptance_within_4d = 0
    all_acceptance_total = 0
    all_acceptance_over_5d = 0

    blocker_critical_acceptance_to_response_total = 0
    blocker_critical_acceptance_to_response_over_1d = 0
    non_blocker_critical_acceptance_to_response_total = 0
    non_blocker_critical_acceptance_to_response_over_5d = 0

    blocker_critical_acceptance_over_1d_issues: list[IssueRecord] = []
    blocker_critical_acceptance_to_response_over_1d_issues: list[IssueRecord] = []
    acceptance_debug_rows: list[str] = []

    for issue in new_issues_window:
        created_dt = _parse_jira_datetime(issue.created_datetime)
        if created_dt is None:
            continue

        first_response_dt = _find_first_assignee_comment_datetime(issue)
        if first_response_dt is not None:
            initial_response_days.append(
                max((first_response_dt - created_dt).total_seconds() / 86400.0, 0.0)
            )

        triage_duration = _duration_in_statuses_days(issue, triage_statuses_cf, now_dt)
        if triage_duration is not None:
            triage_days.append(triage_duration)

        code_fix_duration = _duration_in_statuses_days(issue, code_fix_statuses_cf, now_dt)
        if code_fix_duration is not None:
            code_fix_days.append(code_fix_duration)

        waiting_duration = _duration_in_statuses_days(issue, waiting_statuses_cf, now_dt)
        if waiting_duration is not None:
            waiting_days.append(waiting_duration)

        accepted_dt, acceptance_reason, acceptance_meta = _find_acceptance_details(
            issue,
            tracked_assignee_tokens,
        )
        acceptance_end_dt = accepted_dt if accepted_dt is not None else now_dt
        acceptance_business_days = _business_days_between(created_dt, acceptance_end_dt)

        if args.debug_acceptance:
            assignee_history = _parse_assignee_history(issue)
            history_lines = [
                f"    - {_format_assignee_transition(transition)}"
                for transition in assignee_history
            ]
            if not history_lines:
                history_lines = ["    - (none)"]

            accepted_str = accepted_dt.isoformat() if accepted_dt is not None else "None"
            meta_str = ", ".join(
                f"{key}={value}" for key, value in acceptance_meta.items()
            )
            if not meta_str:
                meta_str = "(none)"

            acceptance_debug_rows.append(
                "\n".join(
                    [
                        f"Issue {issue.key}",
                        f"  priority: {issue.priority}",
                        f"  created: {issue.created_datetime}",
                        f"  current assignee: {issue.assignee}",
                        f"  accepted_at: {accepted_str}",
                        f"  acceptance_business_days: {acceptance_business_days:.4f}",
                        f"  acceptance_reason: {acceptance_reason}",
                        f"  acceptance_meta: {meta_str}",
                        "  assignee_history:",
                        *history_lines,
                    ]
                )
            )

        all_acceptance_total += 1
        if acceptance_business_days > 5.0:
            all_acceptance_over_5d += 1

        priority_eval_dt = accepted_dt if accepted_dt is not None else created_dt
        priority_at_acceptance = _priority_at_datetime(issue, priority_eval_dt)
        is_blocker_critical = (
            _status_cf(priority_at_acceptance) in BLOCKER_CRITICAL_PRIORITIES
        )

        if args.debug_acceptance:
            acceptance_debug_rows[-1] = (
                acceptance_debug_rows[-1]
                + "\n"
                + f"  priority_at_acceptance: {priority_at_acceptance}"
            )

        if is_blocker_critical:
            blocker_critical_acceptance_total += 1
            if acceptance_business_days > 1.0:
                blocker_critical_acceptance_over_1d += 1
                blocker_critical_acceptance_over_1d_issues.append(issue)
        else:
            non_blocker_critical_acceptance_total += 1
            if accepted_dt is not None and acceptance_business_days <= 1.0:
                non_blocker_critical_acceptance_within_1d += 1
            if accepted_dt is not None and acceptance_business_days <= 2.0:
                non_blocker_critical_acceptance_within_2d += 1
            if accepted_dt is not None and acceptance_business_days <= 3.0:
                non_blocker_critical_acceptance_within_3d += 1
            if accepted_dt is not None and acceptance_business_days <= 4.0:
                non_blocker_critical_acceptance_within_4d += 1

        if accepted_dt is None:
            continue

        response_after_acceptance_dt = _find_first_assignee_comment_datetime(
            issue,
            not_before=accepted_dt,
        )
        response_business_days = _business_days_between(
            accepted_dt,
            response_after_acceptance_dt if response_after_acceptance_dt is not None else now_dt,
        )

        if is_blocker_critical:
            blocker_critical_acceptance_to_response_total += 1
            if response_business_days > 1.0:
                blocker_critical_acceptance_to_response_over_1d += 1
                blocker_critical_acceptance_to_response_over_1d_issues.append(issue)
        else:
            non_blocker_critical_acceptance_to_response_total += 1
            if response_business_days > 5.0:
                non_blocker_critical_acceptance_to_response_over_5d += 1

    for issue in resolved_issues_window:
        created_dt = _parse_jira_datetime(issue.created_datetime)
        resolved_dt = _parse_jira_datetime(issue.resolved_datetime)
        if created_dt is None or resolved_dt is None:
            continue
        if resolved_dt < created_dt:
            continue
        resolution_days.append((resolved_dt - created_dt).total_seconds() / 86400.0)

    stale_cutoff = now_dt - timedelta(days=14)
    stale_issues: list[IssueRecord] = []
    for issue in open_issues:
        updated_dt = _parse_jira_datetime(issue.updated_datetime)
        if updated_dt is None:
            continue
        if updated_dt < stale_cutoff:
            stale_issues.append(issue)

    stale_issues.sort(key=lambda issue: issue.updated_datetime)

    print("\nMetrics:")
    print(f"Total number of new issues: {len(new_issues_window)}")
    print(f"Current number of issues in triage: {len(triage_issues)}")
    print(
        "Current number of issues with a code fix being worked on: "
        f"{len(code_fix_issues)}"
    )
    print(f"Current number of issues waiting for reporter: {len(waiting_issues)}")
    print(f"Total number of issues resolved: {len(resolved_issues_window)}")
    print(f"Average time of initial responses: {_fmt_days(_average(initial_response_days))}")
    print(f"Average time of issues in triage: {_fmt_days(_average(triage_days))}")
    print(
        "Average time of issues with a code fix being worked on: "
        f"{_fmt_days(_average(code_fix_days))}"
    )
    print(f"Average time of issues waiting for reporter: {_fmt_days(_average(waiting_days))}")
    print(f"Average time of issue resolution: {_fmt_days(_average(resolution_days))}")

    print("List of issues without any update for more than 2 calendar weeks:")
    if not stale_issues:
        print("- none")
    else:
        for issue in stale_issues:
            print(_format_issue(issue))

    print(
        "Percent of blocker and critical issues that took more than 1 business day "
        f"to be accepted: {_fmt_pct(blocker_critical_acceptance_over_1d, blocker_critical_acceptance_total)}"
    )
    _print_conditional_issue_list(
        "Blocker/Critical issues that took more than 1 business day to be accepted",
        blocker_critical_acceptance_over_1d_issues,
    )
    print(
        "Percent of non-blocker/critical issues that were accepted within 1 business day: "
        f"{_fmt_pct(non_blocker_critical_acceptance_within_1d, non_blocker_critical_acceptance_total)}"
    )
    print(
        "Percent of non-blocker/critical issues that were accepted within 2 business days: "
        f"{_fmt_pct(non_blocker_critical_acceptance_within_2d, non_blocker_critical_acceptance_total)}"
    )
    print(
        "Percent of non-blocker/critical issues that were accepted within 3 business days: "
        f"{_fmt_pct(non_blocker_critical_acceptance_within_3d, non_blocker_critical_acceptance_total)}"
    )
    print(
        "Percent of non-blocker/critical issues that were accepted within 4 business days: "
        f"{_fmt_pct(non_blocker_critical_acceptance_within_4d, non_blocker_critical_acceptance_total)}"
    )
    print(
        "Percent of issues that took more than 5 business days to be accepted: "
        f"{_fmt_pct(all_acceptance_over_5d, all_acceptance_total)}"
    )
    print(
        "Percent of blocker and critical issues that took more than 1 business day "
        "from acceptance to have an initial response: "
        f"{_fmt_pct(blocker_critical_acceptance_to_response_over_1d, blocker_critical_acceptance_to_response_total)}"
    )
    _print_conditional_issue_list(
        "Blocker/Critical issues that took more than 1 business day from acceptance to initial response",
        blocker_critical_acceptance_to_response_over_1d_issues,
    )
    print(
        "Percent of non-blocker/critical issues that took more than 5 business days "
        "from acceptance to have an initial response: "
        f"{_fmt_pct(non_blocker_critical_acceptance_to_response_over_5d, non_blocker_critical_acceptance_to_response_total)}"
    )

    if args.debug_acceptance:
        _print_section("Acceptance debug")
        print(
            "Accepted is defined as first assignee transition where to_assignee "
            "matches any JIRA_ASSIGNEES entry."
        )
        if not acceptance_debug_rows:
            print("- none")
        else:
            for row in acceptance_debug_rows:
                print(row)


if __name__ == "__main__":
    main()
