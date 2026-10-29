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


def entry(metric, model, value, *, date="2026-09-01", **kwargs):
    spec = benchmarks.METRIC_BY_KEY[metric]
    date_kind = kwargs.pop("date_kind", None)
    if date is not None and date_kind is None:
        if spec.source == "livebench":
            date_kind = benchmarks.DATE_KIND_BENCHMARK_RELEASE
        elif spec.source in ("harbor", "epoch"):
            date_kind = benchmarks.DATE_KIND_MODEL_RELEASE
    return benchmarks._make_entry(
        metric, model, value, evaluator=kwargs.pop("evaluator", "Fixture"),
        version=kwargs.pop("version", benchmarks.SOURCES[spec.source].version),
        date=date, date_kind=date_kind,
        agent=kwargs.pop("agent", None), effort=kwargs.pop("effort", None),
        configuration=kwargs.pop("configuration", None),
        ci=kwargs.pop("ci", None), metadata=kwargs.pop("metadata", None),
    )


def state_with(entries_by_source):
    state = {}
    for name in benchmarks.SOURCE_NAMES:
        items = entries_by_source.get(name)
        state[name] = {
            "data": {"entries": items, "meta": {}} if items is not None else None,
            "last_checked": NOW if items is not None else None,
            "last_success": NOW if items is not None else None,
            "error": None,
            "version": benchmarks.SOURCES[name].version,
            "source_updated": "2026-09-01" if items is not None else None,
            "validators": {},
            "stale": items is None,
        }
    return state


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
            self.assertFalse(core.get_features()["mistral"])
            self.assertFalse(core.get_features()["ai_services"])
            self.assertEqual(core.get_mistral_models(), {"error": "not configured"})

        self.assertTrue(core.get_features()["mistral"])
        self.assertTrue(core.get_features()["ai_services"])
        self.assertEqual(
            core.get_mistral_models(), {"error": "configuration unavailable"}
        )

    def test_only_mistral_upstream_routes_are_included_and_aliases_deduplicate(self):
        self.write_config("""
model_list:
  - model_name: mistral-looking-alias
    litellm_params:
      model: openai/glm-4
      api_key: config-secret
  - model_name: GLM
    litellm_params:
      model: mistral/glm-5.3
      api_key: config-secret
  - model_name: GLM
    litellm_params:
      model: mistral/glm-5.3
  - model_name: audio
    litellm_params:
      model: mistral/voxtral-mini-latest
""")
        state = state_with({"harbor": [entry("tb4", "GLM-5.3", 41.82)]})
        with patch.object(benchmarks, "read_state", return_value=state), \
                patch.object(core.requests, "get", side_effect=AssertionError("network read")):
            result = core.get_mistral_models()

        self.assertEqual(
            result["models"],
            [
                {"name": "GLM", "upstream_model": "mistral/glm-5.3",
                 "bench": {"tb4": 41.82}},
                {"name": "audio", "upstream_model": "mistral/voxtral-mini-latest",
                 "bench": {}},
            ],
        )
        self.assertEqual(result["bench_fetched_at"], NOW)
        self.assertIn("bench_metrics", result)
        self.assertEqual(set(result), {"models", "bench_metrics", "bench_fetched_at"})
        self.assertEqual(set(result["models"][0]), {"name", "upstream_model", "bench"})
        self.assertNotIn("config-secret", json.dumps(result))

    def test_exact_upstream_identity_matches_and_unknown_aliases_stay_unscored(self):
        self.write_config("""
model_list:
  - model_name: glm53
    litellm_params:
      model: mistral/glm-5.3
  - model_name: rolling
    litellm_params:
      model: mistral/mistral-small-latest
  - model_name: unrelated
    litellm_params:
      model: mistral/unverified-model
""")
        state = state_with({
            "harbor": [entry("tb4", "GLM-5.3", 41.82)],
            "epoch": [entry("eci", "GLM-5.3", 155.99)],
        })
        with patch.object(benchmarks, "read_state", return_value=state):
            result = core.get_mistral_models()
        self.assertEqual(
            [model["bench"] for model in result["models"]],
            [{"tb4": 41.82, "eci": 155.99}, {}, {}],
        )

    def test_non_chat_audio_and_embedding_routes_never_get_scores(self):
        self.write_config("""
model_list:
  - model_name: audio
    litellm_params:
      model: mistral/voxtral-mini-latest
  - model_name: embeddings
    litellm_params:
      model: mistral/mistral-embed
""")
        state = state_with({
            "harbor": [entry("tb4", "voxtral-mini-latest", 50.0),
                       entry("tb4", "mistral-embed", 60.0)],
        })
        with patch.object(benchmarks, "read_state", return_value=state):
            result = core.get_mistral_models()
        self.assertEqual([model["bench"] for model in result["models"]], [{}, {}])

    def test_legacy_response_ignores_artificial_analysis_cache_shape(self):
        self.write_config("""
model_list:
  - model_name: glm53
    litellm_params:
      model: mistral/glm-5.3
""")
        with patch.object(benchmarks, "read_state", return_value=state_with({})):
            result = core.get_mistral_models()
        self.assertEqual(result["models"][0]["bench"], {})
        self.assertIsNone(result["bench_fetched_at"])
        self.assertNotIn("intelligence", json.dumps(result))

    def test_configuration_errors_are_simple_and_do_not_include_path(self):
        self.assertEqual(
            core.get_mistral_models(), {"error": "configuration unavailable"}
        )

        for invalid in (
            "",                 # empty file parses as null
            "model_list: [",    # malformed YAML
            "[]",               # wrong root type
            "model_list: {}",   # wrong model_list type
            "model_list:\n  - model_name: missing-params\n",  # malformed row
        ):
            with self.subTest(config=invalid):
                self.write_config(invalid)
                self.assertEqual(
                    core.get_mistral_models(), {"error": "invalid configuration"}
                )

        self.write_config("model_list: []\n")
        self.assertEqual(core.get_mistral_models()["models"], [])

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

    def test_api_exposes_legacy_and_new_read_and_fetch_routes(self):
        with patch.object(core, "prime_counters"), \
                patch.object(core, "start_energy_tracker"), \
                patch.object(core, "start_desktop_monitor"):
            import main

        routes = {
            route.path: route.methods
            for route in main.app.routes
            if hasattr(route, "methods")
        }
        self.assertIn("GET", routes["/api/mistral-models"])
        self.assertIn("GET", routes["/api/ai-services"])
        self.assertIn("POST", routes["/api/benchmarks/fetch"])
        self.assertIn("POST", routes["/api/opencode-go-benchmarks/fetch"])

        with patch.object(core, "get_mistral_models", return_value={"models": []}):
            self.assertEqual(main.api_mistral_models(), {"models": []})
        with patch.object(core, "fetch_benchmarks", return_value={"ok": True}):
            self.assertEqual(main.api_benchmarks_fetch(), {"ok": True})

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
            "metrics": [spec.as_dict() for spec in benchmarks.METRICS
                        if spec.source != "deepswe"],
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
