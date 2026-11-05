import unittest
from unittest.mock import patch

from app import server


def snapshot_fixture():
    return {
        "state": "ready",
        "title": "A+ Data Entry Activity",
        "subtitle": "Test snapshot",
        "selectedProject": "all",
        "selectedSite": "all",
        "period": "all",
        "periodOptions": [],
        "projects": [],
        "availableSites": [],
        "metrics": {},
        "projectRows": [],
        "siteRows": [],
        "personRows": [],
        "activitySeries": {
            "metric": "completedForms",
            "bucket": "month",
            "buckets": [],
            "series": [],
            "chartedTotal": 0,
            "unavailableTotal": 0,
        },
        "updatedAt": None,
        "cacheTtlSeconds": 600,
        "stale": False,
        "sources": [],
        "warnings": [],
    }


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.client = server.test_client()

    def test_data_entry_dashboard_and_logo_load(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"A+ Data Entry Activity", response.data)
        self.assertIn(b"APlusLogo.png", response.data)
        self.assertNotIn(b"user-avatar", response.data)
        self.assertNotIn(b"Visit compliance", response.data)
        self.assertNotIn(b"Coordinator view", response.data)
        self.assertNotIn(b"Attribution coverage", response.data)
        self.assertNotIn(b"Form activity", response.data)
        self.assertNotIn(b'class="breadcrumb"', response.data)
        self.assertNotIn(b'data-kpi="recent"', response.data)
        self.assertIn(b'data-kpi="records"', response.data)
        self.assertLess(
            response.data.index(b'data-kpi="records"'),
            response.data.index(b'data-kpi="forms"'),
        )
        self.assertIn(b"sidebarCloseButton", response.data)
        self.assertIn(b'id="trends"', response.data)
        self.assertIn(b'id="activityChart"', response.data)
        self.assertIn(b"00-chart.umd.min.js", response.data)
        self.assertLess(
            response.data.index(b"00-chart.umd.min.js"),
            response.data.index(b"assets/app.js"),
        )
        self.assertEqual(response.data.count(b"All completed forms"), 1)
        self.assertNotIn(b"Current completed forms", response.data)
        self.assertEqual(response.data.count(b'class="sort-button"'), 17)

    @patch("app.build_dashboard_snapshot", return_value=snapshot_fixture())
    def test_custom_dates_are_forwarded_by_all_report_endpoints(self, build_snapshot):
        filters = {"project": "brisk", "site": "11", "period": "custom",
                   "startDate": "2026-01-10", "endDate": "2026-01-11"}
        for path, options in (("/api/dashboard", {}),
                              ("/api/dashboard/cached", {"cache_only": True}),
                              ("/api/dashboard/refresh", {"force": True})):
            with self.subTest(path=path):
                build_snapshot.reset_mock()
                response = (self.client.post(path, json=filters) if options.get("force")
                            else self.client.get(path, query_string=filters))
                self.assertEqual(response.status_code, 200)
                build_snapshot.assert_called_once_with(
                    "brisk", "11", "custom", start_date="2026-01-10",
                    end_date="2026-01-11", **options,
                )

    @patch("app.access_settings", return_value={})
    @patch("dashboard_data.load_projects")
    def test_invalid_custom_range_returns_bad_request(self, load_projects, _settings):
        for dates in ({}, {"startDate": "2026-01-12", "endDate": "2026-01-10"},
                      {"startDate": "2026-02-30", "endDate": "2026-03-01"}):
            filters = {"period": "custom", **dates}
            for path in ("/api/dashboard", "/api/dashboard/cached", "/api/dashboard/refresh"):
                with self.subTest(path=path, dates=dates):
                    response = (self.client.post(path, json=filters) if path.endswith("refresh")
                                else self.client.get(path, query_string=filters))
                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(response.get_json()["ok"])
        load_projects.assert_not_called()

    @patch("app.project_statuses")
    @patch("app.is_redcap_configured", return_value=False)
    def test_health_exposes_status_not_credentials(self, _configured, statuses):
        statuses.return_value = [
            {"id": "brisk", "label": "BRISK", "studyCode": "01", "configured": False}
        ]
        response = self.client.get("/api/health")
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertFalse(payload["redcapConfigured"])
        self.assertNotIn("token", response.get_data(as_text=True).lower())

    @patch("app.build_dashboard_snapshot", return_value=snapshot_fixture())
    def test_dashboard_api_passes_report_filters(self, build_snapshot):
        response = self.client.get(
            "/api/dashboard?project=prism&site=12&period=90"
        )
        self.assertEqual(response.status_code, 200)
        build_snapshot.assert_called_once_with("prism", "12", "90")

    @patch("app.build_dashboard_snapshot", return_value=snapshot_fixture())
    def test_refresh_forces_selected_filters(self, build_snapshot):
        response = self.client.post(
            "/api/dashboard/refresh",
            json={"project": "train", "site": "01", "period": "30"},
        )
        self.assertEqual(response.status_code, 200)
        build_snapshot.assert_called_once_with(
            "train", "01", "30", force=True
        )

    @patch("app.build_dashboard_snapshot", return_value=snapshot_fixture())
    def test_cached_dashboard_never_requests_redcap_refresh(self, build_snapshot):
        response = self.client.get(
            "/api/dashboard/cached?project=brisk&site=11&period=365"
        )
        self.assertEqual(response.status_code, 200)
        build_snapshot.assert_called_once_with(
            "brisk", "11", "365", cache_only=True
        )

    @patch("app.build_dashboard_snapshot", return_value={"state": "empty"})
    def test_cached_dashboard_returns_no_content_when_view_is_unavailable(
        self, build_snapshot
    ):
        response = self.client.get("/api/dashboard/cached")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.data, b"")
        build_snapshot.assert_called_once_with(
            "all", "all", "all", cache_only=True
        )

    @patch("app.build_dashboard_snapshot", return_value=snapshot_fixture())
    @patch("app.access_settings")
    def test_site_viewer_scope_is_enforced_server_side(self, settings, build_snapshot):
        settings.return_value = {
            "enforce_viewer_sites": True,
            "coordinator_emails": [],
            "viewer_sites": {"site@example.org": ["11"]},
        }
        response = self.client.get(
            "/api/dashboard",
            headers={
                "RStudio-Connect-Credentials": '{"user":"site@example.org","groups":[]}'
            },
        )
        self.assertEqual(response.status_code, 200)
        build_snapshot.assert_called_once_with(
            "all", "all", "all", allowed_sites={"11"}
        )

    @patch("app.access_settings")
    def test_unassigned_private_viewer_is_forbidden(self, settings):
        settings.return_value = {
            "enforce_viewer_sites": True,
            "coordinator_emails": [],
            "viewer_sites": {},
        }
        response = self.client.get(
            "/api/dashboard",
            headers={
                "RStudio-Connect-Credentials": '{"user":"unknown@example.org","groups":[]}'
            },
        )
        self.assertEqual(response.status_code, 403)

    @patch("app.test_redcap_connections")
    def test_connection_error_is_sanitized(self, test_connections):
        from redcap_client import RedcapConfigurationError

        test_connections.side_effect = RedcapConfigurationError("Missing named settings.")
        response = self.client.post("/api/redcap/test-connection")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
