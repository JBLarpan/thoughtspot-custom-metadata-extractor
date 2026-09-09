**ThoughtSpot User Access Map**

thoughtspot_user_access_map.py  —  User Guide

Version 1.0  |  ThoughtSpot REST API v2


# 1. Overview

thoughtspot_user_access_map.py produces a complete map of who can access every object in a ThoughtSpot org. It queries effective permissions — meaning access inherited through group membership is included, not just direct shares.

The script produces four CSV files per run:

- access_liveboards_<timestamp>.csv   — effective permissions on every liveboard
- access_answers_<timestamp>.csv       — effective permissions on every answer
- access_models_<timestamp>.csv        — effective permissions on every model, worksheet, table, view, and sql_view
- access_failures_<timestamp>.csv      — objects whose permissions could not be fetched (always written, even if empty)

Typical use cases: access audits, org-wide permission reviews, identifying over-shared or under-shared content, and pre-migration access snapshots.


# 2. Prerequisites


## 2.1  Python

Python 3.10 or later is required (same requirement as thoughtspot_column_lineage.py).

```
python3 --version
```


## 2.2  Install dependencies

Only the requests library is needed (no yaml, no docx):

```
pip install requests
```


## 2.3  ThoughtSpot credentials

You need one of:

- Username + Password — a ThoughtSpot admin account
- Username + Secret Key — a Trusted Authentication secret key (Admin → Security → Trusted Authentication)
The account must have org-level admin access. Without admin access, the metadata/search and security/metadata/fetch-permissions API calls will return partial or empty results.


## 2.4  Network access

The machine running the script must be able to reach the ThoughtSpot cluster on HTTPS (port 443). No special firewall rules are needed beyond standard HTTPS access.


# 3. How to Run


## 3.1  Interactive mode

Navigate to the folder containing the script and run:

```
python3 thoughtspot_user_access_map.py
```

The script will prompt for:

| Prompt | What to enter |
| --- | --- |
| ThoughtSpot URL | Base cluster URL only — e.g.  https://myorg.thoughtspot.cloud
(do NOT include /api/rest/... path) |
| Username | Your ThoughtSpot admin email address |
| Auth method | Type  1  for password,  2  for secret key |
| Password / Key | Entered securely (hidden input) |
| Org | Numeric org ID or org display name. Leave blank for your default org. |


## 3.2  Environment-variable mode (no prompts)

Set these variables before running to skip all interactive prompts:

```
export TS_URL="https://myorg.thoughtspot.cloud"
export TS_USERNAME="admin@example.com"
export TS_PASSWORD="yourpassword"      # or use TS_SECRET_KEY
export TS_ORG="1448606156"             # org ID or name; omit for default org

python3 thoughtspot_user_access_map.py
```


## 3.3  Example console output

```
============================================================
  ThoughtSpot User Access Map
============================================================

ThoughtSpot URL: https://myorg.thoughtspot.cloud
Username (email): admin@example.com
Authentication:  1. Password   2. Secret key
Choose [1/2]: 2
Secret key:
Org to process (id or name — leave blank for your default org): 0

17:30:01  INFO  Connecting to https://myorg.thoughtspot.cloud as admin@example.com
17:30:02  INFO  Org: Primary (id=0)
17:30:02  INFO  Fetching users ...
17:30:03  INFO    142 users found.
17:30:03  INFO  Fetching groups ...
17:30:04  INFO    38 groups found.
17:30:04  INFO  Fetching liveboards ...
17:30:06  INFO    312 liveboards.
17:30:06  INFO  Fetching liveboard permissions ...
17:30:18  INFO    permissions 1 – 25 / 312 ...
           ...
17:31:42  INFO  Finished in 100.3s — writing output files ...
17:31:42  INFO  Written: access_liveboards_20260820_173142.csv  (4821 rows)
17:31:42  INFO  Written: access_answers_20260820_173142.csv     (1205 rows)
17:31:42  INFO  Written: access_models_20260820_173142.csv      (2340 rows)
17:31:42  INFO  Written: access_failures_20260820_173142.csv    (0 rows)
17:31:42  INFO  No failures recorded — every object processed cleanly.
```


# 4. Step-by-step Execution Flow

This section traces every stage of the script in order, with the function names responsible for each stage.

```
Step 1 — Collect credentials  [prompt_credentials()]
```

Reads TS_URL / TS_USERNAME / TS_PASSWORD (or TS_SECRET_KEY) / TS_ORG from environment variables. If any required value is missing, falls back to interactive prompts with a formatted banner. The URL is auto-cleaned — if the user accidentally pastes a full API path (e.g. ending in /api/rest/2.0/auth/token/full), the trailing path is stripped automatically before it is used.

- Env vars present + complete → no prompts shown
- Partial or missing env vars → interactive prompts
- URL stripping: /api/rest/2.0/auth/token/full, /api/rest/2.0, /api/rest, /api are removed
```
Step 2 — Authenticate  [get_token()  →  RefreshableSession()]
```

Calls POST /api/rest/2.0/auth/token/full to obtain a bearer token valid for 86,400 seconds (24 hours). The token is wrapped in a RefreshableSession that automatically re-authenticates and retries if any subsequent API call returns HTTP 401.

- Token validity: 86,400 seconds (24 hours) — covers the longest expected run
- RefreshableSession.refresh() is called transparently on HTTP 401
- Authentication is performed twice: once without org scope (to resolve the org), then again scoped to the resolved org_id
```
Step 3 — Resolve org  [resolve_org()  →  get_current_org_info()]
```

Resolves the user-supplied org identifier (numeric ID or display name) to a validated (org_name, org_id) pair. If no org was specified, get_current_org_info() reads the current_org field from the session's auth context.

- Calls POST /api/rest/2.0/orgs/search and matches client-side
- Numeric ID match takes precedence over name match
- Ambiguous match (multiple orgs with the same name) exits with an informative error
- Blank input → uses the account's default org
```
Step 4 — Build user and group lookups  [build_user_lookup()  →  build_group_lookup()]
```

Paginates through all users and groups in the org and builds in-memory lookup dictionaries {guid: display_name}. These are used later to enrich permission rows with human-readable names for any principals whose names are not returned directly by the permissions API.

- POST /api/rest/2.0/users/search — paginated at 200 per page
- POST /api/rest/2.0/groups/search — paginated at 200 per page
- Failures during lookup are recorded via record_failure() and the run continues
```
Step 5 — List metadata objects  [list_metadata()]
```

Fetches all objects of each type (LIVEBOARD, ANSWER, LOGICAL_TABLE) for the current org using POST /api/rest/2.0/metadata/search. Called three times — once per object type. include_headers: True is passed so that metadata_header (including type and worksheetVersion) is returned for each object, which is needed for accurate LOGICAL_TABLE subtype labeling.

- Paginated at 50 per page until the page is smaller than the batch size
- Failures at any page are recorded and listing stops early for that type
- LOGICAL_TABLE covers: models (V2), worksheets, tables, aggr_worksheets, views, sql_views
```
Step 6 — Fetch effective permissions  [fetch_all_permissions()  →  fetch_permissions_batch()]
```

For each object type, calls POST /api/rest/2.0/security/metadata/fetch-permissions with permission_type: EFFECTIVE in batches of 25 GUIDs. EFFECTIVE permissions include all access inherited through group membership, not just direct shares.

- Batch size: 25 GUIDs per API call
- On batch failure: if batch has 1 GUID → record_failure(); if >1 → retry one at a time to isolate the bad GUID
- principal_type is read directly from the API response: USER (any sub-type) → 'user', USER_GROUP → 'group'
- Retries: up to 4 attempts with exponential back-off (3s, 9s, 27s, 81s) on HTTP 429/502/503/504
```
Step 7 — Resolve object subtypes  [_object_type_label()]
```

For LOGICAL_TABLE objects, determines the correct subtype label by inspecting the metadata_header returned in Step 5. worksheetVersion == 'V2' indicates a new-style Model. All other subtypes are determined from metadata_header.type.

- worksheetVersion == 'V2'  →  'model'
- metadata_header.type == 'WORKSHEET'           →  'worksheet'
- metadata_header.type == 'ONE_TO_ONE_LOGICAL'  →  'table'
- metadata_header.type == 'AGGR_WORKSHEET'      →  'aggr_worksheet'
- metadata_header.type == 'USER_DEFINED'        →  'view'
- metadata_header.type == 'SQL_VIEW'            →  'sql_view'
- LIVEBOARD and ANSWER types are labeled directly without subtype inspection
```
Step 8 — Build output rows  [build_output_rows()]
```

Combines the raw permission rows from Step 6 with the metadata_header data from Step 5 and the user/group lookups from Step 4 to produce final rows matching the ACCESS_FIELDS schema. Enriches owner and principal display names from the lookup dictionaries where the API response does not include them.

- One row per (object, principal, share_mode) combination
- share_mode values: READ, MODIFY, NO_ACCESS
- Principal name falls back to user/group lookup if not in API response (handles deleted principals)
```
Step 9 — Write CSVs  [write_csv()]
```

Writes the four output files. The failures CSV is always written — even if no failures occurred — so downstream processes can reliably check for it.

- access_liveboards_<ts>.csv
- access_answers_<ts>.csv
- access_models_<ts>.csv
- access_failures_<ts>.csv  (always written, 0 rows if clean run)
```
Step 10 — Print failure summary  [log_failure_summary()]
```

After all CSVs are written, prints a roll-up of every failure grouped by object type and stage. If no failures occurred, prints 'No failures recorded — every object processed cleanly.'


# 5. Error and Failure Handling


## 5.1  Retry logic

Every API call goes through api_post() or api_get(), which retry up to 4 times with exponential back-off on transient errors (HTTP 429, 502, 503, 504) and network timeouts.


## 5.2  Token refresh

HTTP 401 responses trigger RefreshableSession.refresh(), which re-calls get_token() and updates the Authorization header automatically, then retries the failed request. This handles token expiry mid-run without any user intervention.


## 5.3  Permissions batch isolation

If a batch of 25 permission requests fails, the script retries each GUID individually to identify the specific object causing the failure. Only that object is recorded as a failure; the rest of the batch succeeds normally.


## 5.4  record_failure() and failures CSV

Every caught exception is passed to record_failure(), which:

- Logs the error immediately with object name, GUID, stage, HTTP status, and server error body
- Appends a structured record to the FAILURES list
- Is written to access_failures_<ts>.csv at the end of the run, regardless of whether there are any failures

| failures CSV column | Description |
| --- | --- |
| timestamp | ISO 8601 timestamp when the failure was recorded |
| org_name | Display name of the org being processed |
| object_type | LIVEBOARD, ANSWER, or LOGICAL_TABLE |
| object_name | Display name of the failed object |
| object_guid | GUID of the failed object |
| stage | API stage where the failure occurred (e.g. fetch_permissions, metadata_search) |
| http_status | HTTP status code returned by the server (if available) |
| reason | Exception message and server error body (truncated to 300 chars) |


# 6. Output Column Reference

All three access CSVs (liveboards, answers, models) share the same schema:

| Column | Description |
| --- | --- |
| org_id | Numeric ID of the org (blank if the default org has no ID) |
| org_name | Display name of the org |
| object_guid | GUID of the liveboard, answer, or model/table/view |
| object_name | Display name of the object |
| object_type | liveboard / answer / model / worksheet / table / aggr_worksheet / view / sql_view |
| owner_guid | GUID of the object owner |
| owner_username | Username (email) of the object owner |
| owner_display_name | Display name of the object owner |
| principal_guid | GUID of the user or group the permission applies to |
| principal_name | Display name of the user or group |
| principal_type | 'user' or 'group' |
| share_mode | READ, MODIFY, or NO_ACCESS |

Each row represents one (object × principal × share_mode) combination.


## 6.1  object_type values for LOGICAL_TABLE


| object_type value | What it means in ThoughtSpot |
| --- | --- |
| model | New-style Model (worksheetVersion == V2 in metadata_header) |
| worksheet | Classic Worksheet |
| table | Native table (ONE_TO_ONE_LOGICAL) |
| aggr_worksheet | Aggregate Worksheet |
| view | ThoughtSpot View (USER_DEFINED) |
| sql_view | SQL View |


# 7. Large File Warning — access_models CSV

The access_models CSV is almost always the largest of the four output files. On a large shared cluster it can exceed 1 million rows and cannot be opened in Google Sheets (limit ~500,000 rows) or Excel (limit ~1,048,576 rows).


## 7.1  Why it is large

Two factors compound:

- LOGICAL_TABLE covers ALL data object types — models, worksheets, tables, views, aggr_worksheets, and sql_views — combined into one file. A large cluster may have thousands of these objects.
- EFFECTIVE permissions multiply rows — every user who inherits access through a group gets one row per object. If the Administrator group has 100 members and there are 1,000 LOGICAL_TABLE objects, that alone produces 100,000 rows.

## 7.2  Check the row count first

Before trying to open the file, check how many rows it has:

```
wc -l access_models_<timestamp>.csv
```

If the count is above ~100,000, use the split approach below.


## 7.3  Split by object_type

Run the following Python snippet to split the combined models file into one file per object_type (model, worksheet, table, view, aggr_worksheet, sql_view). Each resulting file is small enough to open in Google Sheets or Excel:

```
python3 << 'EOF'
import csv
src = 'access_models_<timestamp>.csv'   # replace with actual filename
writers, files = {}, {}

with open(src) as f:
    reader = csv.DictReader(f)
    fields = reader.fieldnames
    for row in reader:
        t = row['object_type']
        if t not in writers:
            path = src.replace('access_models_', f'access_{t}_')
            fh = open(path, 'w', newline='', encoding='utf-8')
            writers[t] = csv.DictWriter(fh, fieldnames=fields)
            writers[t].writeheader()
            files[t] = fh
        writers[t].writerow(row)

for fh in files.values(): fh.close()
print('Split into:', list(writers.keys()))
EOF
```

This produces files like access_model_<ts>.csv, access_table_<ts>.csv, access_worksheet_<ts>.csv etc. in the same folder.


## 7.4  Recommendation for large clusters

Always run on a small org or test cluster first to validate output before running against a large production cluster. On large clusters, plan for the access_models file to exceed spreadsheet tool limits and use the split script above or import directly into a database or BI tool.


# 8. Known Limitations

- Single org per run — the script processes one org at a time. To audit multiple orgs, run the script once per org (set TS_ORG each time).
- NO_ACCESS rows — ThoughtSpot may return NO_ACCESS permission entries for groups that have been explicitly denied. These appear in the output with share_mode = NO_ACCESS.
- Deleted principals — if a user or group has been deleted from ThoughtSpot after a share was created, the principal_name may be empty or fall back to the GUID.
- Scalability — very large orgs (10,000+ objects) may take 20–40 minutes. The 24-hour token window covers this comfortably.

# 9. Troubleshooting


| Error / Symptom | Likely cause | Fix |
| --- | --- | --- |
| Authentication failed: 404 Not Found | Full API URL pasted instead of base URL | Enter only the base URL, e.g. https://myorg.thoughtspot.cloud (the script also strips trailing API paths automatically) |
| Authentication failed: 401 | Wrong password or expired secret key | Verify credentials; rotate secret key in Admin → Security → Trusted Authentication |
| Org '...' not found | Org name typo or wrong account | Check the exact org name in Admin → Orgs, or use the numeric org ID |
| access_models.csv rows all show 'model/worksheet' | Old version of the script without include_headers support | Ensure you are running the latest version of thoughtspot_user_access_map.py |
| Empty access CSVs | Account does not have admin access to the org | Use an account with org-level admin privileges |
| Many rows in failures CSV with HTTP 403 | Insufficient permissions for some objects | Use a full admin account; partial results are still valid for the non-403 objects |
| access_models CSV has 1M+ rows / cannot open in Google Sheets or Excel | Large cluster with many LOGICAL_TABLE objects and EFFECTIVE permissions | Use the split-by-object_type script in Section 7.3 to break the file into smaller per-type files |
| pip install requests fails | Python/pip not in PATH | Use pip3 install requests or python3 -m pip install requests |
