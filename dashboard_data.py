"""Normalize REDCap projects into browser-safe data-entry activity reports."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, time
import re
from typing import Any, Iterable

from redcap_client import (
    CachedProjectData,
    RedcapConfigurationError,
    RedcapRequestError,
    activity_lookback_days,
    cache_policy,
    dashboard_settings,
    get_cached_project_data,
    get_project_data,
    load_projects,
    is_record_creation_log,
)


_RECORD_ID = re.compile(
    r"^(?P<site>\d{2})-(?P<study>\d{2})-(?P<participant>\d{5})$"
)
_COMPLETE_CHANGE = re.compile(
    r"(?:^|,\s*)(?P<field>[A-Za-z][A-Za-z0-9_]*_complete)\s*=\s*'(?P<value>[^']*)'"
)
_ALLOWED_PERIODS = {"all", "30", "90", "365", "custom"}


class ReportingPeriodError(ValueError):
    """Raised when a custom reporting range is missing or invalid."""


def _custom_date_range(start_date: str | None, end_date: str | None):
    dates = []
    for value in (start_date, end_date):
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ReportingPeriodError("Choose both a start date and an end date (YYYY-MM-DD).")
        try:
            dates.append(datetime.strptime(value, "%Y-%m-%d"))
        except ValueError as exc:
            raise ReportingPeriodError("Choose valid start and end dates.") from exc
    start, end = dates
    if start > end:
        raise ReportingPeriodError("The end date must be on or after the start date.")
    return start, datetime.combine(end.date(), time.max)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _parse_timestamp(value: Any) -> datetime | None:
    raw = _text(value)
    if not raw:
        return None
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(raw, pattern)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="minutes") if value else None


def _max_date(values: Iterable[datetime | None]) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _site_code(record_id: str, study_code: str) -> str:
    match = _RECORD_ID.fullmatch(record_id)
    if match and match.group("study") == study_code:
        return match.group("site")
    return ""


def _clean_site_name(value: str) -> str:
    """Remove the project-specific NN-NN suffix from a DAG display label."""
    return re.sub(r"\s+\d{2}-\d{2}\s*$", "", value).strip()


def _project_dimensions(data: CachedProjectData) -> dict[str, Any]:
    dag_by_unique = {
        _text(item.get("unique_group_name")): {
            "id": _text(item.get("data_access_group_id")),
            "name": _text(item.get("data_access_group_name")),
        }
        for item in data.dags
        if _text(item.get("unique_group_name"))
    }
    users: dict[str, dict[str, str]] = {}
    for item in data.users:
        username = _text(item.get("username"))
        if not username:
            continue
        full_name = " ".join(
            value
            for value in (
                _text(item.get("firstname")),
                _text(item.get("lastname")),
            )
            if value
        )
        users[username] = {
            "name": full_name or username,
            "siteId": _text(item.get("data_access_group_id")),
            "siteName": _text(item.get("data_access_group_label")),
        }
    return {
        "dagByUnique": dag_by_unique,
        "users": users,
    }


def _site_for_record(
    record: dict[str, Any], record_id: str, data: CachedProjectData, dimensions: dict[str, Any]
) -> tuple[str, str]:
    unique = _text(record.get("redcap_data_access_group"))
    dag = dimensions["dagByUnique"].get(unique, {})
    code = _site_code(record_id, data.project.study_code)
    dag_name = _clean_site_name(_text(dag.get("name")))
    dag_id = _text(dag.get("id"))
    site_id = code or dag_id or unique or "unassigned"
    site_name = dag_name or (f"Site {code}" if code else "Unassigned site")
    return site_id, site_name


def _completion_events(data: CachedProjectData) -> dict[tuple[str, str], list[dict[str, Any]]]:
    events: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in data.logs:
        record_id = _text(row.get("record"))
        if not record_id:
            continue
        timestamp = _parse_timestamp(row.get("timestamp"))
        for match in _COMPLETE_CHANGE.finditer(_text(row.get("details"))):
            if match.group("value") != "2":
                continue
            field_name = match.group("field")
            events[(record_id, field_name)].append(
                {
                    "username": _text(row.get("username")),
                    "completedAt": timestamp,
                }
            )
    for values in events.values():
        values.sort(key=lambda item: item["completedAt"] or datetime.min, reverse=True)
    return events


def _record_creation_dates(data: CachedProjectData) -> dict[str, datetime]:
    dates: dict[str, datetime] = {}
    for row in data.logs:
        if not is_record_creation_log(row):
            continue
        record_id = _text(row.get("record"))
        created_at = _parse_timestamp(row.get("timestamp"))
        if record_id and created_at and (record_id not in dates or created_at < dates[record_id]):
            dates[record_id] = created_at
    return dates


def _normalize_project(data: CachedProjectData) -> dict[str, Any]:
    dimensions = _project_dimensions(data)
    events = _completion_events(data)
    creation_dates = _record_creation_dates(data)
    instances_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    sites: dict[str, str] = {}
    record_sites: dict[str, str] = {}
    invalid_ids = 0

    for record in data.records:
        record_id = _text(record.get(data.record_id_field))
        if record_id and not _RECORD_ID.fullmatch(record_id):
            invalid_ids += 1
        site_id, site_name = _site_for_record(record, record_id, data, dimensions)
        sites[site_id] = site_name
        if record_id:
            record_sites[record_id] = site_id
        for field_name in data.completion_fields:
            if _text(record.get(field_name)) != "2":
                continue
            instances_by_key[(record_id, field_name)].append(
                {
                    "projectId": data.project.id,
                    "project": data.project.label,
                    "siteId": site_id,
                    "site": site_name,
                }
            )

    completions: list[dict[str, Any]] = []
    for key, instances in instances_by_key.items():
        matching_events = events.get(key, [])
        for index, instance in enumerate(instances):
            event = matching_events[index] if index < len(matching_events) else {}
            username = _text(event.get("username"))
            user = dimensions["users"].get(username, {})
            person = _text(user.get("name")) or username or "Unattributed"
            if instance["siteId"] == "unassigned" and user:
                user_site_id = _text(user.get("siteId"))
                user_site_name = _text(user.get("siteName"))
                if user_site_id:
                    instance["siteId"] = user_site_id
                    instance["site"] = (
                        _clean_site_name(user_site_name)
                        if user_site_name
                        else f"Site {user_site_id}"
                    )
                    sites[instance["siteId"]] = instance["site"]
            completions.append(
                {
                    **instance,
                    "person": person,
                    "personKey": username or "unattributed",
                    "completedAt": event.get("completedAt"),
                    "attributed": bool(username),
                }
            )

    return {
        "completions": completions,
        "sites": sites,
        "recordCreations": [
            {"projectId": data.project.id, "siteId": site_id,
             "createdAt": creation_dates.get(record_id)}
            for record_id, site_id in record_sites.items()
        ],
        "recordCount": len(record_sites),
        "recordCountsBySite": dict(Counter(record_sites.values())),
        "invalidIdCount": invalid_ids,
    }


def _row_key(*parts: str) -> str:
    return "|".join(parts)


def _month_start(value: datetime) -> datetime:
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month(value: datetime) -> datetime:
    return (
        value.replace(year=value.year + 1, month=1)
        if value.month == 12
        else value.replace(month=value.month + 1)
    )


def _activity_series(
    completions: list[dict[str, Any]],
    successful: list[tuple[CachedProjectData, dict[str, Any]]],
    period: str,
    custom_range: tuple[datetime, datetime] | None = None,
    *,
    metric: str = "completedForms",
    timestamp_key: str = "completedAt",
    bucket_override: str | None = None,
) -> dict[str, Any]:
    """Return browser-safe activity counts in filter-aware time buckets."""
    now = datetime.now()
    if custom_range:
        range_start, range_end = custom_range
        days = (range_end.date() - range_start.date()).days + 1
    else:
        days = int(period) if period != "all" else activity_lookback_days()
        range_start = now - timedelta(days=days)
        range_end = now

    if bucket_override == "hour":
        bucket = "hour"
        first = range_start.replace(minute=0, second=0, microsecond=0)
        next_bucket = lambda value: value + timedelta(hours=1)
        bucket_for = lambda value: value.replace(minute=0, second=0, microsecond=0)
    elif bucket_override == "day" or (bucket_override is None and (period == "30" or (custom_range and days <= 30))):
        bucket = "day"
        first = range_start.replace(hour=0, minute=0, second=0, microsecond=0)
        next_bucket = lambda value: value + timedelta(days=1)
        bucket_for = lambda value: value.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif bucket_override is None and (period == "90" or (custom_range and days <= 90)):
        bucket = "week"
        first_day = range_start - timedelta(days=range_start.weekday())
        first = first_day.replace(hour=0, minute=0, second=0, microsecond=0)
        next_bucket = lambda value: value + timedelta(days=7)
        bucket_for = lambda value: (
            value - timedelta(days=value.weekday())
        ).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        bucket = "month"
        first = _month_start(range_start)
        next_bucket = _next_month
        bucket_for = _month_start

    final = bucket_for(range_end)
    bucket_starts: list[datetime] = []
    cursor = first
    while cursor <= final:
        bucket_starts.append(cursor)
        if cursor == final:
            break
        cursor = next_bucket(cursor)
    bucket_values = {value: index for index, value in enumerate(bucket_starts)}

    project_rows = [
        {
            "id": data.project.id,
            "label": data.project.label,
            "values": [0] * len(bucket_starts),
        }
        for data, _ in successful
    ]
    project_values = {row["id"]: row["values"] for row in project_rows}
    charted_total = 0
    for item in completions:
        completed_at = item.get(timestamp_key)
        if not completed_at:
            continue
        bucket_index = bucket_values.get(bucket_for(completed_at))
        values = project_values.get(item["projectId"])
        if bucket_index is None or values is None:
            continue
        values[bucket_index] += 1
        charted_total += 1

    return {
        "metric": metric,
        "bucket": bucket,
        "buckets": [{"start": value.isoformat(timespec="minutes") if bucket == "hour" else value.date().isoformat()} for value in bucket_starts],
        "series": project_rows,
        "chartedTotal": charted_total,
        "unavailableTotal": len(completions) - charted_total,
    }


def build_dashboard_snapshot(
    selected_project: str = "all",
    selected_site: str = "all",
    period: str = "all",
    force: bool = False,
    allowed_sites: set[str] | None = None,
    cache_only: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    if period not in _ALLOWED_PERIODS:
        raise RedcapConfigurationError("Unknown reporting period.")
    custom_range = _custom_date_range(start_date, end_date) if period == "custom" else None
    all_projects = load_projects()
    projects = all_projects
    if selected_project != "all":
        projects = [project for project in all_projects if project.id == selected_project]
        if not projects:
            raise RedcapConfigurationError("Unknown REDCap project selection.")

    successful: list[tuple[CachedProjectData, dict[str, Any]]] = []
    warnings: list[str] = []
    sources: list[dict[str, Any]] = []
    cache_complete = True
    for project in projects:
        try:
            data = (
                get_cached_project_data(project)
                if cache_only
                else get_project_data(project, force=force)
            )
            if data is None:
                cache_complete = False
                sources.append({
                    "id": project.id,
                    "label": project.label,
                    "status": "not-cached",
                })
                continue
            normalized = _normalize_project(data)
        except (RedcapConfigurationError, RedcapRequestError) as exc:
            if cache_only:
                cache_complete = False
            warnings.append(str(exc))
            sources.append({
                "id": project.id, "label": project.label, "status": "error",
            })
            continue
        successful.append((data, normalized))
        warnings.extend(data.warnings)
        if normalized["invalidIdCount"]:
            warnings.append(
                f"{project.label} has {normalized['invalidIdCount']} record ID(s) outside the expected NN-NN-NNNNN pattern."
            )
        sources.append({
            "id": project.id,
            "label": project.label,
            "status": "stale" if data.stale else "ready",
            "fromCache": data.from_cache,
            "fetchedAt": data.fetched_at.isoformat(),
        })

    # A partially cached "All projects" report could look authoritative while
    # silently omitting a project. Let the browser keep the view empty until the
    # complete fresh response arrives instead.
    if cache_only and not cache_complete:
        return {"state": "empty", "sources": sources}

    available_sites: dict[str, str] = {}
    for _, normalized in successful:
        available_sites.update(
            {
                site_id: label
                for site_id, label in normalized["sites"].items()
                if allowed_sites is None or site_id in allowed_sites
            }
        )
    if selected_site != "all" and selected_site not in available_sites:
        raise RedcapConfigurationError("Unknown site selection.")

    cutoff = (
        custom_range[0] if custom_range
        else None if period == "all"
        else datetime.now() - timedelta(days=int(period))
    )
    completions: list[dict[str, Any]] = []
    for _, normalized in successful:
        for item in normalized["completions"]:
            if allowed_sites is not None and item["siteId"] not in allowed_sites:
                continue
            if selected_site != "all" and item["siteId"] != selected_site:
                continue
            if cutoff and (not item["completedAt"] or item["completedAt"] < cutoff):
                continue
            if custom_range and item["completedAt"] > custom_range[1]:
                continue
            completions.append(item)

    # Creation dates are independent of form completion and never inferred from edits.
    record_creations: list[dict[str, Any]] = []
    missing_creation_dates = 0
    creation_start = cutoff or datetime.now() - timedelta(days=activity_lookback_days())
    creation_end = custom_range[1] if custom_range else datetime.now()
    for _, normalized in successful:
        for item in normalized["recordCreations"]:
            if allowed_sites is not None and item["siteId"] not in allowed_sites:
                continue
            if selected_site != "all" and item["siteId"] != selected_site:
                continue
            created_at = item["createdAt"]
            if created_at is None:
                missing_creation_dates += 1
            elif creation_start <= created_at <= creation_end:
                record_creations.append(item)
    record_activity = _activity_series(
        record_creations, successful, period, custom_range,
        metric="recordsCreated", timestamp_key="createdAt",
    )
    record_activity["unavailableTotal"] = missing_creation_dates
    activity_views = {"forms": {}, "records": {}}
    for bucket in ("day", "month"):
        activity_views["forms"][bucket] = _activity_series(
            completions, successful, period, custom_range, bucket_override=bucket,
        )
        creation_view = _activity_series(
            record_creations, successful, period, custom_range,
            metric="recordsCreated", timestamp_key="createdAt", bucket_override=bucket,
        )
        creation_view["unavailableTotal"] = missing_creation_dates
        activity_views["records"][bucket] = creation_view


    project_groups: dict[str, dict[str, Any]] = {}
    records_entered = 0
    for data, normalized in successful:
        project_sites = {
            site_id
            for site_id in normalized["sites"]
            if (allowed_sites is None or site_id in allowed_sites)
            and (selected_site == "all" or site_id == selected_site)
        }
        project_record_count = sum(
            count
            for site_id, count in normalized["recordCountsBySite"].items()
            if (allowed_sites is None or site_id in allowed_sites)
            and (selected_site == "all" or site_id == selected_site)
        )
        records_entered += project_record_count
        project_groups[data.project.id] = {
            "id": data.project.id,
            "project": data.project.label,
            "records": project_record_count,
            "sites": project_sites,
            "formsCompleted": 0,
            "people": set(),
            "dates": [],
        }
    site_groups: dict[str, dict[str, Any]] = {}
    person_groups: dict[str, dict[str, Any]] = {}
    for data, normalized in successful:
        for site_id, site_name in normalized["sites"].items():
            if allowed_sites is not None and site_id not in allowed_sites:
                continue
            if selected_site != "all" and site_id != selected_site:
                continue
            site_groups[_row_key(data.project.id, site_id)] = {
                "siteId": site_id,
                "site": site_name,
                "project": data.project.label,
                "records": normalized["recordCountsBySite"].get(site_id, 0),
                "formsCompleted": 0,
                "people": set(),
                "dates": [],
            }

    for item in completions:
        project = project_groups[item["projectId"]]
        project["formsCompleted"] += 1
        project["sites"].add(item["siteId"])
        if item["attributed"]:
            project["people"].add(item["personKey"])
        project["dates"].append(item["completedAt"])

        site_key = _row_key(item["projectId"], item["siteId"])
        site = site_groups.setdefault(site_key, {
            "siteId": item["siteId"], "site": item["site"],
            "project": item["project"], "formsCompleted": 0,
            "records": 0,
            "people": set(), "dates": [],
        })
        site["formsCompleted"] += 1
        if item["attributed"]:
            site["people"].add(item["personKey"])
        site["dates"].append(item["completedAt"])

        person_key = _row_key(item["projectId"], item["siteId"], item["personKey"])
        person = person_groups.setdefault(person_key, {
            "person": item["person"], "siteId": item["siteId"],
            "site": item["site"], "project": item["project"],
            "formsCompleted": 0, "dates": [],
        })
        person["formsCompleted"] += 1
        person["dates"].append(item["completedAt"])

    project_rows = [
        {
            "id": value["id"], "project": value["project"],
            "records": value["records"], "sites": len(value["sites"]),
            "formsCompleted": value["formsCompleted"],
            "people": len(value["people"]),
            "mostRecentCompletion": _iso(_max_date(value["dates"])),
        }
        for value in project_groups.values()
    ]
    site_rows = [
        {
            **{key: value[key] for key in ("siteId", "site", "project", "records", "formsCompleted")},
            "people": len(value["people"]),
            "mostRecentCompletion": _iso(_max_date(value["dates"])),
        }
        for value in site_groups.values()
    ]
    person_rows = [
        {
            **{key: value[key] for key in ("person", "siteId", "site", "project", "formsCompleted")},
            "mostRecentCompletion": _iso(_max_date(value["dates"])),
        }
        for value in person_groups.values()
    ]
    project_rows.sort(key=lambda row: row["project"])
    site_rows.sort(key=lambda row: (row["site"], row["project"]))
    person_rows.sort(key=lambda row: (-row["formsCompleted"], row["person"], row["project"]))

    attributed = sum(1 for item in completions if item["attributed"])
    if completions and attributed < len(completions):
        warnings.append(
            f"{len(completions) - attributed} completed form(s) could not be attributed within the configured audit-log window."
        )
    fetched_times = [data.fetched_at for data, _ in successful]
    settings = dashboard_settings()
    ttl_seconds, _ = cache_policy()
    return {
        "state": "ready" if successful else "error",
        "title": settings.get("title", "A+ Data Entry Activity"),
        "subtitle": settings.get("subtitle", "Form completion reporting across A+ projects"),
        "selectedProject": selected_project,
        "selectedSite": selected_site,
        "period": period,
        "startDate": start_date if custom_range else None,
        "endDate": end_date if custom_range else None,
        "periodOptions": [
            {"value": "all", "label": "All completed forms"},
            {"value": "30", "label": "Completed in past 30 days"},
            {"value": "90", "label": "Completed in past 90 days"},
            {"value": "365", "label": "Completed in past 12 months"},
            {"value": "custom", "label": "Custom"},
        ],
        "projects": [
            {"id": project.id, "label": project.label, "studyCode": project.study_code}
            for project in all_projects
        ],
        "availableSites": [
            {"id": site_id, "label": label}
            for site_id, label in sorted(available_sites.items(), key=lambda item: item[1])
        ],
        "metrics": {
            "recordsEntered": records_entered,
            "completedForms": len(completions),
            "participatingSites": (
                1 if selected_site != "all" and selected_site in available_sites
                else len(available_sites)
            ),
            "dataEnterers": len({item["person"] for item in completions if item["attributed"]}),
        },
        "projectRows": project_rows,
        "siteRows": site_rows,
        "personRows": person_rows,
        "activitySeries": _activity_series(
            completions, successful, period, custom_range
        ),
        "recordActivitySeries": record_activity,
        "activityViews": activity_views,
        "updatedAt": max(fetched_times).isoformat() if fetched_times else None,
        "cacheTtlSeconds": ttl_seconds,
        "stale": any(source.get("status") == "stale" for source in sources),
        "sources": sources,
        "warnings": list(dict.fromkeys(warnings)),
    }
