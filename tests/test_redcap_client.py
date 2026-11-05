import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from redcap_client import (
    RedcapProject,
    clear_process_cache,
    get_cached_project_data,
    get_project_data,
    load_projects,
)


class RedcapProjectTests(unittest.TestCase):
    def setUp(self):
        self.cache_mode = patch.dict(
            os.environ, {"APLUS_DEV_CACHE_MODE": "off"}
        )
        self.cache_mode.start()

    def tearDown(self):
        clear_process_cache()
        self.cache_mode.stop()

    def test_named_study_credentials_and_codes(self):
        projects = {project.id: project for project in load_projects()}
        self.assertEqual(set(projects), {"brisk", "prism", "train"})
        self.assertEqual(projects["brisk"].study_code, "01")
        self.assertEqual(projects["prism"].study_code, "02")
        self.assertEqual(projects["train"].study_code, "03")
        self.assertEqual(projects["brisk"].api_token_env, "REDCAP_BRISK_API_TOKEN")

    @patch("redcap_client.cache_policy", return_value=(600, 60))
    @patch("redcap_client.reference_cache_ttl_seconds", return_value=86400)
    @patch("redcap_client._fetch_activity_data")
    @patch("redcap_client._fetch_reference_data")
    def test_activity_inputs_are_cached_per_project(
        self, fetch_reference, fetch_activity, _reference_ttl, _cache_policy
    ):
        fetch_reference.return_value = {
            "record_id_field": "record_id",
            "completion_fields": (),
            "users": [],
            "dags": [],
            "warnings": (),
        }
        fetch_activity.return_value = {
            "record_id_field": "record_id",
            "completion_fields": (),
            "records": [],
            "logs": [],
            "warnings": (),
        }
        project = RedcapProject(
            id="brisk",
            label="BRISK",
            study_code="01",
            api_url_env="REDCAP_BRISK_API_URL",
            api_token_env="REDCAP_BRISK_API_TOKEN",
        )
        first = get_project_data(project)
        second = get_project_data(project)
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        fetch_reference.assert_called_once_with(project)
        fetch_activity.assert_called_once()

    @patch("redcap_client.cache_policy", return_value=(600, 60))
    @patch("redcap_client.reference_cache_ttl_seconds", return_value=86400)
    @patch("redcap_client.time.monotonic", side_effect=[100, 701])
    @patch("redcap_client._fetch_activity_data")
    @patch("redcap_client._fetch_reference_data")
    def test_expired_cache_can_be_read_without_refresh(
        self,
        fetch_reference,
        fetch_activity,
        _monotonic,
        _reference_ttl,
        _cache_policy,
    ):
        fetch_reference.return_value = {
            "record_id_field": "record_id",
            "completion_fields": (),
            "users": [],
            "dags": [],
            "warnings": (),
        }
        fetch_activity.return_value = {
            "record_id_field": "record_id",
            "completion_fields": (),
            "records": [],
            "logs": [],
            "warnings": (),
        }
        project = RedcapProject(
            id="prism",
            label="PRISM",
            study_code="02",
            api_url_env="REDCAP_PRISM_API_URL",
            api_token_env="REDCAP_PRISM_API_TOKEN",
        )

        get_project_data(project)
        cached = get_cached_project_data(project)

        self.assertIsNotNone(cached)
        self.assertTrue(cached.from_cache)
        self.assertTrue(cached.stale)
        fetch_reference.assert_called_once_with(project)
        fetch_activity.assert_called_once()

    @patch("redcap_client.cache_policy", return_value=(600, 60))
    @patch("redcap_client.reference_cache_ttl_seconds", return_value=86400)
    @patch("redcap_client.time.monotonic", side_effect=[100, 701])
    @patch("redcap_client._post")
    def test_cold_refresh_uses_only_five_redcap_exports(
        self, post, _monotonic, _reference_ttl, _cache_policy
    ):
        contents = []

        def response_for(_project, payload):
            content = dict(payload)["content"]
            contents.append(content)
            if content == "metadata":
                return [
                    {"field_name": "record_id", "form_name": "demographics"},
                    {"field_name": "age", "form_name": "demographics"},
                    {"field_name": "note", "form_name": "internal_tracking"},
                ]
            return []

        post.side_effect = response_for
        project = RedcapProject(
            id="brisk",
            label="BRISK",
            study_code="01",
            api_url_env="REDCAP_BRISK_API_URL",
            api_token_env="REDCAP_BRISK_API_TOKEN",
            excluded_forms=("internal_tracking",),
        )

        result = get_project_data(project)

        self.assertEqual(
            contents,
            ["metadata", "dag", "user", "record", "log"],
        )
        self.assertEqual(result.record_id_field, "record_id")
        self.assertEqual(result.completion_fields, ("demographics_complete",))

        contents.clear()
        get_project_data(project)
        self.assertEqual(contents, ["record", "log"])

    @patch("redcap_client.cache_policy", return_value=(600, 60))
    @patch("redcap_client.reference_cache_ttl_seconds", return_value=86400)
    @patch("redcap_client.time.monotonic", side_effect=[100, 701])
    @patch("redcap_client._fetch_activity_data")
    @patch("redcap_client._fetch_reference_data")
    def test_routine_refresh_reuses_reference_data(
        self,
        fetch_reference,
        fetch_activity,
        _monotonic,
        _reference_ttl,
        _cache_policy,
    ):
        fetch_reference.return_value = {
            "record_id_field": "record_id",
            "completion_fields": ("demographics_complete",),
            "users": [],
            "dags": [],
            "warnings": (),
        }
        fetch_activity.return_value = {
            "record_id_field": "record_id",
            "completion_fields": ("demographics_complete",),
            "records": [],
            "logs": [],
            "warnings": (),
        }
        project = RedcapProject(
            id="train",
            label="TRAIN",
            study_code="03",
            api_url_env="REDCAP_TRAIN_API_URL",
            api_token_env="REDCAP_TRAIN_API_TOKEN",
        )

        get_project_data(project)
        get_project_data(project)

        fetch_reference.assert_called_once_with(project)
        self.assertEqual(fetch_activity.call_count, 2)

    @patch("redcap_client.cache_policy", return_value=(600, 60))
    @patch("redcap_client.reference_cache_ttl_seconds", return_value=86400)
    @patch("redcap_client._fetch_activity_data")
    @patch("redcap_client._fetch_reference_data")
    def test_preferred_development_cache_survives_process_cache_clear(
        self, fetch_reference, fetch_activity, _reference_ttl, _cache_policy
    ):
        fetch_reference.return_value = {
            "record_id_field": "record_id",
            "completion_fields": ("demographics_complete",),
            "users": [
                {
                    "username": "fflintstone",
                    "firstname": "Fred",
                    "lastname": "Flintstone",
                    "email": "not-needed@example.org",
                }
            ],
            "dags": [
                {
                    "data_access_group_id": "11",
                    "data_access_group_name": "Example Site",
                    "unique_group_name": "site_11",
                }
            ],
            "warnings": (),
        }
        fetch_activity.return_value = {
            "record_id_field": "record_id",
            "completion_fields": ("demographics_complete",),
            "records": [
                {
                    "record_id": "11-01-54321",
                    "redcap_data_access_group": "site_11",
                    "demographics_complete": "2",
                    "diagnosis": "must not persist",
                }
            ],
            "logs": [
                {
                    "record": "11-01-54321",
                    "username": "fflintstone",
                    "timestamp": "2026-09-01 10:30",
                    "action": "Create record 11-01-54321 (API)",
                    "details": (
                        "diagnosis = 'must not persist', "
                        "demographics_complete = '2'"
                    ),
                }
            ],
            "warnings": (),
        }
        project = RedcapProject(
            id="brisk",
            label="BRISK",
            study_code="01",
            api_url_env="REDCAP_BRISK_API_URL",
            api_token_env="REDCAP_BRISK_API_TOKEN",
        )

        with (
            tempfile.TemporaryDirectory() as cache_dir,
            patch.dict(
                os.environ,
                {
                    "APLUS_DEV_CACHE_MODE": "prefer",
                    "APLUS_DEV_CACHE_DIR": cache_dir,
                },
            ),
        ):
            first = get_project_data(project)
            clear_process_cache()
            second = get_project_data(project, force=True)
            cached = get_cached_project_data(project)
            cache_text = (
                Path(cache_dir) / "brisk.json"
            ).read_text(encoding="utf-8")
            cache_payload = json.loads(cache_text)
            # Simulate an older installation whose cache discarded creation events.
            legacy_payload = json.loads(cache_text)
            legacy_payload["schema_version"] = 1
            for row in legacy_payload["activity"]["logs"]:
                row.pop("action", None)
            cache_path = Path(cache_dir) / "brisk.json"
            cache_path.write_text(json.dumps(legacy_payload), encoding="utf-8")
            legacy = get_cached_project_data(project)
            self.assertFalse(legacy.creation_events_available)
            migrated = get_project_data(project)
            self.assertFalse(migrated.from_cache)
            self.assertTrue(migrated.creation_events_available)
            self.assertEqual(migrated.logs[0]["action"], "Create record")
            self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8"))["schema_version"], 2)
            self.assertTrue(get_project_data(project).from_cache)

        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertIsNotNone(cached)
        self.assertEqual(fetch_reference.call_count, 2)
        self.assertEqual(fetch_activity.call_count, 2)
        self.assertEqual(second.records[0]["record_id"], "11-01-00001")
        self.assertNotIn("11-01-54321", cache_text)
        self.assertNotIn("must not persist", cache_text)
        self.assertNotIn("email", cache_text)
        self.assertTrue(cache_payload["sanitized"])
        self.assertEqual(cache_payload["schema_version"], 2)
        self.assertEqual(cache_payload["activity"]["logs"][0]["action"], "Create record")
        self.assertEqual(
            cache_payload["activity"]["logs"][0]["details"],
            "demographics_complete = '2'",
        )

    def test_development_cache_keeps_creation_without_completed_forms(self):
        from datetime import datetime, timezone
        from redcap_client import _sanitized_development_payload
        project = RedcapProject(id="brisk", label="BRISK", study_code="01",
                                api_url_env="URL", api_token_env="TOKEN")
        payload = _sanitized_development_payload(
            project,
            {"record_id_field": "ptid", "completion_fields": ("demo_complete",),
             "users": [], "dags": [], "warnings": []},
            {"records": [{"ptid": "11-01-54321", "demo_complete": "0"}],
             "logs": [{"record": "11-01-54321", "action": "Create record 11-01-54321",
                       "timestamp": "2026-01-01 12:00", "details": "diagnosis = 'private'"},
                      {"record": "11-01-54321", "action": "Update record",
                       "timestamp": "2026-01-02 12:00", "details": "diagnosis = 'private'"}],
             "warnings": []},
            datetime.now(timezone.utc),
        )
        logs = payload["activity"]["logs"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["action"], "Create record")
        self.assertEqual(logs[0]["record"], "11-01-00001")
        self.assertEqual(logs[0]["details"], "")
        self.assertNotIn("54321", str(payload))
        self.assertNotIn("private", str(payload))

    def test_missing_cache_returns_none(self):
        project = RedcapProject(
            id="train",
            label="TRAIN",
            study_code="03",
            api_url_env="REDCAP_TRAIN_API_URL",
            api_token_env="REDCAP_TRAIN_API_TOKEN",
        )
        self.assertIsNone(get_cached_project_data(project))


if __name__ == "__main__":
    unittest.main()
