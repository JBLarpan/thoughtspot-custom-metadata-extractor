#!/usr/bin/env python3
"""
thoughtspot_user_access_map.py — Effective-permissions map for ThoughtSpot objects.

Produces four CSVs per run:
  access_liveboards_<ts>.csv — effective permissions on every liveboard
  access_answers_<ts>.csv    — effective permissions on every answer
  access_models_<ts>.csv     — effective permissions on every model/worksheet/table/view
  access_failures_<ts>.csv   — objects whose permissions could not be fetched

Permissions are EFFECTIVE — includes access inherited through group membership.

Usage:  python3 thoughtspot_user_access_map.py
Env vars: TS_URL, TS_USERNAME, TS_PASSWORD or TS_SECRET_KEY, TS_ORG
"""

import csv
import datetime
import getpass
import logging
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests

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
log = logging.getLogger("ts_access_map")

# ---------------------------------------------------------------------------
# Failure tracking
# ---------------------------------------------------------------------------

FAILURE_FIELDS = [
    "timestamp", "org_name", "object_type", "object_name", "object_guid",
    "stage", "http_status", "reason",
]

FAILURES: list = []


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
        log.info("No failures recorded — every object processed cleanly.")
        return

    log.info("=" * 55)
    log.info("FAILURE SUMMARY — %d object(s) could not be fully processed:", len(FAILURES))
    log.info("=" * 55)

    by_type_stage: Dict[tuple, int] = {}
    for f in FAILURES:
        key = (f["object_type"], f["stage"])
        by_type_stage[key] = by_type_stage.get(key, 0) + 1

    for (obj_type, stage), count in sorted(by_type_stage.items()):
        log.info("  %-20s / %-20s : %d", obj_type, stage, count)

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

ACCESS_FIELDS = [
    "org_id", "org_name",
    "object_guid", "object_name", "object_type",
    "owner_guid", "owner_username", "owner_display_name",
    "principal_guid", "principal_name", "principal_type",
    "share_mode",
]

# Human-readable labels for permission share modes
_SHARE_MODE_LABELS: Dict[str, str] = {
    "READ_ONLY": "READ",
    "MODIFY":    "MODIFY",
    "NO_ACCESS": "NO_ACCESS",
}

# Subtypes within LOGICAL_TABLE — read from metadata_header.type
_LOGICAL_TABLE_SUBTYPES: Dict[str, str] = {
    "WORKSHEET":          "worksheet",
    "MODEL":              "model",
    "ONE_TO_ONE_LOGICAL": "table",
    "AGGR_WORKSHEET":     "aggr_worksheet",
    "USER_DEFINED":       "view",
    "SQL_VIEW":           "sql_view",
}


def _object_type_label(metadata_type: str, item: Optional[Dict] = None) -> str:
    """Return the human-readable object type label for a metadata item.

    For LOGICAL_TABLE, reads metadata_header.type (and worksheetVersion) to
    distinguish model, worksheet, table, view, aggr_worksheet, and sql_view.
    """
    if metadata_type == "LIVEBOARD":
        return "liveboard"
    if metadata_type == "ANSWER":
        return "answer"
    if metadata_type == "LOGICAL_TABLE" and item:
        hdr = item.get("metadata_header") or {}
        # worksheetVersion=V2 means new-style Model regardless of header.type
        if hdr.get("worksheetVersion") == "V2":
            return "model"
        raw = hdr.get("type", "")
        return _LOGICAL_TABLE_SUBTYPES.get(raw, raw.lower() if raw else "model/worksheet")
    return metadata_type.lower()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def prompt_credentials() -> Tuple[str, str, Optional[str], Optional[str], Optional[str]]:
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
    print("  ThoughtSpot User Access Map")
    print("=" * 60)

    base_url = input("\nThoughtSpot URL (e.g. https://myorg.thoughtspot.cloud): ").strip().rstrip("/")
    if not base_url:
        sys.exit("URL is required.")
    # Strip any trailing API path the user may have accidentally pasted
    for _suffix in ("/api/rest/2.0/auth/token/full", "/api/rest/2.0", "/api/rest", "/api"):
        if base_url.endswith(_suffix):
            base_url = base_url[: -len(_suffix)].rstrip("/")
            break

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
    payload: Dict = {"username": username, "validity_time_in_sec": TOKEN_VALIDITY_SEC}
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
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
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
# Retry-aware API helper
# ---------------------------------------------------------------------------

RETRY_STATUS = {429, 502, 503, 504}


def api_post(session, url: str, payload: Dict,
             retries: int = 4, backoff: float = 3.0) -> Dict:
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
            return r.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == retries - 1:
                raise
            wait = backoff ** (attempt + 1)
            log.warning("  Network error — retry %d/%d in %.0fs: %s",
                        attempt + 1, retries, wait, exc)
            time.sleep(wait)
    raise RuntimeError("api_post exhausted retries")


def api_get(session, url: str,
            retries: int = 4, backoff: float = 3.0) -> Dict:
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
            return r.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == retries - 1:
                raise
            wait = backoff ** (attempt + 1)
            log.warning("  Network error — retry %d/%d in %.0fs: %s",
                        attempt + 1, retries, wait, exc)
            time.sleep(wait)
    raise RuntimeError("api_get exhausted retries")


# ---------------------------------------------------------------------------
# Org helpers
# ---------------------------------------------------------------------------

def get_current_org_info(session, base_url: str) -> Tuple[str, Optional[int]]:
    """Return (org_name, org_id) from the current session's auth context."""
    try:
        data        = api_get(session, f"{base_url}/api/rest/2.0/auth/session/user")
        current_org = data.get("current_org") or {}
        name = current_org.get("name") or current_org.get("org_name") or ""
        oid  = current_org.get("id")
        return name, oid
    except Exception:
        return "", None


def resolve_org(session, base_url: str,
                org_identifier: Optional[str]) -> Tuple[str, Optional[int]]:
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
        data     = api_post(session, f"{base_url}/api/rest/2.0/orgs/search",
                            {"org_identifiers": [org_identifier], "record_size": 200})
        all_orgs = data if isinstance(data, list) else []
    except (requests.HTTPError, requests.Timeout, requests.ConnectionError, RuntimeError) as exc:
        sys.exit(f"Failed to look up org '{org_identifier}': {exc}")

    def _name(o: Dict) -> str:
        return o.get("org_name") or o.get("name") or ""

    def _id(o: Dict) -> Optional[int]:
        return o.get("org_id") if o.get("org_id") is not None else o.get("id")

    ident_lower  = org_identifier.strip().lower()
    name_matches = [o for o in all_orgs if _name(o).strip().lower() == ident_lower]

    id_matches: List[Dict] = []
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
# Metadata listing (paginated)
# ---------------------------------------------------------------------------

def list_metadata(session, base_url: str, metadata_type: str) -> List[Dict]:
    """Return all objects of metadata_type for the current org, paginated."""
    items:  List[Dict] = []
    offset = 0
    batch  = 50
    while True:
        try:
            data = api_post(session, f"{base_url}/api/rest/2.0/metadata/search", {
                "metadata":        [{"type": metadata_type}],
                "record_size":     batch,
                "record_offset":   offset,
                "include_headers": True,
            })
        except (requests.HTTPError, requests.Timeout, requests.ConnectionError, RuntimeError) as exc:
            record_failure("metadata_list", f"{metadata_type} (offset {offset})",
                           "", "(current org)", "metadata_search", exc)
            log.warning("  Stopping %s listing early at offset=%d — %d collected so far.",
                        metadata_type, offset, len(items))
            break
        page = data if isinstance(data, list) else []
        if not page:
            break
        items.extend(page)
        if len(page) < batch:
            break
        offset += batch
    return items


# ---------------------------------------------------------------------------
# User and group lookups
# ---------------------------------------------------------------------------

def build_user_lookup(session, base_url: str) -> Dict[str, Dict]:
    """Return {user_guid: {username, display_name}}."""
    lookup: Dict[str, Dict] = {}
    offset = 0
    while True:
        try:
            data = api_post(session, f"{base_url}/api/rest/2.0/users/search",
                            {"record_size": 200, "record_offset": offset})
        except Exception as exc:
            record_failure("user_list", f"users (offset {offset})", "",
                           "(current org)", "users_search", exc)
            break
        batch = data if isinstance(data, list) else []
        if not batch:
            break
        for u in batch:
            uid = u.get("id", "")
            if uid:
                lookup[uid] = {
                    "username":     u.get("name", ""),
                    "display_name": u.get("display_name", "") or u.get("name", ""),
                }
        if len(batch) < 200:
            break
        offset += len(batch)
    return lookup


def build_group_lookup(session, base_url: str) -> Dict[str, str]:
    """Return {group_guid: group_display_name}."""
    lookup: Dict[str, str] = {}
    offset = 0
    while True:
        try:
            data = api_post(session, f"{base_url}/api/rest/2.0/groups/search",
                            {"record_size": 200, "record_offset": offset})
        except Exception as exc:
            record_failure("group_list", f"groups (offset {offset})", "",
                           "(current org)", "groups_search", exc)
            break
        batch = data if isinstance(data, list) else []
        if not batch:
            break
        for g in batch:
            gid = g.get("id", "")
            if gid:
                lookup[gid] = g.get("display_name", "") or g.get("name", "")
        if len(batch) < 200:
            break
        offset += len(batch)
    return lookup


# ---------------------------------------------------------------------------
# Permissions fetch
# ---------------------------------------------------------------------------

def fetch_permissions_batch(session, base_url: str,
                            metadata_type: str, guids: List[str],
                            org_name: str) -> Tuple[List[Dict], List[str]]:
    """Fetch EFFECTIVE permissions for one batch of GUIDs.

    Returns (perm_rows, failed_guids).
    On any failure, the batch is retried one GUID at a time to isolate the bad object.
    """
    if not guids:
        return [], []
    payload = {
        "metadata":        [{"type": metadata_type, "identifier": g} for g in guids],
        "permission_type": "EFFECTIVE",
        "record_size":     500,
    }
    try:
        data = api_post(session,
                        f"{base_url}/api/rest/2.0/security/metadata/fetch-permissions",
                        payload)
    except Exception as exc:
        if len(guids) == 1:
            record_failure(metadata_type.lower(), guids[0], guids[0],
                           org_name, "fetch_permissions", exc)
            return [], [guids[0]]
        log.warning("  Permissions batch of %d failed — retrying one at a time.", len(guids))
        all_rows:   List[Dict] = []
        all_failed: List[str]  = []
        for g in guids:
            rows, failed = fetch_permissions_batch(
                session, base_url, metadata_type, [g], org_name)
            all_rows.extend(rows)
            all_failed.extend(failed)
        return all_rows, all_failed

    # Response is {"metadata_permission_details": [...]}
    details = (data.get("metadata_permission_details") or []) if isinstance(data, dict) else []

    perm_rows: List[Dict] = []
    for obj in details:
        if not obj:
            continue
        obj_guid  = obj.get("metadata_id", "")
        obj_name  = obj.get("metadata_name", "")
        author    = obj.get("metadata_author") or {}
        owner_guid = author.get("id", "")
        owner_name = author.get("name", "")

        for principal_block in (obj.get("principal_permission_info") or []):
            p_type_raw = principal_block.get("principal_type", "")
            p_type     = "user" if p_type_raw == "USER" else "group"
            for perm in (principal_block.get("principal_permissions") or []):
                perm_rows.append({
                    "obj_guid":       obj_guid,
                    "obj_name":       obj_name,
                    "owner_guid":     owner_guid,
                    "owner_name":     owner_name,
                    "principal_guid": perm.get("principal_id", ""),
                    "principal_name": perm.get("principal_name", ""),
                    "principal_type": p_type,
                    "share_mode":     _SHARE_MODE_LABELS.get(
                                          perm.get("permission", ""),
                                          perm.get("permission", "")),
                })
    return perm_rows, []


def fetch_all_permissions(session, base_url: str, metadata_type: str,
                          items: List[Dict], org_name: str,
                          batch_size: int = 25) -> Tuple[List[Dict], List[str]]:
    """Fetch permissions for all items in batches. Returns (perm_rows, failed_guids)."""
    all_rows:   List[Dict] = []
    all_failed: List[str]  = []
    guids = [it["metadata_id"] for it in items if it.get("metadata_id")]

    for i in range(0, len(guids), batch_size):
        batch = guids[i: i + batch_size]
        end   = min(i + batch_size, len(guids))
        log.info("    permissions %d – %d / %d ...", i + 1, end, len(guids))
        rows, failed = fetch_permissions_batch(
            session, base_url, metadata_type, batch, org_name)
        all_rows.extend(rows)
        all_failed.extend(failed)

    return all_rows, all_failed


# ---------------------------------------------------------------------------
# Build final output rows
# ---------------------------------------------------------------------------

def build_output_rows(perm_rows: List[Dict], meta_items: List[Dict],
                      metadata_type: str, user_lookup: Dict[str, Dict],
                      group_lookup: Dict[str, str],
                      org_id: Optional[int], org_name: str) -> List[Dict]:
    """Map raw permission rows to ACCESS_FIELDS, resolving object type per item."""
    # Build {guid: item} so we can look up the metadata_header for subtype resolution
    item_by_guid: Dict[str, Dict] = {it["metadata_id"]: it for it in meta_items
                                     if it.get("metadata_id")}

    output: List[Dict] = []
    for row in perm_rows:
        p_guid = row["principal_guid"]
        p_type = row["principal_type"]

        # principal_name comes directly from the API; fall back to lookup for deleted principals
        p_name = row.get("principal_name", "")
        if not p_name:
            if p_type == "user":
                u      = user_lookup.get(p_guid, {})
                p_name = u.get("display_name") or u.get("username") or p_guid
            else:
                p_name = group_lookup.get(p_guid, p_guid)

        # Owner info from permissions response, enriched from user lookup
        owner_guid         = row.get("owner_guid", "")
        owner_info         = user_lookup.get(owner_guid, {})
        owner_username     = owner_info.get("username", "")     or row.get("owner_name", "")
        owner_display_name = owner_info.get("display_name", "") or row.get("owner_name", "")

        # Resolve the correct object type label (handles LOGICAL_TABLE subtypes)
        obj_guid = row["obj_guid"]
        item     = item_by_guid.get(obj_guid)
        obj_type = _object_type_label(metadata_type, item)

        output.append({
            "org_id":             org_id if org_id is not None else "",
            "org_name":           org_name,
            "object_guid":        obj_guid,
            "object_name":        row["obj_name"],
            "object_type":        obj_type,
            "owner_guid":         owner_guid,
            "owner_username":     owner_username,
            "owner_display_name": owner_display_name,
            "principal_guid":     p_guid,
            "principal_name":     p_name,
            "principal_type":     p_type,
            "share_mode":         row["share_mode"],
        })
    return output


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

def write_csv(path: str, fields: List[str], rows: List[Dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    log.info("  Written: %s  (%d rows)", path, len(rows))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    base_url, username, password, secret_key, prompted_org = prompt_credentials()
    log.info("Connecting to %s as %s", base_url, username)

    t_start = time.time()

    try:
        token = get_token(base_url, username, password, secret_key)
    except requests.HTTPError as exc:
        log.error("Authentication failed: %s", exc)
        sys.exit(1)

    session          = RefreshableSession(base_url, username, password, secret_key, None, token)
    org_name, org_id = resolve_org(session, base_url, prompted_org)
    log.info("Org: %s (id=%s)", org_name, org_id)

    # Re-authenticate scoped to the resolved org
    try:
        org_token = get_token(base_url, username, password, secret_key, org_id)
    except requests.HTTPError as exc:
        log.error("Org-scoped authentication failed: %s", exc)
        sys.exit(1)
    session = RefreshableSession(base_url, username, password, secret_key, org_id, org_token)

    # ── User / group lookups ────────────────────────────────────────────────
    log.info("Fetching users ...")
    user_lookup = build_user_lookup(session, base_url)
    log.info("  %d users found.", len(user_lookup))

    log.info("Fetching groups ...")
    group_lookup = build_group_lookup(session, base_url)
    log.info("  %d groups found.", len(group_lookup))

    # ── Liveboards ─────────────────────────────────────────────────────────
    log.info("Fetching liveboards ...")
    lb_items = list_metadata(session, base_url, "LIVEBOARD")
    log.info("  %d liveboards.", len(lb_items))
    log.info("Fetching liveboard permissions ...")
    lb_perm, lb_fail = fetch_all_permissions(session, base_url, "LIVEBOARD", lb_items, org_name)
    lb_rows = build_output_rows(lb_perm, lb_items, "LIVEBOARD", user_lookup, group_lookup, org_id, org_name)

    # ── Answers ────────────────────────────────────────────────────────────
    log.info("Fetching answers ...")
    ans_items = list_metadata(session, base_url, "ANSWER")
    log.info("  %d answers.", len(ans_items))
    log.info("Fetching answer permissions ...")
    ans_perm, ans_fail = fetch_all_permissions(session, base_url, "ANSWER", ans_items, org_name)
    ans_rows = build_output_rows(ans_perm, ans_items, "ANSWER", user_lookup, group_lookup, org_id, org_name)

    # ── Models / worksheets / tables / views ───────────────────────────────
    log.info("Fetching models/worksheets/tables/views ...")
    mdl_items = list_metadata(session, base_url, "LOGICAL_TABLE")
    log.info("  %d objects.", len(mdl_items))
    log.info("Fetching model/table/view permissions ...")
    mdl_perm, mdl_fail = fetch_all_permissions(session, base_url, "LOGICAL_TABLE", mdl_items, org_name)
    mdl_rows = build_output_rows(mdl_perm, mdl_items, "LOGICAL_TABLE", user_lookup, group_lookup, org_id, org_name)

    # Record permission failures
    all_failed_guids = (
        [(g, "LIVEBOARD") for g in lb_fail]
        + [(g, "ANSWER")        for g in ans_fail]
        + [(g, "LOGICAL_TABLE") for g in mdl_fail]
    )
    name_maps = {
        "LIVEBOARD":     {it["metadata_id"]: it.get("metadata_name", "") for it in lb_items},
        "ANSWER":        {it["metadata_id"]: it.get("metadata_name", "") for it in ans_items},
        "LOGICAL_TABLE": {it["metadata_id"]: it.get("metadata_name", "") for it in mdl_items},
    }

    elapsed = time.time() - t_start
    log.info("=" * 55)
    log.info("Finished in %.1fs — writing output files ...", elapsed)

    write_csv(f"access_liveboards_{ts}.csv", ACCESS_FIELDS, lb_rows)
    write_csv(f"access_answers_{ts}.csv",    ACCESS_FIELDS, ans_rows)
    write_csv(f"access_models_{ts}.csv",     ACCESS_FIELDS, mdl_rows)
    write_csv(f"access_failures_{ts}.csv",   FAILURE_FIELDS, FAILURES)

    log_failure_summary()
    log.info("Done.")
    log.info("  Liveboards : %d access rows", len(lb_rows))
    log.info("  Answers    : %d access rows", len(ans_rows))
    log.info("  Models     : %d access rows", len(mdl_rows))


if __name__ == "__main__":
    main()
