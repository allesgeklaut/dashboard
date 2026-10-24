import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import homelab_core as core  # noqa: E402


class MistralModelsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "litellm.yaml"
        self.config_setting = patch.object(core, "LITELLM_CONFIG_FILE", str(self.config_path))
        self.config_setting.start()
        self.cache = patch.object(core, "get_opencode_go_benchmarks", return_value=None)
        self.cache.start()
        self.opencode_key = patch.object(core, "_read_opencode_go_key", return_value=None)
        self.opencode_key.start()

    def tearDown(self):
        self.opencode_key.stop()
        self.cache.stop()
        self.config_setting.stop()
        self.temp_dir.cleanup()

    def write_config(self, text):
        self.config_path.write_text(text, encoding="utf-8")

    def test_features_gate_on_nonempty_path_even_when_unreadable(self):
        with patch.object(core, "LITELLM_CONFIG_FILE", ""):
            self.assertFalse(core.get_features()["mistral"])
            self.assertEqual(core.get_mistral_models(), {"error": "not configured"})

        self.assertTrue(core.get_features()["mistral"])
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
      model: mistral/glm-4
      api_key: config-secret
  - model_name: GLM
    litellm_params:
      model: mistral/another-model
  - model_name: audio
    litellm_params:
      model: openai/voxtral
""")
        with patch.object(core, "get_opencode_go_benchmarks", return_value={
            "fetched_at": 123.0,
            "models": {"glm4": {"intelligence": 42, "api_key": "cache-secret"}},
        }):
            with patch.object(core.requests, "get", side_effect=AssertionError("network read")):
                result = core.get_mistral_models()

        self.assertEqual(
            result["models"],
            [{"name": "GLM", "upstream_model": "mistral/glm-4",
              "bench": {"intelligence": 42}}],
        )
        self.assertEqual(result["bench_fetched_at"], 123.0)
        self.assertIn("bench_metrics", result)
        self.assertEqual(set(result), {"models", "bench_metrics", "bench_fetched_at"})
        self.assertEqual(set(result["models"][0]), {"name", "upstream_model", "bench"})
        self.assertNotIn("config-secret", json.dumps(result))
        self.assertNotIn("cache-secret", json.dumps(result))

    def test_verified_aliases_match_upstream_identity_not_public_alias(self):
        self.write_config("""
model_list:
  - model_name: mistral-small-4
    litellm_params:
      model: mistral/zai-glm-5-3
  - model_name: mistral-large-4
    litellm_params:
      model: mistral/zai-glm-5-3-flash
  - model_name: glm53
    litellm_params:
      model: mistral/mistral-large-4
  - model_name: glm53flash
    litellm_params:
      model: mistral/mistral-large-4-0
  - model_name: zai-glm-5-3
    litellm_params:
      model: mistral/mistral-small-2603
""")
        with patch.object(core, "get_opencode_go_benchmarks", return_value={
            "fetched_at": 456.0,
            "models": {
                "glm53": {"intelligence": 53},
                "glm53flash": {"intelligence": 54},
                "mistrallarge4preview": {"intelligence": 44},
                "mistralsmall4": {"intelligence": 40},
            },
        }):
            result = core.get_mistral_models()

        self.assertEqual(
            {row["name"]: row["bench"]["intelligence"] for row in result["models"]},
            {
                "mistral-small-4": 53,
                "mistral-large-4": 54,
                "glm53": 44,
                "glm53flash": 44,
                "zai-glm-5-3": 40,
            },
        )

    def test_known_small_latest_matches_but_unknown_and_large_latest_stay_unscored(self):
        self.write_config("""
model_list:
  - model_name: glm53
    litellm_params:
      model: mistral/unverified-model
  - model_name: mistral-small-4
    litellm_params:
      model: mistral/mistral-small-latest
  - model_name: mistral-large-4
    litellm_params:
      model: mistral/mistral-large-latest
""")
        with patch.object(core, "get_opencode_go_benchmarks", return_value={
            "fetched_at": 456.0,
            "models": {
                "glm53": {"intelligence": 53},
                "mistralsmall4": {"intelligence": 40},
                "mistrallarge4preview": {"intelligence": 44},
            },
        }):
            result = core.get_mistral_models()
        self.assertEqual(
            [row["bench"] for row in result["models"]],
            [{}, {"intelligence": 40}, {}],
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
        with patch.object(core, "get_opencode_go_benchmarks", return_value={
            "fetched_at": 456.0,
            "models": {
                "voxtralminilatest": {"intelligence": 50},
                "mistralembed": {"intelligence": 60},
            },
        }):
            result = core.get_mistral_models()
        self.assertEqual([model["bench"] for model in result["models"]], [{}, {}])

    def test_mistral_aliases_do_not_change_opencode_alias_matching(self):
        self.write_config("""
model_list:
  - model_name: beam-free
    litellm_params:
      model: mistral/beam-free
  - model_name: mistral-small-latest
    litellm_params:
      model: mistral/mistral-small-latest
""")
        cache = {"fetched_at": 123.0, "models": {
            "beam10": {"intelligence": 10},
            "mistralsmall4": {"intelligence": 40},
        }}
        with patch.object(core, "get_opencode_go_benchmarks", return_value=cache):
            mistral = core.get_mistral_models()
            opencode = core._attach_benchmarks({"models": [
                {"name": "beam-free"}, {"name": "mistral-small-latest"},
            ]})
        self.assertEqual(mistral["models"][0]["bench"], {})
        self.assertEqual(opencode["models"][0]["bench"], {"intelligence": 10})
        self.assertEqual(opencode["models"][1]["bench"], {})

    def test_malformed_cache_records_and_nonfinite_scores_are_safe_in_api(self):
        self.write_config("""
model_list:
  - model_name: GLM
    litellm_params:
      model: mistral/glm-5.3
""")
        with patch.object(core, "prime_counters"), \
                patch.object(core, "start_energy_tracker"), \
                patch.object(core, "start_desktop_monitor"):
            import main

        with TestClient(main.app) as client:
            with patch.object(core, "get_opencode_go_benchmarks", return_value={
                "fetched_at": float("nan"),
                "models": {"glm53": "bad score record"},
            }):
                bad_record = client.get("/api/mistral-models")
            with patch.object(core, "get_opencode_go_benchmarks", return_value={
                "fetched_at": 123.0,
                "models": {"glm53": {
                    "intelligence": 54.5,
                    "briefcase": float("nan"),
                    "gdpval": float("inf"),
                    "automation": float("-inf"),
                    "tb4": True,
                }},
            }):
                nonfinite_scores = client.get("/api/mistral-models")

        self.assertEqual(bad_record.status_code, 200)
        self.assertEqual(bad_record.json()["models"][0]["bench"], {})
        self.assertIsNone(bad_record.json()["bench_fetched_at"])
        self.assertEqual(nonfinite_scores.status_code, 200)
        self.assertEqual(
            nonfinite_scores.json()["models"][0]["bench"],
            {"intelligence": 54.5},
        )

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
        with patch.object(core, "_fetch_aa_benchmarks", return_value={"ok": True}) as fetch:
            self.assertEqual(core.fetch_benchmarks(), {"ok": True})
            fetch.assert_called_once_with()

        with patch.object(core, "_fetch_aa_benchmarks") as fetch:
            self.assertEqual(
                core.fetch_opencode_go_benchmarks(),
                {"ok": False, "error": "not configured"},
            )
            fetch.assert_not_called()

        with patch.object(core, "_read_opencode_go_key", return_value="test-key"):
            with patch.object(core, "_fetch_aa_benchmarks", return_value={"ok": True}) as fetch:
                self.assertEqual(core.fetch_opencode_go_benchmarks(), {"ok": True})
                fetch.assert_called_once_with()

    def test_shared_fetch_requires_either_supported_configuration(self):
        with patch.object(core, "LITELLM_CONFIG_FILE", ""):
            with patch.object(core, "_fetch_aa_benchmarks") as fetch:
                self.assertEqual(
                    core.fetch_benchmarks(), {"ok": False, "error": "not configured"}
                )
                fetch.assert_not_called()

        with patch.object(core, "_read_opencode_go_key", return_value="test-key"):
            with patch.object(core, "LITELLM_CONFIG_FILE", ""):
                with patch.object(core, "_fetch_aa_benchmarks", return_value={"ok": True}) as fetch:
                    self.assertEqual(core.fetch_benchmarks(), {"ok": True})
                    fetch.assert_called_once_with()

    def test_api_exposes_mistral_read_and_shared_fetch_routes(self):
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
        self.assertIn("POST", routes["/api/benchmarks/fetch"])
        self.assertIn("POST", routes["/api/opencode-go-benchmarks/fetch"])

        with patch.object(core, "get_mistral_models", return_value={"models": []}):
            self.assertEqual(main.api_mistral_models(), {"models": []})
        with patch.object(core, "fetch_benchmarks", return_value={"ok": True}):
            self.assertEqual(main.api_benchmarks_fetch(), {"ok": True})


if __name__ == "__main__":
    unittest.main()
