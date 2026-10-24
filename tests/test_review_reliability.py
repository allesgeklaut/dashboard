import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import homelab_core as core


class BenchmarkIngestionTests(unittest.TestCase):
    def test_scores_never_cross_model_boundaries(self):
        text = r'{\"shortName\":\"A\",\"intelligenceIndex\":1},{\"shortName\":\"B\",\"intelligenceIndex\":2,\"hle\":0.9,\"gdpvalNormalized\":0.4}'
        scores = core._parse_bench_page(text)
        core._attach_gdpval_elo(scores, text)
        self.assertEqual(scores["a"], {"intelligence": 1})
        self.assertEqual(scores["b"]["hle"], 0.9)
        self.assertEqual(scores["b"]["gdpval"], 1300)

    def test_null_scores_and_long_records_are_handled_without_borrowing(self):
        text = r'{\"shortName\":\"A\",\"hle\":null,' + " " * 4000 + r'\"briefcaseElo\":100},{\"shortName\":\"B\",\"hle\":0.5,\"briefcaseElo\":200}'
        self.assertNotIn("hle", core._parse_bench_page(text)["a"])
        self.assertEqual(core._parse_bench_page(text)["a"]["briefcase"], 100)
        self.assertEqual(core._parse_bench_extra_page(text)["a"]["briefcase"], 100)


class PersistenceAndLlamaTests(unittest.TestCase):
    def test_failed_serialization_preserves_previous_energy_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "energy.json"
            original = '{"daily":{"2026-10-22":123}}'
            target.write_text(original)
            with patch.object(core, "_ENERGY_FILE", str(target)), \
                    patch.object(core.json, "dump", side_effect=OSError("fixture failure")), \
                    self.assertLogs(core._log, level="WARNING"):
                core._save_energy()
            self.assertEqual(target.read_text(), original)
            self.assertEqual(list(Path(tmp).iterdir()), [target])

    def test_failed_replace_preserves_file_and_success_commits_new_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "energy.json"
            target.write_text("old")
            with patch.object(core, "_ENERGY_FILE", str(target)), patch.object(core, "_energy_data", {"value": 1}):
                with patch.object(core.os, "replace", side_effect=OSError("fixture failure")), \
                        self.assertLogs(core._log, level="WARNING"):
                    core._save_energy()
                self.assertEqual(target.read_text(), "old")
                core._save_energy()
            self.assertEqual(json.loads(target.read_text()), {"value": 1})
            self.assertEqual(list(Path(tmp).iterdir()), [target])

    def test_llama_resets_are_detected_after_starting_from_zero(self):
        key = core._LLM_TRACKED[0]
        with patch.object(core, "_LLM_COUNTERS", {}):
            flags = [core._llm_restarted({key: n}) for n in (0, 100, 10, 20, 0)]
        self.assertEqual(flags, [False, False, True, False, True])

    def test_llama_response_omits_configured_url_and_raw_exceptions(self):
        secret_url = "https://user:fixture-secret@llama.invalid"
        metrics = Mock(text="llamacpp:tokens_predicted_total 10\n")
        slots = Mock(json=lambda: [])
        with patch.object(core, "LLAMA_SERVER_URL", secret_url), \
                patch.object(core, "LLAMA_BOLT_URL", ""), patch.object(core, "LLAMA_COMPANION_URL", ""), \
                patch.object(core.requests, "get", side_effect=[metrics, slots]):
            response = core.get_llama()
        self.assertNotIn("url", response)
        self.assertNotIn("fixture-secret", json.dumps(response))
        with patch.object(core, "LLAMA_SERVER_URL", secret_url), \
                patch.object(core, "LLAMA_BOLT_URL", ""), patch.object(core, "LLAMA_COMPANION_URL", ""), \
                patch.object(core.requests, "get", side_effect=OSError(secret_url)):
            self.assertEqual(core.get_llama(), {"error": "llama servers unreachable"})

    def test_llama_restart_clears_averages_and_suppresses_cross_process_deltas(self):
        key = core._LLM_TRACKED[0]
        metrics = Mock(text=f"{key} 10\n")
        slots = Mock(json=lambda: [])
        with patch.object(core, "LLAMA_SERVER_URL", "https://fixture.invalid"), \
                patch.object(core, "LLAMA_BOLT_URL", ""), patch.object(core, "LLAMA_COMPANION_URL", ""), \
                patch.object(core, "_LLM_COUNTERS", {key: 100}), \
                patch.object(core, "_LLM_LAST", {key: 100}), \
                patch.object(core, "_LLM_ACTIVE", {"name": "llama-server"}), \
                patch.object(core, "_LLM_AVG", {"gen": 42}), \
                patch.object(core.requests, "get", side_effect=[metrics, slots]), \
                patch.object(core, "_live_rate", return_value=None) as rate:
            result = core.get_llama()
            self.assertEqual(core._LLM_AVG, {})
        self.assertIsNone(result["gen_tps_avg"])
        self.assertTrue(all(call.args[0] == {} for call in rate.call_args_list))


class CacheConcurrencyTests(unittest.TestCase):
    def test_usage_misses_are_coalesced(self):
        barrier = threading.Barrier(2)

        def caller():
            barrier.wait(timeout=3)
            return core.get_opencode_go_usage()

        def upstream(*_args, **_kwargs):
            time.sleep(0.03)  # simulate overlapping requests, never real network
            return Mock(status_code=200, json=lambda: {"usage": {"rolling": {"percent": 10}}})

        with patch.object(core, "_read_opencode_go_key", return_value="dummy"), \
                patch.object(core, "_opencode_cache", {"data": None, "ts": 0}), \
                patch.object(core.requests, "get", side_effect=upstream) as get:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: caller(), range(2)))
        self.assertEqual(get.call_count, 1)
        self.assertEqual(results[0], results[1])

    def test_limits_misses_are_coalesced(self):
        barrier = threading.Barrier(2)

        def caller():
            barrier.wait(timeout=3)
            return core.get_opencode_go_limits()

        def upstream(*_args, **_kwargs):
            time.sleep(0.03)
            return Mock(status_code=200, text="fixture")

        with patch.object(core, "_read_opencode_go_key", return_value="dummy"), \
                patch.object(core, "_opencode_limits_cache", {"data": None, "ts": 0, "fail_ts": 0}), \
                patch.object(core, "_parse_go_limits_mdx", return_value=[{"name": "fixture"}]), \
                patch.object(core, "get_opencode_go_benchmarks", return_value=None), \
                patch.object(core.requests, "get", side_effect=upstream) as get:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: caller(), range(2)))
        self.assertEqual(get.call_count, 1)
        self.assertEqual(results[0], results[1])


class IntegrationOutageTests(unittest.TestCase):
    """Configured-but-unreachable integrations must not look like empty data."""

    def test_portainer_outage_is_an_error_not_an_empty_host(self):
        ok_response = Mock(ok=True, json=lambda: [{"Id": "fixture" * 10, "Names": ["/fixture"],
                                                   "State": "running", "Status": "Up",
                                                   "Image": "fixture/img:1", "Ports": []}])
        with patch.object(core, "PORTAINER_API_KEY", "fixture-key"):
            with patch.object(core, "_eids", [1]), \
                    patch.object(core, "_portainer_request", side_effect=OSError("mock outage")):
                self.assertEqual(core.get_containers(), ([], "error"))
            with patch.object(core, "get_eids", return_value=[]):
                self.assertEqual(core.get_containers(), ([], "error"))
            with patch.object(core, "_eids", [1]), \
                    patch.object(core, "_portainer_request", return_value=ok_response):
                containers, source = core.get_containers()
                self.assertEqual((len(containers), source), (1, "portainer"))
            with patch.object(core, "_eids", [1, 2]), \
                    patch.object(core, "_portainer_request",
                                 side_effect=[ok_response, OSError("mock outage")]):
                containers, source = core.get_containers()
                self.assertEqual((len(containers), source), (1, "portainer"))
        with patch.object(core, "PORTAINER_API_KEY", ""), \
                patch.object(core, "get_eids", return_value=[]):
            self.assertEqual(core.get_containers(), ([], "none"))

    def test_ollama_outage_is_an_error_but_unconfigured_stays_empty(self):
        with patch.object(core, "OLLAMA_URL", "http://fixture.invalid"), \
                patch.object(core.requests, "get", side_effect=OSError("mock outage")):
            self.assertEqual(core.get_ollama_model(), {"error": "unavailable"})
        with patch.object(core, "OLLAMA_URL", "http://fixture.invalid"), \
                patch.object(core.requests, "get", return_value=Mock(ok=False)):
            self.assertEqual(core.get_ollama_model(), {"error": "unavailable"})
        with patch.object(core, "OLLAMA_URL", ""):
            self.assertEqual(core.get_ollama_model(), [])


class ContainerOutageRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(core, "prime_counters"), patch.object(core, "start_energy_tracker"), \
                patch.object(core, "start_desktop_monitor"):
            import main
        cls.client = TestClient(main.app)

    def test_api_reports_unreachable_portainer_instead_of_an_empty_list(self):
        with patch.object(core, "get_containers", return_value=([], "error")):
            self.assertEqual(self.client.get("/api/containers").json(),
                             {"error": "portainer unavailable"})
        with patch.object(core, "get_containers", return_value=(["fixture"], "portainer")):
            self.assertEqual(self.client.get("/api/containers").json(), ["fixture"])
