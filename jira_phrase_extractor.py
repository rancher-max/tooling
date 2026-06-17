from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

from jira_extractor.config import load_config
from jira_extractor.jira_client import JiraClient, write_outputs

_WINDOW_RE = re.compile(
    r"^\s*(\d+)\s*(y|year|years|m|month|months|w|week|weeks|d|day|days)\s*$",
    re.IGNORECASE,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract Jira issues that contain a phrase within a time window "
            "(separate from AI theme analysis)."
        )
    )
    parser.add_argument(
        "--phrase",
        required=True,
        help="Phrase to search for in Jira issue text, for example: MetalLB.",
    )
    parser.add_argument(
        "--window",
        default="1y",
        help="Time window such as 1y, 9m, 2y, 12w, or 30d (default: 1y).",
    )
    parser.add_argument(
        "--date-field",
        choices=["created", "updated"],
        default="created",
        help="Date field used with --window (default: created).",
    )
    parser.add_argument(
        "--projects",
        default="",
        help="Optional comma-separated project keys, for example: SURE,NVSHAS.",
    )
    parser.add_argument(
        "--type",
        dest="issue_types",
        action="append",
        default=[],
        help=(
            "Optional issue type filter. Repeat for multiple types, for example: "
            "--type Bug --type Escalation."
        ),
    )
    parser.add_argument(
        "--base-filename",
        default="jira_phrase_issues",
        help=(
            "Base filename for generated CSV output "
            "(timestamp and extension are appended)."
        ),
    )
    return parser.parse_args()


def _parse_window_to_cutoff_date(raw_window: str) -> str:
    match = _WINDOW_RE.match(raw_window)
    if not match:
        raise ValueError("--window must look like 1y, 9m, 2y, 12w, or 30d.")

    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError("--window amount must be greater than 0.")

    unit_raw = match.group(2).lower()
    days_map = {
        "y": 365,
        "year": 365,
        "years": 365,
        "m": 30,
        "month": 30,
        "months": 30,
        "w": 7,
        "week": 7,
        "weeks": 7,
        "d": 1,
        "day": 1,
        "days": 1,
    }
    days_back = amount * days_map[unit_raw]
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days_back)
    return cutoff.strftime("%Y-%m-%d")


def _parse_projects(raw_projects: str) -> list[str]:
    projects = [item.strip() for item in raw_projects.split(",") if item.strip()]
    if raw_projects.strip() and not projects:
        raise ValueError("--projects must include at least one project key.")
    return projects


def _parse_issue_types(raw_types: list[str]) -> list[str]:
    issue_types = [item.strip() for item in raw_types if item and item.strip()]
    if raw_types and not issue_types:
        raise ValueError("--type must include at least one non-empty value.")
    return issue_types


def _build_jql(
    phrase: str,
    window: str,
    date_field: str,
    projects: str,
    issue_types: list[str],
) -> str:
    phrase_clean = phrase.strip()
    if not phrase_clean:
        raise ValueError("--phrase must be a non-empty string.")

    cutoff_date = _parse_window_to_cutoff_date(window)
    escaped_phrase = phrase_clean.replace('"', r'\\"')
    phrase_clause = f'text ~ "\\\"{escaped_phrase}\\\""'

    clauses: list[str] = [phrase_clause, f'{date_field} >= "{cutoff_date}"']
    project_keys = _parse_projects(projects)
    if project_keys:
        quoted_keys = ", ".join(f'"{key}"' for key in project_keys)
        clauses.append(f"project in ({quoted_keys})")

    parsed_types = _parse_issue_types(issue_types)
    if parsed_types:
        quoted_types = ", ".join(
            f'"{issue_type.replace("\"", r"\\\"")}"'
            for issue_type in parsed_types
        )
        clauses.append(f"issuetype in ({quoted_types})")

    return " AND ".join(clauses)


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


def main() -> None:
    args = _parse_args()

    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.base_filename):
        raise SystemExit(
            "--base-filename may only contain letters, numbers, dot, underscore, and dash."
        )

    try:
        jql = _build_jql(
            phrase=args.phrase,
            window=args.window,
            date_field=args.date_field,
            projects=args.projects,
            issue_types=args.issue_types,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error

    config = load_config()
    client = JiraClient(config, fetch_changelog=False)

    print(f"Generated JQL: {jql}")

    try:
        records = client.search_issues(jql=jql)
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else "?"
        detail = _extract_http_error_message(error)
        print(f"Jira API error ({status}): {detail}", file=sys.stderr)
        raise SystemExit(1) from error
    except requests.RequestException as error:
        print(f"Network error talking to Jira: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    csv_path = write_outputs(
        records,
        config.output_dir,
        base_name=args.base_filename,
    )

    print(f"Issues extracted: {len(records)}")
    print(f"CSV output: {csv_path}")


if __name__ == "__main__":
    main()
