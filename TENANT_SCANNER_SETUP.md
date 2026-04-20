# Tenant-Wide Fabric / Power BI Scanner — Setup One-Pager

## What this gives you

A full **security / exposure inventory** across every workspace in the tenant — **without** requiring workspace-level roles on each workspace:

- Workspaces, datasets, reports, dashboards, dataflows, lakehouses
- Dataset tables, columns, measures (DAX), M expressions
- Data source connections (server, database, kind, gateway)
- Upstream lineage (dataset → dataset, dataset → dataflow)
- Users & roles on every item (workspace, dataset, report)
- Sensitivity labels and endorsement status per item

## Prerequisites (one-time, done by Fabric tenant admin)

### 1. Create a security group
- Entra ID → Groups → New group (Security) → e.g. `grp-fabric-admin-readers`

### 2. Create an app registration (service principal)
- Entra ID → App registrations → New → give it a name
- No API permissions needed — admin access comes from the tenant setting below
- Create a **client secret**; note: `TenantId`, `ApplicationId`, `ClientSecret`
- Add the app's **service principal object** as a member of the security group from step 1

### 3. Enable three Fabric tenant settings
Fabric admin portal → **Tenant settings** → **Admin API settings**:

| Setting | Scope |
|---|---|
| Service principals can use read-only Fabric admin APIs | Specific security group → `grp-fabric-admin-readers` |
| Enhance admin APIs responses with detailed metadata | Specific security group → `grp-fabric-admin-readers` |
| Enhance admin APIs responses with DAX and mashup expressions | Specific security group → `grp-fabric-admin-readers` |

> ⏱ Tenant setting changes can take up to 15 min to propagate.

### 4. (Optional) Enable on a capacity
No capacity action required — admin APIs work regardless of capacity.

## What the app can and cannot see

| Can see | Cannot see |
|---|---|
| Every Fabric / Power BI workspace | Personal "My workspace" (scanner excludes them) |
| All items, their metadata, and users | Actual dataset row values |
| DAX measures and M mashup expressions | Data source credentials / secrets |
| Lineage across workspaces | Git-integrated branches (use Fabric Git APIs separately) |

## API used

`POST /v1.0/myorg/admin/workspaces/getInfo` with:
`lineage=true&datasourceDetails=true&datasetSchema=true&datasetExpressions=true&getArtifactUsers=true`

Scan is async: start → poll `scanStatus` → fetch `scanResult`. Documented limits: 100 workspaces per call, 500 `getInfo` calls / hour, 16 concurrent scans.

## Running the scanner

```powershell
# 1. Provide the SPN to DefaultAzureCredential
$env:AZURE_TENANT_ID     = "<tenant-id>"
$env:AZURE_CLIENT_ID     = "<app-id>"
$env:AZURE_CLIENT_SECRET = "<client-secret>"

# 2. Install and run
pip install -r requirements.txt
python fabric_tenant_scanner.py                 # scan every workspace
python fabric_tenant_scanner.py --modified-days 7   # incremental
python fabric_tenant_scanner.py --raw-json          # also keep the merged JSON
```

Output in `./tenant_scan/`:

```
workspaces.csv
datasets.csv       reports.csv        dashboards.csv    dataflows.csv
tables.csv         columns.csv        measures.csv
expressions.csv    datasources.csv    upstream.csv
users.csv          raw_scan.json (optional)
```

Each CSV is keyed on `workspaceId` so they can be joined for reporting.

## Security & data-handling notes

- The SPN is **read-only**; even with the setting enabled it cannot modify items.
- CSV output contains metadata only (no row data) but **does include M queries, DAX, connection strings, and user identities** — treat as confidential.
- Rotate the client secret regularly and store it in Key Vault (or use a federated credential / managed identity on a schedule runner).

## Answer to "can we do this without tenant access?"

**No.** Only the admin scanner endpoints enumerate workspaces you are not a member of. Any per-workspace API (`getDefinition`, `datasources`, etc.) requires Contributor or higher on each workspace. If tenant-admin buy-in is not possible, the only practical alternative is a Microsoft Purview data-map scan of Fabric / Power BI — which itself uses these same admin APIs under the hood and still requires a tenant admin to configure.
