**ThoughtSpot Column Lineage Extractor**

User Guide & Technical Reference  —  v1.0


# 1. Overview

thoughtspot_column_lineage.py is a standalone Python script that connects to a ThoughtSpot cluster and extracts a complete column-level lineage map for one org per run. It produces four CSV files that answer the question: "Which columns are used — and where?"

| Output File | Contents | Also called |
| --- | --- | --- |
| model_columns_<ts>.csv | Every model/worksheet and all its columns, including formula expressions and physical table mapping | Set 1a |
| liveboard_columns_<ts>.csv | Every Liveboard → Viz → column, with source model, formula classification, and chart type | Set 1b |
| answer_columns_<ts>.csv | Every standalone Answer → column, with source model and formula classification | Set 1c |
| failures_<ts>.csv | Every object that could not be exported or processed (always written, even when empty) | Audit log |


# 2. Prerequisites


## 2.1  Python version

Python 3.10 or later is required (the script uses built-in type-hint syntax introduced in 3.10).

```
python3 --version
```

If Python is not installed, download it from https://www.python.org/downloads/


## 2.2  Python libraries

Install the two required third-party libraries:

```
pip install requests pyyaml
```

| Library | Purpose |
| --- | --- |
| requests | HTTP calls to the ThoughtSpot REST API v2 |
| pyyaml | Parse TML (ThoughtSpot Modelling Language) documents returned as YAML strings |


## 2.3  ThoughtSpot credentials

You need one of:

- Username + Password  — your ThoughtSpot admin account password
- Username + Secret Key  — a Trusted Authentication secret key (Admin → Security → Trusted Auth)
The account must have admin access to the org being processed.


## 2.4  Script file

Place thoughtspot_column_lineage.py in any folder and run it from a terminal in that folder.


# 3. How to Run


## 3.1  Interactive mode (prompts)

Open a terminal, navigate to the folder containing the script, and run:

```
python3 thoughtspot_column_lineage.py
```

The script will prompt for:

| Prompt | What to enter | Example |
| --- | --- | --- |
| ThoughtSpot URL | Full cluster URL | https://myorg.thoughtspot.cloud |
| Username | Admin email address | admin@company.com |
| Auth method [1/2] | 1 = Password,  2 = Secret key | 1 |
| Password | Your password (hidden input) | (hidden) |
| Org to process | Numeric org id, org name, or blank for default org | 1448606156 |


## 3.2  Environment variable mode (no prompts)

Set these variables before running to skip all interactive prompts:

```
export TS_URL="https://myorg.thoughtspot.cloud"
export TS_USERNAME="admin@company.com"
export TS_PASSWORD="yourpassword"       # or use TS_SECRET_KEY
export TS_ORG="1448606156"              # org id or name

python3 thoughtspot_column_lineage.py
```


## 3.3  Command-line arguments


| Argument | Default | Purpose |
| --- | --- | --- |
| --org <id or name> | (from prompt / TS_ORG) | Override the org to process |
| --out-model <path> | model_columns_<ts>.csv | Custom path for model columns CSV |
| --out-lb <path> | liveboard_columns_<ts>.csv | Custom path for liveboard columns CSV |
| --out-ans <path> | answer_columns_<ts>.csv | Custom path for answer columns CSV |
| --out-failures <path> | failures_<ts>.csv | Custom path for failures CSV |
| --debug | off | Enable verbose debug logging |

Example:

```
python3 thoughtspot_column_lineage.py --org "Acme Corp" --debug
```


# 4. What the Script Does — Step by Step

The diagram below shows the complete execution flow. Each step lists the function(s) called. All four steps happen inside a single run for one org.


## Step 1 — Collect Credentials

```
Function: prompt_credentials()
```

Reads ThoughtSpot URL, username, password (or secret key), and target org from environment variables if set, otherwise shows interactive prompts. The org field is optional — if left blank the script uses the account's default org.


## Step 2 — Authenticate

```
Functions: get_token()  →  RefreshableSession()
```

Obtains a 24-hour bearer token from the ThoughtSpot auth endpoint. Wraps it in a RefreshableSession that automatically re-authenticates if a subsequent API call returns HTTP 401 (expired token).


## Step 3 — Resolve Org

```
Functions: resolve_org()  →  get_current_org_info()
```

Looks up the target org by the id or name supplied. Matching is done client-side against the full org list because the ThoughtSpot server-side org filter is unreliable. If no org was specified, get_current_org_info() reads the org from the current session token. The resolved org_id and org_name are used for all subsequent API calls and are written into every CSV row.


## Step 4 — Extract Data   [process_org()]

The main function process_org() authenticates a new org-scoped session and then runs three sub-passes in sequence.


### Sub-pass A — Model Columns  (Set 1a)


| # | Function | What it does |
| --- | --- | --- |
| A1 | list_metadata() | Fetches all LOGICAL_TABLE objects (models and worksheets) for the org, paginated in batches of 50. System User objects are filtered out. |
| A2 | export_tml_batch() | Exports TML for all model GUIDs in batches of 25. If a batch fails, it retries one GUID at a time to isolate the bad object. |
| A3 | collect_table_fqns_from_models() | Scans all model TMLs to collect the unique GUIDs of every physical table referenced. |
| A4 | export_tml_batch() | Exports TML for every physical table GUID collected in A3 (same batch retry logic). |
| A5 | build_phys_table_lookup() | Builds a lookup {table_guid → db_name, schema_name, connection_name} from the physical table TMLs. Ambiguous table names (same name, different GUID) are removed from the lookup to prevent wrong attribution. |
| A6 | fetch_column_guid_lookup() | Calls metadata/search with include_details=True in batches of 10 to get the GUID of every column in every model. |
| A7 | build_formula_index() | Called once per model inside extract_model_columns(). Builds a {formula_name → expression} index from the model TML formulas list. Used to enrich formula columns with their raw expression in the output. |
| A8 | extract_model_columns()  →  _extract_model_columns_one() | Iterates all model TMLs and emits one row per column with full enrichment: column_guid, formula expression (from A7 index), source table name and GUID (from A5 lookup), db/schema/connection details. No search_query parsing — model columns are read directly from the TML column definitions. |


### Sub-pass B — Liveboard Columns  (Set 1b)


| # | Function | What it does |
| --- | --- | --- |
| B1 | list_metadata() | Fetches all LIVEBOARD objects for the org, paginated. System User liveboards excluded. |
| B2 | export_tml_bundle() | For each liveboard, exports its full TML bundle (export_associated=True) — includes the liveboard TML plus the TML of every model, table, or view the liveboard references. |
| B3 | extract_liveboard_columns() | Parses the bundle: builds a per-source formula index (one dict per model GUID, never merged across models), resolves each viz's data source, then extracts columns via parse_search_query() if search_query is present, or resolve_and_classify() as fallback. |
| B4 | _classify_sq_token() | Classifies each search_query token as: answer-level formula, model-level formula, cohort, or physical column. |
| B5 | resolve_and_classify() | Fallback classifier (used when search_query is absent): strips ThoughtSpot display prefixes (Total, Average, Month(), etc.) using _candidates(), then checks formula indexes. |


### Sub-pass C — Answer Columns  (Set 1c)


| # | Function | What it does |
| --- | --- | --- |
| C1 | list_metadata() | Fetches all standalone ANSWER objects for the org, paginated. System User answers excluded. |
| C2 | export_tml_bundle() | Exports the full TML bundle for each answer (export_associated=True) — includes the answer TML plus the TML of every model, table, or view the answer references. |
| C3 | extract_answer_columns() | Parses the bundle: builds a per-source formula index (one dict per model GUID, never merged across models), resolves the answer's data source, then extracts columns via parse_search_query() if search_query is present, or resolve_and_classify() as fallback. Identical pipeline to extract_liveboard_columns(). |
| C4 | _classify_sq_token() | Classifies each search_query token as: answer-level formula, model-level formula, cohort, or physical column. Same function as B4 — shared between liveboard and answer passes. |
| C5 | resolve_and_classify() | Fallback classifier used when search_query is absent: strips ThoughtSpot display prefixes (Total, Average, Month(), etc.) using _candidates(), then checks the per-source formula index and physical column lookup. Same function as B5. |


## Step 5 — Write Output Files

```
Function: write_csv()  ×4  →  log_failure_summary()
```

Writes all four CSV files. The failures CSV is always written even if there are zero failures — an empty failures file is the audit trail confirming a clean run. log_failure_summary() prints a grouped count of failures to the console.


# 5. Retry and Error Handling


| Situation | How it is handled |
| --- | --- |
| HTTP 401 (token expired) | RefreshableSession auto-requests a new token and retries the call transparently. |
| HTTP 429 / 502 / 503 / 504 | api_post() / api_get() retry up to 4 times with exponential backoff (3s, 9s, 27s, 81s). |
| Network timeout / connection error | Same exponential-backoff retry logic. After 4 attempts the object is recorded as a failure. |
| TML batch rejected (bad GUID) | export_tml_batch() retries each GUID individually so only the truly bad object is recorded as a failure, not the whole batch. |
| Single object export fails | record_failure() logs it and stores it for the failures CSV. The run continues — no crash. |
| Malformed TML on one model | _extract_model_columns_one() raises inside a try/except in extract_model_columns(), recording just that model as failed. |


# 6. Output File Column Reference


## 6.1  model_columns_<ts>.csv


| Column | Description |
| --- | --- |
| org_id / org_name | Org the object belongs to |
| model_guid | GUID of the model or worksheet |
| model_name | Display name of the model |
| model_type | "model" or "worksheet" |
| column_guid | GUID of the column ("hidden" if the API does not expose it) |
| column_name | Display name of the column inside the model |
| column_type | MEASURE, ATTRIBUTE, etc. |
| aggregation | Default aggregation (SUM, COUNT, etc.) |
| is_calculated | True if this is a formula column |
| formula_scope | "model" for model-level formulas, blank otherwise |
| formula_expr | The raw formula expression (only for calculated columns) |
| source_table_alias | Table alias used inside the model TML |
| source_table_name | Physical table display name |
| source_table_guid | GUID of the physical table |
| db_name / schema_name | Database and schema from the physical table TML |
| connection_name | ThoughtSpot connection the table belongs to |
| db_column_name | Underlying database column name (from column_id in TML) |


## 6.2  liveboard_columns_<ts>.csv


| Column | Description |
| --- | --- |
| org_id / org_name | Org the object belongs to |
| liveboard_guid | GUID of the liveboard |
| liveboard_name | Display name of the liveboard |
| viz_id | Internal ID of the visualization within the liveboard |
| viz_guid | GUID of the visualization (pinned answer) |
| viz_name | Display name of the visualization |
| viz_display_mode | TABLE_MODE or CHART_MODE |
| viz_chart_type | Chart type (COLUMN, LINE, PIE, etc.) — blank for table views |
| column_name | Resolved column name |
| is_calculated | True if this is a formula column |
| formula_scope | "answer" for answer-level, "model" for model-level, "cohort" for cohort columns |
| formula_expr | Formula expression if is_calculated is True |
| source_type | "model", "worksheet", "table", "view", or "aggr_worksheet" |
| source_guid | GUID of the data source the viz is built on |
| source_name | Display name of the data source |


## 6.3  answer_columns_<ts>.csv

Same structure as liveboard_columns but with answer_guid and answer_name instead of liveboard fields. No viz_id, viz_guid, viz_display_mode, or viz_chart_type columns.


## 6.4  failures_<ts>.csv


| Column | Description |
| --- | --- |
| timestamp | ISO-8601 timestamp when the failure was recorded |
| org_name | Org the failed object belongs to |
| object_type | "model", "liveboard", "answer", "table", "metadata_list", or "org" |
| object_name | Display name of the failed object |
| object_guid | GUID of the failed object |
| stage | Which step failed: tml_export, tml_export_batch, metadata_search, extract_columns, column_guid_fetch, or auth |
| http_status | HTTP status code returned by ThoughtSpot (blank for network errors) |
| reason | Full error message including the server response body (truncated to 300 chars) |


# 7. Known Limitations

- AGGR_WORKSHEET objects — ThoughtSpot returns an empty TML for aggregated worksheets so their columns cannot be extracted. These objects appear in failures_*.csv if their TML export fails, or are silently absent from model_columns.
- Hidden columns — ThoughtSpot's API does not return GUIDs for hidden model columns. These appear in model_columns with column_guid = "hidden".
- One org per run — the script always processes exactly one org. Run it separately for each org.
- System User objects — liveboards, answers, and models owned by the ThoughtSpot System User (built-in samples, TS: AI and BI Stats, etc.) are automatically excluded from all output.


# 8. Troubleshooting


| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Authentication failed | Wrong password or secret key | Double-check credentials. For secret key, confirm Trusted Auth is enabled. |
| No org found matching '…' | Org name typo, or account has no access to that org | Use the numeric org id instead of the name. |
| Model shows in failures (HTTP 403) | Admin account does not have access to that model | Use a full admin account (not org-scoped viewer). |
| Zero rows in liveboard_columns | All liveboards are System User owned, or TML exports all failed | Check failures CSV. Run with --debug for detailed logs. |
| Missing dependency error | requests or pyyaml not installed | Run:  pip install requests pyyaml |
| Slow run (large org) | Many liveboards or answers with large bundles | Normal. The script logs progress for each liveboard and answer. Token is valid 24 hours. |

— End of Document —
