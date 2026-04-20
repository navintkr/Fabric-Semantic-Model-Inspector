"""
Export full details (TMDL/M queries, data sources) for all semantic models
in a Microsoft Fabric workspace.

Prerequisites:
    pip install -r requirements.txt
    az login            # or set AZURE_* env vars for a service principal

Usage:
    python get_semantic_model_details.py <workspace-name-or-id> [output-dir]

    # or via environment variables
    FABRIC_WORKSPACE=<name-or-id> python get_semantic_model_details.py
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests
from azure.identity import DefaultAzureCredential

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
PBI_BASE = "https://api.powerbi.com/v1.0/myorg"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
_credential = None


def _get_credential():
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    return _credential


def get_token(scope: str = FABRIC_SCOPE) -> str:
    """Uses DefaultAzureCredential (az login, env vars, managed identity, etc.)."""
    return _get_credential().get_token(scope).token


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Fabric REST helpers
# ---------------------------------------------------------------------------
def get_workspace_id(token: str, name_or_id: str) -> str:
    # If caller already passed a GUID, trust it
    if len(name_or_id) == 36 and name_or_id.count("-") == 4:
        return name_or_id
    r = requests.get(f"{FABRIC_BASE}/workspaces", headers=auth_headers(token))
    r.raise_for_status()
    for ws in r.json().get("value", []):
        if ws["displayName"] == name_or_id:
            return ws["id"]
    raise RuntimeError(f"Workspace '{name_or_id}' not found")


def list_semantic_models(token: str, workspace_id: str) -> list:
    r = requests.get(
        f"{FABRIC_BASE}/workspaces/{workspace_id}/semanticModels",
        headers=auth_headers(token),
    )
    r.raise_for_status()
    return r.json().get("value", [])


def get_semantic_model_definition(token: str, workspace_id: str, model_id: str) -> dict:
    """
    Calls getDefinition (LRO). Returns the decoded TMDL/TMSL parts including
    model.bim or definition.pbism / model.tmdl files which contain M queries
    and data source info.
    """
    url = (
        f"{FABRIC_BASE}/workspaces/{workspace_id}"
        f"/semanticModels/{model_id}/getDefinition"
    )
    r = requests.post(url, headers=auth_headers(token))

    # Long-running operation: poll until complete
    if r.status_code == 202:
        op_url = r.headers.get("Location") or r.headers.get("Operation-Location")
        retry_after = int(r.headers.get("Retry-After", "2"))
        while True:
            time.sleep(retry_after)
            poll = requests.get(op_url, headers=auth_headers(token))
            poll.raise_for_status()
            status = (poll.json().get("status") or "").lower()
            if status == "succeeded":
                res = requests.get(op_url.rstrip("/") + "/result", headers=auth_headers(token))
                res.raise_for_status()
                return res.json()
            if status == "failed":
                raise RuntimeError(f"LRO failed: {poll.text}")
            retry_after = int(poll.headers.get("Retry-After", "2"))
    r.raise_for_status()
    return r.json()


def decode_parts(definition: dict) -> dict:
    """Decode base64 payloads from the definition response."""
    decoded = {}
    for part in definition.get("definition", {}).get("parts", []):
        path = part["path"]
        payload = part.get("payload", "")
        if part.get("payloadType", "").lower() == "inlinebase64" and payload:
            try:
                decoded[path] = base64.b64decode(payload).decode("utf-8")
            except Exception:
                decoded[path] = f"<binary {len(payload)} bytes>"
        else:
            decoded[path] = payload
    return decoded


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "workspace",
        nargs="?",
        default=os.environ.get("FABRIC_WORKSPACE"),
        help="Workspace display name or ID (or set FABRIC_WORKSPACE env var)",
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=os.environ.get("OUTPUT_DIR", "./semantic_model_exports"),
        help="Output directory (default: ./semantic_model_exports)",
    )
    args = parser.parse_args()

    if not args.workspace:
        parser.error("workspace is required (argument or FABRIC_WORKSPACE env var)")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True)
    token = get_token(FABRIC_SCOPE)
    pbi_token = get_token(PBI_SCOPE)

    ws_id = get_workspace_id(token, args.workspace)
    print(f"Workspace: {args.workspace} ({ws_id})")

    models = list_semantic_models(token, ws_id)
    print(f"Found {len(models)} semantic models\n")

    summary = []
    for m in models:
        name, model_id = m["displayName"], m["id"]
        print(f"--- {name} ({model_id}) ---")
        entry = {"name": name, "id": model_id}

        # 1) Try Fabric getDefinition (needs Contributor)
        try:
            definition = get_semantic_model_definition(token, ws_id, model_id)
            parts = decode_parts(definition)
            model_dir = output_dir / name.replace("/", "_")
            model_dir.mkdir(exist_ok=True)
            for path, content in parts.items():
                out_file = model_dir / path.replace("/", os.sep)
                out_file.parent.mkdir(parents=True, exist_ok=True)
                out_file.write_text(content, encoding="utf-8")
                print(f"  wrote {out_file}")
            entry["files"] = list(parts.keys())

            bim_path = next((p for p in parts if p.endswith("model.bim")), None)
            if bim_path:
                bim = json.loads(parts[bim_path])
                model = bim.get("model", {})
                entry["dataSources"] = model.get("dataSources", [])
                exprs = [
                    {"name": e.get("name"), "expression": e.get("expression")}
                    for e in model.get("expressions", [])
                ]
                tables = []
                for tbl in model.get("tables", []):
                    cols = [
                        {
                            "name": c.get("name"),
                            "dataType": c.get("dataType"),
                            "sourceColumn": c.get("sourceColumn"),
                            "formatString": c.get("formatString"),
                            "summarizeBy": c.get("summarizeBy"),
                        }
                        for c in tbl.get("columns", [])
                    ]
                    meas = [{"name": m.get("name"), "expression": m.get("expression")} for m in tbl.get("measures", [])]
                    tables.append({"name": tbl.get("name"), "columns": cols, "measures": meas})
                    for p in tbl.get("partitions", []):
                        src = p.get("source", {})
                        if src.get("type") == "m":
                            exprs.append(
                                {
                                    "name": f"{tbl['name']}.{p.get('name','')}",
                                    "expression": src.get("expression"),
                                }
                            )
                entry["mExpressions"] = exprs
                entry["tables"] = tables
            else:
                # TMDL format: scan .tmdl files for partition M expressions and columns
                exprs = []
                tables = []
                for path, content in parts.items():
                    if not (path.endswith(".tmdl") and "/tables/" in path):
                        continue
                    tbl_name = Path(path).stem
                    columns = []
                    measures = []
                    lines = content.splitlines()
                    i = 0
                    while i < len(lines):
                        stripped = lines[i].strip()

                        # Column block
                        if stripped.startswith("column "):
                            col_name = stripped[len("column "):].strip("'\"")
                            col = {"name": col_name}
                            base_indent = len(lines[i]) - len(lines[i].lstrip())
                            j = i + 1
                            while j < len(lines):
                                ln = lines[j]
                                if not ln.strip():
                                    j += 1
                                    continue
                                indent = len(ln) - len(ln.lstrip())
                                if indent <= base_indent:
                                    break
                                s = ln.strip()
                                if ":" in s and not s.startswith("annotation"):
                                    k, _, v = s.partition(":")
                                    col[k.strip()] = v.strip()
                                j += 1
                            columns.append(col)
                            i = j
                            continue

                        # Measure block
                        if stripped.startswith("measure "):
                            m_name = stripped[len("measure "):].split("=")[0].strip().strip("'\"")
                            measures.append({"name": m_name})

                        # Partition / M expression block
                        if stripped.startswith("partition ") and stripped.endswith("= m"):
                            part_name = stripped.split()[1]
                            j = i + 1
                            while j < len(lines) and "source =" not in lines[j]:
                                j += 1
                            if j < len(lines):
                                base_indent = len(lines[j]) - len(lines[j].lstrip())
                                code_lines = []
                                k = j + 1
                                while k < len(lines):
                                    ln = lines[k]
                                    if ln.strip() == "" or (len(ln) - len(ln.lstrip()) > base_indent):
                                        code_lines.append(ln)
                                        k += 1
                                    else:
                                        break
                                nonempty = [ln for ln in code_lines if ln.strip()]
                                if nonempty:
                                    common = min(len(ln) - len(ln.lstrip()) for ln in nonempty)
                                    code = "\n".join(ln[common:] if len(ln) >= common else ln for ln in code_lines).strip()
                                    exprs.append({
                                        "name": f"{tbl_name}.{part_name}",
                                        "expression": code,
                                    })
                        i += 1
                    tables.append({"name": tbl_name, "columns": columns, "measures": measures})
                entry["mExpressions"] = exprs
                entry["tables"] = tables

            # Always also fetch data source metadata (server/kind) via Power BI API
            try:
                pbi_headers = {"Authorization": f"Bearer {pbi_token}"}
                sources = requests.get(
                    f"{PBI_BASE}/groups/{ws_id}/datasets/{model_id}/datasources",
                    headers=pbi_headers,
                )
                if sources.ok:
                    entry.setdefault("datasources", sources.json().get("value", []))
            except Exception:
                pass
        except Exception as e:
            print(f"  getDefinition failed ({e}) -- falling back to Power BI API")
            entry["getDefinitionError"] = str(e)

            # 2) Power BI REST fallback: datasources + dataset metadata
            pbi_headers = {"Authorization": f"Bearer {pbi_token}"}
            try:
                ds = requests.get(
                    f"{PBI_BASE}/groups/{ws_id}/datasets/{model_id}",
                    headers=pbi_headers,
                )
                if ds.ok:
                    entry["dataset"] = ds.json()
                sources = requests.get(
                    f"{PBI_BASE}/groups/{ws_id}/datasets/{model_id}/datasources",
                    headers=pbi_headers,
                )
                if sources.ok:
                    entry["datasources"] = sources.json().get("value", [])
                    print(f"  datasources: {len(entry['datasources'])}")
                    for s in entry["datasources"]:
                        print(f"    - {s.get('datasourceType')}: {s.get('connectionDetails')}")
                else:
                    print(f"  datasources call failed: {sources.status_code} {sources.text}")
            except Exception as e2:
                print(f"  Power BI fallback failed: {e2}")

        summary.append(entry)

    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    print(f"\nSummary written to {output_dir/'summary.json'}")

    # -----------------------------------------------------------------------
    # Pretty-print a human-readable summary
    # -----------------------------------------------------------------------
    print("\n" + "=" * 72)
    print(f"SUMMARY: {len(summary)} semantic model(s) in workspace")
    print("=" * 72)
    for entry in summary:
        print(f"\n## {entry['name']}  ({entry['id']})")

        if entry.get("getDefinitionError"):
            print(f"  [getDefinition blocked: {entry['getDefinitionError'][:120]}]")

        # Data sources (from model.bim if available, else Power BI fallback)
        ds_list = entry.get("dataSources") or entry.get("datasources") or []
        if ds_list:
            print("  Data sources:")
            for ds in ds_list:
                if "connectionDetails" in ds:
                    print(f"    - {ds.get('datasourceType')}: {ds.get('connectionDetails')}")
                else:
                    details = {k: v for k, v in ds.items() if k in ("name", "type", "connectionString", "server", "database", "account", "warehouse")}
                    print(f"    - {details}")

        # M expressions
        exprs = entry.get("mExpressions") or []
        if exprs:
            print(f"  M expressions ({len(exprs)}):")
            for ex in exprs:
                snippet = (ex.get("expression") or "").strip().replace("\n", " ")[:140]
                print(f"    - {ex.get('name')}: {snippet}...")

        # Tables and columns
        tables = entry.get("tables") or []
        if tables:
            print(f"  Tables ({len(tables)}):")
            for t in tables:
                cols = t.get("columns") or []
                meas = t.get("measures") or []
                print(f"    - {t['name']}  ({len(cols)} cols, {len(meas)} measures)")
                for c in cols:
                    dtype = c.get("dataType") or ""
                    src = c.get("sourceColumn") or ""
                    src_str = f" [src={src}]" if src and src != c.get("name") else ""
                    print(f"        {c['name']}: {dtype}{src_str}")

        files = entry.get("files") or []
        if files:
            print(f"  Exported files: {len(files)} (see {output_dir/entry['name']})")


if __name__ == "__main__":
    main()
