# Jira Issue Data Extractor

A secure, CLI-based toolset for pulling issue data from Jira. The repo contains two independent entry points:

| Script | Purpose |
|---|---|
| `main.py` | Extract issues for any JQL query, compute time metrics, and run local AI theme analysis via Ollama. Writes a CSV and a Markdown report. |
| `jira_metrics_report.py` | Print a quick terminal metrics summary scoped to a fixed set of assignees — active issue counts, weekly averages, and wait-time breakdown. No files are written. |

## What It Extracts (`main.py`)
- Issue title, assignee, account name, Rancher team, reporter
- Created / updated / resolved date-time
- Current status, type, priority, resolution
- Description and comments
- Issue key (for traceability)
- Status change history (used for time-in-status metrics)

## Quick Start

### 1. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
```

**Jira Server / Data Center:**
- `JIRA_BASE_URL=https://jira.suse.com`
- `JIRA_API_TOKEN=<Personal Access Token>` — create at
  *Profile → Personal Access Tokens* in Jira.

---

## `main.py` — Theme Analysis & Time Metrics

Runs a JQL query, fetches the full issue set (including status changelog for time-in-status metrics), writes a CSV, and produces a Markdown report with AI-generated themes via a local Ollama instance.

### Run

```bash
python3 main.py 'project IN ("SURE", "NVSHAS") AND created >= -7d AND issuetype in ("Bug", "Escalation")'
```

### Output

Timestamped files written to `output/`:
- `jira_issues_<timestamp>.csv` — raw issue data
- `jira_issue_themes_<timestamp>.md` — Markdown report containing:
  - **Status breakdown** — closed vs. open counts
  - **Time metrics** (always shown):
    - *Time to initial response* — creation to first comment by the assignee
    - *Time actively worked* — open time excluding *Waiting for Reporter* periods
    - *Time waiting for reporter* — total time spent in *Waiting for Reporter* status
  - **Resolution statistics** (shown when JQL targets resolved issues):
    - Average and median time to resolution
  - **Issues by customer and team**
  - **AI-generated themes, cross-cutting observations, and team-specific action items**

If Ollama is unreachable or returns invalid JSON, extraction still completes and the tool prints a "Theme analysis skipped" message.

### CLI options

```bash
python3 main.py \
  --timeout 1200 \
  --ollama-retries 3 \
  --page-title 'Weekly Escalation Report' \
  --base-filename escalations_weekly \
  'project IN ("SURE", "NVSHAS") AND created >= -7d AND issuetype in ("Bug", "Escalation")'
```

| Flag | Default | Description |
|---|---|---|
| `--timeout` | `900` | Seconds per Ollama request |
| `--ollama-retries` | `2` | Retries for transient Ollama failures (timeouts, temporary connectivity, 5xx/429) |
| `--page-title` | `"Jira Issue Theme Analysis"` | Heading in the Markdown report |
| `--base-filename` | `"jira_issue_themes"` | Base name for output files |

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `JIRA_EXTRACTOR_OUTPUT_DIR` | `output` | Directory for output files |
| `OLLAMA_ENABLED` | `true` | Set to `false` to skip AI analysis |
| `OLLAMA_MODEL` | `llama3.1:8b` | Ollama model to use |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint (`OLLAMA_BASE_URL` is also accepted as a fallback) |

---

## `jira_metrics_report.py` — Assignee Metrics Dashboard

Prints a terminal summary for a fixed set of assignees: active issue counts, issues waiting for reporter, and weekly new/resolved averages. No files are written. Requires `JIRA_ASSIGNEES` to be set.

### Run

```bash
JIRA_ASSIGNEES="jane.doe,john.smith" python3 jira_metrics_report.py
```

Or set `JIRA_ASSIGNEES` in your `.env` file and run:

```bash
python3 jira_metrics_report.py
```

### CLI options

| Flag | Default | Description |
|---|---|---|
| `--weeks` | `12` | Rolling window for new/resolved weekly averages |
| `--projects` | `SURE,NVSHAS` | Comma-separated Jira project keys |

Example:

```bash
python3 jira_metrics_report.py --weeks 8 --projects SURE,NVSHAS,OBS
```

### Required environment variable

| Variable | Description |
|---|---|
| `JIRA_ASSIGNEES` | Comma-separated list of Jira usernames to scope the report to |

---

## `jira_phrase_extractor.py` — Phrase/Window Issue Extractor

Standalone phrase-based extractor with no AI theme analysis. It builds JQL from
`--phrase` and `--window`, fetches matching issues, and writes CSV output.

### Run

```bash
# Last year (default: 1y)
python3 jira_phrase_extractor.py --phrase "MetalLB"

# Last 9 months
python3 jira_phrase_extractor.py --phrase "MetalLB" --window 9m

# Last 2 years
python3 jira_phrase_extractor.py --phrase "MetalLB" --window 2y

# Scope to specific projects
python3 jira_phrase_extractor.py --phrase "MetalLB" --window 1y --projects SURE,NVSHAS

# Only bug issues
python3 jira_phrase_extractor.py --phrase "MetalLB" --window 2y --type Bug

# Multiple issue types
python3 jira_phrase_extractor.py --phrase "MetalLB" --window 2y --type Bug --type Escalation
```

Generated JQL looks like:

```text
text ~ "\"MetalLB\"" AND created >= "2025-06-04"
```

The extractor intentionally uses an absolute cutoff date for compatibility with
Jira instances that reject relative year/month period syntax like `-2y`.

Switch the date field for window filtering:

```bash
python3 jira_phrase_extractor.py --phrase "MetalLB" --window 9m --date-field updated
```

### CLI options

| Flag | Default | Description |
|---|---|---|
| `--phrase` | _(required)_ | Phrase to search for in issue text. |
| `--window` | `1y` | Time window (`1y`, `9m`, `2y`, `12w`, `30d`, etc.). |
| `--date-field` | `created` | Date field used for the window (`created` or `updated`). |
| `--projects` | _(unset)_ | Optional comma-separated project keys (`SURE,NVSHAS`). |
| `--type` | _(unset)_ | Optional issue type filter; repeat for multiple values (`--type Bug --type Escalation`). |
| `--base-filename` | `jira_phrase_issues` | Base filename for generated CSV output. |

---

## Notes
- Jira Server / DC uses `POST /rest/api/2/search` with `startAt`/`total` pagination.
- `main.py` fetches the status changelog per issue (`GET /rest/api/2/issue/{key}/changelog`) to compute time-in-status metrics. This adds one extra API call per issue.
- The "Account Name" field is hardcoded to `customfield_23901`.
- The "Rancher Team" field is hardcoded to `customfield_23900`.
- AI analysis runs only against your local Ollama endpoint. No issue data is sent to any hosted LLM service.

### Finding the Rancher Team custom field id

```bash
curl -sS \
  -H "Authorization: Bearer ${JIRA_API_TOKEN}" \
  -H "Accept: application/json" \
  "${JIRA_BASE_URL}/rest/api/2/field" | jq -r '.[] | select(.name == "Rancher Team") | .id'
```
