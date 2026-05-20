from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

import requests

from jira_extractor.ai_analyzer import (
    OllamaAnalyzer,
    OllamaUnavailableError,
    write_theme_outputs,
)
from jira_extractor.config import load_config
from jira_extractor.jira_client import JiraClient, write_outputs


logger = logging.getLogger("jira_extractor")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract Jira issue data using a single JQL query."
    )
    parser.add_argument(
        "jql",
        help="Jira Query Language (JQL) string used for extraction.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help=(
            "Timeout in seconds for each Ollama request "
            "(default: 900)."
        ),
    )
    parser.add_argument(
        "--page-title",
        default="Jira Issue Theme Analysis",
        help="Title used as the top heading in the generated Markdown report.",
    )
    parser.add_argument(
        "--base-filename",
        default="jira_issues",
        help=(
            "Base filename for generated CSV/Markdown outputs "
            "(timestamp and extension are appended)."
        ),
    )
    return parser.parse_args()


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def run() -> tuple[Path, int]:
    _configure_logging()
    args = parse_args()
    config = load_config()
    client = JiraClient(config)

    jql = args.jql.strip()
    if not jql:
        raise SystemExit("JQL argument must be a non-empty string.")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be greater than 0.")
    if not args.page_title.strip():
        raise SystemExit("--page-title must be a non-empty string.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.base_filename):
        raise SystemExit(
            "--base-filename may only contain letters, numbers, dot, underscore, and dash."
        )

    logger.info("Searching Jira: %s", jql)
    records = client.search_issues(jql=jql)

    csv_path = write_outputs(
        records,
        config.output_dir,
        base_name=args.base_filename,
    )

    print(f"JQL: {jql}")
    print(f"Issues extracted: {len(records)}")
    print(f"CSV output: {csv_path}")

    if not config.ollama_enabled:
        print("Theme analysis skipped: OLLAMA_ENABLED is false.")
        return csv_path, len(records)

    if not records:
        print("Theme analysis skipped: no issues returned.")
        return csv_path, len(records)

    analyzer = OllamaAnalyzer(
        model=config.ollama_model,
        base_url=config.ollama_host,
        timeout_seconds=args.timeout,
    )
    try:
        analyzer.ensure_available()
        analysis = analyzer.analyze(records, jql=jql)
        analysis_md = write_theme_outputs(
            analysis,
            config.output_dir,
            page_title=args.page_title,
            base_name=args.base_filename,
        )
        print(f"Theme analysis Markdown: {analysis_md}")
    except OllamaUnavailableError as error:
        print(f"Theme analysis skipped: {error}")
    except ValueError as error:
        print(f"Theme analysis skipped: model returned invalid JSON ({error}).")
    except requests.JSONDecodeError as error:
        print(f"Theme analysis skipped: model returned invalid JSON ({error}).")
    except requests.RequestException as error:
        print(f"Theme analysis skipped: Ollama request failed ({error}).")

    return csv_path, len(records)


def main() -> None:
    try:
        run()
    except ValueError as error:
        # Missing/invalid configuration.
        print(f"Configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else "?"
        print(f"Jira API error ({status}): {error}", file=sys.stderr)
        raise SystemExit(1) from error
    except requests.RequestException as error:
        print(f"Network error talking to Jira: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
