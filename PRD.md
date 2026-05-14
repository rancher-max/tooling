# Product Requirements Document: Jira Issue Extractor and Local Theme Analyzer

## 1. Purpose
Build a lightweight, secure command-line tool that:
- Extracts selected issue data from an internal Jira instance
- Uses a single user-provided JQL query as the only extraction input
- Uses a local LLM (Ollama) to infer recurring problem and resolution themes

## 2. Primary Use Cases
### 2.1 JQL-Based Extraction and Analysis
Users provide one JQL query string and the tool:
- Extracts all matching issues
- Exports normalized issue data
- Runs local AI analysis on extracted records (when Ollama is available)

Example query:

project = "SUSE Rancher Escalations" AND assignee in (mross) AND resolved >= -7d ORDER BY resolved DESC

### 2.2 Theme Discovery
Users can analyze extracted issues to identify:
- What problems recurred
- Typical resolution patterns
- Impacted components and clusters of similar issues

## 3. Required Data Fields
The extractor must output the following per issue:
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

Additional field included for traceability:
- Jira issue key

## 4. Functional Requirements
1. Support credentials from environment variables and optional local .env file.
2. Accept one required JQL query string from CLI input.
3. Fetch all pages of results from Jira Search API.
4. Convert Jira rich-text (ADF) descriptions/comments to readable plain text.
5. Export results to:
- JSON (complete structured output)
- CSV (analyst-friendly tabular output)
6. Optional local AI analysis with Ollama:
- Accept extracted issues as context
- Produce structured JSON themes
- Produce markdown summary report
7. Print summary after run:
- JQL used
- Total issues extracted
- Output file locations

## 5. Non-Functional Requirements
1. Security
- No credentials hardcoded in repository.
- Local secrets file (.env) excluded via .gitignore.
- Credentials read only at runtime.
2. Reliability
- Retry transient API errors and rate limits.
- Handle empty result sets without failing.
3. Performance
- Use pagination at 100 records per request.
4. Operability
- Single-command CLI invocation with one input argument.
- Minimal dependencies.
5. Privacy
- AI inference runs only against local Ollama endpoint by default.
- No issue payloads sent to hosted LLM APIs.

## 6. Technical Design
### 6.1 Architecture
- main.py: executable entrypoint
- jira_extractor/config.py: configuration loading and validation
- jira_extractor/cli.py: argument parsing and orchestration
- jira_extractor/jira_client.py: Jira API interactions, mapping, output writers
- jira_extractor/formatter.py: ADF to plain-text conversion
- jira_extractor/ai_analyzer.py: local Ollama prompt building, response parsing, theme report writers

### 6.2 Data Flow
1. Load env configuration.
2. Parse one JQL CLI input.
3. Search Jira with pagination.
4. Transform issue JSON into normalized records.
5. Optionally send issue set to local Ollama for theme extraction.
6. Write JSON and CSV outputs to timestamped files.
7. If AI enabled, write theme JSON and markdown outputs.

## 7. Configuration Model
Required:
- JIRA_BASE_URL
- JIRA_API_TOKEN

CLI input:
- jql (required positional argument)

Optional environment variables:
- OLLAMA_MODEL
- OLLAMA_HOST
- JIRA_EXTRACTOR_OUTPUT_DIR

## 8. Error Handling
- Missing required env vars: fail with actionable message.
- Jira auth/permission failures: HTTP error surfaced.
- No issues returned: still generate empty output files with headers.

## 9. Success Criteria
1. Tool executes with one command and no code changes.
2. Output files contain all required fields.
3. Credentials are not committed to GitHub by default.
4. Query is fully user-defined through one JQL input.
6. Local theme report is generated without external LLM dependency.
