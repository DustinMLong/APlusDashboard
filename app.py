import json
import os
from pathlib import Path

from dash import Dash, html
from flask import jsonify, request

from dashboard_data import ReportingPeriodError, build_dashboard_snapshot
from redcap_client import (
    RedcapConfigurationError,
    RedcapRequestError,
    access_settings,
    is_redcap_configured,
    project_statuses,
    test_redcap_connections,
)


BASE_DIR = Path(__file__).resolve().parent

# Dash is the Connect Cloud-supported application wrapper. The visible page is
# authored in dashboard.html plus assets/styles.css and assets/app.js.
app = Dash(__name__, title="A+ Data Entry Activity", update_title=None)
app.layout = html.Div(id="dash-runtime", hidden=True)
app.index_string = (BASE_DIR / "dashboard.html").read_text(encoding="utf-8")

# Dash runs on Flask, so narrowly scoped JSON endpoints can live beside it.
server = app.server


class ViewerAccessError(RuntimeError):
    """Raised when a signed-in viewer has no configured site scope."""


def viewer_site_scope() -> set[str] | None:
    """Return the trusted Connect Cloud viewer's allowed site IDs."""
    settings = access_settings()
    if not settings.get("enforce_viewer_sites", False):
        return None
    raw_credentials = request.headers.get("RStudio-Connect-Credentials", "")
    try:
        email = str(json.loads(raw_credentials).get("user", "")).strip().casefold()
    except (json.JSONDecodeError, AttributeError):
        email = ""
    if not email:
        raise ViewerAccessError(
            "Private Connect Cloud sign-in is required for this dashboard."
        )
    coordinators = {
        str(item).strip().casefold()
        for item in settings.get("coordinator_emails", [])
    }
    if email in coordinators:
        return None
    assignments = {
        str(key).strip().casefold(): {str(site).strip() for site in value}
        for key, value in settings.get("viewer_sites", {}).items()
        if isinstance(value, list)
    }
    allowed = assignments.get(email)
    if not allowed:
        raise ViewerAccessError("Your account has no assigned A+ site access.")
    return allowed


def json_no_store(payload: dict, status: int = 200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


@server.get("/api/health")
def health():
    return json_no_store(
        {
            "status": "ok",
            "redcapConfigured": is_redcap_configured(),
            "projects": project_statuses(),
        }
    )


@server.post("/api/redcap/test-connection")
def redcap_connection_test():
    try:
        results = test_redcap_connections()
    except RedcapConfigurationError as exc:
        return json_no_store({"ok": False, "message": str(exc)}, 503)
    except RedcapRequestError as exc:
        return json_no_store({"ok": False, "message": str(exc)}, 502)

    return json_no_store(
        {
            "ok": True,
            "message": f"REDCap accepted {len(results)} project credential set(s).",
            "projects": results,
        }
    )


def custom_period_arguments(values):
    if values.get("period") != "custom":
        return {}
    return {"start_date": values.get("startDate"), "end_date": values.get("endDate")}


@server.get("/api/dashboard")
def dashboard_data():
    selected_project = request.args.get("project", "all")
    selected_site = request.args.get("site", "all")
    period = request.args.get("period", "all")
    try:
        allowed_sites = viewer_site_scope()
        return json_no_store(
            build_dashboard_snapshot(
                selected_project,
                selected_site,
                period,
                **custom_period_arguments(request.args),
                **({"allowed_sites": allowed_sites} if allowed_sites is not None else {}),
            )
        )
    except ReportingPeriodError as exc:
        return json_no_store({"ok": False, "message": str(exc)}, 400)
    except ViewerAccessError as exc:
        return json_no_store({"ok": False, "message": str(exc)}, 403)
    except RedcapConfigurationError as exc:
        return json_no_store({"ok": False, "message": str(exc)}, 503)
    except RedcapRequestError as exc:
        return json_no_store({"ok": False, "message": str(exc)}, 502)


@server.get("/api/dashboard/cached")
def cached_dashboard_data():
    """Return a matching process-cache view without waiting for REDCap."""
    selected_project = request.args.get("project", "all")
    selected_site = request.args.get("site", "all")
    period = request.args.get("period", "all")
    try:
        allowed_sites = viewer_site_scope()
        snapshot = build_dashboard_snapshot(
            selected_project,
            selected_site,
            period,
            **custom_period_arguments(request.args),
            cache_only=True,
            **({"allowed_sites": allowed_sites} if allowed_sites is not None else {}),
        )
        if snapshot.get("state") == "empty":
            response = server.make_response(("", 204))
            response.headers["Cache-Control"] = "no-store"
            return response
        return json_no_store(snapshot)
    except ReportingPeriodError as exc:
        return json_no_store({"ok": False, "message": str(exc)}, 400)
    except ViewerAccessError as exc:
        return json_no_store({"ok": False, "message": str(exc)}, 403)
    except RedcapConfigurationError as exc:
        return json_no_store({"ok": False, "message": str(exc)}, 503)


@server.post("/api/dashboard/refresh")
def refresh_dashboard_data():
    body = request.get_json(silent=True) or {}
    selected_project = str(body.get("project", "all"))
    selected_site = str(body.get("site", "all"))
    period = str(body.get("period", "all"))
    try:
        allowed_sites = viewer_site_scope()
        return json_no_store(
            build_dashboard_snapshot(
                selected_project,
                selected_site,
                period,
                **custom_period_arguments(body),
                force=True,
                **({"allowed_sites": allowed_sites} if allowed_sites is not None else {}),
            )
        )
    except ReportingPeriodError as exc:
        return json_no_store({"ok": False, "message": str(exc)}, 400)
    except ViewerAccessError as exc:
        return json_no_store({"ok": False, "message": str(exc)}, 403)
    except RedcapConfigurationError as exc:
        return json_no_store({"ok": False, "message": str(exc)}, 503)
    except RedcapRequestError as exc:
        return json_no_store({"ok": False, "message": str(exc)}, 502)


if __name__ == "__main__":
    # A direct local run reuses the sanitized disk cache indefinitely. Connect
    # Cloud imports the server and never executes this development-only default.
    os.environ.setdefault("APLUS_DEV_CACHE_MODE", "prefer")

    # Local development only. Connect Cloud imports `server` above and does not
    # execute this block. Watch the external page template in addition to the
    # Python modules and files in assets/ that Dash already monitors.
    app.run(
        debug=True,
        dev_tools_hot_reload=True,
        extra_files=[str(BASE_DIR / "dashboard.html")],
    )
