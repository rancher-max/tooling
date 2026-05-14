from __future__ import annotations

import argparse
import logging
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

    logger.info("Searching Jira: %s", jql)
    records = client.search_issues(jql=jql)

    csv_path = write_outputs(records, config.output_dir)

    print(f"JQL: {jql}")
    print(f"Issues extracted: {len(records)}")
    print(f"CSV output: {csv_path}")

    if not config.ollama_enabled:
        print("Theme analysis skipped: OLLAMA_ENABLED is false.")
        return csv_path, len(records)

    if not records:
        print("Theme analysis skipped: no issues returned.")
        return csv_path, len(records)

    analyzer = OllamaAnalyzer(model=config.ollama_model, base_url=config.ollama_host)
    try:
        analyzer.ensure_available()
        analysis = analyzer.analyze(records, jql=jql)
        analysis_md = write_theme_outputs(analysis, config.output_dir)
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
