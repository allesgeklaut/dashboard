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

    def test_prometheus_parser_keys_labelled_and_plain_lines_by_metric_name(self):
        parsed = core._parse_prometheus(
            "# TYPE llamacpp:uptime_seconds gauge\n"
            "llamacpp:uptime_seconds 12.5\n"
            'vllm:generation_tokens_total{model_name="qwen3.8-flash-next-iq3_s"} 26362\n'
            "unclosed{a=\"x\n"
        )
        self.assertEqual(parsed, {"llamacpp:uptime_seconds": 12.5,
                                  "vllm:generation_tokens_total": 26362.0})

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

    def test_strata_prefill_excludes_prefix_cache_hits(self):
        # 500 prompt tokens arrived, 400 of them were cache hits → only 100
        # tokens of real prefill over 10 s of TTFT time = 10 t/s, not 90.
        cur = {"vllm:generation_tokens_total": 10, "vllm:prompt_tokens_total": 1600,
               "vllm:prefix_cache_hits_total": 900,
               "vllm:time_to_first_token_seconds_sum": 12.0}
        prev = {"vllm:prompt_tokens_total": 1100, "vllm:prefix_cache_hits_total": 500,
                "vllm:time_to_first_token_seconds_sum": 2.0}
        with patch.object(core, "_LLM_AVG", {}):
            stats = core._vllm_stats(cur, prev, "strata", 2.0)
        self.assertEqual(stats["prompt_tps"], 10.0)
        self.assertEqual(stats["prompt_tps_avg"], 10.0)

    def test_strata_prefill_never_counts_cached_tokens_in_fallback(self):
        common = {"vllm:generation_tokens_total": 10}
        # fully cached window: every arriving prompt token was a hit → no rate
        cur = dict(common, **{"vllm:prompt_tokens_total": 1500,
                              "vllm:prefix_cache_hits_total": 1500,
                              "vllm:time_to_first_token_seconds_sum": 1.1})
        prev = dict(common, **{"vllm:prompt_tokens_total": 1000,
                               "vllm:prefix_cache_hits_total": 1000,
                               "vllm:time_to_first_token_seconds_sum": 1.0})
        with patch.object(core, "_LLM_AVG", {}):
            stats = core._vllm_stats(cur, prev, "strata", 2.0)
        self.assertIsNone(stats["prompt_tps"])
        self.assertIsNone(stats["prompt_tps_avg"])
        # cached prompt scheduled but first token not yet timed (ΔTTFT == 0):
        # the 1000 uncached tokens, never the 29000 hits, may yield a rate
        cur = {"vllm:prompt_tokens_total": 30000, "vllm:prefix_cache_hits_total": 29000,
               "vllm:time_to_first_token_seconds_sum": 5.0, "vllm:generation_tokens_total": 0}
        prev = {"vllm:prompt_tokens_total": 0, "vllm:prefix_cache_hits_total": 0,
                "vllm:time_to_first_token_seconds_sum": 5.0, "vllm:generation_tokens_total": 0}
        with patch.object(core, "_LLM_AVG", {}):
            stats = core._vllm_stats(cur, prev, "strata", 2.0)
        self.assertEqual(stats["prompt_tps"], 500.0)   # 1000 uncached / 2 s, not 15000

    def test_strata_metrics_map_onto_the_llama_card_fields(self):
        # Strata/vLLM label every series — the bare metric name must survive.
        metrics = Mock(text=(
            'vllm:generation_tokens_total{model_name="strata-fixture"} 500\n'
            'vllm:prompt_tokens_total{model_name="strata-fixture"} 1000\n'
            'vllm:prefix_cache_hits_total{model_name="strata-fixture"} 900\n'
            'vllm:prefix_cache_queries_total{model_name="strata-fixture"} 2000\n'
            'vllm:num_requests_running{model_name="strata-fixture"} 1\n'
            'vllm:num_requests_waiting{model_name="strata-fixture"} 0\n'
            'vllm:inter_token_latency_seconds_sum{model_name="strata-fixture"} 20\n'
            'vllm:inter_token_latency_seconds_count{model_name="strata-fixture"} 400\n'
            'vllm:time_to_first_token_seconds_sum{model_name="strata-fixture"} 5\n'
        ))
        with patch.object(core, "LLAMA_SERVER_URL", ""), \
                patch.object(core, "LLAMA_BOLT_URL", ""), patch.object(core, "LLAMA_COMPANION_URL", ""), \
                patch.object(core, "STRATA_URL", "https://strata.invalid"), \
                patch.object(core, "STRATA_API_KEY", "fixture-key"), \
                patch.object(core, "_LLM_LAST", {}), patch.object(core, "_LLM_COUNTERS", {}), \
                patch.object(core, "_LLM_AVG", {}), patch.object(core, "_LLM_SCRAPE", {"t": 0.0}), \
                patch.object(core, "_LLM_ACTIVE", {"name": None}), \
                patch.object(core.requests, "get", return_value=metrics) as get:
            result = core.get_llama()
        # Prometheus text must be requested explicitly and the key sent —
        # plain /metrics would return JSON (the Strata web monitor dialect).
        self.assertTrue(get.call_args.args[0].endswith("/metrics?format=prometheus"))
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer fixture-key")
        self.assertEqual(result["server"], "strata")
        self.assertEqual(result["phase"], "processing")   # running, no deltas on first scrape
        self.assertEqual(result["tokens_predicted"], 500)
        self.assertEqual(result["prompt_tokens"], 1000)
        self.assertEqual(result["requests_processing"], 1)
        self.assertEqual(result["gen_tps_avg"], 20.0)     # 400 itl samples / 20 s of latency
        self.assertIsNone(result["draft_accept"])         # no speculative-decode counters
        self.assertEqual(result["cache_hit"], 0.45)       # 900 hits / 2000 queries

    def test_strata_inkling_is_probed_when_the_qwen_tenant_is_down(self):
        metrics = Mock(text=(
            'vllm:generation_tokens_total{model_name="inkling-2-mini-sq-15"} 500\n'
            'vllm:prompt_tokens_total{model_name="inkling-2-mini-sq-15"} 1000\n'
            'vllm:num_requests_running{model_name="inkling-2-mini-sq-15"} 1\n'
            'vllm:num_requests_waiting{model_name="inkling-2-mini-sq-15"} 0\n'
        ))
        with patch.object(core, "LLAMA_SERVER_URL", ""), \
                patch.object(core, "LLAMA_BOLT_URL", ""), patch.object(core, "LLAMA_COMPANION_URL", ""), \
                patch.object(core, "STRATA_URL", "https://strata.invalid"), \
                patch.object(core, "STRATA_INKLING_URL", "https://strata-inkling.invalid"), \
                patch.object(core, "STRATA_API_KEY", "fixture-key"), \
                patch.object(core, "_LLM_LAST", {}), patch.object(core, "_LLM_COUNTERS", {}), \
                patch.object(core, "_LLM_AVG", {}), patch.object(core, "_LLM_SCRAPE", {"t": 0.0}), \
                patch.object(core, "_LLM_ACTIVE", {"name": None}), \
                patch.object(core.requests, "get", side_effect=[OSError("qwen down"), metrics]) as get:
            result = core.get_llama()
        self.assertEqual(get.call_count, 2)
        self.assertTrue(get.call_args.args[0].startswith(
            "https://strata-inkling.invalid/metrics?format=prometheus"))
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer fixture-key")
        self.assertEqual(result["server"], "strata-inkling")
        self.assertEqual(result["phase"], "processing")
        self.assertEqual(result["tokens_predicted"], 500)
        self.assertEqual(result["prompt_tokens"], 1000)

    def test_llama_feature_is_enabled_by_the_inkling_url_alone(self):
        with patch.object(core, "LLAMA_SERVER_URL", ""), \
                patch.object(core, "LLAMA_BOLT_URL", ""), \
                patch.object(core, "LLAMA_COMPANION_URL", ""), \
                patch.object(core, "STRATA_URL", ""), \
                patch.object(core, "STRATA_INKLING_URL", "https://strata-inkling.invalid"):
            self.assertTrue(core.get_features()["llama"])


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
            return Mock(status_code=200, json=Mock(return_value={
                "data": [{"id": "fixture", "go": {"monthly_usage": {"go": 100}}}]
            }))

        with patch.object(core, "_read_opencode_go_key", return_value="dummy"), \
                patch.object(core, "_opencode_limits_cache", {"data": None, "ts": 0, "fail_ts": 0}), \
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
        with patch.object(core, "LIFECYCLE_URL", "https://gateway.invalid"):
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
        with patch.object(core, "LIFECYCLE_URL", ""), \
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
