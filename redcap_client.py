"""Multi-project REDCap client with a guarded process-local activity cache."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from dotenv import load_dotenv


load_dotenv()

REDCAP_BRISK_API_URL= https://redcap.school.wakehealth.edu/api/
REDCAP_BRISK_API_TOKEN = 3AD4C73132F99216883104A16EE65120


REDCAP_PRISM_API_URL = https://redcap.school.wakehealth.edu/api/
REDCAP_PRISM_API_TOKE = 169D74BEDD1A04F57B219058B68ACD93


REDCAP_TRAIN_API_URL = https://redcap.school.wakehealth.edu/api/
REDCAP_TRAIN_API_TOKEN = FDDD655DD3B908C2C5DF555F6C6CBCF4

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "dashboard_config.toml"
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
_DEV_RECORD_ID = re.compile(r"^(?P<site>\d{2})-\d{2}-\d{5}$")
_DEV_COMPLETE_CHANGE = re.compile(
    r"(?:^|,\s*)(?P<field>[A-Za-z][A-Za-z0-9_]*_complete)\s*=\s*'(?P<value>[^']*)'"
)
_DEV_CACHE_SCHEMA_VERSION = 2
_DEV_CACHE_MODES = {"off", "prefer", "refresh"}


class RedcapConfigurationError(RuntimeError):
    """Raised when required REDCap settings are absent or unsafe."""


class RedcapRequestError(RuntimeError):
    """Raised when REDCap cannot complete a request."""


@dataclass(frozen=True)
class RedcapProject:
    id: str
    label: str
    study_code: str
    api_url_env: str
    api_token_env: str
    excluded_forms: tuple[str, ...] = field(default_factory=tuple)

    def credentials(self) -> tuple[str, str]:
        api_url = os.getenv(self.api_url_env, "").strip()
        api_token = os.getenv(self.api_token_env, "").strip()
        if not api_url or not api_token:
            raise RedcapConfigurationError(
                f"{self.label} requires {self.api_url_env} and {self.api_token_env}."
            )
        parsed = urlparse(api_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RedcapConfigurationError(
                f"{self.api_url_env} must be a complete HTTPS URL."
            )
        return api_url, api_token


@dataclass(frozen=True)
class CachedProjectData:
    project: RedcapProject
    record_id_field: str
    completion_fields: tuple[str, ...]
    records: list[dict[str, Any]]
    logs: list[dict[str, Any]]
    users: list[dict[str, Any]]
    dags: list[dict[str, Any]]
    fetched_at: datetime
    expires_at: datetime
    from_cache: bool
    stale: bool = False
    creation_events_available: bool = True
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class _ReferenceCacheEntry:
    record_id_field: str
    completion_fields: tuple[str, ...]
    users: list[dict[str, Any]]
    dags: list[dict[str, Any]]
    warnings: tuple[str, ...]
    fetched_monotonic: float
    expires_monotonic: float


@dataclass
class _ActivityCacheEntry:
    # Keep the schema used for this records export with the activity data so a
    # stale fallback cannot be interpreted using a newer metadata version.
    record_id_field: str
    completion_fields: tuple[str, ...]
    records: list[dict[str, Any]]
    logs: list[dict[str, Any]]
    warnings: tuple[str, ...]
    fetched_at: datetime
    fetched_monotonic: float
    expires_monotonic: float


_REFERENCE_CACHE: dict[str, _ReferenceCacheEntry] = {}
_ACTIVITY_CACHE: dict[str, _ActivityCacheEntry] = {}
_CACHE_GUARD = threading.Lock()
_PROJECT_LOCKS: dict[str, threading.Lock] = {}
_DEV_REFRESHED_PROJECTS: set[str] = set()


def _read_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise RedcapConfigurationError("dashboard_config.toml is missing.")
    with CONFIG_PATH.open("rb") as config_file:
        return tomllib.load(config_file)


def dashboard_settings() -> dict[str, Any]:
    return _read_config().get("dashboard", {})


def access_settings() -> dict[str, Any]:
    return _read_config().get("access", {})


def load_projects() -> list[RedcapProject]:
    raw_projects = _read_config().get("projects", [])
    if not raw_projects:
        raise RedcapConfigurationError(
            "dashboard_config.toml must contain at least one [[projects]] block."
        )

    projects: list[RedcapProject] = []
    seen_ids: set[str] = set()
    seen_codes: set[str] = set()
    for raw in raw_projects:
        project_id = str(raw.get("id", "")).strip()
        study_code = str(raw.get("study_code", "")).strip()
        if not _PROJECT_ID.fullmatch(project_id):
            raise RedcapConfigurationError(
                "Project ids must start with a lowercase letter and contain only "
                "lowercase letters, numbers, underscores, or hyphens."
            )
        if project_id in seen_ids:
            raise RedcapConfigurationError(f"Duplicate project id: {project_id}")
        if not re.fullmatch(r"\d{2}", study_code):
            raise RedcapConfigurationError(
                f"{project_id} study_code must contain exactly two digits."
            )
        if study_code in seen_codes:
            raise RedcapConfigurationError(f"Duplicate study code: {study_code}")
        seen_ids.add(project_id)
        seen_codes.add(study_code)
        projects.append(
            RedcapProject(
                id=project_id,
                label=str(raw.get("label", project_id)).strip(),
                study_code=study_code,
                api_url_env=str(raw.get("api_url_env", "")).strip(),
                api_token_env=str(raw.get("api_token_env", "")).strip(),
                excluded_forms=tuple(
                    str(item).strip()
                    for item in raw.get("excluded_forms", [])
                    if str(item).strip()
                ),
            )
        )
    return projects


def project_statuses() -> list[dict[str, Any]]:
    return [
        {
            "id": project.id,
            "label": project.label,
            "studyCode": project.study_code,
            "configured": bool(
                os.getenv(project.api_url_env, "").strip()
                and os.getenv(project.api_token_env, "").strip()
            ),
        }
        for project in load_projects()
    ]


def is_redcap_configured() -> bool:
    statuses = project_statuses()
    return bool(statuses) and all(item["configured"] for item in statuses)


def _timeout_seconds() -> float:
    try:
        timeout = float(os.getenv("REDCAP_TIMEOUT_SECONDS", "20"))
    except ValueError as exc:
        raise RedcapConfigurationError(
            "REDCAP_TIMEOUT_SECONDS must be a number."
        ) from exc
    if not 1 <= timeout <= 120:
        raise RedcapConfigurationError(
            "REDCAP_TIMEOUT_SECONDS must be between 1 and 120."
        )
    return timeout


def cache_policy() -> tuple[int, int]:
    settings = dashboard_settings()
    try:
        ttl = int(
            os.getenv(
                "REDCAP_CACHE_TTL_SECONDS",
                settings.get("cache_ttl_seconds", 600),
            )
        )
        minimum = int(settings.get("minimum_refresh_seconds", 60))
    except (TypeError, ValueError) as exc:
        raise RedcapConfigurationError("Cache settings must be integers.") from exc
    if not 30 <= ttl <= 86400:
        raise RedcapConfigurationError(
            "Cache TTL must be between 30 seconds and 24 hours."
        )
    return ttl, max(10, min(minimum, ttl))


def reference_cache_ttl_seconds() -> int:
    """Return the TTL for slow-changing REDCap schema and directory data."""
    settings = dashboard_settings()
    try:
        ttl = int(
            os.getenv(
                "REDCAP_REFERENCE_CACHE_TTL_SECONDS",
                settings.get("reference_cache_ttl_seconds", 86400),
            )
        )
    except (TypeError, ValueError) as exc:
        raise RedcapConfigurationError(
            "Reference cache TTL must be an integer."
        ) from exc
    if not 300 <= ttl <= 604800:
        raise RedcapConfigurationError(
            "Reference cache TTL must be between 5 minutes and 7 days."
        )
    return ttl


def activity_lookback_days() -> int:
    try:
        days = int(dashboard_settings().get("activity_lookback_days", 365))
    except (TypeError, ValueError) as exc:
        raise RedcapConfigurationError(
            "activity_lookback_days must be an integer."
        ) from exc
    if not 30 <= days <= 3650:
        raise RedcapConfigurationError(
            "activity_lookback_days must be between 30 and 3650."
        )
    return days


def development_cache_mode() -> str:
    """Return the explicitly selected local persistent-cache behavior."""
    mode = os.getenv("APLUS_DEV_CACHE_MODE", "off").strip().casefold() or "off"
    if mode not in _DEV_CACHE_MODES:
        choices = ", ".join(sorted(_DEV_CACHE_MODES))
        raise RedcapConfigurationError(
            f"APLUS_DEV_CACHE_MODE must be one of: {choices}."
        )
    return mode


def _development_cache_path(project: RedcapProject) -> Path:
    configured = os.getenv("APLUS_DEV_CACHE_DIR", "").strip()
    directory = Path(configured).expanduser() if configured else BASE_DIR / ".dev-cache"
    if not directory.is_absolute():
        directory = BASE_DIR / directory
    return directory / f"{project.id}.json"


def _dictionary_list(value: Any, description: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise RedcapConfigurationError(
            f"The local development cache contains invalid {description}."
        )
    return value


def _read_development_cache(
    project: RedcapProject, *, from_cache: bool = True
) -> CachedProjectData | None:
    path = _development_cache_path(project)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RedcapConfigurationError(
            f"The local {project.label} development cache could not be read."
        ) from exc

    if not isinstance(payload, dict):
        raise RedcapConfigurationError(
            f"The local {project.label} development cache is invalid."
        )
    identity = payload.get("project")
    if (
        not isinstance(identity, dict)
        or payload.get("schema_version") not in (1, _DEV_CACHE_SCHEMA_VERSION)
        or payload.get("sanitized") is not True
        or identity.get("id") != project.id
        or identity.get("study_code") != project.study_code
    ):
        raise RedcapConfigurationError(
            f"The local {project.label} development cache is incompatible; "
            "regenerate it with APLUS_DEV_CACHE_MODE=refresh."
        )
    reference = payload.get("reference")
    activity = payload.get("activity")
    if not isinstance(reference, dict) or not isinstance(activity, dict):
        raise RedcapConfigurationError(
            f"The local {project.label} development cache is incomplete."
        )
    record_id_field = str(activity.get("record_id_field", "")).strip()
    completion_fields = activity.get("completion_fields")
    if not record_id_field or not isinstance(completion_fields, list):
        raise RedcapConfigurationError(
            f"The local {project.label} development cache has an invalid schema."
        )
    try:
        fetched_at = datetime.fromisoformat(str(payload["saved_at"]))
    except (KeyError, ValueError) as exc:
        raise RedcapConfigurationError(
            f"The local {project.label} development cache has an invalid timestamp."
        ) from exc
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)

    ttl_seconds, _ = cache_policy()
    saved_label = fetched_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    stored_warnings = [
        str(item)
        for item in (*reference.get("warnings", []), *activity.get("warnings", []))
        if str(item).strip()
    ]
    if payload.get("schema_version") == 1:
        stored_warnings.append(
            "This older development cache does not include record-creation events; "
            "regenerate it with APLUS_DEV_CACHE_MODE=refresh to chart creation dates."
        )
    warnings = tuple(dict.fromkeys([
        *stored_warnings,
        f"Development cache active for {project.label}; using sanitized "
        f"local data saved {saved_label}.",
    ]))
    return CachedProjectData(
        project=project,
        record_id_field=record_id_field,
        completion_fields=tuple(str(item) for item in completion_fields),
        records=_dictionary_list(activity.get("records"), "record data"),
        logs=_dictionary_list(activity.get("logs"), "audit data"),
        users=_dictionary_list(reference.get("users"), "user data"),
        dags=_dictionary_list(reference.get("dags"), "DAG data"),
        fetched_at=fetched_at,
        expires_at=fetched_at + timedelta(seconds=ttl_seconds),
        from_cache=from_cache,
        stale=False,
        creation_events_available=payload.get("schema_version") == _DEV_CACHE_SCHEMA_VERSION,
        warnings=warnings,
    )


def is_record_creation_log(row: dict[str, Any]) -> bool:
    """Match explicit record creation, including API/import action suffixes."""
    return bool(re.match(r"^create record\b", str(row.get("action", "")).strip(), re.I))


def _sanitized_development_payload(
    project: RedcapProject,
    reference: dict[str, Any],
    activity: dict[str, Any],
    fetched_at: datetime,
) -> dict[str, Any]:
    """Keep only dashboard inputs and replace participant record identifiers."""
    record_id_field = str(reference["record_id_field"])
    completion_fields = tuple(str(item) for item in reference["completion_fields"])
    completion_field_set = set(completion_fields)
    record_id_map: dict[str, str] = {}
    site_counts: dict[str, int] = {}
    records: list[dict[str, Any]] = []

    for row in activity["records"]:
        original_id = str(row.get(record_id_field, "")).strip()
        if original_id and original_id not in record_id_map:
            match = _DEV_RECORD_ID.fullmatch(original_id)
            if match:
                site = match.group("site")
                site_counts[site] = site_counts.get(site, 0) + 1
                if site_counts[site] > 99999:
                    raise RedcapConfigurationError(
                        f"{project.label} has too many records for sanitized development IDs."
                    )
                record_id_map[original_id] = (
                    f"{site}-{project.study_code}-{site_counts[site]:05d}"
                )
            else:
                record_id_map[original_id] = f"dev-record-{len(record_id_map) + 1:05d}"

        sanitized = {record_id_field: record_id_map.get(original_id, "")}
        for key in (
            "redcap_data_access_group",
            "redcap_event_name",
            "redcap_repeat_instrument",
            "redcap_repeat_instance",
            *completion_fields,
        ):
            if key in row:
                sanitized[key] = row[key]
        records.append(sanitized)

    logs: list[dict[str, Any]] = []
    for row in activity["logs"]:
        sanitized_id = record_id_map.get(str(row.get("record", "")).strip())
        if not sanitized_id:
            continue
        changes = [
            f"{match.group('field')} = '2'"
            for match in _DEV_COMPLETE_CHANGE.finditer(str(row.get("details", "")))
            if match.group("field") in completion_field_set
            and match.group("value") == "2"
        ]
        created = is_record_creation_log(row)
        if changes or created:
            logs.append({
                "record": sanitized_id,
                "username": str(row.get("username", "")),
                "timestamp": str(row.get("timestamp", "")),
                "details": ", ".join(changes),
                # Do not retain raw action text, which may include participant IDs.
                "action": "Create record" if created else "Update record",
            })

    user_keys = (
        "username", "firstname", "lastname",
        "data_access_group_id", "data_access_group_label",
    )
    dag_keys = (
        "data_access_group_id", "data_access_group_name", "unique_group_name",
    )
    return {
        "schema_version": _DEV_CACHE_SCHEMA_VERSION,
        "sanitized": True,
        "saved_at": fetched_at.isoformat(),
        "project": {"id": project.id, "study_code": project.study_code},
        "reference": {
            "users": [
                {key: row[key] for key in user_keys if key in row}
                for row in reference["users"]
            ],
            "dags": [
                {key: row[key] for key in dag_keys if key in row}
                for row in reference["dags"]
            ],
            "warnings": list(reference["warnings"]),
        },
        "activity": {
            "record_id_field": record_id_field,
            "completion_fields": list(completion_fields),
            "records": records,
            "logs": logs,
            "warnings": list(activity["warnings"]),
        },
    }


def _write_development_cache(project: RedcapProject, payload: dict[str, Any]) -> None:
    path = _development_cache_path(project)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RedcapConfigurationError(
            f"The local {project.label} development cache could not be written."
        ) from exc


def _fetch_and_store_development_data(project: RedcapProject) -> CachedProjectData:
    reference = _fetch_reference_data(project)
    now = time.monotonic()
    reference_entry = _ReferenceCacheEntry(
        **reference,
        fetched_monotonic=now,
        expires_monotonic=now + reference_cache_ttl_seconds(),
    )
    activity = _fetch_activity_data(project, reference_entry)
    fetched_at = datetime.now(timezone.utc)
    payload = _sanitized_development_payload(project, reference, activity, fetched_at)
    _write_development_cache(project, payload)
    result = _read_development_cache(project, from_cache=False)
    assert result is not None
    return result


def _post(project: RedcapProject, form_data: list[tuple[str, str]]) -> Any:
    api_url, api_token = project.credentials()
    payload = [("token", api_token), *form_data]
    try:
        with httpx.Client(
            timeout=_timeout_seconds(),
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ) as client:
            response = client.post(api_url, content=urlencode(payload))
    except httpx.RequestError as exc:
        raise RedcapRequestError(f"{project.label} could not be reached.") from exc

    if response.is_redirect:
        raise RedcapRequestError(
            f"{project.label} redirected the request; check its exact API URL."
        )
    if not response.is_success:
        raise RedcapRequestError(
            f"{project.label} returned HTTP {response.status_code}."
        )
    try:
        result = response.json()
    except ValueError as exc:
        raise RedcapRequestError(
            f"{project.label} returned an unexpected response."
        ) from exc
    if isinstance(result, dict) and result.get("error"):
        raise RedcapRequestError(f"{project.label} rejected the request.")
    return result


def _list_response(project: RedcapProject, name: str, payload: list[tuple[str, str]]) -> list[dict[str, Any]]:
    result = _post(project, payload)
    if not isinstance(result, list):
        raise RedcapRequestError(f"{project.label} returned invalid {name} data.")
    return [item for item in result if isinstance(item, dict)]


def _optional_list(
    project: RedcapProject,
    name: str,
    payload: list[tuple[str, str]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    try:
        return _list_response(project, name, payload)
    except RedcapRequestError:
        warnings.append(
            f"{project.label} {name} could not be exported; related attribution may be incomplete."
        )
        return []


def _basic_payload(content: str) -> list[tuple[str, str]]:
    return [("content", content), ("format", "json"), ("returnFormat", "json")]


def _fetch_reference_data(project: RedcapProject) -> dict[str, Any]:
    """Fetch slow-changing schema, site, and user reference data."""
    warnings: list[str] = []
    metadata = _list_response(project, "metadata", _basic_payload("metadata"))
    if not metadata or not metadata[0].get("field_name"):
        raise RedcapRequestError(f"{project.label} returned no record-id field.")

    form_names = tuple(
        dict.fromkeys(
            str(item.get("form_name", "")).strip()
            for item in metadata
            if str(item.get("form_name", "")).strip()
            and str(item.get("form_name", "")).strip() not in project.excluded_forms
        )
    )
    if not form_names:
        raise RedcapRequestError(f"{project.label} returned no reportable forms.")

    dags = _optional_list(project, "DAG", _basic_payload("dag"), warnings)
    users = _optional_list(project, "user", _basic_payload("user"), warnings)
    return {
        "record_id_field": str(metadata[0]["field_name"]),
        "completion_fields": tuple(f"{name}_complete" for name in form_names),
        "users": users,
        "dags": dags,
        "warnings": tuple(warnings),
    }


def _fetch_activity_data(
    project: RedcapProject, reference: _ReferenceCacheEntry
) -> dict[str, Any]:
    """Fetch current completion status and the audit data used for attribution."""
    warnings: list[str] = []
    record_payload: list[tuple[str, str]] = [
        ("content", "record"),
        ("action", "export"),
        ("format", "json"),
        ("type", "flat"),
        ("rawOrLabel", "raw"),
        ("rawOrLabelHeaders", "raw"),
        ("exportDataAccessGroups", "true"),
        ("returnFormat", "json"),
        ("fields[]", reference.record_id_field),
    ]
    record_payload.extend(
        ("fields[]", field_name) for field_name in reference.completion_fields
    )
    records = _list_response(project, "record", record_payload)

    begin = datetime.now().astimezone() - timedelta(days=activity_lookback_days())
    log_payload = _basic_payload("log") + [
        ("logtype", "record"),
        ("beginTime", begin.strftime("%Y-%m-%d %H:%M")),
    ]
    logs = _optional_list(project, "audit log", log_payload, warnings)
    return {
        "record_id_field": reference.record_id_field,
        "completion_fields": reference.completion_fields,
        "records": records,
        "logs": logs,
        "warnings": tuple(warnings),
    }


def _project_lock(project_id: str) -> threading.Lock:
    with _CACHE_GUARD:
        return _PROJECT_LOCKS.setdefault(project_id, threading.Lock())


def get_project_data(project: RedcapProject, force: bool = False) -> CachedProjectData:
    """Return reporting inputs with independent reference and activity caches."""
    ttl_seconds, minimum_refresh = cache_policy()
    reference_ttl = reference_cache_ttl_seconds()

    with _project_lock(project.id):
        mode = development_cache_mode()
        if mode != "off":
            cached = _read_development_cache(project)
            already_refreshed = project.id in _DEV_REFRESHED_PROJECTS
            if cached is not None and cached.creation_events_available and (mode == "prefer" or already_refreshed):
                return cached
            result = _fetch_and_store_development_data(project)
            if mode == "refresh":
                _DEV_REFRESHED_PROJECTS.add(project.id)
            return result

        now = time.monotonic()
        with _CACHE_GUARD:
            reference = _REFERENCE_CACHE.get(project.id)
            activity = _ACTIVITY_CACHE.get(project.id)

        reference_from_cache = bool(
            reference and now < reference.expires_monotonic
        )
        reference_stale = False
        reference_warning: str | None = None
        if not reference_from_cache:
            try:
                fetched_reference = _fetch_reference_data(project)
            except (RedcapConfigurationError, RedcapRequestError):
                if reference is None:
                    raise
                reference_stale = True
                reference_warning = (
                    f"Using stale {project.label} reference data because its refresh failed."
                )
            else:
                reference = _ReferenceCacheEntry(
                    **fetched_reference,
                    fetched_monotonic=now,
                    expires_monotonic=now + reference_ttl,
                )
                with _CACHE_GUARD:
                    _REFERENCE_CACHE[project.id] = reference

        # A reference value either came from cache, was fetched, or caused an
        # exception above. This assertion keeps the type clear below.
        assert reference is not None

        activity_is_current = bool(
            activity
            and (
                (not force and now < activity.expires_monotonic)
                or (force and now - activity.fetched_monotonic < minimum_refresh)
            )
        )
        activity_stale = False
        activity_warning: str | None = None
        if not activity_is_current:
            try:
                fetched_activity = _fetch_activity_data(project, reference)
            except (RedcapConfigurationError, RedcapRequestError):
                if activity is None:
                    raise
                activity_stale = True
                activity_warning = (
                    f"Using stale {project.label} activity data because its refresh failed."
                )
            else:
                fetched_at = datetime.now(timezone.utc)
                activity = _ActivityCacheEntry(
                    **fetched_activity,
                    fetched_at=fetched_at,
                    fetched_monotonic=now,
                    expires_monotonic=now + ttl_seconds,
                )
                with _CACHE_GUARD:
                    _ACTIVITY_CACHE[project.id] = activity

        assert activity is not None
        return _cached_result(
            project,
            reference,
            activity,
            from_cache=reference_from_cache and activity_is_current,
            stale=reference_stale or activity_stale,
            extra_warnings=tuple(
                warning
                for warning in (reference_warning, activity_warning)
                if warning
            ),
        )


def get_cached_project_data(project: RedcapProject) -> CachedProjectData | None:
    """Return an existing cache entry immediately, even when its TTL has elapsed."""
    if development_cache_mode() != "off":
        return _read_development_cache(project)

    now = time.monotonic()
    with _CACHE_GUARD:
        reference = _REFERENCE_CACHE.get(project.id)
        activity = _ACTIVITY_CACHE.get(project.id)
    if reference is None or activity is None:
        return None
    return _cached_result(
        project,
        reference,
        activity,
        from_cache=True,
        stale=(
            now >= reference.expires_monotonic
            or now >= activity.expires_monotonic
        ),
    )


def _cached_result(
    project: RedcapProject,
    reference: _ReferenceCacheEntry,
    activity: _ActivityCacheEntry,
    from_cache: bool,
    stale: bool = False,
    extra_warnings: tuple[str, ...] = (),
) -> CachedProjectData:
    ttl_seconds, _ = cache_policy()
    warnings = tuple(
        dict.fromkeys(reference.warnings + activity.warnings + extra_warnings)
    )
    return CachedProjectData(
        project=project,
        record_id_field=activity.record_id_field,
        completion_fields=activity.completion_fields,
        records=activity.records,
        logs=activity.logs,
        users=reference.users,
        dags=reference.dags,
        fetched_at=activity.fetched_at,
        expires_at=activity.fetched_at + timedelta(seconds=ttl_seconds),
        from_cache=from_cache,
        stale=stale,
        warnings=warnings,
    )


def test_redcap_connections() -> list[dict[str, str]]:
    results = []
    for project in load_projects():
        _post(project, _basic_payload("project"))
        results.append({"id": project.id, "label": project.label, "status": "ok"})
    return results


def clear_process_cache() -> None:
    with _CACHE_GUARD:
        _REFERENCE_CACHE.clear()
        _ACTIVITY_CACHE.clear()
        _DEV_REFRESHED_PROJECTS.clear()
