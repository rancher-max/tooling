# Jira Issue Data Extractor

A secure, CLI-based extractor for pulling issue data from Jira using a single JQL query. After extraction, the tool automatically attempts local AI theme analysis through Ollama.

## What It Extracts
- Issue title
- Assignee
- Account Name
- Rancher Team
- Reporter
- Created date/time
- Updated date/time
- Current status
- Type
- Priority
- Resolution
- Resolved date/time
- Description
- Comments
- Issue key (for traceability)

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

### 3. Run extraction with one JQL query

```bash
python3 main.py 'project IN ("SURE", "NVSHAS") AND created >= -7d AND issuetype in ("Bug", "Escalation")'
```

## Output
The tool writes timestamped files into output/:
- Issue CSV report
- Theme Markdown report (if Ollama is reachable)

The theme report includes:
- Cross-cutting observations
- Global themes
- Team-specific themes and potential action items per Rancher Team

If Ollama is unreachable, or if the model returns invalid JSON, extraction still completes and the tool prints a "Theme analysis skipped" message.

## Runtime Configuration
The command-line interface accepts:
- Required positional argument: JQL query string
- Optional argument: `--timeout` (seconds per Ollama request, default: `900`)
- Optional argument: `--page-title` (title for Markdown report heading)
- Optional argument: `--base-filename` (base name for CSV/Markdown output files)

Example:

```bash
python3 main.py --timeout 1200 --page-title 'Weekly Report' --base-filename escalations_weekly 'project IN ("SURE", "NVSHAS") AND created >= -7d AND issuetype in ("Bug", "Escalation")'
```

Optional runtime behavior can be controlled by environment variables:
- `JIRA_EXTRACTOR_OUTPUT_DIR` (default: `output`)
- `OLLAMA_ENABLED` (default: `true`; set to `false` to skip AI analysis)
- `OLLAMA_MODEL` (default: `llama3.1:8b`)
- `OLLAMA_HOST` (default: `http://localhost:11434`)

For `jira_metrics_report.py`, `JIRA_ASSIGNEES` is required and must be a comma-separated list.

Compatibility note:
- `OLLAMA_BASE_URL` is still accepted as a fallback for existing setups.

## Notes
- Jira Server / DC uses `POST /rest/api/2/search` with `startAt`/`total`
  pagination.
- The "Account Name" field is hardcoded to `customfield_23901`.
- The "Rancher Team" field is hardcoded to `customfield_23900`.
- AI analysis runs only against your local Ollama endpoint. No issue data is
  sent to any hosted LLM service.

### Finding the Rancher Team custom field id

Use this command and share the `customfield_xxxxx` id it returns:

```bash
curl -sS \
  -H "Authorization: Bearer ${JIRA_API_TOKEN}" \
  -H "Accept: application/json" \
  "${JIRA_BASE_URL}/rest/api/2/field" | jq -r '.[] | select(.name == "Rancher Team") | .id'
```
