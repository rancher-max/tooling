from __future__ import annotations

import argparse
import os
from datetime import datetime

import requests

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

    client = JiraClient(config)

    try:
        active_issues = client.search_issues(jqls["active"])
        waiting_issues = client.search_issues(jqls["active_waiting_for_reporter"])
        new_issues_window = client.search_issues(jqls["new_created_window"])
        resolved_issues_window = client.search_issues(jqls["resolved_window"])
    except requests.HTTPError as error:
        print("Jira query failed with HTTP error.")
        print(f"Details: {_extract_http_error_message(error)}")
        _print_section("JQL attempted")
        for name, query in jqls.items():
            print(f"{name}: {query}")
        raise SystemExit(1) from error

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

    _print_section("Metrics")
    print(f"Average number of new issues per week: {avg_new_per_week:.2f}")
    print(f"Current number of active issues: {len(active_issues)}")
    print(
        "Current number of active issues waiting for reporter: "
        f"{len(waiting_issues)}"
    )
    print(f"Average number of resolved issues per week: {avg_resolved_per_week:.2f}")

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