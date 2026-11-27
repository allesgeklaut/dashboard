"""Legacy Mistral/AI-services compatibility tests (the AA score sources are gone)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import benchmarks  # noqa: E402
import homelab_core as core  # noqa: E402

NOW = 1_800_000_000.0


class OpenCodeGoToggleTests(unittest.TestCase):
    def test_disabled_ignores_saved_key_and_blocks_requests(self):
        with patch.object(core, "OPENCODE_GO_ENABLED", False), \
             patch.object(core, "OPENCODE_GO_API_KEY", "saved-key"), \
             patch("builtins.open") as auth_file, \
             patch.object(core.requests, "get") as request:
            self.assertIsNone(core._read_opencode_go_key())
            self.assertFalse(core.get_features()["opencode_go"])
            self.assertEqual(core.get_opencode_go_usage(), {"error": "not configured"})
            self.assertEqual(core.get_opencode_go_limits(), {"error": "not configured"})
            auth_file.assert_not_called()
            request.assert_not_called()

    def test_enabled_uses_existing_key(self):
        with patch.object(core, "OPENCODE_GO_ENABLED", True), \
             patch.object(core, "OPENCODE_GO_API_KEY", "saved-key"):
            self.assertEqual(core._read_opencode_go_key(), "saved-key")


class MistralCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "litellm.yaml"
        self.config_setting = patch.object(core, "LITELLM_CONFIG_FILE", str(self.config_path))
        self.config_setting.start()
        self.opencode_key = patch.object(core, "_read_opencode_go_key", return_value=None)
        self.opencode_key.start()

    def tearDown(self):
        self.opencode_key.stop()
        self.config_setting.stop()
        self.temp_dir.cleanup()

    def write_config(self, text):
        self.config_path.write_text(text, encoding="utf-8")

    def test_features_gate_on_nonempty_path_even_when_unreadable(self):
        with patch.object(core, "LITELLM_CONFIG_FILE", ""):
            self.assertFalse(core.get_features()["ai_services"])
        self.assertTrue(core.get_features()["ai_services"])
        self.assertNotIn("mistral", core.get_features())

    def test_shared_fetch_accepts_mistral_but_legacy_wrapper_stays_opencode_only(self):
        expected = {"ok": True, "refreshing": True, "started": True,
                    "sources": ["harbor", "epoch"]}
        with patch.object(benchmarks, "request_refresh", return_value=expected) as refresh:
            self.assertEqual(core.fetch_benchmarks(), expected)
            refresh.assert_called_once_with()

        with patch.object(benchmarks, "request_refresh") as refresh:
            self.assertEqual(
                core.fetch_opencode_go_benchmarks(),
                {"ok": False, "error": "not configured"},
            )
            refresh.assert_not_called()

        with patch.object(core, "_read_opencode_go_key", return_value="test-key"):
            with patch.object(benchmarks, "request_refresh", return_value=expected) as refresh:
                self.assertEqual(core.fetch_opencode_go_benchmarks(), expected)
                refresh.assert_called_once_with()

    def test_shared_fetch_requires_either_supported_configuration(self):
        with patch.object(core, "LITELLM_CONFIG_FILE", ""):
            with patch.object(benchmarks, "request_refresh") as refresh:
                self.assertEqual(
                    core.fetch_benchmarks(), {"ok": False, "error": "not configured"}
                )
                refresh.assert_not_called()

        with patch.object(core, "_read_opencode_go_key", return_value="test-key"):
            with patch.object(core, "LITELLM_CONFIG_FILE", ""):
                with patch.object(benchmarks, "request_refresh",
                                  return_value={"ok": True, "refreshing": True}) as refresh:
                    self.assertEqual(core.fetch_benchmarks()["ok"], True)
                    refresh.assert_called_once_with()

    def test_api_exposes_legacy_and_new_fetch_routes(self):
        with patch.object(core, "prime_counters"), \
                patch.object(core, "start_energy_tracker"), \
                patch.object(core, "start_desktop_monitor"):
            import main

        routes = {
            route.path: route.methods
            for route in main.app.routes
            if hasattr(route, "methods")
        }
        self.assertIn("GET", routes["/api/ai-services"])
        self.assertNotIn("/api/mistral-models", routes)
        self.assertIn("POST", routes["/api/benchmarks/fetch"])
        self.assertIn("POST", routes["/api/opencode-go-benchmarks/fetch"])

        with patch.object(core, "fetch_benchmarks", return_value={"ok": True}):
            self.assertEqual(main.api_benchmarks_fetch(), {"ok": True})

    def test_go_limits_parser_reads_base_prices_and_skips_free_models(self):
        payload = {
            "data": [
                {   # paid chat model with a full cost block
                    "id": "zz-last",
                    "go": {"monthly_usage": {"go": 200}},
                    "cost": {"text": {"input": {"price": 1.4, "cache": {"read": 0.26}},
                                      "output": {"price": 4.4}}},
                },
                {   # paid model whose long-context overrides must be ignored
                    "id": "aa-first",
                    "go": {"monthly_usage": {"go": 15}},
                    "cost": {"text": {
                        "input": {"price": 0.1, "cache": {"read": 0.01},
                                  "overrides": [{"when": {"context": {"gt": 100000}},
                                                 "price": 0.5}]},
                        "output": {"price": 0.5,
                                   "overrides": [{"when": {"context": {"gt": 100000}},
                                                  "price": 2.5}]}}},
                },
                {   # paid model without a cost block: row kept, prices null
                    "id": "mm-middle",
                    "go": {"monthly_usage": {"go": 60}},
                },
                {   # free model: skipped entirely
                    "id": "free-model",
                    "go": {"monthly_usage": {"go": 0}},
                    "cost": {"text": {"input": {"price": 1}, "output": {"price": 1}}},
                },
                {   # negative and boolean prices are not numbers: nulls
                    "id": "bad-prices",
                    "go": {"monthly_usage": {"go": 10}},
                    "cost": {"text": {"input": {"price": True},
                                      "output": {"price": -1}}},
                },
                {   # non-finite prices (inf/NaN parse under Python json): nulls
                    "id": "inf-prices",
                    "go": {"monthly_usage": {"go": 30}},
                    "cost": {"text": {"input": {"price": float("inf")},
                                      "output": {"price": float("nan")}}},
                },
                {   # paid entry without a usable id: skipped, never sorted
                    "id": None,
                    "go": {"monthly_usage": {"go": 80}},
                },
                {   # empty-string id is equally unusable: skipped
                    "id": "",
                    "go": {"monthly_usage": {"go": 90}},
                },
                "not-a-dict",
            ]
        }
        rows = core._go_limits_from_models_payload(payload)
        self.assertEqual([r["name"] for r in rows],
                         ["aa-first", "bad-prices", "inf-prices",
                          "mm-middle", "zz-last"])
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["zz-last"],
                         {"name": "zz-last", "input_price": 1.4,
                          "output_price": 4.4, "monthly": 200})
        self.assertEqual(by_name["aa-first"]["input_price"], 0.1)
        self.assertEqual(by_name["aa-first"]["output_price"], 0.5)
        self.assertIsNone(by_name["mm-middle"]["input_price"])
        self.assertIsNone(by_name["mm-middle"]["output_price"])
        self.assertEqual(by_name["mm-middle"]["monthly"], 60)
        self.assertIsNone(by_name["bad-prices"]["input_price"])
        self.assertIsNone(by_name["bad-prices"]["output_price"])
        self.assertIsNone(by_name["inf-prices"]["input_price"])
        self.assertIsNone(by_name["inf-prices"]["output_price"])
        self.assertEqual(by_name["inf-prices"]["monthly"], 30)
        self.assertNotIn("free-model", by_name)
        self.assertEqual(len(rows), 5)
        # Defensive shapes: non-dict payload and empty data are both fine.
        self.assertEqual(core._go_limits_from_models_payload(None), [])
        self.assertEqual(core._go_limits_from_models_payload({"data": []}), [])

    def test_legacy_limits_response_has_no_benchmark_attachment(self):
        with patch.object(core, "_read_opencode_go_key", return_value="dummy"), \
                patch.object(core, "_opencode_limits_cache",
                             {"data": {"models": [{"name": "fixture"}]},
                              "ts": NOW, "fail_ts": 0}), \
                patch.object(core.time, "time", return_value=NOW):
            result = core.get_opencode_go_limits()
        self.assertEqual(result, {"models": [{"name": "fixture"}]})
        self.assertNotIn("bench", json.dumps(result))
        self.assertNotIn("bench_metrics", result)


class AiServicesRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(core, "prime_counters"), \
                patch.object(core, "start_energy_tracker"), \
                patch.object(core, "start_desktop_monitor"):
            import main
        cls.client = TestClient(main.app)

    def test_ai_services_route_matches_the_shared_contract(self):
        payload = {
            "models": [],
            "metrics": [spec.as_dict() for spec in benchmarks.METRICS],
            "sources": {},
            "refreshing": False,
        }
        with patch.object(core, "get_ai_services", return_value=payload):
            response = self.client.get("/api/ai-services")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)

    def test_obsolete_aa_fetch_response_shape_is_gone(self):
        with patch.object(core, "fetch_benchmarks",
                          return_value={"ok": True, "refreshing": True,
                                        "started": True, "sources": []}):
            headers = {"Origin": "https://testserver", "X-Homelab-Request": "1"}
            response = self.client.post("/api/benchmarks/fetch", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["refreshing"], True)


if __name__ == "__main__":
    unittest.main()
