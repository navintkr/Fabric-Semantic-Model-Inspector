"""
Fabric / Power BI tenant-wide inventory via the Admin Scanner API.

Produces a full security/exposure inventory across every workspace in the
tenant — WITHOUT requiring workspace-level roles on each workspace.

Requires:
  * A service principal (app registration) whose object is a member of a
    security group listed in the Fabric tenant setting:
      "Allow service principals to use read-only admin APIs"
  * Tenant settings enabled:
      - Service principals can use read-only admin APIs
      - Enhance admin APIs responses with detailed metadata
      - Enhance admin APIs responses with DAX and mashup expressions

Auth: DefaultAzureCredential
  * Local dev:  set AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET
  * Or:         az login --service-principal -u <app-id> -p <secret> --tenant <tid>

Usage:
  python fabric_tenant_scanner.py                 # scan all workspaces
  python fabric_tenant_scanner.py --modified-days 30
  python fabric_tenant_scanner.py --output ./out
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from azure.identity import DefaultAzureCredential

PBI = "https://api.powerbi.com/v1.0/myorg"
SCOPE = "https://analysis.windows.net/powerbi/api/.default"

# Admin scanner limits (per Microsoft docs, as of 2026):
GETINFO_BATCH = 100            # max workspaces per getInfo call
GETINFO_MAX_CALLS_PER_HOUR = 500
SCAN_POLL_SECS = 5
SCAN_POLL_TIMEOUT_SECS = 60 * 30  # 30 min per scan

_cred = None


def token() -> str:
    global _cred
    if _cred is None:
        _cred = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    return _cred.get_token(SCOPE).token


def headers() -> dict:
    return {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}


def list_modified_workspaces(modified_since: datetime | None) -> list[str]:
    """Return workspace IDs modified since the given UTC datetime.

    If modified_since is None, returns *all* workspaces in the tenant.
    """
    params = {"excludePersonalWorkspaces": "true"}
    if modified_since is not None:
        # The admin API requires the 7-digit fractional-seconds ISO 8601 format
        # (e.g. 2026-03-21T19:22:16.0000000Z) and, when modifiedSince is provided,
        # excludeInActiveWorkspaces must also be set.
        ts = modified_since.astimezone(timezone.utc).replace(microsecond=0)
        params["modifiedSince"] = ts.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        params["excludeInActiveWorkspaces"] = "true"
    r = requests.get(f"{PBI}/admin/workspaces/modified", headers=headers(), params=params, timeout=120)
    r.raise_for_status()
    data = r.json()
    return [w["id"] for w in data]


def start_scan(workspace_ids: list[str]) -> str:
    """POST getInfo for a batch of up to 100 workspace IDs. Returns scan id."""
    params = {
        "lineage": "true",
        "datasourceDetails": "true",
        "datasetSchema": "true",
        "datasetExpressions": "true",
        "getArtifactUsers": "true",
    }
    body = {"workspaces": workspace_ids}
    r = requests.post(
        f"{PBI}/admin/workspaces/getInfo",
        headers=headers(),
        params=params,
        json=body,
        timeout=120,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"getInfo failed: {r.status_code} {r.text}")
    return r.json()["id"]


def wait_for_scan(scan_id: str) -> None:
    deadline = time.monotonic() + SCAN_POLL_TIMEOUT_SECS
    while True:
        r = requests.get(f"{PBI}/admin/workspaces/scanStatus/{scan_id}", headers=headers(), timeout=60)
        r.raise_for_status()
        status = r.json().get("status")
        if status == "Succeeded":
            return
        if status in ("Failed", "NotStarted"):
            raise RuntimeError(f"Scan {scan_id} status={status}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"Scan {scan_id} not complete within {SCAN_POLL_TIMEOUT_SECS}s (last status={status})")
        time.sleep(SCAN_POLL_SECS)


def get_scan_result(scan_id: str) -> dict:
    r = requests.get(f"{PBI}/admin/workspaces/scanResult/{scan_id}", headers=headers(), timeout=300)
    r.raise_for_status()
    return r.json()


def scan_all(workspace_ids: list[str]) -> dict:
    """Run as many batches as needed and merge scanResult payloads."""
    merged: dict = {"workspaces": [], "datasourceInstances": [], "misconfiguredDatasourceInstances": []}
    total = len(workspace_ids)
    batches = [workspace_ids[i : i + GETINFO_BATCH] for i in range(0, total, GETINFO_BATCH)]
    print(f"Scanning {total} workspaces in {len(batches)} batch(es) of <= {GETINFO_BATCH}")

    for n, batch in enumerate(batches, 1):
        print(f"  [{n}/{len(batches)}] starting scan for {len(batch)} workspaces ...", flush=True)
        scan_id = start_scan(batch)
        wait_for_scan(scan_id)
        result = get_scan_result(scan_id)
        merged["workspaces"].extend(result.get("workspaces", []) or [])
        merged["datasourceInstances"].extend(result.get("datasourceInstances", []) or [])
        merged["misconfiguredDatasourceInstances"].extend(result.get("misconfiguredDatasourceInstances", []) or [])
        print(f"      got {len(result.get('workspaces', []) or [])} workspaces")
    return merged


# ---------- flatteners to CSV --------------------------------------------------

def _row(base: dict, **extra) -> dict:
    r = dict(base)
    r.update(extra)
    return r


def flatten(scan: dict, out_dir: Path) -> None:
    ws_rows, ds_rows, rep_rows, dash_rows, df_rows = [], [], [], [], []
    table_rows, col_rows, measure_rows = [], [], []
    expr_rows, datasource_rows, upstream_rows = [], [], []
    user_rows = []
    dsi_rows = []

    for dsi in scan.get("datasourceInstances", []) or []:
        dsi_rows.append({
            "datasourceId": dsi.get("datasourceId"),
            "datasourceType": dsi.get("datasourceType"),
            "gatewayId": dsi.get("gatewayId"),
            "connectionDetails": json.dumps(dsi.get("connectionDetails") or {}),
        })

    for ws in scan.get("workspaces", []) or []:
        wid = ws.get("id")
        wname = ws.get("name")
        wtype = ws.get("type")
        state = ws.get("state")
        capacity = ws.get("capacityId")
        ws_base = {
            "workspaceId": wid,
            "workspaceName": wname,
            "workspaceType": wtype,
            "workspaceState": state,
            "capacityId": capacity,
        }
        ws_rows.append(ws_base)

        for u in ws.get("users", []) or []:
            user_rows.append(_row(ws_base,
                                  itemType="Workspace",
                                  itemId=wid,
                                  itemName=wname,
                                  principalType=u.get("principalType"),
                                  identifier=u.get("identifier") or u.get("emailAddress"),
                                  displayName=u.get("displayName"),
                                  role=u.get("groupUserAccessRight") or u.get("datasetUserAccessRight") or u.get("reportUserAccessRight")))

        for d in ws.get("datasets", []) or []:
            did = d.get("id")
            dname = d.get("name")
            ds_row = _row(ws_base,
                          datasetId=did,
                          datasetName=dname,
                          configuredBy=d.get("configuredBy"),
                          createdDate=d.get("createdDate"),
                          sensitivityLabelId=(d.get("sensitivityLabel") or {}).get("labelId"),
                          endorsement=(d.get("endorsementDetails") or {}).get("endorsement"))
            ds_rows.append(ds_row)

            for u in d.get("users", []) or []:
                user_rows.append(_row(ws_base,
                                      itemType="Dataset",
                                      itemId=did,
                                      itemName=dname,
                                      principalType=u.get("principalType"),
                                      identifier=u.get("identifier") or u.get("emailAddress"),
                                      displayName=u.get("displayName"),
                                      role=u.get("datasetUserAccessRight")))

            for t in d.get("tables", []) or []:
                tname = t.get("name")
                table_rows.append(_row(ds_row, tableName=tname, hidden=t.get("isHidden"), description=t.get("description")))
                for c in t.get("columns", []) or []:
                    col_rows.append(_row(ds_row, tableName=tname, columnName=c.get("name"),
                                          dataType=c.get("dataType"), hidden=c.get("isHidden"),
                                          columnType=c.get("columnType")))
                for m in t.get("measures", []) or []:
                    measure_rows.append(_row(ds_row, tableName=tname, measureName=m.get("name"),
                                              expression=(m.get("expression") or "")[:4000],
                                              hidden=m.get("isHidden"), description=m.get("description")))
                for s in t.get("source", []) or []:
                    if s.get("expression"):
                        expr_rows.append(_row(ds_row, scope="Partition", name=f"{tname}",
                                              expression=s.get("expression")[:8000]))

            for e in d.get("expressions", []) or []:
                expr_rows.append(_row(ds_row, scope="Shared", name=e.get("name"),
                                       expression=(e.get("expression") or "")[:8000],
                                       description=e.get("description")))

            for up in d.get("upstreamDataflows", []) or []:
                upstream_rows.append(_row(ds_row, upstreamType="Dataflow",
                                           upstreamId=up.get("targetDataflowId"),
                                           upstreamWorkspaceId=up.get("groupId")))
            for up in d.get("upstreamDatasets", []) or []:
                upstream_rows.append(_row(ds_row, upstreamType="Dataset",
                                           upstreamId=up.get("targetDatasetId"),
                                           upstreamWorkspaceId=up.get("groupId")))

            for dsrc in d.get("datasources", []) or []:
                datasource_rows.append(_row(ds_row,
                                             datasourceId=dsrc.get("datasourceId"),
                                             datasourceType=dsrc.get("datasourceType"),
                                             gatewayId=dsrc.get("gatewayId"),
                                             connectionDetails=json.dumps(dsrc.get("connectionDetails") or {})))

        for r in ws.get("reports", []) or []:
            rid = r.get("id")
            rname = r.get("name")
            rep_rows.append(_row(ws_base,
                                  reportId=rid,
                                  reportName=rname,
                                  reportType=r.get("reportType"),
                                  datasetId=r.get("datasetId"),
                                  createdDate=r.get("createdDate"),
                                  modifiedDate=r.get("modifiedDate"),
                                  createdBy=r.get("createdBy"),
                                  modifiedBy=r.get("modifiedBy"),
                                  sensitivityLabelId=(r.get("sensitivityLabel") or {}).get("labelId"),
                                  endorsement=(r.get("endorsementDetails") or {}).get("endorsement")))
            for u in r.get("users", []) or []:
                user_rows.append(_row(ws_base,
                                      itemType="Report",
                                      itemId=rid,
                                      itemName=rname,
                                      principalType=u.get("principalType"),
                                      identifier=u.get("identifier") or u.get("emailAddress"),
                                      displayName=u.get("displayName"),
                                      role=u.get("reportUserAccessRight")))

        for d in ws.get("dashboards", []) or []:
            dash_rows.append(_row(ws_base,
                                   dashboardId=d.get("id"),
                                   displayName=d.get("displayName"),
                                   sensitivityLabelId=(d.get("sensitivityLabel") or {}).get("labelId")))

        for f in ws.get("dataflows", []) or []:
            df_rows.append(_row(ws_base,
                                 dataflowId=f.get("objectId"),
                                 dataflowName=f.get("name"),
                                 configuredBy=f.get("configuredBy"),
                                 sensitivityLabelId=(f.get("sensitivityLabel") or {}).get("labelId"),
                                 endorsement=(f.get("endorsementDetails") or {}).get("endorsement")))

    def dump(name: str, rows: list[dict]) -> None:
        path = out_dir / f"{name}.csv"
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        keys: list[str] = []
        seen = set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
        print(f"  wrote {path}  ({len(rows)} rows)")

    out_dir.mkdir(parents=True, exist_ok=True)
    dump("workspaces", ws_rows)
    dump("datasets", ds_rows)
    dump("reports", rep_rows)
    dump("dashboards", dash_rows)
    dump("dataflows", df_rows)
    dump("tables", table_rows)
    dump("columns", col_rows)
    dump("measures", measure_rows)
    dump("expressions", expr_rows)
    dump("datasources", datasource_rows)
    dump("datasource_instances", dsi_rows)
    dump("upstream", upstream_rows)
    dump("users", user_rows)


def main() -> int:
    p = argparse.ArgumentParser(description="Tenant-wide Fabric / Power BI inventory via the Admin Scanner API")
    p.add_argument("--modified-days", type=int, default=None,
                   help="Only scan workspaces modified in the last N days (max 30). Omit to scan ALL workspaces.")
    p.add_argument("--output", default="tenant_scan", help="Output directory (default: ./tenant_scan)")
    p.add_argument("--raw-json", action="store_true", help="Also write the merged raw scanResult to raw_scan.json")
    args = p.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    modified_since = None
    if args.modified_days is not None:
        if args.modified_days < 1 or args.modified_days > 30:
            print("--modified-days must be 1..30", file=sys.stderr)
            return 2
        modified_since = datetime.now(timezone.utc) - timedelta(days=args.modified_days)

    print("Listing workspaces ...")
    ws_ids = list_modified_workspaces(modified_since)
    print(f"Got {len(ws_ids)} workspace ids")
    if not ws_ids:
        return 0

    scan = scan_all(ws_ids)

    if args.raw_json:
        (out_dir / "raw_scan.json").write_text(json.dumps(scan, indent=2), encoding="utf-8")
        print(f"  wrote {out_dir / 'raw_scan.json'}")

    print("Flattening to CSV ...")
    flatten(scan, out_dir)
    print(f"\nDone. Inventory written to: {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
