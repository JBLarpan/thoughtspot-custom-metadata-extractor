#!/usr/bin/env python3
"""
thoughtspot_column_lineage.py — Column lineage extractor for ThoughtSpot.

Produces four CSVs per run:
  model_columns_<ts>.csv     — all model/worksheet columns          (Set 1a)
  liveboard_columns_<ts>.csv — Liveboard → Viz → column             (Set 1b)
  answer_columns_<ts>.csv    — standalone Answer → column           (Set 1c)
  failures_<ts>.csv          — objects that could not be processed

Usage:  python thoughtspot_column_lineage.py [--debug]
Env vars: TS_URL, TS_USERNAME, TS_PASSWORD or TS_SECRET_KEY, TS_ORG
"""

import argparse
import csv
import datetime
import getpass
import logging
import os
import re as _re
import sys
import time
from typing import Optional

import requests

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency: run  pip install pyyaml  then retry.")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("ts_col_lineage")

# ---------------------------------------------------------------------------
# Failure tracking
# ---------------------------------------------------------------------------

FAILURE_FIELDS = [
    "timestamp", "org_name", "object_type", "object_name", "object_guid",
    "stage", "http_status", "reason",
]

FAILURES: list[dict] = []


def _extract_http_status(exc: Exception) -> str:
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None):
        return str(resp.status_code)
    return ""


def _extract_error_body(exc: Exception, limit: int = 300) -> str:
    resp = getattr(exc, "response", None)
    if resp is None:
        return ""
    try:
        body = resp.text or ""
    except Exception:
        return ""
    return " ".join(body.split())[:limit]


def record_failure(object_type: str, object_name: str, object_guid: str,
                   org_name: str, stage: str, exc: Exception) -> None:
    """Log and store one failure for the end-of-run failures CSV."""
    status = _extract_http_status(exc)
    body   = _extract_error_body(exc)
    reason = str(exc)
    if body:
        reason = f"{reason} | server said: {body}"

    guid_part   = f" [{object_guid}]" if object_guid else ""
    status_part = f" (HTTP {status})" if status else ""
    log.error("  FAILED — %s '%s'%s during %s%s in org '%s': %s",
              object_type.upper(), object_name or "(unnamed)", guid_part,
              stage, status_part, org_name, reason)

    FAILURES.append({
        "timestamp":   datetime.datetime.now().isoformat(timespec="seconds"),
        "org_name":    org_name,
        "object_type": object_type,
        "object_name": object_name,
        "object_guid": object_guid,
        "stage":       stage,
        "http_status": status,
        "reason":      reason,
    })


def log_failure_summary() -> None:
    """Print a roll-up of every failure after the run completes."""
    if not FAILURES:
        log.info("No failures recorded — every object exported cleanly.")
        return

    log.info("=" * 55)
    log.info("FAILURE SUMMARY — %d object(s) could not be fully processed:", len(FAILURES))
    log.info("=" * 55)

    by_type_stage: dict[tuple, int] = {}
    for f in FAILURES:
        key = (f["object_type"], f["stage"])
        by_type_stage[key] = by_type_stage.get(key, 0) + 1

    for (obj_type, stage), count in sorted(by_type_stage.items()):
        log.info("  %-10s / %-20s : %d", obj_type, stage, count)

    log.info("-" * 55)
    for f in FAILURES:
        guid_part   = f" [{f['object_guid']}]" if f["object_guid"] else ""
        status_part = f" (HTTP {f['http_status']})" if f["http_status"] else ""
        log.info("  - %s '%s'%s — %s%s: %s",
                 f["object_type"], f["object_name"] or "(unnamed)", guid_part,
                 f["stage"], status_part, f["reason"])
    log.info("=" * 55)


# ---------------------------------------------------------------------------
# Output field definitions
# ---------------------------------------------------------------------------

MODEL_COL_FIELDS = [
    "org_id", "org_name",
    "model_guid", "model_name", "model_type",
    "column_guid", "column_name", "column_type", "aggregation",
    "is_calculated", "formula_scope", "formula_expr",
    "source_table_alias", "source_table_name", "source_table_guid",
    "db_name", "schema_name", "connection_name",
    "db_column_name",
]

LB_COL_FIELDS = [
    "org_id", "org_name",
    "liveboard_guid", "liveboard_name",
    "viz_id", "viz_guid", "viz_name",
    "viz_display_mode", "viz_chart_type",
    "column_name",
    "is_calculated", "formula_scope", "formula_expr",
    "source_type", "source_guid", "source_name",
]

ANS_COL_FIELDS = [
    "org_id", "org_name",
    "answer_guid", "answer_name",
    "column_name",
    "is_calculated", "formula_scope", "formula_expr",
    "source_type", "source_guid", "source_name",
]


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def prompt_credentials() -> tuple[str, str, Optional[str], Optional[str], Optional[str]]:
    """Return (base_url, username, password, secret_key, org_identifier) from env or prompts."""
    env_url  = os.environ.get("TS_URL", "").rstrip("/")
    env_user = os.environ.get("TS_USERNAME", "")
    env_pass = os.environ.get("TS_PASSWORD")
    env_key  = os.environ.get("TS_SECRET_KEY")
    env_org  = os.environ.get("TS_ORG", "").strip() or None

    if env_url and env_user and (env_pass or env_key):
        log.info("Using credentials from environment variables.")
        if env_org:
            log.info("Using org from TS_ORG: %s", env_org)
        return env_url, env_user, env_pass, env_key, env_org

    print("\n" + "=" * 60)
    print("  ThoughtSpot Column Lineage Extractor")
    print("=" * 60)

    base_url = input("\nThoughtSpot URL (e.g. https://myorg.thoughtspot.cloud): ").strip().rstrip("/")
    if not base_url:
        sys.exit("URL is required.")

    username = input("Username (email): ").strip()
    if not username:
        sys.exit("Username is required.")

    print("\nAuthentication:  1. Password   2. Secret key")
    choice = input("Choose [1/2]: ").strip()

    password: Optional[str]   = None
    secret_key: Optional[str] = None
    if choice == "2":
        secret_key = getpass.getpass("Secret key: ")
    else:
        password = getpass.getpass("Password: ")

    org_input = input(
        "\nOrg to process (id or name — leave blank for your default org): "
    ).strip()

    print()
    return base_url, username, password, secret_key, org_input or None


# ---------------------------------------------------------------------------
# Auth / session helpers
# ---------------------------------------------------------------------------

TOKEN_VALIDITY_SEC = 86400  # 24 hours — covers the longest expected run


def get_token(base_url: str, username: str, password: Optional[str],
              secret_key: Optional[str], org_id: Optional[int] = None) -> str:
    """Request a bearer token from ThoughtSpot."""
    payload: dict = {"username": username, "validity_time_in_sec": TOKEN_VALIDITY_SEC}
    if secret_key:
        payload["secret_key"] = secret_key
    elif password:
        payload["password"] = password
    if org_id is not None:
        payload["org_id"] = org_id
    r = requests.post(f"{base_url}/api/rest/2.0/auth/token/full", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["token"]


def make_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return s


class RefreshableSession:
    """Wraps requests.Session and re-authenticates automatically on HTTP 401."""

    def __init__(self, base_url: str, username: str, password: Optional[str],
                 secret_key: Optional[str], org_id: Optional[int], token: str) -> None:
        self._base_url   = base_url
        self._username   = username
        self._password   = password
        self._secret_key = secret_key
        self._org_id     = org_id
        self._session    = make_session(token)

    @property
    def headers(self) -> dict:
        return self._session.headers

    def refresh(self) -> None:
        log.warning("  Bearer token expired — re-authenticating...")
        token = get_token(self._base_url, self._username, self._password,
                          self._secret_key, self._org_id)
        self._session.headers.update({"Authorization": f"Bearer {token}"})
        log.info("  Token refreshed successfully.")

    def post(self, url: str, **kwargs):
        return self._session.post(url, **kwargs)

    def get(self, url: str, **kwargs):
        return self._session.get(url, **kwargs)


# ---------------------------------------------------------------------------
# Retry-aware API helpers
# ---------------------------------------------------------------------------

RETRY_STATUS = {429, 502, 503, 504}


def api_post(session, url: str, payload: dict,
             retries: int = 4, backoff: float = 3.0) -> requests.Response:
    """POST with exponential-backoff retry and automatic token refresh on 401."""
    for attempt in range(retries):
        try:
            r = session.post(url, json=payload, timeout=120)
            if r.status_code == 401 and attempt < retries - 1 and hasattr(session, "refresh"):
                log.warning("  HTTP 401 — refreshing token (attempt %d/%d)", attempt + 1, retries)
                session.refresh()
                continue
            if r.status_code in RETRY_STATUS and attempt < retries - 1:
                wait = backoff ** (attempt + 1)
                log.warning("  HTTP %d — retry %d/%d in %.0fs",
                            r.status_code, attempt + 1, retries, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == retries - 1:
                raise
            wait = backoff ** (attempt + 1)
            log.warning("  Network error — retry %d/%d in %.0fs: %s",
                        attempt + 1, retries, wait, exc)
            time.sleep(wait)
    raise RuntimeError("api_post exhausted retries")


def api_get(session, url: str,
            retries: int = 4, backoff: float = 3.0) -> requests.Response:
    """GET with the same retry and token-refresh logic as api_post."""
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=120)
            if r.status_code == 401 and attempt < retries - 1 and hasattr(session, "refresh"):
                log.warning("  HTTP 401 — refreshing token (attempt %d/%d)", attempt + 1, retries)
                session.refresh()
                continue
            if r.status_code in RETRY_STATUS and attempt < retries - 1:
                wait = backoff ** (attempt + 1)
                log.warning("  HTTP %d — retry %d/%d in %.0fs",
                            r.status_code, attempt + 1, retries, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == retries - 1:
                raise
            wait = backoff ** (attempt + 1)
            log.warning("  Network error — retry %d/%d in %.0fs: %s",
                        attempt + 1, retries, wait, exc)
            time.sleep(wait)
    raise RuntimeError("api_get exhausted retries")


# ---------------------------------------------------------------------------
# TML parsing helpers
# ---------------------------------------------------------------------------

def parse_edoc(raw) -> dict:
    """Return edoc as a dict — handles both YAML string and already-parsed dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = yaml.safe_load(raw)
            return parsed if isinstance(parsed, dict) else {}
        except yaml.YAMLError:
            return {}
    return {}


def build_formula_index(formulas_list: list) -> dict[str, str]:
    """Return {formula_name_or_id: expr} from a TML formulas[] list."""
    index: dict[str, str] = {}
    for f in (formulas_list or []):
        if not isinstance(f, dict):
            continue
        name = f.get("name", "")
        expr = f.get("expr", "")
        fid  = f.get("id", "")
        if name:
            index[name] = expr
        if fid:
            index[fid] = expr
    return index


def build_formula_name_map(formulas_list: list) -> dict:
    """Return {formula_id: formula_display_name} for resolving formula_ tokens."""
    mapping: dict = {}
    for f in (formulas_list or []):
        if not isinstance(f, dict):
            continue
        fid  = f.get("id", "")
        name = f.get("name", "")
        if fid and name:
            mapping[fid] = name
    return mapping


# ---------------------------------------------------------------------------
# Org helpers
# ---------------------------------------------------------------------------

def get_current_org_info(session, base_url: str) -> tuple[str, Optional[int]]:
    """Return (org_name, org_id) from the current session's auth context."""
    try:
        r = api_get(session, f"{base_url}/api/rest/2.0/auth/session/user")
        data        = r.json()
        current_org = data.get("current_org") or {}
        name = current_org.get("name") or current_org.get("org_name") or ""
        oid  = current_org.get("id")
        return name, oid
    except Exception:
        return "", None


def resolve_org(session, base_url: str,
                org_identifier: Optional[str]) -> tuple[str, Optional[int]]:
    """Resolve an org id or name to (org_name, org_id). Falls back to session default if None.

    Matching is done client-side because the server-side org_identifiers filter
    is unreliable — it can return every org the account can see regardless of the filter.
    """
    if not org_identifier:
        name, oid = get_current_org_info(session, base_url)
        if not name and oid is None:
            sys.exit(
                "Could not determine your default org. "
                "Re-run and specify --org / TS_ORG explicitly."
            )
        return name, oid

    try:
        r        = api_post(session, f"{base_url}/api/rest/2.0/orgs/search",
                            {"org_identifiers": [org_identifier]})
        all_orgs = r.json() or []
    except (requests.HTTPError, requests.Timeout, requests.ConnectionError, RuntimeError) as exc:
        sys.exit(f"Failed to look up org '{org_identifier}': {exc}")

    def _name(o: dict) -> str:
        return o.get("name") or o.get("org_name") or ""

    def _id(o: dict) -> Optional[int]:
        return o.get("id") if o.get("id") is not None else o.get("org_id")

    ident_lower  = org_identifier.strip().lower()
    name_matches = [o for o in all_orgs if _name(o).strip().lower() == ident_lower]

    id_matches: list[dict] = []
    if org_identifier.strip().lstrip("-").isdigit():
        want_id    = int(org_identifier.strip())
        id_matches = [o for o in all_orgs if _id(o) == want_id]

    orgs = id_matches or name_matches

    if not orgs:
        sys.exit(
            f"No org found matching '{org_identifier}' among "
            f"{len(all_orgs)} org(s) your account can access."
        )
    if len(orgs) > 1:
        matches = ", ".join(f"{_name(o)} (id={_id(o)})" for o in orgs)
        sys.exit(
            f"Org '{org_identifier}' matched more than one org: {matches}. "
            "Re-run with a more specific org id or exact name."
        )

    org = orgs[0]
    return _name(org), _id(org)


# ---------------------------------------------------------------------------
# Metadata listing (paginated, System User objects excluded)
# ---------------------------------------------------------------------------

def is_system_object(obj: dict) -> bool:
    """Return True if the object is owned by System User (ThoughtSpot built-ins)."""
    header = obj.get("metadata_header") or {}
    return (header.get("authorDisplayName") == "System User"
            or header.get("authorName") == "system")


def list_metadata(session, base_url: str,
                  metadata_type: str, org_label: str) -> list[dict]:
    """Return all non-system objects of metadata_type for the current org, paginated."""
    results: list[dict] = []
    skipped = 0
    offset  = 0
    batch   = 50
    while True:
        try:
            r = api_post(session, f"{base_url}/api/rest/2.0/metadata/search", {
                "metadata":        [{"type": metadata_type}],
                "record_offset":   offset,
                "record_size":     batch,
                "include_headers": True,
            })
        except (requests.HTTPError, requests.Timeout, requests.ConnectionError, RuntimeError) as exc:
            record_failure("metadata_list", f"{metadata_type} (offset {offset})",
                           "", org_label, "metadata_search", exc)
            log.warning("  [%s] Stopping %s listing early — %d collected so far.",
                        org_label, metadata_type, len(results))
            break
        page = r.json() or []
        if not page:
            break
        for obj in page:
            if is_system_object(obj):
                skipped += 1
            else:
                results.append(obj)
        if len(page) < batch:
            break
        offset += batch
    log.info("  [%s] %d %s object(s) found (%d System User skipped).",
             org_label, len(results), metadata_type, skipped)
    return results


# ---------------------------------------------------------------------------
# TML export helpers
# ---------------------------------------------------------------------------

def export_tml_bundle(session, base_url: str, guid: str, label: str,
                      org_label: str, associated: bool = True,
                      obj_type: str = "object") -> list[dict]:
    """Export TML for a single GUID, optionally including associated objects."""
    try:
        r = api_post(session, f"{base_url}/api/rest/2.0/metadata/tml/export", {
            "metadata":          [{"identifier": guid}],
            "export_associated": associated,
            "export_fqn":        True,
        })
        bundle = r.json() or []
        log.debug("    [%s] TML export '%s' → %d object(s)", org_label, label, len(bundle))
        return bundle
    except (requests.HTTPError, requests.Timeout, requests.ConnectionError, RuntimeError) as exc:
        record_failure(obj_type, label, guid, org_label, "tml_export", exc)
        return []


def export_tml_batch(session, base_url: str, guids: list[str],
                     org_label: str, obj_type: str = "object",
                     names_by_guid: Optional[dict] = None) -> list[dict]:
    """Export TML for multiple GUIDs in one call (no associated objects).

    ThoughtSpot rejects the whole batch if any single GUID is bad. On failure
    the batch is retried one GUID at a time so only the real bad object is recorded.
    """
    if not guids:
        return []
    names_by_guid = names_by_guid or {}
    try:
        r = api_post(session, f"{base_url}/api/rest/2.0/metadata/tml/export", {
            "metadata":          [{"identifier": g} for g in guids],
            "export_associated": False,
            "export_fqn":        True,
        })
        items = r.json() or []
        log.debug("    [%s] batch TML %d guids → %d item(s)", org_label, len(guids), len(items))
        return items
    except (requests.HTTPError, requests.Timeout, requests.ConnectionError, RuntimeError) as exc:
        if len(guids) == 1:
            record_failure(obj_type, names_by_guid.get(guids[0], ""),
                           guids[0], org_label, "tml_export_batch", exc)
            return []
        log.warning("  [%s] Batch of %d failed — retrying one at a time to isolate the bad object.",
                    org_label, len(guids))
        items: list[dict] = []
        for g in guids:
            items.extend(export_tml_batch(session, base_url, [g], org_label,
                                          obj_type=obj_type, names_by_guid=names_by_guid))
        return items


# ---------------------------------------------------------------------------
# Set 1a — Model columns
# ---------------------------------------------------------------------------

# AGGR_WORKSHEET objects are excluded: ThoughtSpot returns an empty TML for this type.
MODEL_KEYS = ("model", "worksheet")

_INFO_TYPE_TO_SOURCE_TYPE: dict[str, str] = {
    "ONE_TO_ONE_LOGICAL": "table",
    "AGGR_WORKSHEET":     "aggr_worksheet",
    "USER_DEFINED":       "view",
    "WORKSHEET":          "worksheet",
    "MODEL":              "model",
    "model":              "model",
    "worksheet":          "worksheet",
    "table":              "table",
    "aggr_worksheet":     "aggr_worksheet",
    "view":               "view",
}


def _source_type_from_info(info: dict) -> str:
    raw = (info.get("type") or "").strip()
    return _INFO_TYPE_TO_SOURCE_TYPE.get(raw, raw.lower() if raw else "unknown")


def collect_table_fqns_from_models(model_items: list[dict]) -> set[str]:
    """Collect all unique physical table GUIDs referenced across model TMLs."""
    fqns: set[str] = set()
    for item in model_items:
        edoc = parse_edoc(item.get("edoc", {}))
        for key in MODEL_KEYS:
            if key not in edoc:
                continue
            node = edoc[key]
            for tbl_key in ("model_tables", "tables"):
                for t in (node.get(tbl_key) or []):
                    if isinstance(t, dict):
                        fqn = t.get("fqn") or t.get("id") or ""
                        if fqn:
                            fqns.add(fqn)
            break
    return fqns


def build_phys_table_lookup(table_items: list[dict]) -> dict[str, dict]:
    """Build {table_guid → detail} from physical table TML items.

    Also indexed by table_name as a fallback. If two tables share the same name,
    the name key is removed to prevent silent wrong lookups — GUID is always safe.
    """
    lookup: dict[str, dict] = {}
    name_to_guid:    dict[str, str] = {}
    ambiguous_names: set[str]       = set()

    for item in table_items:
        edoc = parse_edoc(item.get("edoc", {}))
        info = item.get("info", {})
        tbl  = edoc.get("table", {})
        if not tbl:
            continue
        conn   = tbl.get("connection", {})
        detail = {
            "table_guid":      info.get("id", ""),
            "table_name":      tbl.get("name", info.get("name", "")),
            "db_name":         tbl.get("db", ""),
            "schema_name":     tbl.get("schema", ""),
            "connection_name": conn.get("name", "") if isinstance(conn, dict) else str(conn),
        }

        for key in (detail["table_guid"], info.get("fqn", "")):
            if key:
                lookup[key] = detail

        tname     = detail["table_name"]
        prev_guid = name_to_guid.get(tname)
        if not tname:
            continue
        if prev_guid is None:
            name_to_guid[tname] = detail["table_guid"]
            lookup[tname]       = detail
        elif prev_guid != detail["table_guid"] and tname not in ambiguous_names:
            ambiguous_names.add(tname)
            lookup.pop(tname, None)

    return lookup


def fetch_column_guid_lookup(session, base_url: str, model_guids: list[str],
                             org_label: str, batch_size: int = 10) -> dict[str, dict[str, str]]:
    """Return {model_guid → {column_name → column_guid}} via metadata/search include_details."""
    result: dict[str, dict[str, str]] = {}
    total = len(model_guids)
    for i in range(0, total, batch_size):
        batch = model_guids[i: i + batch_size]
        log.info("  [%s] Column GUID fetch %d-%d / %d",
                 org_label, i + 1, min(i + batch_size, total), total)
        try:
            r = api_post(session, f"{base_url}/api/rest/2.0/metadata/search", {
                "metadata":        [{"type": "LOGICAL_TABLE", "identifier": g} for g in batch],
                "include_details": True,
                "include_headers": True,
                "record_size":     -1,
            })
            for obj in (r.json() or []):
                mguid   = obj.get("metadata_id") or obj.get("id", "")
                columns = (obj.get("metadata_detail") or {}).get("columns") or []
                col_map: dict[str, str] = {}
                for col in columns:
                    header = col.get("header") or {}
                    cname  = header.get("name", "")
                    cguid  = header.get("id", "")
                    if cname and cguid:
                        col_map[cname]         = cguid
                        col_map[cname.lower()] = cguid
                if mguid:
                    result[mguid] = col_map
        except Exception as exc:
            record_failure("model",
                           f"column GUID batch {i + 1}-{min(i + batch_size, total)}",
                           ", ".join(batch), org_label, "column_guid_fetch", exc)
    return result


def extract_model_columns(model_items: list[dict], org_id: Optional[int], org_name: str,
                          phys_table_lookup: Optional[dict] = None,
                          column_guid_lookup: Optional[dict] = None) -> list[dict]:
    """Extract one CSV row per model column across all model TML items."""
    rows: list[dict]   = []
    phys_table_lookup  = phys_table_lookup  or {}
    column_guid_lookup = column_guid_lookup or {}

    for item in model_items:
        info = item.get("info", {}) or {}
        try:
            rows.extend(_extract_model_columns_one(
                item, org_id, org_name, phys_table_lookup, column_guid_lookup))
        except Exception as exc:
            record_failure("model", info.get("name", ""), info.get("id", ""),
                           org_name, "extract_columns", exc)
    return rows


def _extract_model_columns_one(item: dict, org_id: Optional[int], org_name: str,
                                phys_table_lookup: dict,
                                column_guid_lookup: dict) -> list[dict]:
    """Extract column rows for a single model TML item."""
    rows: list[dict] = []
    edoc = parse_edoc(item.get("edoc", {}))
    info = item.get("info", {})

    for key in MODEL_KEYS:
        if key not in edoc:
            continue
        node = edoc[key]
        if not isinstance(node, dict):
            continue

        model_guid = info.get("id", "")
        model_name = node.get("name", info.get("name", ""))
        model_type = key

        alias_to_table: dict[str, dict] = {}
        for tbl_key in ("model_tables", "tables"):
            for t in (node.get(tbl_key) or []):
                if not isinstance(t, dict):
                    continue
                alias = t.get("alias") or t.get("name") or ""
                fqn   = t.get("fqn") or t.get("id") or ""
                if alias:
                    alias_to_table[alias] = {
                        "source_table_name": t.get("name", ""),
                        "source_table_guid": fqn,
                    }

        formula_idx  = build_formula_index(node.get("formulas") or [])
        col_guid_map = column_guid_lookup.get(model_guid, {})

        for col in (node.get("columns") or []):
            if not isinstance(col, dict):
                continue

            col_name    = col.get("name", "")
            props       = col.get("properties", {}) or {}
            col_type    = props.get("column_type", "")
            aggregation = props.get("aggregation", "")
            formula_id  = col.get("formula_id", "")
            column_id   = col.get("column_id", "")

            is_hidden     = bool(props.get("is_hidden"))
            is_calc       = bool(formula_id)
            formula_scope = "model" if is_calc else ""
            formula_expr  = formula_idx.get(formula_id, "") if is_calc else ""

            if "::" in (column_id or ""):
                src_alias, db_col = column_id.split("::", 1)
            else:
                src_alias, db_col = "", column_id

            tbl_info    = alias_to_table.get(src_alias, {})
            tbl_guid    = tbl_info.get("source_table_guid", "")
            tbl_name    = tbl_info.get("source_table_name", "")
            phys_detail = (phys_table_lookup.get(tbl_guid)
                           or phys_table_lookup.get(tbl_name) or {})

            rows.append({
                "org_id":             org_id if org_id is not None else "",
                "org_name":           org_name,
                "model_guid":         model_guid,
                "model_name":         model_name,
                "model_type":         model_type,
                "column_guid":        ("hidden" if is_hidden else
                                       (col_guid_map.get(col_name)
                                        or col_guid_map.get(col_name.lower(), ""))),
                "column_name":        col_name,
                "column_type":        col_type,
                "aggregation":        aggregation,
                "is_calculated":      is_calc,
                "formula_scope":      formula_scope,
                "formula_expr":       formula_expr,
                "source_table_alias": src_alias,
                "source_table_name":  phys_detail.get("table_name", tbl_name),
                "source_table_guid":  tbl_guid,
                "db_name":            phys_detail.get("db_name", ""),
                "schema_name":        phys_detail.get("schema_name", ""),
                "connection_name":    phys_detail.get("connection_name", ""),
                "db_column_name":     db_col,
            })
        break  # one MODEL_KEY per edoc

    return rows


# ---------------------------------------------------------------------------
# Display-name → model column name resolver
# ---------------------------------------------------------------------------

_DATE_PAREN = _re.compile(
    r'^(?:day of week|hour of day|hour_of_day|day|week|month|quarter|year'
    r'|daily|weekly|monthly|quarterly|yearly'
    r'|month_of_year|day_of_week|quarter_of_year|day_of_month'
    r'|week_of_year|day_of_year|hour_of_day)\((.+)\)$',
    _re.IGNORECASE,
)
_PAREN_SUFFIX = _re.compile(r'^(.+?)\s*\([^)]+\)$')
_DATE_PREFIX  = _re.compile(r'^(?:daily|weekly|monthly|quarterly|yearly)\s+(.+)$', _re.IGNORECASE)
_SET_SUFFIX   = _re.compile(r'\s+set(?:\s+\d+)?$', _re.IGNORECASE)
_AGG_PREFIXES = [
    "unique number of ", "unique count ", "number of ",
    "minimum ", "maximum ", "average ", "avg ",
    "total ", "count ", "sum ", "max ", "min ", "std dev ", "variance ",
]


def _candidates(display_name: str) -> list:
    """Return candidate raw column names by stripping ThoughtSpot display prefixes/suffixes."""
    cands = [display_name]

    no_set = _SET_SUFFIX.sub("", display_name).strip()
    if no_set != display_name:
        cands.append(no_set)

    for src in (display_name, no_set):
        m = _DATE_PAREN.match(src)
        if m:
            cands.append(m.group(1))
            break

    m = _DATE_PREFIX.match(display_name)
    if m:
        cands.append(m.group(1))

    current = display_name
    while True:
        low      = current.lower()
        stripped = None
        intermediate = None
        for prefix in _AGG_PREFIXES:
            if low.startswith(prefix):
                stripped  = current[len(prefix):]
                space_pos = prefix.index(" ")
                if space_pos + 1 < len(prefix):
                    intermediate = current[space_pos + 1:]
                break
        if stripped is None:
            break
        cands.append(stripped)
        if intermediate and intermediate not in cands:
            cands.append(intermediate)
        current = stripped

    for cand in list(cands):
        pm = _PAREN_SUFFIX.match(cand)
        if pm:
            base = pm.group(1).strip()
            if base and base not in cands:
                cands.append(base)

    return cands


def parse_search_query(sq: str) -> list:
    """Extract unique column tokens from a TML search_query string.

    Strips multi-join alias prefixes: [ModelName_1::ColName] → ColName.
    """
    seen:   set  = set()
    tokens: list = []
    for token in _re.findall(r'\[([^\]]+)\]', sq or ""):
        if "::" in token and not token.lower().startswith("formula_"):
            token = token.split("::", 1)[1].strip()
        low = token.lower()
        if low not in seen:
            seen.add(low)
            tokens.append(token)
    return tokens


def resolve_column_name(display_name: str, model_col_lookup: dict) -> str:
    """Map a viz display name to the underlying model column name."""
    for cand in _candidates(display_name):
        hit = model_col_lookup.get(cand.lower())
        if hit:
            return hit
    return display_name


def _classify_sq_token(token: str, ans_formula_idx: dict, model_formula_idx: dict,
                        cohort_names: frozenset, formula_name_map: dict) -> tuple:
    """Classify one search_query token as physical, model formula, answer formula, or cohort.

    Returns (col_name, is_calculated, formula_scope, formula_expr).
    """
    low = token.lower()
    if low.startswith("formula_"):
        fid          = token[8:]
        display_name = formula_name_map.get(token) or formula_name_map.get(fid) or fid
        return display_name, True, "answer", ans_formula_idx.get(fid, "")
    if token in ans_formula_idx:
        return token, True, "answer", ans_formula_idx[token]
    if token in model_formula_idx:
        return token, True, "model", model_formula_idx[token]
    if low in cohort_names:
        return token, True, "cohort", ""
    return token, False, "", ""


def _register_source_by_name(source_info_by_name: dict, name_to_guid: dict,
                              sinfo: dict) -> None:
    """Register a data source by name, removing the entry if two GUIDs share the same name.

    Prevents silent wrong-source attribution when a bundle has two sources with identical names.
    """
    name = sinfo.get("source_name", "")
    if not name:
        return
    guid      = sinfo.get("source_guid", "")
    prev_guid = name_to_guid.get(name)
    if prev_guid is None:
        name_to_guid[name]        = guid
        source_info_by_name[name] = sinfo
    elif prev_guid != guid:
        name_to_guid[name] = "__AMBIGUOUS__"
        source_info_by_name.pop(name, None)


def resolve_and_classify(display_name: str, col_lookup: dict, ans_formula_idx: dict,
                         model_formula_idx: dict,
                         cohort_names: frozenset = frozenset()) -> tuple:
    """Fallback classifier used when search_query is absent — strips display prefixes first.

    Returns (col_name, is_calculated, formula_scope, formula_expr).
    """
    for cand in _candidates(display_name):
        if cand in ans_formula_idx:
            return cand, True, "answer", ans_formula_idx[cand]
        if cand in model_formula_idx:
            return cand, True, "model", model_formula_idx[cand]
        if cand.lower() in cohort_names:
            return cand, True, "cohort", ""
    resolved = resolve_column_name(display_name, col_lookup)
    return resolved, False, "", ""


# ---------------------------------------------------------------------------
# Set 1b — Liveboard → Viz → Column
# ---------------------------------------------------------------------------

def extract_liveboard_columns(bundle: list[dict], lb_guid: str, lb_name: str,
                              org_id: Optional[int], org_name: str,
                              model_col_lookup: Optional[dict] = None) -> list[dict]:
    """Extract one row per Liveboard → Viz → Column from a TML bundle."""
    rows: list[dict] = []

    # Formula indexes are kept per source GUID, not merged across the whole bundle.
    # A liveboard can pull from multiple models — merging would risk one model's
    # formula overwriting a same-named formula from a different model.
    model_formula_idx_by_source: dict[str, dict[str, str]] = {}
    source_info_by_name: dict[str, dict] = {}
    source_info_by_fqn:  dict[str, dict] = {}
    source_name_to_guid: dict[str, str]  = {}
    cohort_names_lower:  set[str]        = set()

    for item in bundle:
        edoc = parse_edoc(item.get("edoc", {}))
        info = item.get("info", {})
        registered = False

        if "cohort" in edoc:
            cohort_node = edoc["cohort"]
            if isinstance(cohort_node, dict):
                cname = cohort_node.get("name", info.get("name", ""))
                if cname:
                    cohort_names_lower.add(cname.lower())
            registered = True

        for key in MODEL_KEYS:
            if key not in edoc:
                continue
            node = edoc[key]
            if not isinstance(node, dict):
                continue
            source_guid = info.get("id", "")
            sinfo = {
                "source_guid": source_guid,
                "source_name": node.get("name", info.get("name", "")),
                "source_type": key,
            }
            model_formula_idx_by_source[source_guid] = build_formula_index(
                node.get("formulas") or [])
            _register_source_by_name(source_info_by_name, source_name_to_guid, sinfo)
            if info.get("id"):
                source_info_by_fqn[info["id"]]  = sinfo
            if info.get("fqn"):
                source_info_by_fqn[info["fqn"]] = sinfo
            registered = True
            break

        status_code = (info.get("status") or {}).get("status_code", "")
        if (not registered and status_code != "ERROR"
                and info.get("type") not in ("answer", "liveboard", "")):
            sinfo = {
                "source_guid": info.get("id", ""),
                "source_name": info.get("name", ""),
                "source_type": _source_type_from_info(info),
            }
            _register_source_by_name(source_info_by_name, source_name_to_guid, sinfo)
            if info.get("id"):
                source_info_by_fqn[info["id"]]  = sinfo
            if info.get("fqn"):
                source_info_by_fqn[info["fqn"]] = sinfo

    cohort_names = frozenset(cohort_names_lower)

    for item in bundle:
        edoc = parse_edoc(item.get("edoc", {}))
        if "liveboard" not in edoc:
            continue
        lb_node = edoc["liveboard"]
        if not isinstance(lb_node, dict):
            continue

        for viz in (lb_node.get("visualizations") or []):
            if not isinstance(viz, dict):
                continue

            viz_id       = viz.get("id", "")
            viz_guid     = viz.get("viz_guid", "")
            answer       = viz.get("answer", {}) or {}
            viz_name     = answer.get("name", viz_id)
            display_mode = answer.get("display_mode", "")
            chart_node   = answer.get("chart") or {}
            chart_type   = chart_node.get("type", "") if isinstance(chart_node, dict) else ""

            ans_formula_idx  = build_formula_index(answer.get("formulas") or [])
            formula_name_map = build_formula_name_map(answer.get("formulas") or [])
            sq_tokens        = parse_search_query(answer.get("search_query", ""))

            viz_source: dict = {}
            for tbl_ref in (answer.get("tables") or []):
                if not isinstance(tbl_ref, dict):
                    continue
                found = (source_info_by_fqn.get(tbl_ref.get("fqn", ""))
                         or source_info_by_name.get(tbl_ref.get("name", ""))
                         or {})
                if found:
                    viz_source = found
                    break

            viz_col_lookup    = (model_col_lookup or {}).get(viz_source.get("source_guid", ""), {})
            viz_model_formula = model_formula_idx_by_source.get(
                viz_source.get("source_guid", ""), {})

            if sq_tokens:
                col_classify = [
                    _classify_sq_token(t, ans_formula_idx, viz_model_formula,
                                       cohort_names, formula_name_map)
                    for t in sq_tokens
                ]
            else:
                col_classify = []
                for col_entry in (answer.get("answer_columns") or []):
                    col_name = (col_entry.get("name", "")
                                if isinstance(col_entry, dict) else str(col_entry))
                    col_classify.append(resolve_and_classify(
                        col_name, viz_col_lookup, ans_formula_idx,
                        viz_model_formula, cohort_names))

            for col_name, is_calc, formula_scope, formula_expr in col_classify:
                rows.append({
                    "org_id":           org_id if org_id is not None else "",
                    "org_name":         org_name,
                    "liveboard_guid":   lb_guid,
                    "liveboard_name":   lb_name,
                    "viz_id":           viz_id,
                    "viz_guid":         viz_guid,
                    "viz_name":         viz_name,
                    "viz_display_mode": display_mode,
                    "viz_chart_type":   chart_type,
                    "column_name":      col_name,
                    "is_calculated":    is_calc,
                    "formula_scope":    formula_scope,
                    "formula_expr":     formula_expr,
                    "source_type":      viz_source.get("source_type", ""),
                    "source_guid":      viz_source.get("source_guid", ""),
                    "source_name":      viz_source.get("source_name", ""),
                })
        break  # one liveboard per bundle root

    return [r for r in rows if r.get("source_guid")]


# ---------------------------------------------------------------------------
# Set 1c — Standalone Answer → Column
# ---------------------------------------------------------------------------

def extract_answer_columns(bundle: list[dict], ans_guid: str, ans_name: str,
                           org_id: Optional[int], org_name: str,
                           model_col_lookup: Optional[dict] = None) -> list[dict]:
    """Extract one row per Answer → Column from a TML bundle."""
    rows: list[dict] = []

    # Same per-source formula index pattern as extract_liveboard_columns.
    model_formula_idx_by_source: dict[str, dict[str, str]] = {}
    source_info_by_name: dict[str, dict] = {}
    source_info_by_fqn:  dict[str, dict] = {}
    source_name_to_guid: dict[str, str]  = {}
    cohort_names_lower:  set[str]        = set()

    for item in bundle:
        edoc = parse_edoc(item.get("edoc", {}))
        info = item.get("info", {})
        registered = False

        if "cohort" in edoc:
            cohort_node = edoc["cohort"]
            if isinstance(cohort_node, dict):
                cname = cohort_node.get("name", info.get("name", ""))
                if cname:
                    cohort_names_lower.add(cname.lower())
            registered = True

        for key in MODEL_KEYS:
            if key not in edoc:
                continue
            node = edoc[key]
            if not isinstance(node, dict):
                continue
            source_guid = info.get("id", "")
            sinfo = {
                "source_guid": source_guid,
                "source_name": node.get("name", info.get("name", "")),
                "source_type": key,
            }
            model_formula_idx_by_source[source_guid] = build_formula_index(
                node.get("formulas") or [])
            _register_source_by_name(source_info_by_name, source_name_to_guid, sinfo)
            if info.get("id"):
                source_info_by_fqn[info["id"]]  = sinfo
            if info.get("fqn"):
                source_info_by_fqn[info["fqn"]] = sinfo
            registered = True
            break

        status_code = (info.get("status") or {}).get("status_code", "")
        if (not registered and status_code != "ERROR"
                and info.get("type") not in ("answer", "liveboard", "")):
            sinfo = {
                "source_guid": info.get("id", ""),
                "source_name": info.get("name", ""),
                "source_type": _source_type_from_info(info),
            }
            _register_source_by_name(source_info_by_name, source_name_to_guid, sinfo)
            if info.get("id"):
                source_info_by_fqn[info["id"]]  = sinfo
            if info.get("fqn"):
                source_info_by_fqn[info["fqn"]] = sinfo

    cohort_names = frozenset(cohort_names_lower)

    for item in bundle:
        edoc = parse_edoc(item.get("edoc", {}))
        if "answer" not in edoc:
            continue
        answer = edoc["answer"]
        if not isinstance(answer, dict):
            continue

        ans_formula_idx  = build_formula_index(answer.get("formulas") or [])
        formula_name_map = build_formula_name_map(answer.get("formulas") or [])
        sq_tokens        = parse_search_query(answer.get("search_query", ""))

        ans_source: dict = {}
        for tbl_ref in (answer.get("tables") or []):
            if not isinstance(tbl_ref, dict):
                continue
            found = (source_info_by_fqn.get(tbl_ref.get("fqn", ""))
                     or source_info_by_name.get(tbl_ref.get("name", ""))
                     or {})
            if found:
                ans_source = found
                break

        ans_col_lookup    = (model_col_lookup or {}).get(ans_source.get("source_guid", ""), {})
        ans_model_formula = model_formula_idx_by_source.get(
            ans_source.get("source_guid", ""), {})

        if sq_tokens:
            col_classify = [
                _classify_sq_token(t, ans_formula_idx, ans_model_formula,
                                   cohort_names, formula_name_map)
                for t in sq_tokens
            ]
        else:
            col_classify = []
            for col_entry in (answer.get("answer_columns") or []):
                col_name = (col_entry.get("name", "")
                            if isinstance(col_entry, dict) else str(col_entry))
                col_classify.append(resolve_and_classify(
                    col_name, ans_col_lookup, ans_formula_idx,
                    ans_model_formula, cohort_names))

        for col_name, is_calc, formula_scope, formula_expr in col_classify:
            rows.append({
                "org_id":        org_id if org_id is not None else "",
                "org_name":      org_name,
                "answer_guid":   ans_guid,
                "answer_name":   ans_name,
                "column_name":   col_name,
                "is_calculated": is_calc,
                "formula_scope": formula_scope,
                "formula_expr":  formula_expr,
                "source_type":   ans_source.get("source_type", ""),
                "source_guid":   ans_source.get("source_guid", ""),
                "source_name":   ans_source.get("source_name", ""),
            })
        break  # one answer per export

    return [r for r in rows if r.get("source_guid")]


# ---------------------------------------------------------------------------
# Per-org processing
# ---------------------------------------------------------------------------

def process_org(base_url: str, username: str, password: Optional[str],
                secret_key: Optional[str], org_id: Optional[int],
                org_name: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Run all three extraction passes for one org. Returns (model_rows, lb_rows, ans_rows)."""
    log.info("─" * 55)
    log.info("Org: %s (id=%s)", org_name, org_id)
    log.info("─" * 55)

    try:
        token = get_token(base_url, username, password, secret_key, org_id=org_id)
    except requests.HTTPError as exc:
        record_failure("org", org_name, str(org_id) if org_id is not None else "",
                       org_name, "auth", exc)
        return [], [], []

    session = RefreshableSession(base_url, username, password, secret_key, org_id, token)

    # ── Set 1a: model columns ────────────────────────────────────────────────
    log.info("  [%s] Fetching all LOGICAL_TABLE objects ...", org_name)
    all_logical = list_metadata(session, base_url, "LOGICAL_TABLE", org_name)
    all_guids   = [obj.get("metadata_id") or obj.get("id", "")
                   for obj in all_logical
                   if obj.get("metadata_id") or obj.get("id")]
    model_name_by_guid = {
        (obj.get("metadata_id") or obj.get("id", "")): (obj.get("metadata_name") or obj.get("name", ""))
        for obj in all_logical
    }

    all_model_items: list[dict] = []
    batch_size = 25
    for i in range(0, len(all_guids), batch_size):
        batch = all_guids[i: i + batch_size]
        log.info("  [%s] Model TML batch %d-%d / %d",
                 org_name, i + 1, min(i + batch_size, len(all_guids)), len(all_guids))
        all_model_items.extend(export_tml_batch(
            session, base_url, batch, org_name,
            obj_type="model", names_by_guid=model_name_by_guid))

    table_fqn_list = list(collect_table_fqns_from_models(all_model_items))
    log.info("  [%s] Fetching %d physical table TMLs ...", org_name, len(table_fqn_list))
    phys_items: list[dict] = []
    for i in range(0, len(table_fqn_list), batch_size):
        phys_items.extend(export_tml_batch(
            session, base_url, table_fqn_list[i: i + batch_size],
            org_name, obj_type="table"))
    phys_table_lookup = build_phys_table_lookup(phys_items)

    log.info("  [%s] Fetching column GUIDs for %d models ...", org_name, len(all_guids))
    column_guid_lookup = fetch_column_guid_lookup(session, base_url, all_guids, org_name)

    model_rows = extract_model_columns(
        all_model_items, org_id, org_name, phys_table_lookup, column_guid_lookup)
    log.info("  [%s] Model columns: %d rows.", org_name, len(model_rows))

    # {model_guid: {col_name_lower: col_name_original}} used for display-name resolution
    model_col_lookup: dict[str, dict] = {}
    for row in model_rows:
        mguid = row.get("model_guid", "")
        cname = row.get("column_name", "")
        if mguid and cname:
            model_col_lookup.setdefault(mguid, {})[cname.lower()] = cname

    # ── Set 1b: liveboards ───────────────────────────────────────────────────
    lb_rows: list[dict] = []
    liveboards = list_metadata(session, base_url, "LIVEBOARD", org_name)
    for lb in liveboards:
        lb_guid = lb.get("metadata_id") or lb.get("id", "")
        lb_name = lb.get("metadata_name") or lb.get("name", "")
        if not lb_guid:
            continue
        log.info("  [%s] Liveboard: %s", org_name, lb_name)
        bundle = export_tml_bundle(session, base_url, lb_guid, lb_name, org_name,
                                   associated=True, obj_type="liveboard")
        if not bundle:
            continue
        try:
            lb_rows.extend(extract_liveboard_columns(
                bundle, lb_guid, lb_name, org_id, org_name,
                model_col_lookup=model_col_lookup))
        except Exception as exc:
            record_failure("liveboard", lb_name, lb_guid, org_name, "extract_columns", exc)

    # ── Set 1c: standalone answers ───────────────────────────────────────────
    ans_rows: list[dict] = []
    answers = list_metadata(session, base_url, "ANSWER", org_name)
    for ans in answers:
        ans_guid = ans.get("metadata_id") or ans.get("id", "")
        ans_name = ans.get("metadata_name") or ans.get("name", "")
        if not ans_guid:
            continue
        log.info("  [%s] Answer: %s", org_name, ans_name)
        bundle = export_tml_bundle(session, base_url, ans_guid, ans_name, org_name,
                                   associated=True, obj_type="answer")
        if not bundle:
            continue
        try:
            ans_rows.extend(extract_answer_columns(
                bundle, ans_guid, ans_name, org_id, org_name,
                model_col_lookup=model_col_lookup))
        except Exception as exc:
            record_failure("answer", ans_name, ans_guid, org_name, "extract_columns", exc)

    log.info("  [%s] Done — %d model-col rows, %d lb-col rows, %d answer-col rows.",
             org_name, len(model_rows), len(lb_rows), len(ans_rows))
    return model_rows, lb_rows, ans_rows


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

def write_csv(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    log.info("  Written: %s  (%d rows)", path, len(rows))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    parser = argparse.ArgumentParser(description="ThoughtSpot column lineage extractor")
    parser.add_argument("--org", default=None,
                        help="Org id or name to process (overrides prompt / TS_ORG)")
    parser.add_argument("--out-model",    default=f"model_columns_{ts}.csv")
    parser.add_argument("--out-lb",       default=f"liveboard_columns_{ts}.csv")
    parser.add_argument("--out-ans",      default=f"answer_columns_{ts}.csv")
    parser.add_argument("--out-failures", default=f"failures_{ts}.csv")
    parser.add_argument("--debug", action="store_true", help="Verbose debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    base_url, username, password, secret_key, prompted_org = prompt_credentials()
    org_identifier = args.org or prompted_org
    log.info("Connecting to %s as %s", base_url, username)

    t_start = time.time()

    try:
        token = get_token(base_url, username, password, secret_key)
    except requests.HTTPError as exc:
        log.error("Authentication failed: %s", exc)
        sys.exit(1)

    session          = RefreshableSession(base_url, username, password, secret_key, None, token)
    org_name, org_id = resolve_org(session, base_url, org_identifier)
    log.info("Org: %s (id=%s)", org_name, org_id)

    all_model_rows, all_lb_rows, all_ans_rows = process_org(
        base_url, username, password, secret_key, org_id, org_name)

    elapsed = time.time() - t_start
    log.info("=" * 55)
    log.info("Finished in %.1fs — writing output files ...", elapsed)

    write_csv(args.out_model,    MODEL_COL_FIELDS, all_model_rows)
    write_csv(args.out_lb,       LB_COL_FIELDS,    all_lb_rows)
    write_csv(args.out_ans,      ANS_COL_FIELDS,   all_ans_rows)
    write_csv(args.out_failures, FAILURE_FIELDS,   FAILURES)
    log_failure_summary()
    log.info("Done.")


if __name__ == "__main__":
    main()
