# Jira Issue Data Extractor

A secure, CLI-based extractor for pulling issue data from Jira using a single JQL query. After extraction, the tool automatically attempts local AI theme analysis through Ollama.

## What It Extracts
- Issue title
- Assignee
- Account Name
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
python3 main.py 'project = "SUSE Rancher Escalations" AND assignee in (mross) AND resolved >= -7d ORDER BY resolved DESC'
```

## Output
The tool writes timestamped files into output/:
- Issue JSON report
- Issue CSV report
- Theme JSON report (if Ollama is reachable)
- Theme Markdown report (if Ollama is reachable)

If Ollama is unreachable, or if the model returns invalid JSON, extraction still completes and the tool prints a "Theme analysis skipped" message.

## Runtime Configuration
The command-line interface accepts one input only: the JQL query string.

Optional runtime behavior can be controlled by environment variables:
- `JIRA_EXTRACTOR_OUTPUT_DIR` (default: `output`)
- `OLLAMA_ENABLED` (default: `true`; set to `false` to skip AI analysis)
- `OLLAMA_MODEL` (default: `llama3.1:8b`)
- `OLLAMA_HOST` (default: `http://localhost:11434`)

Compatibility note:
- `OLLAMA_BASE_URL` is still accepted as a fallback for existing setups.

## Notes
- Jira Server / DC uses `POST /rest/api/2/search` with `startAt`/`total`
  pagination.
- The "Account Name" field is hardcoded to `customfield_23901`.
- AI analysis runs only against your local Ollama endpoint. No issue data is
  sent to any hosted LLM service.
