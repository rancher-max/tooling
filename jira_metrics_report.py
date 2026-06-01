from __future__ import annotations

import argparse
import os
import statistics
from datetime import datetime

import requests

from jira_extractor.ai_analyzer import _compute_stats
from jira_extractor.config import load_config
from jira_extractor.jira_client import IssueRecord, JiraClient


DEFAULT_PROJECTS = ["SURE", "NVSHAS"]
DEFAULT_ACTIVE_STATUSES = ["In Triage", "In Development", "New", "Eng PR Review"]
DEFAULT_WAITING_FOR_REPORTER_STATUSES = ["Waiting for Reporter"]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate Jira metrics report with weekly averages and active issue "
            "counts for a fixed assignee/project scope."
        )
    )
    parser.add_argument(
        "--weeks",
        type=int,
        default=12,
        help="Rolling week window used for new/resolved weekly averages (default: 12).",
    )
    parser.add_argument(
        "--projects",
        default=",".join(DEFAULT_PROJECTS),
        help="Comma-separated Jira project keys.",
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
    active_statuses = _in_clause(DEFAULT_ACTIVE_STATUSES)
    waiting_statuses = _in_clause(DEFAULT_WAITING_FOR_REPORTER_STATUSES)
    historical_assignee_clause = _assignee_was_clause(assignees)

    return {
        "active": (
            f"{scope} AND status in ({active_statuses}) "
            "ORDER BY priority DESC, updated DESC"
        ),
        "active_waiting_for_reporter": (
            f"{scope} AND status in ({waiting_statuses}) "
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

    print("Starting Jira metrics collection...", flush=True)
    print("This can take a while for large issue sets.", flush=True)

    try:
        print("[1/4] Fetching active issues...", flush=True)
        active_issues = client.search_issues(jqls["active"])
        print(f"[1/4] Done. Retrieved {len(active_issues)} active issues.", flush=True)

        print("[2/4] Fetching active issues waiting for reporter...", flush=True)
        waiting_issues = client.search_issues(jqls["active_waiting_for_reporter"])
        print(
            f"[2/4] Done. Retrieved {len(waiting_issues)} waiting-for-reporter issues.",
            flush=True,
        )

        print(f"[3/4] Fetching issues created in the last {args.weeks} weeks...", flush=True)
        new_issues_window = client.search_issues(jqls["new_created_window"])
        print(
            f"[3/4] Done. Retrieved {len(new_issues_window)} newly created issues.",
            flush=True,
        )

        print(f"[4/4] Fetching resolved issues from the last {args.weeks} weeks...", flush=True)
        resolved_issues_window = client.search_issues(jqls["resolved_window"])
        print(
            f"[4/4] Done. Retrieved {len(resolved_issues_window)} resolved issues.",
            flush=True,
        )
    except requests.HTTPError as error:
        print("Jira query failed with HTTP error.")
        print(f"Details: {_extract_http_error_message(error)}")
        _print_section("JQL attempted")
        for name, query in jqls.items():
            print(f"{name}: {query}")
        raise SystemExit(1) from error

    print("Computing metrics...", flush=True)

    avg_new_per_week = len(new_issues_window) / args.weeks
    avg_resolved_per_week = len(resolved_issues_window) / args.weeks

    resolved_with_timestamp = [
        issue
        for issue in resolved_issues_window
        if _parse_jira_datetime(issue.resolved_datetime) is not None
    ]

    print("Jira Metrics Report")
    print("===================")
    print(f"Projects: {', '.join(projects)}")
    print(f"Assignees ({len(assignees)}): {', '.join(assignees)}")
    print(f"Rolling window (weeks): {args.weeks}")

    resolved_stats = _compute_stats(resolved_issues_window)
    active_stats = _compute_stats(active_issues)

    # Add context so a median of 0 is easy to interpret.
    resolved_waiting_days: list[float] = []
    for issue in resolved_issues_window:
        issue_stats = _compute_stats([issue])
        if issue_stats.avg_time_waiting_days is not None:
            resolved_waiting_days.append(issue_stats.avg_time_waiting_days)
    resolved_waiting_non_zero = [days for days in resolved_waiting_days if days > 0]
    resolved_waiting_non_zero_median = (
        round(statistics.median(resolved_waiting_non_zero), 2)
        if resolved_waiting_non_zero
        else None
    )
    resolved_waiting_any_count = len(resolved_waiting_non_zero)
    resolved_waiting_total = len(resolved_waiting_days)
    resolved_waiting_any_pct = (
        (resolved_waiting_any_count / resolved_waiting_total) * 100.0
        if resolved_waiting_total
        else 0.0
    )

    _print_section("Metrics")
    print(f"Average number of new issues per week: {avg_new_per_week:.2f}")
    print(f"Current number of active issues: {len(active_issues)}")
    print(
        "Current number of active issues waiting for reporter: "
        f"{len(waiting_issues)}"
    )
    print(f"Average number of resolved issues per week: {avg_resolved_per_week:.2f}")

    def _fmt(val: float | None) -> str:
        return f"{val} days" if val is not None else "N/A"

    _print_section(f"Time metrics — resolved issues (last {args.weeks} weeks)")
    print(
        f"  Avg time to initial response:  {_fmt(resolved_stats.avg_initial_response_days)}"
        f"  |  Median: {_fmt(resolved_stats.median_initial_response_days)}"
    )
    print(
        f"  Avg time actively worked:      {_fmt(resolved_stats.avg_time_actively_worked_days)}"
        f"  |  Median: {_fmt(resolved_stats.median_time_actively_worked_days)}"
    )
    print(
        f"  Avg time waiting for reporter: {_fmt(resolved_stats.avg_time_waiting_days)}"
        f"  |  Median: {_fmt(resolved_stats.median_time_waiting_days)}"
    )
    print(
        "  Resolved issues with any waiting time: "
        f"{resolved_waiting_any_count}/{resolved_waiting_total} "
        f"({resolved_waiting_any_pct:.1f}%)"
    )
    print(
        "  Median waiting time (only issues that waited): "
        f"{_fmt(resolved_waiting_non_zero_median)}"
    )
    print(
        f"  Avg time to resolution:        {_fmt(resolved_stats.avg_resolution_days)}"
        f"  |  Median: {_fmt(resolved_stats.median_resolution_days)}"
    )

    _print_section("Time metrics — currently active issues")
    print(
        f"  Avg time actively worked so far:      {_fmt(active_stats.avg_time_actively_worked_days)}"
        f"  |  Median: {_fmt(active_stats.median_time_actively_worked_days)}"
    )
    print(
        f"  Avg time waiting for reporter so far: {_fmt(active_stats.avg_time_waiting_days)}"
        f"  |  Median: {_fmt(active_stats.median_time_waiting_days)}"
    )

    _print_section("Data quality")
    print(
        "Resolved issues with parseable resolved timestamp: "
        f"{len(resolved_with_timestamp)}/{len(resolved_issues_window)}"
    )

    _print_section("JQL used")
    print("active:")
    print(jqls["active"])
    print("\nactive_waiting_for_reporter:")
    print(jqls["active_waiting_for_reporter"])
    print("\nnew_created_window:")
    print(jqls["new_created_window"])
    print("\nresolved_window:")
    print(jqls["resolved_window"])

    _print_issue_list("Active issues list", active_issues)
    _print_issue_list("Active issues waiting for reporter list", waiting_issues)


if __name__ == "__main__":
    main()