# A+ Data Entry Activity Dashboard

An APlus-branded reporting application that combines BRISK, PRISM, and TRAIN
data-entry activity from REDCap. The interface is hand-written HTML/CSS/
JavaScript served by a Python Dash/Flask application on Posit Connect Cloud.

## Reports

The dashboard provides:

- Record, completed-form, participating-site, and data-enterer KPIs
- A project summary for BRISK, PRISM, and TRAIN
- Site-by-project data-entry progress
- Person-by-site-by-project completion activity
- Completed-form activity trend (daily, weekly, or monthly bars)
- Project, site, and reporting-period filters, including a custom date range
- Search, print, and CSV export controls

Each table includes a total. Data shown in the browser is aggregated; REDCap
tokens and participant record identifiers remain on the Python server.

## Reporting definitions

**Records Entered** counts distinct current REDCap records for each project and
site, including records with no completed forms. These counts respect project
and site filters and viewer access, but do not change with the completion-date
filter. Project and site totals use the same definition as the Records entered
KPI.

A **completed form** is one unique record/event/repeating-instrument instance
whose REDCap-generated `<instrument_name>_complete` value is currently `2`.
Values `0` and `1` are not counted.

The attributed **person** and **most recent form completion** come from the most
recent matching REDCap audit entry in which that form's completion field was set
to `2`. The current audit lookback is 365 days. Forms completed before that
window remain in current totals but may be marked `Unattributed`.

The two-digit prefix in `NN-NN-NNNNN` supplies the site code. The record's
REDCap Data Access Group supplies the readable site name and remains a fallback
when a record ID does not match that pattern.

The configured study codes are BRISK `01`, PRISM `02`, and TRAIN `03`. DAG names
are used as display labels; their project-specific trailing `NN-NN` suffix is
removed in the dashboard.

The **Completed forms over time** chart uses the same currently-complete form
instances and most-recent completion timestamps as the table totals. It uses
daily buckets for a 30-day view, weekly buckets for 90 days, and monthly
buckets for a 12-month or all-forms view. The chart clearly reports any current
completions without an audit-log timestamp, which cannot be placed in time.

The table-icon button on **Records entered** switches the graph to **Records
created over time**; the button on **Completed forms** restores the completion
trend. The creation chart counts each current record once, using its earliest
explicit `Create record` audit timestamp, including API creation events. It uses
the same daily, weekly, or monthly buckets and project/site/access filters as
the completion chart. Records lacking a creation event within the audit-log
lookback are reported as unavailable, never dated from a later edit or form
completion. Record KPI and table totals continue to count all current records.
Older development caches are automatically refreshed once to retain creation events.

Choose **Custom** in Reporting period to open the date-range pop-up. Select a
start and end date, then click **Apply date range**. Use **Edit** beside the
reporting period to change it later; **Cancel** or Escape keeps the current report. Both dates are included, using the completion timestamps from
REDCap. Custom ranges use daily chart buckets through 30 days, weekly buckets
through 90 days, and monthly buckets for longer ranges. Date-filtered reports
require an available audit timestamp; selecting older dates does not extend the
configured audit lookback. Records entered remains a count of current records.

## Project layout

```text
app.py                  Dash/Flask app and aggregate JSON endpoints
dashboard.html          Active report template
dashboard_config.toml   Projects, study codes, exclusions, cache, and access policy
dashboard_data.py       Completion attribution and report aggregation
redcap_client.py        REDCap API client and process-local cache
assets/APlusLogo.png    Dashboard logo
assets/styles.css       APlus visual theme
assets/app.js           Rendering, filtering, printing, and CSV exports
.env.example            Local secret-name template without real credentials
tests/                  Endpoint, cache, access, and aggregation checks
```

## REDCap configuration

The application expects separately named credentials for each project:

```dotenv
REDCAP_BRISK_API_URL=https://your-redcap.example/api/
REDCAP_BRISK_API_TOKEN=...
REDCAP_PRISM_API_URL=https://your-redcap.example/api/
REDCAP_PRISM_API_TOKEN=...
REDCAP_TRAIN_API_URL=https://your-redcap.example/api/
REDCAP_TRAIN_API_TOKEN=...
REDCAP_CACHE_TTL_SECONDS=600
REDCAP_REFERENCE_CACHE_TTL_SECONDS=86400
```

The same URL can be used for multiple projects, but every project has its own
token. Tokens need API Export permission plus access to metadata, records,
logging, users, and DAGs. Instrument and user-DAG-mapping exports are not used.
If an optional attribution export is unavailable, current completion totals
continue to work and the dashboard displays a warning.

No manual field mapping is required. The application discovers form names and
their generated completion fields from each project's metadata.

### Excluding forms

Administrative or test instruments can be removed from all counts in
`dashboard_config.toml` using REDCap instrument names rather than labels:

```toml
excluded_forms = ["internal_tracking", "test_form"]
```

## Cache behavior

Every project has independent, thread-safe in-memory caches. Records and audit
logs have a default TTL of 10 minutes, while slow-changing metadata, DAGs, and
users have a default TTL of 24 hours. Consequently, a cold project load makes
five REDCap requests, while a routine data refresh makes only the two activity
requests. Manual activity refreshes have a 60-second minimum interval. If
REDCap temporarily fails after a successful fetch, the last snapshot remains
available with a stale-data warning.

The browser asks for a matching cache-only report and a current report in
parallel. A cached project/site/period view is displayed immediately with a
refreshing indicator, then replaced when the current report arrives. If no
matching cached view exists, report values stay empty so activity from a
different project or site is never shown under the new filter selection.

The production cache is process-local and is cleared by an application restart.
The audit history request is bounded by `activity_lookback_days` in
`dashboard_config.toml`. If activity volume eventually makes that export too
large, move audit aggregates into a managed external database rather than the
temporary Connect Cloud application filesystem.

### Persistent local development cache

Running `python app.py` locally defaults to `APLUS_DEV_CACHE_MODE=prefer`.
The first request for a project that has no saved data makes one normal cold
REDCap fetch and writes `.dev-cache/<project>.json`. Later requests and app
restarts reuse that file indefinitely; the dashboard Refresh button does not
bypass it.

The files are reduced to dashboard-required fields before they are written.
Participant record IDs are replaced with synthetic IDs, and audit details other
than completed-form changes are discarded. Site and data-enterer names remain,
so the directory is still local-only data and must not be shared. It is ignored
by Git and is not part of the explicit Posit Publisher file list.

To intentionally replace every project's saved data, launch one local session
in refresh mode:

```powershell
$env:APLUS_DEV_CACHE_MODE = "refresh"
python app.py
```

After the refreshed page has loaded, stop that session and remove the override
with `Remove-Item Env:APLUS_DEV_CACHE_MODE`. Set the mode to `off` when a
local session should use the normal in-memory/live-API behavior. An optional
`APLUS_DEV_CACHE_DIR` can place the files somewhere other than `.dev-cache`.

## Coordinator and site access

The checked-in configuration is a coordinator view:

```toml
[access]
enforce_viewer_sites = false
```

Do not share that view with site personnel because its site dropdown is a report
filter, not an access control.

For site distribution, first configure private sharing in Connect Cloud. Then
add trusted viewer emails and site codes and enable enforcement:

```toml
[access]
enforce_viewer_sites = true
coordinator_emails = ["aplus-coordinator@example.org"]

[access.viewer_sites]
"site-user@example.org" = ["11"]
"multi-site-user@example.org" = ["11", "12"]
```

The Python endpoints read Connect Cloud's trusted
`RStudio-Connect-Credentials` request header. Coordinators receive all sites;
other viewers receive only their configured site codes. Missing or unassigned
viewer identities receive HTTP 403. Never enable site enforcement on public or
private-link sharing, because those modes do not provide an authenticated viewer
identity.

## Run and verify on Windows

```powershell
.\.venv\Scripts\Activate.ps1
python app.py
```

Open <http://127.0.0.1:8050>. Run the tests with:

```powershell
python -m unittest discover -s tests -v
```

## Publish

The Publisher configuration already includes the active HTML, configuration,
Python modules, and `assets` directory. Add all credential names from
`.env.example` as Connect Cloud secrets, keep `.env` and `.venv` out of the
deployment, and republish from the Posit Publisher extension.

## Safety boundaries

The application exposes only purpose-built aggregate endpoints. Do not add a
generic REDCap proxy, allow arbitrary field parameters from the browser, or
return record-level data. Give each API token only the rights required for these
reports.
