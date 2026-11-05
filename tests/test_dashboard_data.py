import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from dashboard_data import ReportingPeriodError, build_dashboard_snapshot
from redcap_client import CachedProjectData, RedcapProject


class DashboardAggregationTests(unittest.TestCase):
    def test_custom_range_includes_whole_boundary_days_and_scopes_all_reports(self):
        project = RedcapProject(
            id="brisk", label="BRISK", study_code="01",
            api_url_env="URL", api_token_env="TOKEN",
        )
        timestamps = [
            "2026-01-09 23:59:59", "2026-01-10 00:00:00",
            "2026-01-11 23:59:59", "2026-01-12 00:00:00", None,
            "2026-01-10 12:00:00",
        ]
        records, logs = [], []
        for index, timestamp in enumerate(timestamps):
            record_id = f"{'12' if index == 5 else '11'}-01-{index:05d}"
            records.append({"ptid": record_id, "demographics_complete": "2"})
            if timestamp:
                logs.append({
                    "record": record_id, "username": "tester", "timestamp": timestamp,
                    "details": "demographics_complete = '2'",
                })
        now = datetime.now(timezone.utc)
        data = CachedProjectData(
            project=project, record_id_field="ptid",
            completion_fields=("demographics_complete",), records=records,
            logs=logs, users=[], dags=[], fetched_at=now,
            expires_at=now + timedelta(minutes=10), from_cache=False,
        )
        with (
            patch("dashboard_data.load_projects", return_value=[project]),
            patch("dashboard_data.get_project_data", return_value=data),
            patch("dashboard_data.get_cached_project_data", return_value=data),
            patch("dashboard_data.dashboard_settings", return_value={}),
            patch("dashboard_data.cache_policy", return_value=(600, 60)),
        ):
            for cache_only in (False, True):
                with self.subTest(cache_only=cache_only):
                    result = build_dashboard_snapshot(
                        period="custom", start_date="2026-01-10", end_date="2026-01-11",
                        allowed_sites={"11"}, cache_only=cache_only,
                    )
                    self.assertEqual(result["metrics"]["completedForms"], 2)
                    self.assertEqual(result["metrics"]["recordsEntered"], 5)
                    for rows in ("projectRows", "siteRows"):
                        self.assertEqual(sum(row["records"] for row in result[rows]), 5)
                    for rows in ("projectRows", "siteRows", "personRows"):
                        self.assertEqual(sum(row["formsCompleted"] for row in result[rows]), 2)
                    self.assertEqual(result["activitySeries"]["buckets"], [
                        {"start": "2026-01-10"}, {"start": "2026-01-11"},
                    ])
                    self.assertEqual(result["activitySeries"]["series"][0]["values"], [1, 1])
                    self.assertEqual(result["startDate"], "2026-01-10")
                    self.assertEqual(result["endDate"], "2026-01-11")
            single_day = build_dashboard_snapshot(
                period="custom", start_date="2026-01-11", end_date="2026-01-11",
            )
            self.assertEqual(single_day["metrics"]["completedForms"], 1)
            self.assertEqual(len(single_day["activitySeries"]["buckets"]), 1)
            for end_date, bucket in (("2026-02-15", "week"), ("2026-06-30", "month")):
                result = build_dashboard_snapshot(
                    period="custom", start_date="2026-01-10", end_date=end_date,
                )
                self.assertEqual(result["activitySeries"]["bucket"], bucket)
                self.assertEqual(result["activitySeries"]["chartedTotal"], 4)

    def test_record_creation_trend_deduplicates_and_respects_dates_and_site_access(self):
        project = RedcapProject(id="brisk", label="BRISK", study_code="01",
                                api_url_env="URL", api_token_env="TOKEN")
        ids = [f"11-01-{index:05d}" for index in range(1, 5)] + ["12-01-00005"]
        records = [{"ptid": record_id, "demographics_complete": "0"} for record_id in ids]
        records.append({**records[0], "redcap_repeat_instance": "2"})
        logs = [
            {"record": ids[0], "action": "Create record (API)", "timestamp": "2026-01-10 00:00:00"},
            {"record": ids[0], "action": "Create record", "timestamp": "2026-01-11 12:00:00"},
            {"record": ids[1], "action": "Create record", "timestamp": "2026-01-11 23:59:59"},
            {"record": ids[2], "action": "Create record", "timestamp": "2026-01-09 23:59:59"},
            {"record": ids[3], "action": "Update record", "timestamp": "2026-01-10 12:00:00",
             "details": "demographics_complete = '2'"},
            {"record": ids[4], "action": "Create record", "timestamp": "2026-01-10 12:00:00"},
            {"record": "11-01-99999", "action": "Create record", "timestamp": "2026-01-10 12:00:00"},
        ]
        now = datetime.now(timezone.utc)
        data = CachedProjectData(project=project, record_id_field="ptid",
                                 completion_fields=("demographics_complete",), records=records,
                                 logs=logs, users=[], dags=[], fetched_at=now,
                                 expires_at=now + timedelta(minutes=10), from_cache=False)
        with (
            patch("dashboard_data.load_projects", return_value=[project]),
            patch("dashboard_data.get_project_data", return_value=data),
            patch("dashboard_data.get_cached_project_data", return_value=data),
            patch("dashboard_data.dashboard_settings", return_value={}),
            patch("dashboard_data.cache_policy", return_value=(600, 60)),
        ):
            for cache_only in (False, True):
                for scope in ({"allowed_sites": {"11"}}, {"selected_site": "11"}):
                    result = build_dashboard_snapshot(
                        period="custom", start_date="2026-01-10", end_date="2026-01-11",
                        cache_only=cache_only, **scope,
                    )
                    activity = result["recordActivitySeries"]
                    self.assertEqual(activity["metric"], "recordsCreated")
                    self.assertEqual(activity["series"][0]["values"], [1, 1])
                    self.assertEqual(activity["chartedTotal"], 2)
                    self.assertEqual(activity["unavailableTotal"], 1)
                    self.assertEqual(result["metrics"]["recordsEntered"], 4)
                    self.assertEqual(result["activitySeries"]["chartedTotal"], 0)
                    self.assertNotIn(ids[0], str(result))
            for end_date, bucket in (("2026-02-15", "week"), ("2026-06-30", "month")):
                result = build_dashboard_snapshot(
                    selected_project="brisk", period="custom", start_date="2026-01-10",
                    end_date=end_date, allowed_sites={"11"},
                )
                self.assertEqual(result["recordActivitySeries"]["bucket"], bucket)
                self.assertEqual(result["recordActivitySeries"]["chartedTotal"], 2)
            result = build_dashboard_snapshot(period="custom", start_date="2026-01-10", end_date="2026-01-11")
            self.assertEqual(result["recordActivitySeries"]["chartedTotal"], 3)
            empty = build_dashboard_snapshot(period="custom", start_date="2027-01-01", end_date="2027-01-02")
            self.assertEqual(empty["recordActivitySeries"]["chartedTotal"], 0)
            self.assertEqual(empty["recordActivitySeries"]["unavailableTotal"], 1)

    def test_explicit_chart_intervals_preserve_hour_dates_and_totals(self):
        from dashboard_data import _activity_series
        from types import SimpleNamespace
        successful = [(SimpleNamespace(project=SimpleNamespace(id="brisk", label="BRISK")), {})]
        events = [{"projectId": "brisk", "completedAt": datetime(2026, 1, 10, hour, minute)}
                  for hour, minute in ((0, 1), (0, 59), (23, 59))]
        dates = (datetime(2026, 1, 10), datetime(2026, 1, 10, 23, 59, 59))
        for interval, count in (("hour", 24), ("day", 1), ("month", 1)):
            result = _activity_series(events, successful, "custom", dates, bucket_override=interval)
            self.assertEqual(result["bucket"], interval)
            self.assertEqual(len(result["buckets"]), count)
            self.assertEqual(result["chartedTotal"], 3)
            self.assertEqual(sum(result["series"][0]["values"]), 3)
            if interval == "hour":
                self.assertEqual(result["buckets"][0]["start"], "2026-01-10T00:00")
                self.assertEqual(result["series"][0]["values"][0], 2)
                self.assertEqual(result["series"][0]["values"][-1], 1)

    @patch("dashboard_data.load_projects")
    def test_custom_dates_are_validated_before_loading_data(self, load_projects):
        for start, end in (
            (None, None), ("2026-01-01", None), (None, "2026-01-01"),
            ("bad", "2026-01-01"), ("2026-02-30", "2026-03-01"),
            ("2026-01-02", "2026-01-01"), (123, "2026-01-01"),
        ):
            with self.subTest(start=start, end=end), self.assertRaises(ReportingPeriodError):
                build_dashboard_snapshot(period="custom", start_date=start, end_date=end)
        load_projects.assert_not_called()

    def test_cache_only_snapshot_does_not_fetch_when_cache_is_missing(self):
        project = RedcapProject(
            id="brisk",
            label="BRISK",
            study_code="01",
            api_url_env="URL",
            api_token_env="TOKEN",
        )
        with (
            patch("dashboard_data.load_projects", return_value=[project]),
            patch("dashboard_data.get_cached_project_data", return_value=None),
            patch("dashboard_data.get_project_data") as get_project_data,
        ):
            result = build_dashboard_snapshot(cache_only=True)

        self.assertEqual(result["state"], "empty")
        get_project_data.assert_not_called()

    def test_completed_form_is_attributed_without_exposing_record_id(self):
        project = RedcapProject(
            id="brisk",
            label="BRISK",
            study_code="01",
            api_url_env="URL",
            api_token_env="TOKEN",
        )
        now = datetime.now(timezone.utc)
        data = CachedProjectData(
            project=project,
            record_id_field="ptid",
            completion_fields=("demographics_complete",),
            records=[
                {
                    "ptid": "11-01-00001",
                    "redcap_data_access_group": "site_11",
                    "demographics_complete": "2",
                },
                {
                    "ptid": "12-01-00002",
                    "redcap_data_access_group": "site_12",
                    "demographics_complete": "0",
                },
                {
                    "ptid": "11-01-00001",
                    "redcap_data_access_group": "site_11",
                    "redcap_repeat_instance": "2",
                    "demographics_complete": "0",
                },
            ],
            logs=[
                {
                    "record": "11-01-00001",
                    "username": "fflintstone",
                    "timestamp": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M"),
                    "details": "demographics_complete = '2'",
                }
            ],
            users=[
                {"username": "fflintstone", "firstname": "Fred", "lastname": "Flintstone"}
            ],
            dags=[
                {
                    "data_access_group_id": "11",
                    "data_access_group_name": "Example Site",
                    "unique_group_name": "site_11",
                },
                {
                    "data_access_group_id": "12",
                    "data_access_group_name": "Zero Activity Site",
                    "unique_group_name": "site_12",
                },
            ],
            fetched_at=now,
            expires_at=now + timedelta(minutes=10),
            from_cache=False,
        )
        with (
            patch("dashboard_data.load_projects", return_value=[project]),
            patch("dashboard_data.get_project_data", return_value=data),
            patch("dashboard_data.dashboard_settings", return_value={}),
            patch("dashboard_data.cache_policy", return_value=(600, 60)),
        ):
            result = build_dashboard_snapshot()
            site_result = build_dashboard_snapshot(selected_site="11", period="30")

        self.assertEqual(result["metrics"]["recordsEntered"], 2)
        self.assertEqual(result["projectRows"][0]["records"], 2)
        self.assertEqual([row["records"] for row in result["siteRows"]], [1, 1])
        self.assertEqual(site_result["projectRows"][0]["records"], 1)
        self.assertEqual(site_result["siteRows"][0]["records"], 1)
        self.assertEqual(result["metrics"]["completedForms"], 1)
        self.assertEqual(result["metrics"]["participatingSites"], 2)
        self.assertEqual(result["periodOptions"][0]["label"], "All completed forms")
        self.assertEqual(result["personRows"][0]["person"], "Fred Flintstone")
        self.assertEqual(result["siteRows"][0]["site"], "Example Site")
        self.assertEqual(len(result["siteRows"]), 2)
        self.assertEqual(result["siteRows"][1]["formsCompleted"], 0)
        self.assertNotIn("11-01-00001", str(result))
        self.assertEqual(site_result["metrics"]["recordsEntered"], 1)
        activity = result["activitySeries"]
        self.assertEqual(activity["bucket"], "month")
        self.assertEqual(activity["chartedTotal"], 1)
        self.assertEqual(activity["unavailableTotal"], 0)
        self.assertEqual(sum(activity["series"][0]["values"]), 1)
        self.assertEqual(site_result["activitySeries"]["bucket"], "day")


if __name__ == "__main__":
    unittest.main()
