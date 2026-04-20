# Fabric Semantic Model Inspector

Export the full definition (TMDL / TMSL / M queries), data source connection info, tables, and columns for **every semantic model** in a Microsoft Fabric workspace.

Useful for:

- Auditing what data your Power BI / Fabric semantic models actually connect to.
- Capturing the M (Power Query) transformations for review or source control.
- Generating an inventory before a migration or re-platforming.

---

## How it works

The script calls two Microsoft REST APIs using your interactive Azure credential:

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/workspaces/{id}/semanticModels/{id}/getDefinition` | Returns the full TMDL/TMSL definition of the model, including M queries. |
| `GET  /v1.0/myorg/groups/{id}/datasets/{id}/datasources`     | Returns data source metadata (server, database, kind) — used as a fallback if `getDefinition` is blocked (e.g. sensitivity labels). |

Everything is written under `./semantic_model_exports/` (configurable) plus a consolidated `summary.json`.

---

## Requirements

- Python 3.10 +
- Azure CLI (`az`) for interactive login, **or** service-principal env vars (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`).
- Network access to `api.fabric.microsoft.com` and `api.powerbi.com`.

### Access / permissions needed

To get full output (including TMDL and M queries) the signed-in identity needs **all** of the following:

1. **Workspace role: Contributor, Member, or Admin** on the target workspace.
   - *Viewer is not sufficient* — the `getDefinition` API requires Contributor+.
2. **Build permission** on each semantic model (implicit for Contributor and above).
3. Tenant setting **"Export semantic models"** enabled for the user.
4. **No protected sensitivity label** on the item. If a Microsoft Purview label with encryption is applied, `getDefinition` returns `403 ItemHasProtectedLabel`. You then need either the "Remove label" usage right on the label, or the label must be removed by the information-protection admin.

If `getDefinition` fails, the script automatically falls back to the Power BI `datasources` endpoint, which only requires **Viewer + Build** and still returns the server / database / kind of each data source (but **not** the M query text).

### Azure AD / Microsoft Entra scopes used

- `https://api.fabric.microsoft.com/.default`
- `https://analysis.windows.net/powerbi/api/.default`

Both are delegated user scopes — no app registration or client secret required when using `az login`.

---

## Install

```bash
git clone https://github.com/<your-org>/fabric-semantic-model-inspector.git
cd fabric-semantic-model-inspector

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

---

## Usage

```bash
# 1. Sign in once
az login

# 2. Run against a workspace (pass name or GUID)
python get_semantic_model_details.py "my-workspace-name"

# Or pass a workspace ID
python get_semantic_model_details.py 00000000-0000-0000-0000-000000000000

# Change output directory
python get_semantic_model_details.py "my-workspace-name" ./out

# Or use environment variables
FABRIC_WORKSPACE="my-workspace-name" python get_semantic_model_details.py
```

### Outputs

```
semantic_model_exports/
├── <ModelA>/
│   ├── .platform
│   ├── definition.pbism
│   └── definition/
│       ├── database.tmdl
│       ├── model.tmdl
│       └── tables/
│           └── <Table>.tmdl
├── <ModelB>/
│   └── ...
└── summary.json        # consolidated metadata
```

`summary.json` contains, for each model:

- `id`, `name`
- `dataSources` / `datasources` (server, database, kind)
- `tables` — list of `{ name, columns[], measures[] }`
- `mExpressions` — each table partition / shared expression with its full M code
- `files` — list of exported parts
- `getDefinitionError` — present only if TMDL export was blocked (sensitivity label or missing role)

---

## Sample output

```
Workspace: snowflake-integration (d1fb2bc8-…-…-……)
Found 1 semantic models

--- SnowflakeMenuModel (efd18fc1-…-…-……) ---
  wrote semantic_model_exports\SnowflakeMenuModel\definition.pbism
  wrote semantic_model_exports\SnowflakeMenuModel\definition\tables\MENU.tmdl
  wrote semantic_model_exports\SnowflakeMenuModel\definition\model.tmdl
  wrote semantic_model_exports\SnowflakeMenuModel\definition\database.tmdl
  wrote semantic_model_exports\SnowflakeMenuModel\.platform

Summary written to semantic_model_exports\summary.json

========================================================================
SUMMARY: 1 semantic model(s) in workspace
========================================================================

## SnowflakeMenuModel  (efd18fc1-…-…-……)
  Data sources:
    - Extension: {'path': 'xxxxxxx-xxxxxxx.snowflakecomputing.com;MY_WAREHOUSE', 'kind': 'Snowflake'}
  M expressions (1):
    - MENU.MENU: let Source = Snowflake.Databases("xxxxxxx-xxxxxxx.snowflakecomputing.com", "MY_WAREHOUSE"), ...
  Tables (1):
    - MENU  (10 cols, 0 measures)
        MENU_ID: int64
        MENU_TYPE_ID: int64
        MENU_TYPE: string
        TRUCK_BRAND_NAME: string
        MENU_ITEM_ID: int64
        MENU_ITEM_NAME: string
        ITEM_CATEGORY: string
        ITEM_SUBCATEGORY: string
        COST_OF_GOODS_USD: double
        SALE_PRICE_USD: double
  Exported files: 5 (see semantic_model_exports\SnowflakeMenuModel)
```

Example M query extracted into `semantic_model_exports/SnowflakeMenuModel/definition/tables/MENU.tmdl`:

```m
let
    Source = Snowflake.Databases("xxxxxxx-xxxxxxx.snowflakecomputing.com", "MY_WAREHOUSE"),
    MY_DB_Database = Source{[Name="MY_DB", Kind="Database"]}[Data],
    MY_SCHEMA_Schema = MY_DB_Database{[Name="MY_SCHEMA", Kind="Schema"]}[Data],
    MENU_Table = MY_SCHEMA_Schema{[Name="MENU", Kind="Table"]}[Data]
in
    MENU_Table
```

---

## Troubleshooting

| Error | Cause | Fix |
| --- | --- | --- |
| `403 ItemHasProtectedLabel` | Model has an encrypted Purview sensitivity label. | Ask a Purview admin to remove the label or grant you the "Remove label" usage right. The fallback still returns data source metadata. |
| `403 Forbidden` (no label error code) | Role on workspace is Viewer. | Ask workspace admin for Contributor role. |
| `Workspace '...' not found` | Name typo or you don't have any role on the workspace. | Check the name in the Fabric portal, or pass the workspace GUID instead. |
| `DefaultAzureCredential failed` | Not signed in. | Run `az login`, or set `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` for a service principal. |

---

## Security & privacy

- No credentials are stored or printed. Authentication goes through `DefaultAzureCredential`.
- Exported files contain model metadata (table/column names and M queries). They do not contain data rows, but M queries may reference connection strings / server names — treat the output as sensitive and review before sharing.
- Do not commit `semantic_model_exports/` to a public repository.

---

## License

MIT
