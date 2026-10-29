"""Regression tests for grouping LiteLLM inventory by upstream model + mode.

The inventory must not take a group's mode from whichever route happens to be
configured first.  Chat and embedding routes that share one upstream model ID
are separate entries, so stock benchmark scores stay attached to the chat
route regardless of configuration order.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import benchmarks  # noqa: E402
import homelab_core as core  # noqa: E402

NOW = 1_800_000_000.0


def tb4_entry(model="GPT-6.1 Sol", value=58.18):
    return benchmarks._make_entry(
        "tb4", model, value, evaluator="Fixture",
        version=benchmarks.SOURCES["harbor"].version,
        date="2026-09-01", date_kind=benchmarks.DATE_KIND_MODEL_RELEASE,
    )


def offline_state(entries_by_source):
    """A fully in-memory benchmark state; no feeds or store are touched."""
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


CHAT_FIRST = """
model_list:
  - model_name: sol
    litellm_params:
      model: openai/gpt-6.1-sol
  - model_name: sol-embed
    litellm_params:
      model: openai/gpt-6.1-sol
    model_info:
      mode: embedding
"""

EMBEDDING_FIRST = """
model_list:
  - model_name: sol-embed
    litellm_params:
      model: openai/gpt-6.1-sol
    model_info:
      mode: embedding
  - model_name: sol
    litellm_params:
      model: openai/gpt-6.1-sol
"""


class InventoryModeGroupingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / "litellm.yaml"
        self.patches = [
            patch.object(core, "LITELLM_CONFIG_FILE", str(self.config)),
            patch.object(core, "_read_opencode_go_key", return_value=None),
        ]
        for started in self.patches:
            started.start()

    def tearDown(self):
        for started in reversed(self.patches):
            started.stop()
        self.temp.cleanup()

    def write(self, text):
        self.config.write_text(text, encoding="utf-8")

    def inventory(self):
        inventory, error = core._read_litellm_inventory()
        self.assertIsNone(error)
        return inventory

    def test_shared_upstream_chat_and_embedding_are_order_independent(self):
        state = offline_state({"harbor": [tb4_entry()]})
        outcomes = {}
        for label, config_text in (("chat first", CHAT_FIRST),
                                   ("embedding first", EMBEDDING_FIRST)):
            with self.subTest(order=label):
                self.write(config_text)
                inventory = self.inventory()
                self.assertEqual(len(inventory), 2)
                by_mode = {row["mode"]: row for row in inventory}
                self.assertEqual(set(by_mode), {"chat", "embedding"})

                chat = by_mode["chat"]
                embedding = by_mode["embedding"]
                self.assertEqual(chat["upstream_model"], "openai/gpt-6.1-sol")
                self.assertEqual(embedding["upstream_model"], "openai/gpt-6.1-sol")
                self.assertEqual(chat["provider"], "openai")
                self.assertEqual(embedding["provider"], "openai")
                self.assertEqual(chat["routes"],
                                 [{"name": "sol", "provider": "openai"}])
                self.assertEqual(embedding["routes"],
                                 [{"name": "sol-embed", "provider": "openai"}])

                payload = benchmarks.build_payload(inventory, state)
                models = {model["mode"]: model for model in payload["models"]}
                self.assertEqual(models["chat"]["bench"]["tb4"], 58.18)
                self.assertNotIn("tb4", models["chat"]["missing"])
                self.assertEqual(models["embedding"]["bench"], {})
                self.assertEqual(models["embedding"]["missing"]["tb4"],
                                 "unsupported modality")
                self.assertTrue(all(value == "unsupported modality"
                                    for value in models["embedding"]["missing"].values()))

                outcomes[label] = {
                    "groups": sorted(
                        (row["upstream_model"], row["mode"],
                         tuple(sorted(route["name"] for route in row["routes"])))
                        for row in inventory
                    ),
                    "chat_bench": models["chat"]["bench"],
                    "embedding_bench": models["embedding"]["bench"],
                }
        self.assertEqual(outcomes["chat first"], outcomes["embedding first"])

    def test_identical_upstream_and_mode_aliases_still_group_together(self):
        config_text = """
model_list:
  - model_name: sol-a
    litellm_params:
      model: openai/gpt-6.1-sol
  - model_name: sol-b
    litellm_params:
      model: openai/gpt-6.1-sol
  - model_name: sol-a
    litellm_params:
      model: openai/gpt-6.1-sol
"""
        state = offline_state({"harbor": [tb4_entry()]})
        self.write(config_text)
        inventory = self.inventory()
        self.assertEqual(len(inventory), 1)
        model = inventory[0]
        self.assertEqual(model["name"], "sol-a")
        self.assertEqual(model["mode"], "chat")
        self.assertEqual(model["routes"], [
            {"name": "sol-a", "provider": "openai"},
            {"name": "sol-b", "provider": "openai"},
        ])
        payload = benchmarks.build_payload(inventory, state)
        self.assertEqual(payload["models"][0]["bench"]["tb4"], 58.18)

    def test_explicit_and_inferred_modes_join_only_matching_groups(self):
        self.write("""
model_list:
  - model_name: embed-explicit
    litellm_params:
      model: openai/text-embedding-3-large
    model_info:
      mode: Embedding
  - model_name: embed-inferred
    litellm_params:
      model: openai/text-embedding-3-large
  - model_name: explicit-completion
    litellm_params:
      model: custom/unknown-model
    model_info:
      mode: completion
""")
        inventory = self.inventory()
        self.assertEqual(len(inventory), 2)
        by_mode = {row["mode"]: row for row in inventory}
        self.assertEqual(set(by_mode), {"embedding", "completion"})
        embedding = by_mode["embedding"]
        self.assertEqual(embedding["upstream_model"],
                         "openai/text-embedding-3-large")
        self.assertEqual(embedding["routes"], [
            {"name": "embed-explicit", "provider": "openai"},
            {"name": "embed-inferred", "provider": "openai"},
        ])
        completion = by_mode["completion"]
        self.assertEqual(completion["upstream_model"], "custom/unknown-model")
        self.assertEqual(completion["routes"],
                         [{"name": "explicit-completion", "provider": "custom"}])

    def test_different_transport_prefixes_stay_separate_providers(self):
        self.write("""
model_list:
  - model_name: openai-sol
    litellm_params:
      model: openai/gpt-6.1-sol
  - model_name: azure-sol
    litellm_params:
      model: azure/gpt-6.1-sol
  - model_name: bare-sol
    litellm_params:
      model: gpt-6.1-sol
""")
        inventory = self.inventory()
        self.assertEqual(len(inventory), 3)
        by_name = {row["name"]: row for row in inventory}
        self.assertEqual(set(by_name), {"openai-sol", "azure-sol", "bare-sol"})
        self.assertEqual(by_name["openai-sol"]["provider"], "openai")
        self.assertEqual(by_name["azure-sol"]["provider"], "azure")
        self.assertEqual(by_name["bare-sol"]["provider"], "unknown")
        for row in inventory:
            self.assertEqual(row["mode"], "chat")
            self.assertEqual(row["routes"],
                             [{"name": row["name"], "provider": row["provider"]}])

    def test_only_safe_fields_are_read_and_exposed(self):
        self.write("""
model_list:
  - model_name: sol
    litellm_params:
      model: openai/gpt-6.1-sol
      api_key: sk-config-secret
      api_base: https://secret.invalid/v1
      api_version: "2026-01-01"
      extra_headers:
        Authorization: Bearer extra-secret
      timeout: 5
      max_retries: 2
    model_info:
      mode: chat
      id: internal-secret-id
      base_model: secret-base
""")
        inventory = self.inventory()
        serialized = json.dumps(inventory)
        for secret in ("sk-config-secret", "secret.invalid", "extra-secret",
                       "internal-secret-id", "secret-base", "api_key",
                       "api_base", "extra_headers"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(inventory[0]["routes"],
                         [{"name": "sol", "provider": "openai"}])
        self.assertEqual(
            set(inventory[0]),
            {"name", "upstream_model", "provider", "mode", "routes"},
        )

        state = offline_state({})
        with patch.object(benchmarks, "read_state", return_value=state), \
                patch.object(benchmarks, "ensure_refresh_scheduled") as ensure, \
                patch.object(benchmarks, "is_refreshing", return_value=False):
            payload = core.get_ai_services()
        ensure.assert_called_once_with()
        payload_text = json.dumps(payload)
        for secret in ("sk-config-secret", "secret.invalid", "extra-secret",
                       "internal-secret-id", "secret-base"):
            self.assertNotIn(secret, payload_text)

    def test_variant_guard_blocks_only_its_own_mode_group(self):
        self.write("""
model_list:
  - model_name: sol
    litellm_params:
      model: openai/gpt-6.1-sol
  - model_name: sol-abliterated
    litellm_params:
      model: openai/gpt-6.1-sol
  - model_name: sol-embed
    litellm_params:
      model: openai/gpt-6.1-sol
    model_info:
      mode: embedding
""")
        state = offline_state({"harbor": [tb4_entry()]})
        inventory = self.inventory()
        self.assertEqual(len(inventory), 2)
        payload = benchmarks.build_payload(inventory, state)
        models = {model["mode"]: model for model in payload["models"]}
        self.assertEqual(models["chat"]["bench"], {})
        self.assertEqual(models["chat"]["missing"]["tb4"], "unmatched identity")
        self.assertEqual(models["embedding"]["missing"]["tb4"],
                         "unsupported modality")

    def test_legacy_mistral_wrapper_keeps_mode_groups_separate(self):
        self.write("""
model_list:
  - model_name: glm53
    litellm_params:
      model: mistral/glm-5.3
      api_key: config-secret
  - model_name: mistral-embed
    litellm_params:
      model: mistral/glm-5.3
    model_info:
      mode: embedding
  - model_name: other
    litellm_params:
      model: openai/gpt-6.1-sol
""")
        state = offline_state({"harbor": [tb4_entry("GLM-5.3", 41.82)]})
        with patch.object(benchmarks, "read_state", return_value=state), \
                patch.object(benchmarks, "ensure_refresh_scheduled") as ensure:
            result = core.get_mistral_models()
        ensure.assert_not_called()
        self.assertEqual(set(result), {"models", "bench_metrics", "bench_fetched_at"})
        self.assertEqual(len(result["models"]), 2)
        self.assertEqual({model["upstream_model"] for model in result["models"]},
                         {"mistral/glm-5.3"})
        by_name = {model["name"]: model for model in result["models"]}
        self.assertEqual(set(by_name), {"glm53", "mistral-embed"})
        self.assertEqual(by_name["glm53"]["bench"]["tb4"], 41.82)
        self.assertEqual(by_name["mistral-embed"]["bench"], {})
        self.assertNotIn("config-secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
