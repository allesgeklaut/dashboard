"""Offline tests for benchmark adapters, snapshots, identities and the API contract."""
import asyncio
import contextlib
import http.server
import json
import math
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import benchmarks  # noqa: E402
import homelab_core as core  # noqa: E402


NOW = 1_800_000_000.0


# ── Fixtures ────────────────────────────────────────────────────────────────
_row_counter = 0


def _row_id():
    global _row_counter
    _row_counter += 1
    return f"00000000-0000-4000-8000-{_row_counter:012d}"


def harbor_row(label="GPT-6.1 Sol", accuracy=58.18, *, date="2026-09-29",
               effort="max", agent="Codex", row_id=None, ci=3.11, status="display",
               model_url="https://example.invalid/model", row_updated=None):
    row = {
        "id": row_id or _row_id(),
        "rank": 1,
        "metadata": {
            "date": date,
            "model_display": {"label": label, "url": model_url},
            "agent_display": {"label": agent, "url": "https://example.invalid/agent"},
            "reasoning_effort": effort,
        },
        "metrics": {"accuracy": accuracy, "accuracy_ci95_half_width": ci, "n_trials": 330},
        "status": status,
        "n_trials": 330,
    }
    if row_updated is not None:
        row["updated_at"] = row_updated
    return row


def harbor_response(rows, *, page=1, total=None, page_size=100, total_pages=None,
                    leaderboard=None):
    total = len(rows) if total is None else total
    total_pages = max(1, math.ceil(total / page_size)) if total_pages is None else total_pages
    payload = {
        "leaderboard": {
            "package": benchmarks.HARBOR_PACKAGE,
            "name": benchmarks.HARBOR_LEADERBOARD,
            "updated_at": "2026-10-23T00:29:02.387044+00:00",
            **(leaderboard or {}),
        },
        "rows": rows,
        "pagination": {"total": total, "page": page, "page_size": page_size,
                       "total_pages": total_pages},
    }
    return benchmarks.Response(200, {}, json.dumps(payload).encode())


def csv_response(header, rows):
    lines = [",".join(header)] + [",".join(str(value) for value in row) for row in rows]
    return benchmarks.Response(200, {}, ("\n".join(lines) + "\n").encode())


def epoch_response(rows=None):
    header = ["Model", "Display name", "eci", "eci_ci_low", "eci_ci_high", "date",
              "Organization", "Country (of organization)", "Model accessibility",
              "Accessibility group", "model_versions"]
    rows = rows if rows is not None else [
        ["Claude Opus 5.5", "Claude Opus 5.5", 168.83, 165.25, 173.95, "2026-09-22",
         "Anthropic", "United States of America", "API access", "Closed weights", ""],
        ["GPT-6.1 Sol", "GPT-6.1 Sol", 168.37, 165.0, 174.43, "2026-09-29",
         "OpenAI", "United States of America", "API access", "Closed weights", ""],
    ]
    return csv_response(header, rows)


LIVEBENCH_HEADER = ["model", "long_agentic", "reasoning", "coding", "browser",
                    "data_analysis", "research", "design"]
LIVEBENCH_ROWS = [
    ["claude-opus-5.5", 72.8, 92.2, 96.3, 88.4, 80.3, 85.0, 86.9],
    ["gpt-6.1-sol", 68.7, 92.6, 96.0, 93.3, 82.7, 83.5, 82.1],
    ["mistral-large-4", 8.8, 83.9, 60.0, 0.6, 76.5, 28.1, 65.6],
]


def livebench_categories():
    return {
        "Long Agentic": ["long_agentic"],
        "Reasoning": ["reasoning"],
        "Coding": ["coding"],
        "Browser": ["browser"],
        "Data Analysis": ["data_analysis"],
        "Research": ["research"],
        "Design": ["design"],
        "_overall_weights": {"Long Agentic": 2.0, "Coding": 1.5, "Design": 1.5},
    }


def livebench_categories_response(categories=None):
    return benchmarks.Response(200, {}, json.dumps(categories or livebench_categories()).encode())


# ── BenchLM fixtures ────────────────────────────────────────────────────────
# Test-only sentinel credential; never a real key.
BENCHLM_TEST_KEY = "benchlm-test-sentinel-0000notarealkey"


@contextlib.contextmanager
def benchlm_env(key):
    """Set BENCHLM_DATA_KEY for one test (key=None removes it); always restored."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BENCHLM_DATA_KEY", None)
        if key is not None:
            os.environ["BENCHLM_DATA_KEY"] = key
        yield


def benchlm_item(model, score, *, rank=None, interval=None, source_model_id=None):
    item = {"modelKey": model, "score": score}
    if source_model_id is not None:
        # Vals-capture row shape: the board publishes no modelKey, only a
        # publisher-prefixed sourceModelId.
        item["sourceModelId"] = source_model_id
    if rank is not None:
        item["rank"] = rank
    if interval is not None:
        item["scoreInterval90Lower"], item["scoreInterval90Upper"] = interval
    return item


def benchlm_page_dict(items, *, offset=0, next_offset=None, **overrides):
    payload = {
        "dataAsOf": "2026-10-28T09:15:00Z",
        "methodology": {"version": "benchalign-2026.10.28"},
        "items": items,
        "pagination": {"offset": offset, "limit": benchmarks.BENCHLM_PAGE_SIZE,
                       "nextOffset": next_offset},
    }
    payload.update(overrides)
    return payload


def benchlm_raw_response(payload):
    return benchmarks.Response(200, {}, json.dumps(payload).encode())


def benchlm_page(items, **overrides):
    return benchlm_raw_response(benchlm_page_dict(items, **overrides))


def benchlm_mcp_envelope(text, **overrides):
    envelope = {"jsonrpc": "2.0", "id": 1,
                "result": {"content": [{"type": "text", "text": text}]}}
    envelope.update(overrides)
    return benchmarks.Response(200, {}, json.dumps(envelope).encode())


def benchlm_mcp_response(items, *, data_as_of="2026-10-28T09:15:00Z"):
    return benchlm_mcp_envelope(
        json.dumps({"dataAsOf": data_as_of, "items": items}))


def benchlm_standard_responses():
    """One page per ranking surface plus one MCP response per TB4 board."""
    overall = [benchlm_item("gpt-6-1-sol", 91.5, rank=1, interval=(89.0, 94.0)),
               benchlm_item("glm-5-3", 80.25, rank=2)]
    coding = [benchlm_item("gpt-6-1-sol", 88.0, rank=1)]
    agentic = [benchlm_item("kimi-k2-7-code", 76.5, rank=1)]
    knowledge = [benchlm_item("mimo-v2-6-pro", 68.0, rank=1)]
    # Both TB4 boards cover gpt-6-1-sol; the higher value (vals-capture) wins.
    tb4_recorded = [benchlm_item("gpt-6-1-sol", 55.5)]
    tb4_vals = [benchlm_item("gpt-6-1-sol", 61.25)]
    return [benchlm_page(overall), benchlm_page(coding), benchlm_page(agentic),
            benchlm_page(knowledge),
            benchlm_mcp_response(tb4_recorded),
            benchlm_mcp_response(tb4_vals)]


# Snapshot of every sourceModelId the Vals TB4 board published on 2026-10-29
# (all 57 rows; that board carries no modelKey).  Pinned so the board-row
# conversion is exercised against the real identifier shapes — vendor
# prefixes, dots, uppercase, underscores, version suffixes — instead of a
# hand-picked sample; refresh deliberately if the board's ids ever change.
BENCHLM_VALS_SOURCE_MODEL_IDS = (
    "alibaba/qwen3.8-max",
    "alibaba/qwen3.8-max-internal",
    "anthropic/claude-fable-5",
    "anthropic/claude-fable-5-1",
    "anthropic/claude-haiku-5-5",
    "anthropic/claude-opus-4-8",
    "anthropic/claude-opus-5",
    "anthropic/claude-opus-5-5",
    "anthropic/claude-sonnet-5",
    "anthropic/claude-sonnet-5-5",
    "deepseek/deepseek-v4-flash-0731",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-pro-0813",
    "deepseek/deepseek-v4.1-flash",
    "deepseek/deepseek-v4.1-flash-1017",
    "fireworks/ember-1",
    "google/gemini-3.1-pro-preview",
    "google/gemini-3.5-flash",
    "google/gemini-3.7-flash",
    "google/gemini-3.8-flash",
    "google/gemini-4-argon",
    "google/gemini-4-carbon",
    "grok/grok-4.5",
    "grok/grok-4.6",
    "grok/grok-4.7",
    "inception/mercury-2.5",
    "inception/mercury-3",
    "kimi/kimi-k3",
    "kimi/kimi-k3.1-pro",
    "meta/muse-spark-open",
    "meta/muse-wonder",
    "meta/muse-wonder-full",
    "meta/muse_spark_1_2",
    "meta/muse_spark_1_3",
    "meta/muse_spark_1_3_max",
    "minimax/MiniMax-M3",
    "minimax/MiniMax-M3.1-flash",
    "mistralai/mistral-large-4",
    "openai/gpt-5.4-mini-2026-03-17",
    "openai/gpt-5.6-luna",
    "openai/gpt-5.6-sol",
    "openai/gpt-5.6-terra",
    "openai/gpt-6-astra",
    "openai/gpt-6-luna",
    "openai/gpt-6-sol",
    "openai/gpt-6.1-sol",
    "openai/gpt-6.1-terra",
    "reflection/Beam-1.0",
    "stepfun/step-5-preview",
    "tencent/hy4-preview",
    "thinkingmachines/inkling",
    "thinkingmachines/inkling-small",
    "xiaomi/mimo-v2.5-pro",
    "xiaomi/mimo-v2.6-flash",
    "xiaomi/mimo-v2.6-pro",
    "zai/glm-5.3",
    "zai/glm-5.3-flash",
)


def benchlm_tb4_responses(recorded_items, vals_items):
    """One ranking page per surface plus one MCP response per TB4 board."""
    return [benchlm_page([benchlm_item("m", 90.0)]),
            benchlm_page([benchlm_item("c", 80.0)]),
            benchlm_page([benchlm_item("a", 70.0)]),
            benchlm_page([benchlm_item("k", 60.0)]),
            benchlm_mcp_response(list(recorded_items)),
            benchlm_mcp_response(list(vals_items))]


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, json_body=None, headers=None, deadline):
        self.calls.append({"method": method, "url": url, "json": json_body,
                           "headers": dict(headers or {})})
        if not self.responses:
            raise AssertionError("unexpected extra request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def deadline(seconds=30.0):
    return benchmarks.Deadline(seconds)


_UNSET = object()


def entry(metric, model, value, *, date=_UNSET, source=None, **kwargs):
    spec = benchmarks.METRIC_BY_KEY[metric]
    # An explicit source may also be one of the metric's fallback sources:
    # BenchLM supplies tb4 rows although Harbor stays the primary source.
    assert source is None or source in (spec.source, *spec.fallback)
    resolved_source = source or spec.source
    if date is _UNSET:
        date = "2026-09-01"
    date_kind = kwargs.pop("date_kind", None)
    if date is not None and date_kind is None:
        if spec.source == "livebench":
            date_kind = benchmarks.DATE_KIND_BENCHMARK_RELEASE
        elif resolved_source == "benchlm":
            # BenchLM dates are board capture dates, never eval dates.
            date_kind = benchmarks.DATE_KIND_SOURCE_SNAPSHOT
        elif spec.source in ("harbor", "epoch"):
            date_kind = benchmarks.DATE_KIND_MODEL_RELEASE
    return benchmarks._make_entry(
        metric, model, value, evaluator=kwargs.pop("evaluator", "Fixture"),
        version=kwargs.pop("version", benchmarks.SOURCES[resolved_source].version),
        date=date, date_kind=date_kind,
        agent=kwargs.pop("agent", None), effort=kwargs.pop("effort", None),
        configuration=kwargs.pop("configuration", None),
        ci=kwargs.pop("ci", None), metadata=kwargs.pop("metadata", None),
    )


def snapshot(entries, *, version=None, updated="2026-09-01", validators=None, meta=None):
    if version is None:
        if entries:
            source = benchmarks.METRIC_BY_KEY[entries[0]["metric"]].source
            version = benchmarks.SOURCES[source].version
        else:
            version = benchmarks.SOURCES["harbor"].version
    return {"entries": entries, "version": version, "source_updated": updated,
            "validators": validators or {}, "meta": meta or {}}


def static_fetcher(value):
    def fetch(client, previous, deadline):
        if isinstance(value, Exception):
            raise value
        return value
    return fetch


def empty_state(now=NOW):
    return {
        name: {"data": None, "last_checked": None, "last_success": None, "error": None,
               "version": benchmarks.SOURCES[name].version, "source_updated": None,
               "validators": {}, "stale": True}
        for name in benchmarks.SOURCE_NAMES
    }


def make_state(entries_by_source, *, now=NOW, stale_by_source=None):
    state = empty_state(now)
    for name, entries in entries_by_source.items():
        if entries is None:
            continue
        state[name].update({
            "data": {"entries": entries, "meta": {}},
            "last_checked": now,
            "last_success": now,
            "error": None,
            "source_updated": "2026-09-01",
            "stale": bool((stale_by_source or {}).get(name, False)),
        })
    return state


def inventory_model(name, upstream, *, mode="chat", provider=None, routes=None):
    provider = provider or (upstream.split("/", 1)[0] if "/" in upstream else "unknown")
    return {"name": name, "upstream_model": upstream, "provider": provider,
            "mode": mode, "routes": routes or [{"name": name, "provider": provider}]}


def temp_store(**kwargs):
    directory = tempfile.TemporaryDirectory()
    path = str(Path(directory.name) / "benchmarks.json")
    kwargs.setdefault("clock", lambda: NOW)
    store = benchmarks.BenchmarkStore(path=path, **kwargs)
    return directory, store, path


# ── Harbor ──────────────────────────────────────────────────────────────────
class HarborAdapterTests(unittest.TestCase):
    def test_full_pagination_is_required_and_all_rows_are_retained(self):
        first = [harbor_row(label=f"Model {i}", row_id=f"id-{i}") for i in range(100)]
        second = [harbor_row(label=f"Model {i}", row_id=f"id-{i}") for i in range(100, 150)]
        client = StubClient([
            harbor_response(first, page=1, total=150),
            harbor_response(second, page=2, total=150),
        ])
        result = benchmarks.fetch_harbor(client, None, deadline())
        self.assertEqual(len(result["entries"]), 150)
        self.assertEqual([call["json"]["page"] for call in client.calls], [1, 2])
        self.assertEqual(result["version"], "4-0-0")
        self.assertEqual(result["source_updated"], "2026-10-23")
        self.assertEqual(result["meta"]["pages"], 2)

    def test_repeated_or_missing_or_inconsistent_pages_are_rejected(self):
        rows = [harbor_row(row_id=f"id-{i}") for i in range(2)]
        page_rows = [harbor_row(row_id="id-0"), harbor_row(row_id="id-1")]
        cases = {
            "repeated page number": [
                harbor_response(page_rows, page=1, total=4, page_size=2),
                harbor_response(page_rows, page=1, total=4, page_size=2),
            ],
            "rows missing": [
                harbor_response(page_rows, page=1, total=4, page_size=2),
                harbor_response(page_rows[:1], page=2, total=4, page_size=2),
            ],
            "totals changed": [
                harbor_response(page_rows, page=1, total=4, page_size=2),
                harbor_response(page_rows, page=2, total=5, page_size=2),
            ],
            "duplicate row ids": [
                # Full page 2 so the duplicate-id branch is reached, not the
                # shorter-row-count pagination rejection.
                harbor_response(page_rows, page=1, total=4, page_size=2),
                harbor_response([dict(page_rows[0]), harbor_row(row_id="id-2")],
                                page=2, total=4, page_size=2),
            ],
        }
        for label, responses in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(benchmarks.SourceError):
                    benchmarks.fetch_harbor(StubClient(responses), None, deadline())

    def test_empty_and_inconsistent_pagination_metadata_are_rejected(self):
        payloads = [
            harbor_response([], total=0, total_pages=1),
            harbor_response([harbor_row()], total=1, total_pages=2),
        ]
        bad_pagination = harbor_response([harbor_row()])
        bad_pagination = benchmarks.Response(200, {}, json.dumps({
            "leaderboard": {"package": benchmarks.HARBOR_PACKAGE,
                            "name": benchmarks.HARBOR_LEADERBOARD},
            "rows": [harbor_row()],
            "pagination": {"page": 1},
        }).encode())
        payloads.append(bad_pagination)
        for response in payloads:
            with self.subTest(response=response.body[:80]):
                with self.assertRaises(benchmarks.SourceError):
                    benchmarks.fetch_harbor(StubClient([response]), None, deadline())

    def test_ranges_dates_and_schema_are_strict(self):
        bad_rows = [
            harbor_row(accuracy=101),
            harbor_row(accuracy=-1),
            harbor_row(ci=-0.1),
            harbor_row(date="2026-9-1"),
            harbor_row(date="not-a-date"),
        ]
        for row in bad_rows:
            with self.subTest(row=row):
                with self.assertRaises(benchmarks.SourceError):
                    benchmarks.fetch_harbor(
                        StubClient([harbor_response([row])]), None, deadline())
        wrong_leaderboard = harbor_response(
            [harbor_row()], leaderboard={"name": "3-0-0"})
        with self.assertRaises(benchmarks.SourceError):
            benchmarks.fetch_harbor(StubClient([wrong_leaderboard]), None, deadline())

    def test_non_display_rows_are_skipped_but_normal_rows_survive(self):
        rows = [harbor_row(row_id="visible"), harbor_row(row_id="draft", status="draft")]
        result = benchmarks.fetch_harbor(StubClient([harbor_response(rows)]), None, deadline())
        self.assertEqual([item["model"] for item in result["entries"]], ["GPT-6.1 Sol"])

    def test_long_agent_and_effort_compose_within_configuration_bound(self):
        long_agent = "A" * 195
        row = harbor_row(agent=long_agent, effort="max")
        result = benchmarks.fetch_harbor(
            StubClient([harbor_response([row])]), None, deadline())
        entry = result["entries"][0]
        self.assertEqual(entry["agent"], long_agent)
        self.assertEqual(entry["effort"], "max")
        self.assertLessEqual(len(entry["configuration"]), 200)
        self.assertTrue(entry["configuration"].endswith(" / max"))

    def test_source_updated_never_uses_model_release_dates(self):
        rows = [harbor_row(date="2026-10-25")]
        response = harbor_response(rows, leaderboard={"updated_at": None})
        result = benchmarks.fetch_harbor(StubClient([response]), None, deadline())
        # The leaderboard has no validated feed timestamp, so source_updated is
        # null even though the row carries a model release date.
        self.assertIsNone(result["source_updated"])
        self.assertEqual(result["entries"][0]["date"], "2026-10-25")
        self.assertEqual(result["entries"][0]["date_kind"],
                         benchmarks.DATE_KIND_MODEL_RELEASE)

    def test_invalid_optional_feed_timestamps_become_null_not_failure(self):
        rows = [harbor_row(row_updated="not-a-date")]
        response = harbor_response(rows, leaderboard={"updated_at": "also-bad"})
        result = benchmarks.fetch_harbor(StubClient([response]), None, deadline())
        self.assertIsNone(result["source_updated"])
        self.assertEqual(len(result["entries"]), 1)

    def test_source_updated_falls_back_to_validated_row_mutation_timestamps(self):
        rows = [
            harbor_row(row_id="a", row_updated="2026-10-20T01:02:03Z"),
            harbor_row(row_id="b", row_updated="2026-10-22T04:05:06Z"),
        ]
        response = harbor_response(rows, leaderboard={"updated_at": None})
        result = benchmarks.fetch_harbor(StubClient([response]), None, deadline())
        self.assertEqual(result["source_updated"], "2026-10-22")
        self.assertNotEqual(result["source_updated"], max(
            item["date"] for item in result["entries"]))


# ── Epoch ───────────────────────────────────────────────────────────────────
class EpochAdapterTests(unittest.TestCase):
    def test_csv_is_normalized_and_validators_are_returned(self):
        response = epoch_response()
        response = benchmarks.Response(200, {"etag": 'W/"fixture"'}, response.body)
        result = benchmarks.fetch_epoch(StubClient([response]), None, deadline())
        self.assertEqual(len(result["entries"]), 2)
        first = result["entries"][0]
        self.assertEqual(first["metric"], "eci")
        self.assertEqual(first["value"], 168.83)
        self.assertEqual(first["ci"], {"low": 165.25, "high": 173.95, "half_width": 4.35})
        self.assertEqual(first["date"], "2026-09-22")
        self.assertEqual(first["date_kind"], benchmarks.DATE_KIND_MODEL_RELEASE)
        # The CSV has no feed-update timestamp; row dates are model releases.
        self.assertIsNone(result["source_updated"])
        self.assertEqual(result["validators"]["etag"], 'W/"fixture"')

    def test_last_modified_is_the_only_epoch_source_update_date(self):
        body = epoch_response().body
        response = benchmarks.Response(
            200, {"last-modified": "Thu, 15 Oct 2026 12:00:00 GMT"}, body)
        result = benchmarks.fetch_epoch(StubClient([response]), None, deadline())
        self.assertEqual(result["source_updated"], "2026-10-15")
        self.assertNotEqual(result["source_updated"], max(
            item["date"] for item in result["entries"]))

    def test_conditional_304_is_not_modified(self):
        client = StubClient([benchmarks.Response(304, {}, b"")])
        previous = {"validators": {"etag": 'W/"fixture"'}}
        result = benchmarks.fetch_epoch(client, previous, deadline())
        self.assertTrue(result["not_modified"])
        self.assertEqual(client.calls[0]["headers"]["If-None-Match"], 'W/"fixture"')

    def test_schema_ranges_and_ci_consistency_are_validated(self):
        header = ["Model", "eci", "date"]
        cases = [
            [["Model X", "not-a-number", "2026-09-01"]],
            [["Model X", 1001, "2026-09-01"]],
            [["Model X", 100, "2026-13-01"]],
            [["Model X", 100, "20260901"]],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                with self.assertRaises(benchmarks.SourceError):
                    benchmarks.fetch_epoch(
                        StubClient([csv_response(header, rows)]), None, deadline())
        bad_ci = csv_response(
            ["Model", "eci", "eci_ci_low", "eci_ci_high", "date"],
            [["Model X", 100, 120, 130, "2026-09-01"]])
        with self.assertRaises(benchmarks.SourceError):
            benchmarks.fetch_epoch(StubClient([bad_ci]), None, deadline())
        missing_header = csv_response(["Model", "date"], [["Model X", "2026-09-01"]])
        with self.assertRaises(benchmarks.SourceError):
            benchmarks.fetch_epoch(StubClient([missing_header]), None, deadline())
        empty = csv_response(["Model", "eci", "date"], [])
        with self.assertRaises(benchmarks.SourceError):
            benchmarks.fetch_epoch(StubClient([empty]), None, deadline())


# ── LiveBench ───────────────────────────────────────────────────────────────
class LiveBenchAdapterTests(unittest.TestCase):
    def test_table_and_categories_pair_is_parsed_when_opted_in(self):
        client = StubClient([
            benchmarks.Response(200, {"etag": 'W/"t"'}, csv_response(LIVEBENCH_HEADER, LIVEBENCH_ROWS).body),
            livebench_categories_response(),
        ])
        result = benchmarks.fetch_livebench(client, None, deadline())
        by_metric = {}
        for item in result["entries"]:
            by_metric.setdefault(item["metric"], {})[item["model"]] = item["value"]
        self.assertEqual(by_metric["livebench_coding"]["claude-opus-5.5"], 96.3)
        self.assertEqual(by_metric["livebench_long_agentic"]["mistral-large-4"], 8.8)
        self.assertEqual(result["source_updated"], "2026-10-25")
        for item in result["entries"]:
            self.assertEqual(item["date"], "2026-10-25")
            self.assertEqual(item["date_kind"], benchmarks.DATE_KIND_BENCHMARK_RELEASE)
        self.assertEqual(result["validators"]["table"]["etag"], 'W/"t"')
        self.assertEqual(result["meta"]["suite_weights"]["Coding"], 1.5)

    def test_both_conditional_304s_skip_the_source(self):
        client = StubClient([
            benchmarks.Response(304, {}, b""),
            benchmarks.Response(304, {}, b""),
        ])
        previous = {"validators": {"table": {"etag": 'W/"t"'},
                                   "categories": {"etag": 'W/"c"'}}}
        result = benchmarks.fetch_livebench(client, previous, deadline())
        self.assertTrue(result["not_modified"])
        self.assertEqual(client.calls[0]["headers"]["If-None-Match"], 'W/"t"')
        self.assertEqual(client.calls[1]["headers"]["If-None-Match"], 'W/"c"')

    def test_one_304_forces_a_consistent_unconditional_pair(self):
        table_body = csv_response(LIVEBENCH_HEADER, LIVEBENCH_ROWS).body
        cases = [
            ([benchmarks.Response(304, {}, b""), livebench_categories_response(),
              benchmarks.Response(200, {}, table_body)], [0, 1, 2]),
            ([benchmarks.Response(200, {}, table_body), benchmarks.Response(304, {}, b""),
              livebench_categories_response()], [0, 1, 2]),
        ]
        for responses, _ in cases:
            client = StubClient(responses)
            result = benchmarks.fetch_livebench(client, None, deadline())
            self.assertFalse(result.get("not_modified"))
            self.assertEqual(len(client.calls), 3)
            self.assertNotIn("If-None-Match", client.calls[2]["headers"])

    def test_second_304_for_unconditional_refetch_is_rejected(self):
        previous = {"validators": {"table": {"etag": 'W/"t"'},
                                   "categories": {"etag": 'W/"c"'}}}
        client = StubClient([
            benchmarks.Response(304, {}, b""),
            livebench_categories_response(),
            benchmarks.Response(304, {}, b""),
        ])
        with self.assertRaises(benchmarks.SourceError):
            benchmarks.fetch_livebench(client, previous, deadline())

    def test_categories_mismatch_and_range_drift_are_rejected(self):
        table_body = csv_response(LIVEBENCH_HEADER, LIVEBENCH_ROWS).body
        bad_categories = livebench_categories()
        del bad_categories["Design"]
        with self.assertRaises(benchmarks.SourceError):
            benchmarks.fetch_livebench(
                StubClient([benchmarks.Response(200, {}, table_body),
                            livebench_categories_response(bad_categories)]),
                None, deadline())
        bad_rows = [list(LIVEBENCH_ROWS[0])]
        bad_rows[0][3] = 100.1
        with self.assertRaises(benchmarks.SourceError):
            benchmarks.fetch_livebench(
                StubClient([benchmarks.Response(200, {}, csv_response(
                                LIVEBENCH_HEADER, bad_rows).body),
                            livebench_categories_response()]),
                None, deadline())

    def test_advertised_row_cap_keeps_expanded_entries_within_limit(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
        rows = [[f"model-{index}", *values]
                for index in range(benchmarks.MAX_LIVEBENCH_ROWS)]
        result = benchmarks.fetch_livebench(
            StubClient([
                benchmarks.Response(200, {}, csv_response(LIVEBENCH_HEADER, rows).body),
                livebench_categories_response(),
            ]), None, deadline())
        self.assertEqual(len(result["entries"]),
                         benchmarks.MAX_LIVEBENCH_ROWS * len(LIVEBENCH_HEADER[1:]))
        self.assertLessEqual(len(result["entries"]), benchmarks.MAX_ENTRIES)
        self.assertEqual(len(benchmarks._validated_entries(
            result["entries"], source="livebench")), len(result["entries"]))

        too_many = rows + [["model-overflow", *values]]
        with self.assertRaises(benchmarks.SourceError):
            benchmarks.fetch_livebench(
                StubClient([
                    benchmarks.Response(200, {}, csv_response(
                        LIVEBENCH_HEADER, too_many).body),
                    livebench_categories_response(),
                ]), None, deadline())

    def test_blank_suite_values_are_kept_as_not_evaluated(self):
        rows = [["claude-opus-5.5", 72.8, 92.2, "", 88.4, 80.3, 85.0, 86.9]]
        result = benchmarks.fetch_livebench(
            StubClient([benchmarks.Response(200, {}, csv_response(
                            LIVEBENCH_HEADER, rows).body),
                        livebench_categories_response()]), None, deadline())
        metrics = {item["metric"] for item in result["entries"]}
        self.assertNotIn("livebench_coding", metrics)
        self.assertIn("livebench_reasoning", metrics)


# ── BenchLM (BenchLM.ai Data API; opt-in, credentialed) ─────────────────────
class BenchLMAdapterTests(unittest.TestCase):
    def test_happy_path_reads_all_four_surfaces_then_both_mcp_boards(self):
        with benchlm_env(BENCHLM_TEST_KEY):
            client = StubClient(benchlm_standard_responses())
            result = benchmarks.fetch_benchlm(client, None, deadline())
        self.assertEqual([call["method"] for call in client.calls],
                         ["GET", "GET", "GET", "GET", "POST", "POST"])
        for index, surface in enumerate(
                surface for _metric, surface in benchmarks.BENCHLM_SURFACES):
            self.assertEqual(client.calls[index]["url"],
                             f"{benchmarks.BENCHLM_RANKINGS_URL}?surface={surface}"
                             f"&limit={benchmarks.BENCHLM_PAGE_SIZE}&offset=0")
        for board_index, (_label, board_id) in enumerate(benchmarks.BENCHLM_TB4_BOARDS):
            post = client.calls[4 + board_index]
            self.assertEqual(post["url"], benchmarks.BENCHLM_MCP_URL)
            self.assertEqual(post["json"], {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": benchmarks.BENCHLM_MCP_TOOL,
                           "arguments": {"boardId": board_id,
                                         "limit": benchmarks.BENCHLM_PAGE_SIZE}},
            })
        entries = {(item["metric"], item["model"]): item
                   for item in result["entries"]}
        self.assertEqual(set(entries), {
            ("benchalign_overall", "gpt-6-1-sol"),
            ("benchalign_overall", "glm-5-3"),
            ("benchalign_coding", "gpt-6-1-sol"),
            ("benchalign_agentic", "kimi-k2-7-code"),
            ("benchalign_knowledge", "mimo-v2-6-pro"),
            ("tb4", "gpt-6-1-sol"),
        })
        overall = entries[("benchalign_overall", "gpt-6-1-sol")]
        self.assertEqual(overall["value"], 91.5)
        self.assertEqual(overall["ci"], {"low": 89.0, "high": 94.0, "half_width": 2.5})
        self.assertEqual(overall["date"], "2026-10-28")
        self.assertEqual(overall["date_kind"], benchmarks.DATE_KIND_SOURCE_SNAPSHOT)
        self.assertEqual(overall["evaluator"], benchmarks.BENCHLM_COMPOSITE_EVALUATOR)
        self.assertEqual(overall["version"], "benchalign-2026.10.28")
        self.assertEqual(overall["metadata"], {"rank": 1})
        coding = entries[("benchalign_coding", "gpt-6-1-sol")]
        self.assertEqual(coding["value"], 88.0)
        self.assertIsNone(coding["ci"])
        self.assertEqual(coding["evaluator"], benchmarks.BENCHLM_COMPOSITE_EVALUATOR)
        agentic = entries[("benchalign_agentic", "kimi-k2-7-code")]
        self.assertEqual(agentic["value"], 76.5)
        self.assertEqual(agentic["evaluator"], benchmarks.BENCHLM_COMPOSITE_EVALUATOR)
        knowledge = entries[("benchalign_knowledge", "mimo-v2-6-pro")]
        self.assertEqual(knowledge["value"], 68.0)
        self.assertEqual(knowledge["evaluator"], benchmarks.BENCHLM_COMPOSITE_EVALUATOR)
        tb4 = entries[("tb4", "gpt-6-1-sol")]
        # The model is on both boards; the higher vals-capture value wins and
        # the entry carries the winning board's label and board id.
        self.assertEqual(tb4["value"], 61.25)
        self.assertEqual(tb4["evaluator"], benchmarks.BENCHLM_TB4_EVALUATOR)
        self.assertEqual(tb4["version"], benchmarks.BENCHLM_TB4_BOARDS[1][0])
        self.assertEqual(tb4["metadata"], {"board": benchmarks.BENCHLM_TB4_BOARDS[1][1]})
        self.assertEqual(tb4["date"], "2026-10-28")
        self.assertEqual(tb4["date_kind"], benchmarks.DATE_KIND_SOURCE_SNAPSHOT)
        self.assertEqual(result["version"], "benchalign-2026.10.28")
        self.assertEqual(result["source_updated"], "2026-10-28")
        self.assertEqual(result["validators"], {})
        self.assertEqual(result["meta"], {"pages": 4, "rows": 6})
        # The whole result also passes the store's per-source validation.
        self.assertEqual(
            benchmarks._validated_entries(result["entries"], source="benchlm"),
            result["entries"])

    def test_pagination_follows_next_offset_and_second_page_carries_offset_100(self):
        overall_p1 = [benchlm_item("model-a", 90.0)]
        overall_p2 = [benchlm_item("model-a", 89.0),  # duplicate row: skipped
                      benchlm_item("model-b", 85.0)]
        coding_p1 = [benchlm_item("model-a", 80.0)]
        coding_p2 = [benchlm_item("model-c", 70.0)]
        agentic_p1 = [benchlm_item("model-a", 75.0)]
        agentic_p2 = [benchlm_item("model-d", 65.0)]
        knowledge_p1 = [benchlm_item("model-a", 72.0)]
        knowledge_p2 = [benchlm_item("model-e", 62.0)]
        responses = [
            benchlm_page(overall_p1, offset=0, next_offset=100),
            benchlm_page(overall_p2, offset=100, next_offset=None),
            benchlm_page(coding_p1, offset=0, next_offset=100),
            benchlm_page(coding_p2, offset=100, next_offset=None),
            benchlm_page(agentic_p1, offset=0, next_offset=100),
            benchlm_page(agentic_p2, offset=100, next_offset=None),
            benchlm_page(knowledge_p1, offset=0, next_offset=100),
            benchlm_page(knowledge_p2, offset=100, next_offset=None),
            benchlm_mcp_response([benchlm_item("model-a", 60.0)]),
            # Second board repeats model-a with a higher value: it wins.
            benchlm_mcp_response([benchlm_item("model-a", 65.0)]),
        ]
        with benchlm_env(BENCHLM_TEST_KEY):
            client = StubClient(responses)
            result = benchmarks.fetch_benchlm(client, None, deadline())
        self.assertEqual(len(client.calls), 10)
        self.assertIn("surface=overall&limit=100&offset=0", client.calls[0]["url"])
        self.assertIn("surface=overall&limit=100&offset=100", client.calls[1]["url"])
        self.assertIn("surface=coding&limit=100&offset=0", client.calls[2]["url"])
        self.assertIn("surface=coding&limit=100&offset=100", client.calls[3]["url"])
        self.assertIn("surface=agentic&limit=100&offset=0", client.calls[4]["url"])
        self.assertIn("surface=agentic&limit=100&offset=100", client.calls[5]["url"])
        self.assertIn("surface=knowledge&limit=100&offset=0", client.calls[6]["url"])
        self.assertIn("surface=knowledge&limit=100&offset=100", client.calls[7]["url"])
        seen = {(item["metric"], item["model"]): item for item in result["entries"]}
        self.assertEqual(set(seen), {
            ("benchalign_overall", "model-a"), ("benchalign_overall", "model-b"),
            ("benchalign_coding", "model-a"), ("benchalign_coding", "model-c"),
            ("benchalign_agentic", "model-a"), ("benchalign_agentic", "model-d"),
            ("benchalign_knowledge", "model-a"), ("benchalign_knowledge", "model-e"),
            ("tb4", "model-a"),
        })
        # The repeated (overall, model-a) row kept its first occurrence.
        self.assertEqual(seen[("benchalign_overall", "model-a")]["value"], 90.0)
        # The tb4 row present on both boards kept the higher second-board value
        # and the winning board's provenance.
        self.assertEqual(seen[("tb4", "model-a")]["value"], 65.0)
        self.assertEqual(seen[("tb4", "model-a")]["version"],
                         benchmarks.BENCHLM_TB4_BOARDS[1][0])
        self.assertEqual(seen[("tb4", "model-a")]["metadata"],
                         {"board": benchmarks.BENCHLM_TB4_BOARDS[1][1]})
        self.assertEqual(result["meta"], {"pages": 8, "rows": 9})

    def test_read_cap_is_derived_from_the_surface_and_board_count(self):
        # Regression guard: the cap must scale with both BENCHLM_SURFACES and
        # BENCHLM_TB4_BOARDS so every surface and every TB4 board is always
        # reachable (2 surfaces hardcoded here once starved the remaining
        # surfaces of reads).
        self.assertEqual(
            benchmarks.BENCHLM_MAX_REQUESTS,
            len(benchmarks.BENCHLM_SURFACES) * benchmarks.BENCHLM_MAX_PAGES
            + len(benchmarks.BENCHLM_TB4_BOARDS))
        self.assertEqual(benchmarks.BENCHLM_MAX_REQUESTS, 14)

    def test_each_surface_is_requested_once_in_order_and_yields_its_metric(self):
        with benchlm_env(BENCHLM_TEST_KEY):
            client = StubClient(benchlm_standard_responses())
            result = benchmarks.fetch_benchlm(client, None, deadline())
        gets = [call for call in client.calls if call["method"] == "GET"]
        surfaces = [urlsplit(call["url"]).query for call in gets]
        expected = [f"surface={surface}&limit={benchmarks.BENCHLM_PAGE_SIZE}&offset=0"
                    for _metric, surface in benchmarks.BENCHLM_SURFACES]
        self.assertEqual(surfaces, expected)
        metric_keys = {item["metric"] for item in result["entries"]}
        for metric, _surface in benchmarks.BENCHLM_SURFACES:
            self.assertIn(metric, metric_keys)

    def test_read_cap_yields_exactly_fourteen_requests_and_never_raises(self):
        responses = []
        for surface_index in range(len(benchmarks.BENCHLM_SURFACES)):
            for page_index in range(benchmarks.BENCHLM_MAX_PAGES):
                offset = page_index * benchmarks.BENCHLM_PAGE_SIZE
                responses.append(benchlm_page(
                    [benchlm_item(f"model-{surface_index}-{page_index}",
                                  50.0 + page_index)],
                    offset=offset,
                    next_offset=offset + benchmarks.BENCHLM_PAGE_SIZE))
        responses.append(benchlm_mcp_response([benchlm_item("mcp-model", 42.0)]))
        responses.append(benchlm_mcp_response([benchlm_item("mcp-model", 43.0)]))
        responses.append(benchlm_page([benchlm_item("never-fetched", 1.0)]))
        with benchlm_env(BENCHLM_TEST_KEY):
            client = StubClient(responses)
            # An always-advancing nextOffset must not raise and must stop at
            # the hard cap: 12 GETs + 2 POSTs, no more.
            result = benchmarks.fetch_benchlm(client, None, deadline())
        self.assertEqual(len(client.calls), benchmarks.BENCHLM_MAX_REQUESTS)
        self.assertEqual(benchmarks.BENCHLM_MAX_REQUESTS, 14)
        self.assertEqual(sum(call["method"] == "GET" for call in client.calls), 12)
        self.assertEqual(sum(call["method"] == "POST" for call in client.calls), 2)
        self.assertEqual([call["json"]["params"]["arguments"]["boardId"]
                          for call in client.calls if call["method"] == "POST"],
                         [board_id for _label, board_id in benchmarks.BENCHLM_TB4_BOARDS])
        self.assertEqual(result["meta"], {"pages": 12, "rows": 13})
        self.assertEqual(len(result["entries"]), 13)
        tb4 = [item for item in result["entries"] if item["metric"] == "tb4"]
        self.assertEqual(len(tb4), 1)
        self.assertEqual(tb4[0]["value"], 43.0)  # higher second-board value won
        self.assertEqual(tb4[0]["version"], benchmarks.BENCHLM_TB4_BOARDS[1][0])

    def test_hitting_the_cap_mid_pagination_stops_reads_and_skips_the_mcp_posts(self):
        responses = [benchlm_page([benchlm_item(f"m{i}", 10.0 + i)],
                                  offset=i * 100, next_offset=(i + 1) * 100)
                     for i in range(4)]
        responses.append(benchlm_mcp_response([benchlm_item("mcp", 1.0)]))
        responses.append(benchlm_mcp_response([benchlm_item("mcp2", 2.0)]))
        with benchlm_env(BENCHLM_TEST_KEY), \
                patch.object(benchmarks, "BENCHLM_MAX_REQUESTS", 3):
            client = StubClient(responses)
            result = benchmarks.fetch_benchlm(client, None, deadline())
        self.assertEqual(len(client.calls), 3)  # cap reached: stop, never raise
        self.assertTrue(all(call["method"] == "GET" for call in client.calls))
        self.assertEqual(len(result["entries"]), 3)

    def test_tb4_row_on_both_boards_keeps_the_higher_value_and_winning_board(self):
        recorded = [benchlm_item("shared", 42.3), benchlm_item("tie", 50.0),
                    benchlm_item("recorded-only", 10.0)]
        vals = [benchlm_item("shared", 19.7), benchlm_item("tie", 50.0),
                benchlm_item("vals-only", 88.0)]
        responses = [benchlm_page([benchlm_item("m", 90.0)]),
                     benchlm_page([benchlm_item("c", 80.0)]),
                     benchlm_page([benchlm_item("a", 70.0)]),
                     benchlm_page([benchlm_item("k", 60.0)]),
                     benchlm_mcp_response(recorded),
                     benchlm_mcp_response(vals)]
        with benchlm_env(BENCHLM_TEST_KEY):
            client = StubClient(responses)
            result = benchmarks.fetch_benchlm(client, None, deadline())
        posts = [call for call in client.calls if call["method"] == "POST"]
        self.assertEqual([call["json"]["params"]["arguments"]["boardId"]
                          for call in posts],
                         [board_id for _label, board_id in benchmarks.BENCHLM_TB4_BOARDS])
        tb4 = {item["model"]: item for item in result["entries"]
               if item["metric"] == "tb4"}
        self.assertEqual(set(tb4), {"shared", "tie", "recorded-only", "vals-only"})
        # Higher value wins and carries the winning board's version/metadata.
        self.assertEqual(tb4["shared"]["value"], 42.3)
        self.assertEqual(tb4["shared"]["version"], benchmarks.BENCHLM_TB4_BOARDS[0][0])
        self.assertEqual(tb4["shared"]["metadata"],
                         {"board": benchmarks.BENCHLM_TB4_BOARDS[0][1]})
        # An exact tie keeps the earlier board in tuple order.
        self.assertEqual(tb4["tie"]["value"], 50.0)
        self.assertEqual(tb4["tie"]["version"], benchmarks.BENCHLM_TB4_BOARDS[0][0])
        self.assertEqual(tb4["tie"]["metadata"],
                         {"board": benchmarks.BENCHLM_TB4_BOARDS[0][1]})
        self.assertEqual(tb4["recorded-only"]["value"], 10.0)
        self.assertEqual(tb4["recorded-only"]["version"],
                         benchmarks.BENCHLM_TB4_BOARDS[0][0])
        self.assertEqual(result["meta"], {"pages": 4, "rows": 8})

    def test_tb4_row_only_on_the_second_board_is_still_returned(self):
        responses = [benchlm_page([benchlm_item("m", 90.0)]),
                     benchlm_page([benchlm_item("c", 80.0)]),
                     benchlm_page([benchlm_item("a", 70.0)]),
                     benchlm_page([benchlm_item("k", 60.0)]),
                     benchlm_mcp_response([benchlm_item("first-only", 30.0)]),
                     benchlm_mcp_response([benchlm_item("vals-only", 88.0)])]
        with benchlm_env(BENCHLM_TEST_KEY):
            client = StubClient(responses)
            result = benchmarks.fetch_benchlm(client, None, deadline())
        tb4 = {item["model"]: item for item in result["entries"]
               if item["metric"] == "tb4"}
        self.assertEqual(set(tb4), {"first-only", "vals-only"})
        self.assertEqual(tb4["vals-only"]["value"], 88.0)
        self.assertEqual(tb4["vals-only"]["version"], benchmarks.BENCHLM_TB4_BOARDS[1][0])
        self.assertEqual(tb4["vals-only"]["metadata"],
                         {"board": benchmarks.BENCHLM_TB4_BOARDS[1][1]})
        self.assertEqual(tb4["vals-only"]["evaluator"], benchmarks.BENCHLM_TB4_EVALUATOR)
        self.assertEqual(result["meta"], {"pages": 4, "rows": 6})

    def test_mcp_row_without_model_key_converts_source_model_id_to_canonical_slug(self):
        # The Vals board publishes no modelKey, only a publisher-prefixed
        # sourceModelId; the board parser converts it to BenchLM's canonical
        # slug (vendor prefix dropped, lowercased, "." and "_" -> "-").
        cases = [
            ("alibaba/qwen3.8-max", "qwen3-8-max"),
            ("mistralai/mistral-large-4", "mistral-large-4"),
            ("minimax/MiniMax-M3", "minimax-m3"),
            ("meta/muse_spark_1_3", "muse-spark-1-3"),
            ("reflection/Beam-1.0", "beam-1-0"),
            # Version suffixes name distinct variants: kept, never collapsed.
            ("deepseek/deepseek-v4.1-flash-1017", "deepseek-v4-1-flash-1017"),
            ("zai/glm-5.3", "glm-5-3"),
            ("xiaomi/mimo-v2.6-flash", "mimo-v2-6-flash"),
        ]
        for source_model_id, expected in cases:
            with self.subTest(source_model_id=source_model_id):
                responses = benchlm_tb4_responses(
                    [], [benchlm_item(None, 27.5, source_model_id=source_model_id)])
                with benchlm_env(BENCHLM_TEST_KEY):
                    client = StubClient(responses)
                    result = benchmarks.fetch_benchlm(client, None, deadline())
                tb4 = {item["model"]: item for item in result["entries"]
                       if item["metric"] == "tb4"}
                self.assertEqual(set(tb4), {expected})
                self.assertEqual(tb4[expected]["value"], 27.5)
                self.assertEqual(tb4[expected]["version"],
                                 benchmarks.BENCHLM_TB4_BOARDS[1][0])
                self.assertEqual(tb4[expected]["metadata"],
                                 {"board": benchmarks.BENCHLM_TB4_BOARDS[1][1]})

    def test_cross_board_rows_with_mixed_identifier_styles_merge_into_one_entry(self):
        # The recorded board keys the row canonically (qwen3-8-max) while the
        # Vals row only carries the publisher id (alibaba/qwen3.8-max); after
        # conversion both name the same model, so they merge into one entry
        # and the higher value wins with the winning board's provenance.
        responses = benchlm_tb4_responses(
            [benchlm_item("qwen3-8-max", 30.0)],
            [benchlm_item(None, 34.343, source_model_id="alibaba/qwen3.8-max")])
        with benchlm_env(BENCHLM_TEST_KEY):
            client = StubClient(responses)
            result = benchmarks.fetch_benchlm(client, None, deadline())
        tb4 = [item for item in result["entries"] if item["metric"] == "tb4"]
        self.assertEqual(len(tb4), 1)
        self.assertEqual(tb4[0]["model"], "qwen3-8-max")
        self.assertEqual(tb4[0]["value"], 34.343)
        self.assertEqual(tb4[0]["version"], benchmarks.BENCHLM_TB4_BOARDS[1][0])
        self.assertEqual(tb4[0]["metadata"],
                         {"board": benchmarks.BENCHLM_TB4_BOARDS[1][1]})

    def test_mcp_row_with_both_identifier_fields_null_is_rejected(self):
        rows = [
            ("modelKey null and sourceModelId null",
             {"modelKey": None, "sourceModelId": None, "score": 1.0}),
            ("modelKey null, sourceModelId absent", benchlm_item(None, 1.0)),
            ("modelKey blank, sourceModelId null",
             {"modelKey": "   ", "sourceModelId": None, "score": 1.0}),
        ]
        for label, row in rows:
            with self.subTest(label=label):
                responses = benchlm_tb4_responses([benchlm_item("r", 10.0)], [row])
                with benchlm_env(BENCHLM_TEST_KEY):
                    client = StubClient(responses)
                    with self.assertRaises(benchmarks.SourceError) as caught:
                        benchmarks.fetch_benchlm(client, None, deadline())
                self.assertEqual(str(caught.exception), "invalid response schema")
                self.assertNotIn(BENCHLM_TEST_KEY, str(caught.exception))

    def test_mcp_row_with_non_string_identifier_values_is_rejected(self):
        rows = [
            ("non-string sourceModelId (int)",
             benchlm_item(None, 1.0, source_model_id=123)),
            ("non-string sourceModelId (list)",
             benchlm_item(None, 1.0, source_model_id=["alibaba/qwen3.8-max"])),
            ("non-string modelKey", {"modelKey": 7, "score": 1.0}),
        ]
        for label, row in rows:
            with self.subTest(label=label):
                responses = benchlm_tb4_responses([benchlm_item("r", 10.0)], [row])
                with benchlm_env(BENCHLM_TEST_KEY):
                    client = StubClient(responses)
                    with self.assertRaises(benchmarks.SourceError) as caught:
                        benchmarks.fetch_benchlm(client, None, deadline())
                self.assertEqual(str(caught.exception), "invalid response schema")

    def test_canonical_model_key_is_used_verbatim_and_never_rewritten(self):
        # A present, non-empty modelKey is canonical already: dots, case, and
        # underscores survive untouched; only the sourceModelId fallback is
        # normalized.
        responses = benchlm_tb4_responses(
            [], [benchlm_item("some.model", 12.0),
                 benchlm_item("UPPER_Case.Key", 13.0)])
        with benchlm_env(BENCHLM_TEST_KEY):
            client = StubClient(responses)
            result = benchmarks.fetch_benchlm(client, None, deadline())
        tb4 = {item["model"] for item in result["entries"] if item["metric"] == "tb4"}
        self.assertEqual(tb4, {"some.model", "UPPER_Case.Key"})

    def test_board_model_key_covers_every_real_vals_identifier(self):
        # Every sourceModelId the Vals TB4 board actually published must
        # convert to a canonical slug: lowercase alphanumeric segments joined
        # by single hyphens, with no two distinct ids colliding.
        ids = BENCHLM_VALS_SOURCE_MODEL_IDS
        self.assertEqual(len(ids), 57)
        converted = {}
        for ident in ids:
            with self.subTest(source_model_id=ident):
                key = benchmarks._board_model_key({"sourceModelId": ident})
                self.assertIsNotNone(key)
                self.assertRegex(key, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
            converted[ident] = key
        # No two distinct publisher ids collapse onto one canonical key.
        self.assertEqual(len(set(converted.values())), len(ids))
        # Representative shapes, pinned one-to-one.
        expected = {
            "alibaba/qwen3.8-max": "qwen3-8-max",
            "mistralai/mistral-large-4": "mistral-large-4",
            "minimax/MiniMax-M3": "minimax-m3",       # uppercase
            "meta/muse_spark_1_3": "muse-spark-1-3",   # underscores
            "reflection/Beam-1.0": "beam-1-0",         # uppercase + dot + digits
            # Version suffix preserved, never collapsed to the base key.
            "deepseek/deepseek-v4.1-flash-1017": "deepseek-v4-1-flash-1017",
        }
        for ident, key in expected.items():
            self.assertEqual(converted[ident], key)
        # Version-suffixed variants stay distinct from their base keys.
        self.assertNotEqual(converted["deepseek/deepseek-v4.1-flash-1017"],
                            converted["deepseek/deepseek-v4.1-flash"])
        self.assertNotEqual(converted["kimi/kimi-k3.1-pro"],
                            converted["kimi/kimi-k3"])

    def test_oversized_items_lists_are_rejected_before_row_allocation(self):
        too_many = [benchlm_item(f"model-{i}", 50.0)
                    for i in range(benchmarks.BENCHLM_PAGE_SIZE + 1)]
        cases = [
            ("rankings page", [benchlm_page(too_many)], 1),
            ("mcp inner items", [
                benchlm_page([benchlm_item("m", 90.0)]),
                benchlm_page([benchlm_item("c", 80.0)]),
                benchlm_page([benchlm_item("a", 70.0)]),
                benchlm_page([benchlm_item("k", 60.0)]),
                benchlm_mcp_response(too_many),
                benchlm_mcp_response([benchlm_item("t", 50.0)]),
            ], 5),
        ]
        for label, responses, expected_calls in cases:
            with self.subTest(label=label):
                with benchlm_env(BENCHLM_TEST_KEY):
                    client = StubClient(responses)
                    with self.assertRaises(benchmarks.SourceError) as caught:
                        benchmarks.fetch_benchlm(client, None, deadline())
                self.assertEqual(str(caught.exception), "invalid response schema")
                self.assertEqual(len(client.calls), expected_calls)
                self.assertNotIn(BENCHLM_TEST_KEY, str(caught.exception))

    def test_max_entries_is_enforced_after_each_page_not_once_at_the_end(self):
        def page(offset):
            items = [benchlm_item(f"model-{offset}-{i}", 50.0)
                     for i in range(benchmarks.BENCHLM_PAGE_SIZE)]
            return benchlm_page(items, offset=offset,
                                next_offset=offset + benchmarks.BENCHLM_PAGE_SIZE)

        with benchlm_env(BENCHLM_TEST_KEY), \
                patch.object(benchmarks, "MAX_ENTRIES", 150):
            client = StubClient([page(0), page(100), page(200)])
            with self.assertRaises(benchmarks.SourceError) as caught:
                benchmarks.fetch_benchlm(client, None, deadline())
        self.assertEqual(str(caught.exception), "invalid response schema")
        # The per-page cap fired after the second page (100 + 100 > 150), so
        # the third request was never made.
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn(BENCHLM_TEST_KEY, str(caught.exception))

    def test_every_request_carries_the_bearer_key_and_mcp_protocol_headers(self):
        with benchlm_env(BENCHLM_TEST_KEY):
            client = StubClient(benchlm_standard_responses())
            benchmarks.fetch_benchlm(client, None, deadline())
            for call in client.calls:
                self.assertEqual(call["headers"]["Authorization"],
                                 f"Bearer {BENCHLM_TEST_KEY}")
            get_headers = client.calls[0]["headers"]
            self.assertEqual(get_headers["Accept"], "application/json")
            self.assertNotIn("MCP-Protocol-Version", get_headers)
            self.assertNotIn("Content-Type", get_headers)
            mcp_headers = client.calls[-1]["headers"]
            self.assertEqual(mcp_headers["Content-Type"], "application/json")
            self.assertEqual(mcp_headers["Accept"],
                             "application/json, text/event-stream")
            self.assertEqual(mcp_headers["MCP-Protocol-Version"],
                             benchmarks.BENCHLM_MCP_PROTOCOL_VERSION)
            # Direct header construction reads the key at request time only.
            self.assertEqual(benchmarks._benchlm_request_headers(), {
                "Authorization": f"Bearer {BENCHLM_TEST_KEY}",
                "Accept": "application/json",
            })
            mcp = benchmarks._benchlm_request_headers(mcp=True)
            self.assertEqual(mcp["Authorization"], f"Bearer {BENCHLM_TEST_KEY}")
            self.assertEqual(mcp["MCP-Protocol-Version"],
                             benchmarks.BENCHLM_MCP_PROTOCOL_VERSION)

    def test_missing_or_blank_key_fails_before_any_request(self):
        for key in (None, "", "   "):
            with self.subTest(key=key):
                with benchlm_env(key):
                    client = StubClient(benchlm_standard_responses())
                    with self.assertRaises(benchmarks.SourceError) as caught:
                        benchmarks.fetch_benchlm(client, None, deadline())
                self.assertEqual(str(caught.exception), "unable to start refresh")
                self.assertEqual(client.calls, [])  # zero requests recorded

    def test_http_401_is_sanitized_and_never_echoes_the_data_key(self):
        def responder(handler, _server):
            # A hostile upstream that echoes the credential back in the 401
            # body: the sanitized error must still be exactly "HTTP 401".
            echoed = handler.headers.get("Authorization", "")
            _write_response(handler, json.dumps({"detail": echoed}).encode(),
                            status=401)

        client = _LocalHttpClient(sleep=lambda _seconds: None)
        with _LocalFeedServer(responder) as server, \
                benchlm_env(BENCHLM_TEST_KEY), \
                patch.object(benchmarks, "BENCHLM_RANKINGS_URL",
                             server.base_url + "/v1/rankings/current"):
            with self.assertRaises(benchmarks.SourceError) as caught:
                benchmarks.fetch_benchlm(client, None, deadline())
        self.assertEqual(str(caught.exception), "HTTP 401")
        self.assertNotIn(BENCHLM_TEST_KEY, str(caught.exception))
        self.assertEqual(len(server.seen), 1)  # 401 is not retried
        self.assertEqual(server.seen[0]["headers"]["authorization"],
                         f"Bearer {BENCHLM_TEST_KEY}")

    def test_rankings_schema_failures_raise_sanitized_errors(self):
        good = [benchlm_item("gpt-6-1-sol", 91.5)]
        missing_as_of = benchlm_page_dict(good)
        del missing_as_of["dataAsOf"]
        schema = "invalid response schema"
        pagination = "invalid pagination"
        cases = [
            ("missing dataAsOf", benchlm_raw_response(missing_as_of), schema),
            ("null dataAsOf", benchlm_page(good, dataAsOf=None), schema),
            ("invalid dataAsOf", benchlm_page(good, dataAsOf="2026-13-40"), schema),
            ("short dataAsOf", benchlm_page(good, dataAsOf="2026-1-1"), schema),
            ("payload not a dict", benchmarks.Response(200, {}, b"[1,2]"), schema),
            ("methodology not a dict", benchlm_page(good, methodology=None), schema),
            ("version missing", benchlm_page(good, methodology={}), schema),
            ("items not a list", benchlm_page({"modelKey": "m"}), schema),
            ("item not a dict", benchlm_page([3]), schema),
            ("modelKey missing", benchlm_page([{"score": 50.0}]), schema),
            ("text score", benchlm_page([benchlm_item("m", "high")]), schema),
            ("bool score", benchlm_page([{"modelKey": "m", "score": True}]), schema),
            ("score above 100", benchlm_page([benchlm_item("m", 100.5)]), schema),
            ("negative score", benchlm_page([benchlm_item("m", -0.5)]), schema),
            ("rank zero", benchlm_page([benchlm_item("m", 50.0, rank=0)]), schema),
            ("wrong offset", benchlm_page(good, pagination={
                "offset": 7, "limit": 100, "nextOffset": None}), pagination),
            ("wrong limit", benchlm_page(good, pagination={
                "offset": 0, "limit": 50, "nextOffset": None}), pagination),
            ("offset not an int", benchlm_page(good, pagination={
                "offset": "0", "limit": 100, "nextOffset": None}), schema),
            ("negative nextOffset", benchlm_page(good, pagination={
                "offset": 0, "limit": 100, "nextOffset": -1}), pagination),
            ("bool nextOffset", benchlm_page(good, pagination={
                "offset": 0, "limit": 100, "nextOffset": True}), pagination),
        ]
        for label, response, message in cases:
            with self.subTest(label=label):
                with benchlm_env(BENCHLM_TEST_KEY):
                    client = StubClient([response])
                    with self.assertRaises(benchmarks.SourceError) as caught:
                        benchmarks.fetch_benchlm(client, None, deadline())
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(len(client.calls), 1)
                self.assertNotIn(BENCHLM_TEST_KEY, str(caught.exception))

    def test_malformed_mcp_envelopes_raise_sanitized_errors(self):
        schema = "invalid response schema"
        cases = [
            ("no result", {"jsonrpc": "2.0", "id": 1}, schema),
            ("result not a dict", {"result": []}, schema),
            ("no content", {"result": {}}, schema),
            ("empty content", {"result": {"content": []}}, schema),
            ("no text block",
             {"result": {"content": [{"type": "image", "data": "x"}]}}, schema),
            ("non-JSON text", benchlm_mcp_envelope("not json"),
             "invalid JSON response"),
            ("inner not a dict", benchlm_mcp_envelope("[1,2]"), schema),
            ("inner missing dataAsOf",
             benchlm_mcp_envelope(json.dumps({"items": []})), schema),
            ("inner items not a list",
             benchlm_mcp_envelope(json.dumps(
                 {"dataAsOf": "2026-10-28", "items": {}})), schema),
            ("inner bad score",
             benchlm_mcp_envelope(json.dumps(
                 {"dataAsOf": "2026-10-28",
                  "items": [{"modelKey": "m", "score": 200.0}]})), schema),
        ]
        for label, envelope, message in cases:
            with self.subTest(label=label):
                with benchlm_env(BENCHLM_TEST_KEY):
                    client = StubClient([
                        benchlm_page([benchlm_item("m", 90.0)]),
                        benchlm_page([benchlm_item("c", 80.0)]),
                        benchlm_page([benchlm_item("a", 70.0)]),
                        benchlm_page([benchlm_item("k", 60.0)]),
                        envelope if isinstance(envelope, benchmarks.Response)
                        else benchlm_raw_response(envelope),
                        benchlm_mcp_response([benchlm_item("t", 50.0)]),
                    ])
                    with self.assertRaises(benchmarks.SourceError) as caught:
                        benchmarks.fetch_benchlm(client, None, deadline())
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(len(client.calls), 5)  # 4 GETs then the first POST
                self.assertNotIn(BENCHLM_TEST_KEY, str(caught.exception))

    def test_empty_surfaces_and_empty_boards_raise_empty_feed(self):
        with benchlm_env(BENCHLM_TEST_KEY):
            client = StubClient([benchlm_page([]), benchlm_page([]),
                                 benchlm_page([]), benchlm_page([]),
                                 benchlm_mcp_response([]),
                                 benchlm_mcp_response([])])
            with self.assertRaises(benchmarks.SourceError) as caught:
                benchmarks.fetch_benchlm(client, None, deadline())
        self.assertEqual(str(caught.exception), "empty feed")
        self.assertEqual(len(client.calls), 6)

    def test_empty_ranking_surfaces_still_contribute_the_mcp_boards(self):
        with benchlm_env(BENCHLM_TEST_KEY):
            client = StubClient([
                benchlm_page([]),
                benchlm_page([]),
                benchlm_page([]),
                benchlm_page([]),
                benchlm_mcp_response([benchlm_item("mcp-model", 42.0)]),
                benchlm_mcp_response([benchlm_item("vals-model", 40.0)]),
            ])
            result = benchmarks.fetch_benchlm(client, None, deadline())
        self.assertEqual([(item["metric"], item["model"], item["value"])
                          for item in result["entries"]],
                         [("tb4", "mcp-model", 42.0), ("tb4", "vals-model", 40.0)])
        self.assertEqual(result["source_updated"], "2026-10-28")
        self.assertEqual(result["version"], "benchalign-2026.10.28")
        self.assertEqual(result["meta"], {"pages": 4, "rows": 2})

    def test_ci_interval_mapping(self):
        cases = [
            ((89.0, 94.0), {"low": 89.0, "high": 94.0, "half_width": 2.5}),
            ((0, 100), {"low": 0.0, "high": 100.0, "half_width": 50.0}),
            ((50, 50), {"low": 50.0, "high": 50.0, "half_width": 0.0}),
            ((94.0, 89.0), None),   # inverted bounds
            ((-1.0, 50.0), None),   # below range
            ((50.0, 101.0), None),  # above range
            (("89", "94"), None),   # non-numeric
            ((None, 94.0), None),   # partial
            ((True, 94.0), None),   # bool is not a number
        ]
        for interval, expected in cases:
            with self.subTest(interval=interval):
                item = benchlm_item("m", 90.0)
                item["scoreInterval90Lower"], item["scoreInterval90Upper"] = interval
                self.assertEqual(benchmarks._benchlm_ci(item), expected)
        self.assertIsNone(benchmarks._benchlm_ci({"modelKey": "m", "score": 90.0}))
        # Integration: a page row's interval lands on the entry's ci.
        with benchlm_env(BENCHLM_TEST_KEY):
            result = benchmarks.fetch_benchlm(StubClient([
                benchlm_page([benchlm_item("m", 90.0, interval=(88.0, 92.0))]),
                benchlm_page([benchlm_item("c", 80.0)]),
                benchlm_page([benchlm_item("a", 70.0)]),
                benchlm_page([benchlm_item("k", 60.0)]),
                benchlm_mcp_response([benchlm_item("t", 50.0)]),
                benchlm_mcp_response([benchlm_item("t", 51.0)]),
            ]), None, deadline())
        self.assertEqual(result["entries"][0]["ci"],
                         {"low": 88.0, "high": 92.0, "half_width": 2.0})
        self.assertIsNone(result["entries"][1]["ci"])
        # Both TB4 boards were read; "t" is merged to the higher second-board
        # value and carries that board's provenance.
        tb4 = [item for item in result["entries"] if item["metric"] == "tb4"]
        self.assertEqual(len(tb4), 1)
        self.assertEqual(tb4[0]["value"], 51.0)
        self.assertEqual(tb4[0]["version"], benchmarks.BENCHLM_TB4_BOARDS[1][0])
        self.assertEqual(tb4[0]["metadata"],
                         {"board": benchmarks.BENCHLM_TB4_BOARDS[1][1]})

    def test_validated_entries_accepts_benchlm_rows_and_rejects_foreign_metrics(self):
        tb4_row = entry("tb4", "gpt-6-1-sol", 55.5, source="benchlm",
                        evaluator=benchmarks.BENCHLM_TB4_EVALUATOR,
                        version=benchmarks.BENCHLM_TB4_BOARDS[0][0],
                        date="2026-10-28",
                        metadata={"board": benchmarks.BENCHLM_TB4_BOARDS[0][1]})
        self.assertEqual(tb4_row["date_kind"], benchmarks.DATE_KIND_SOURCE_SNAPSHOT)
        overall = entry("benchalign_overall", "gpt-6-1-sol", 91.5, source="benchlm",
                        evaluator=benchmarks.BENCHLM_COMPOSITE_EVALUATOR,
                        version="benchalign-2026.10.28", date="2026-10-28")
        coding = entry("benchalign_coding", "glm-5-3", 77.0, source="benchlm",
                       evaluator=benchmarks.BENCHLM_COMPOSITE_EVALUATOR,
                       version="benchalign-2026.10.28", date="2026-10-28")
        agentic = entry("benchalign_agentic", "glm-5-3", 66.0, source="benchlm",
                        evaluator=benchmarks.BENCHLM_COMPOSITE_EVALUATOR,
                        version="benchalign-2026.10.28", date="2026-10-28")
        knowledge = entry("benchalign_knowledge", "glm-5-3", 61.0, source="benchlm",
                          evaluator=benchmarks.BENCHLM_COMPOSITE_EVALUATOR,
                          version="benchalign-2026.10.28", date="2026-10-28")
        cleaned = benchmarks._validated_entries(
            [tb4_row, overall, coding, agentic, knowledge], source="benchlm")
        self.assertEqual([item["metric"] for item in cleaned],
                         ["tb4", "benchalign_overall", "benchalign_coding",
                          "benchalign_agentic", "benchalign_knowledge"])
        # A benchlm row for a metric it neither owns nor falls back for (eci,
        # livebench_*) is rejected.
        for foreign in (entry("eci", "Model", 100.0),
                        entry("livebench_coding", "model", 80.0)):
            with self.subTest(metric=foreign["metric"]):
                with self.assertRaises(benchmarks.SourceError):
                    benchmarks._validated_entries([foreign], source="benchlm")

    def test_validated_entries_rejects_benchalign_rows_under_harbor(self):
        # The reverse masquerade direction: a benchalign row even wearing
        # Harbor's own pinned version must be rejected when the snapshot is
        # validated as a harbor one.
        row = benchmarks._make_entry(
            "benchalign_overall", "gpt-6-1-sol", 91.5,
            evaluator=benchmarks.BENCHLM_COMPOSITE_EVALUATOR,
            version=benchmarks.SOURCES["harbor"].version,
            date="2026-09-01", date_kind=benchmarks.DATE_KIND_MODEL_RELEASE)
        with self.assertRaises(benchmarks.SourceError) as caught:
            benchmarks._validated_entries([row], source="harbor")
        self.assertEqual(str(caught.exception), "invalid response schema")

    def test_index_metric_range_is_enforced_on_the_reload_path(self):
        good = entry("benchalign_overall", "gpt-6-1-sol", 91.5, source="benchlm",
                     evaluator=benchmarks.BENCHLM_COMPOSITE_EVALUATOR,
                     version="benchalign-2026.10.28", date="2026-10-28")
        # A tampered cache cannot smuggle an out-of-range index value through
        # _sanitize_entry/_validated_entries (the fetch path already enforces
        # 0-100 on BenchLM scores).
        for bad_value in (5000.0, 1000.5, -1.0):
            with self.subTest(value=bad_value):
                tampered = dict(good, value=bad_value)
                with self.assertRaises(benchmarks.SourceError) as caught:
                    benchmarks._sanitize_entry(tampered)
                self.assertEqual(str(caught.exception), "invalid response schema")
                with self.assertRaises(benchmarks.SourceError):
                    benchmarks._validated_entries([tampered], source="benchlm")
        # The 0-1000 index bound itself is accepted on the reload path.
        self.assertEqual(
            benchmarks._sanitize_entry(dict(good, value=1000.0))["value"], 1000.0)
        # eci is an index metric too and keeps the same bound.
        with self.assertRaises(benchmarks.SourceError):
            benchmarks._sanitize_entry(
                dict(entry("eci", "Model", 100.0), value=1001.0))


class BenchLMSecurityTests(unittest.TestCase):
    def test_benchlm_endpoints_are_fixed_https_and_allowlisted(self):
        for url in (benchmarks.BENCHLM_RANKINGS_URL, benchmarks.BENCHLM_MCP_URL):
            benchmarks.HttpClient._validate_url(url)  # must not raise
            parts = urlsplit(url)
            self.assertEqual(parts.scheme, "https")
            self.assertIn(parts.hostname, benchmarks.ALLOWED_HOSTS)

    def test_data_key_never_leaks_into_result_cache_or_payload(self):
        with benchlm_env(BENCHLM_TEST_KEY):
            result = benchmarks.fetch_benchlm(
                StubClient(benchlm_standard_responses()), None, deadline())
        # (a) the fetch result itself
        self.assertNotIn(BENCHLM_TEST_KEY, json.dumps(result))
        # (b) the persisted cache file bytes after a real store refresh
        directory, store, path = temp_store(fetchers={
            "benchlm": benchmarks.fetch_benchlm,
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("harbor", "epoch", "livebench")},
        }, client_factory=lambda: StubClient(benchlm_standard_responses()))
        try:
            with benchlm_env(BENCHLM_TEST_KEY), \
                    patch.object(benchmarks, "BENCHLM_ENABLED", True):
                self.assertTrue(store.refresh_source("benchlm"))
            self.assertNotIn(BENCHLM_TEST_KEY.encode(), Path(path).read_bytes())
            state = store.read_state(now=NOW)
            self.assertNotIn(BENCHLM_TEST_KEY, json.dumps(state))
            self.assertEqual(len(state["benchlm"]["data"]["entries"]), 6)
            reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: NOW)
            reloaded_state = reloaded.read_state(now=NOW)
            self.assertNotIn(BENCHLM_TEST_KEY, json.dumps(reloaded_state))
            self.assertEqual(len(reloaded_state["benchlm"]["data"]["entries"]), 6)
            # (c) the API payload built from the reloaded state
            payload = benchmarks.build_payload(
                [inventory_model("gpt-6.1-sol", "openai/gpt-6.1-sol")],
                reloaded_state, benchlm_enabled=True)
            self.assertNotIn(BENCHLM_TEST_KEY, json.dumps(payload))
            model = payload["models"][0]
            self.assertEqual(model["bench"]["benchalign_overall"], 91.5)
            self.assertEqual(model["bench"]["tb4"], 61.25)  # max across TB4 boards
            self.assertEqual(model["results"]["tb4"][0]["source"], "benchlm")
            self.assertEqual(model["results"]["tb4"][0]["version"],
                             benchmarks.BENCHLM_TB4_BOARDS[1][0])
        finally:
            directory.cleanup()

    def test_source_enabled_gate_follows_the_module_flag_and_explicit_override(self):
        with patch.object(benchmarks, "BENCHLM_ENABLED", False):
            self.assertFalse(benchmarks._source_enabled("benchlm"))
            self.assertTrue(benchmarks._source_enabled("benchlm",
                                                        benchlm_enabled=True))
        with patch.object(benchmarks, "BENCHLM_ENABLED", True):
            self.assertTrue(benchmarks._source_enabled("benchlm"))
            self.assertFalse(benchmarks._source_enabled("benchlm",
                                                         benchlm_enabled=False))
        # Without an override the import-time env gate decides.
        self.assertEqual(benchmarks._source_enabled("benchlm"),
                         benchmarks.BENCHLM_ENABLED)

    def test_disabled_benchlm_is_never_fetched_by_the_store(self):
        called = []

        def fetcher(*args):
            called.append(True)
            return snapshot([entry("benchalign_overall", "gpt-6-1-sol", 91.5,
                                   source="benchlm", version="benchalign-2026.10.28",
                                   date="2026-10-28")])

        directory = tempfile.TemporaryDirectory()
        store = benchmarks.BenchmarkStore(
            path=str(Path(directory.name) / "b.json"), clock=lambda: NOW,
            fetchers={"benchlm": fetcher})
        try:
            with patch.object(benchmarks, "BENCHLM_ENABLED", False):
                self.assertNotIn("benchlm", store._enabled_sources())
                self.assertFalse(store.refresh_source("benchlm"))
            with patch.object(benchmarks, "BENCHLM_ENABLED", True):
                self.assertIn("benchlm", store._enabled_sources())
                self.assertTrue(store.refresh_source("benchlm"))
            self.assertEqual(len(called), 1)  # only the enabled run fetched
        finally:
            directory.cleanup()

    def test_sanitize_source_record_round_trip_and_tamper_rejection(self):
        good = entry("benchalign_overall", "gpt-6-1-sol", 91.5, source="benchlm",
                     evaluator=benchmarks.BENCHLM_COMPOSITE_EVALUATOR,
                     version="benchalign-2026.10.28", date="2026-10-28")
        tb4 = entry("tb4", "glm-5-3", 60.0, source="benchlm",
                    evaluator=benchmarks.BENCHLM_TB4_EVALUATOR,
                    version=benchmarks.BENCHLM_TB4_BOARDS[0][0], date="2026-10-28",
                    metadata={"board": benchmarks.BENCHLM_TB4_BOARDS[0][1]})
        record = {
            "schema_version": benchmarks.SOURCE_SCHEMA_VERSION,
            "version": "benchalign-2026.10.28",
            "last_checked": NOW, "last_success": NOW,
            "source_updated": "2026-10-28T09:15:00Z",
            "error": None, "validators": {},
            "data": {"entries": [good, tb4], "meta": {"pages": 2, "rows": 2}},
        }
        state = benchmarks._sanitize_source_record("benchlm", record)
        self.assertIsNotNone(state)
        # The dynamic methodology version is accepted, not pinned.
        self.assertEqual(state["version"], "benchalign-2026.10.28")
        self.assertEqual(state["source_updated"], "2026-10-28")
        self.assertEqual(state["last_success"], NOW)
        self.assertIsNone(state["error"])
        self.assertEqual(len(state["data"]["entries"]), 2)
        self.assertEqual(state["data"]["entries"][1]["version"],
                         benchmarks.BENCHLM_TB4_BOARDS[0][0])
        # The tb4 row's board id survives the sanitize round trip.
        self.assertEqual(state["data"]["entries"][1]["metadata"],
                         {"board": benchmarks.BENCHLM_TB4_BOARDS[0][1]})
        self.assertEqual(state["data"]["meta"], {"pages": 2, "rows": 2})
        tampered = [
            ("wrong schema_version", {**record, "schema_version": 99}),
            ("missing version",
             {key: value for key, value in record.items() if key != "version"}),
            ("control char in version", {**record, "version": "bad\nversion"}),
            ("foreign metric row",
             {**record, "data": {"entries": [entry("eci", "X", 100.0)], "meta": {}}}),
            ("empty entries", {**record, "data": {"entries": [], "meta": {}}}),
            ("oversized timestamp", {**record, "last_success": 10 ** 400}),
            ("bad source_updated", {**record, "source_updated": "not-a-date"}),
        ]
        for label, bad in tampered:
            with self.subTest(label=label):
                self.assertIsNone(benchmarks._sanitize_source_record("benchlm", bad))
        self.assertIsNone(benchmarks._sanitize_source_record("benchlm", "nope"))


# ── HTTP and provenance security ────────────────────────────────────────────
class _LocalHttpClient(benchmarks.HttpClient):
    """Test seam: allow the loopback URL used by the transport tests only.

    ``benchmarks.HttpClient._validate_url`` (the production fixed HTTPS
    allowlist) is deliberately not modified or weakened by this subclass.
    """

    @staticmethod
    def _validate_url(url: str) -> None:  # noqa: ARG004 - intentional override
        return None


def _write_response(handler, body=b"{}", *, status=200, headers=()):
    handler.send_response(status)
    for key, value in headers:
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


def _transport_responder(handler, server):
    """Loopback-only responder for one transport behaviour per path."""
    if handler.path == "/ok":
        _write_response(handler, b'{"ok":true}')
    elif handler.path == "/big":
        _write_response(handler, b"x" * 11)
    elif handler.path == "/redirect":
        handler.send_response(302)
        handler.send_header("Location", "https://evil.invalid/")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
    elif handler.path == "/flaky":
        server.state["flaky"] = server.state.get("flaky", 0) + 1
        if server.state["flaky"] < 3:
            _write_response(handler, b"", status=503)
        else:
            _write_response(handler, b'{"ok":true}')
    elif handler.path == "/always-503":
        _write_response(handler, b"", status=503)
    elif handler.path == "/slowheaders":
        # The absolute deadline must include waiting for response headers.
        time.sleep(0.6)
        _write_response(handler, b"{}")
    elif handler.path == "/slowbody":
        # A true slow drip: 1 byte every 60 ms (the reported regression) for a
        # total that would take 2.4 s.  A transport that buffers to a fixed
        # chunk size before checking the clock cannot return near 120 ms.
        handler.send_response(200)
        handler.send_header("Content-Length", "40")
        handler.end_headers()
        try:
            for _ in range(40):
                handler.wfile.write(b"x")
                handler.wfile.flush()
                time.sleep(0.06)
        except (BrokenPipeError, ConnectionResetError):
            return
    elif handler.path == "/partial":
        # Declares more body than it sends, then half-closes: httpx must map
        # this to a sanitized network error, never an exception with the URL.
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        handler.wfile.write(b"12345")
        handler.wfile.flush()
        handler.close_connection = True
        try:
            handler.connection.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    else:
        _write_response(handler, b"", status=404)


class _LocalFeedServer:
    """Daemon-only loopback HTTP server used only by these transport tests."""

    def __init__(self, responder):
        self.responder = responder
        self.seen = []
        self.state = {}
        self._httpd = None
        self._thread = None

    def __enter__(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence test output
                pass

            def do_GET(self):
                outer._handle(self)

            def do_POST(self):
                outer._handle(self)

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def _handle(self, handler):
        length = int(handler.headers.get("content-length") or 0)
        body = handler.rfile.read(length) if length else b""
        self.seen.append({
            "method": handler.command,
            "path": handler.path,
            "body": body,
            "headers": {key.lower(): value
                        for key, value in handler.headers.items()},
        })
        try:
            self.responder(handler, self)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def __exit__(self, *exc_info):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(2)
        return False


class HttpSecurityTests(unittest.TestCase):
    def test_only_fixed_public_https_endpoints_are_accepted(self):
        for url in (benchmarks.HARBOR_ENDPOINT, benchmarks.EPOCH_CSV_URL,
                    benchmarks.LIVEBENCH_CSV_URL, benchmarks.LIVEBENCH_CATEGORIES_URL):
            benchmarks.HttpClient._validate_url(url)
        rejected = [
            "http://epoch.ai/data/eci_scores.csv",
            "https://evil.invalid/data.csv",
            "https://user:secret@epoch.ai/data/eci_scores.csv",
            "https://epoch.ai:8443/data/eci_scores.csv",
            "ftp://epoch.ai/data/eci_scores.csv",
            "https://epoch.ai.evil.invalid/data.csv",
        ]
        for url in rejected:
            with self.subTest(url=url):
                with self.assertRaises(benchmarks.SourceError):
                    benchmarks.HttpClient._validate_url(url)

    def test_httpx_client_is_hardened_scoped_and_closed_per_request(self):
        created = []
        closed = []
        real_client = httpx.AsyncClient

        class SpyClient(real_client):
            def __init__(self, *args, **kwargs):
                created.append(kwargs)
                super().__init__(*args, **kwargs)

            async def __aexit__(self, *exc_info):
                try:
                    return await super().__aexit__(*exc_info)
                finally:
                    closed.append(True)

        client = _LocalHttpClient(sleep=lambda _seconds: None)
        with _LocalFeedServer(_transport_responder) as server, \
                patch.object(benchmarks.httpx, "AsyncClient", SpyClient):
            first = client.request("GET", server.base_url + "/ok",
                                   deadline=deadline())
            second = client.request("GET", server.base_url + "/ok",
                                    deadline=deadline())
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(len(created), 2)  # one client per synchronous request
        self.assertEqual(closed, [True, True])
        for kwargs in created:
            self.assertIs(kwargs["trust_env"], False)
            self.assertIs(kwargs["follow_redirects"], False)
            self.assertIs(kwargs["verify"], True)
            self.assertIsNone(kwargs["timeout"])
            self.assertNotIn("auth", kwargs)
        for request in server.seen:
            self.assertEqual(request["headers"]["user-agent"], benchmarks.USER_AGENT)
            self.assertEqual(request["headers"]["accept"],
                             "application/json, text/csv;q=0.9")
            self.assertNotIn("authorization", request["headers"])
            self.assertNotIn("cookie", request["headers"])

    def test_post_json_body_reaches_the_fixed_endpoint(self):
        client = _LocalHttpClient(sleep=lambda _seconds: None)
        with _LocalFeedServer(_transport_responder) as server:
            result = client.request(
                "POST", server.base_url + "/ok",
                json_body={"package": "terminal-bench/terminal-bench", "page": 2},
                deadline=deadline())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(server.seen[0]["method"], "POST")
        self.assertEqual(json.loads(server.seen[0]["body"]),
                         {"package": "terminal-bench/terminal-bench", "page": 2})

    def test_redirects_are_rejected_without_being_followed(self):
        client = _LocalHttpClient(sleep=lambda _seconds: None)
        with _LocalFeedServer(_transport_responder) as server:
            with self.assertRaises(benchmarks.SourceError) as caught:
                client.request("GET", server.base_url + "/redirect",
                               deadline=deadline())
        self.assertEqual(str(caught.exception), "upstream redirect rejected")
        self.assertEqual(len(server.seen), 1)  # Location was never requested

    def test_retries_use_bounded_backoff_and_sanitized_errors(self):
        client = _LocalHttpClient(sleep=lambda _seconds: None)
        with _LocalFeedServer(_transport_responder) as server:
            result = client.request("GET", server.base_url + "/flaky",
                                    deadline=deadline())
            self.assertEqual(result.status_code, 200)
            self.assertEqual([item["path"] for item in server.seen],
                             ["/flaky", "/flaky", "/flaky"])
            with self.assertRaises(benchmarks.SourceError) as caught:
                client.request("GET", server.base_url + "/always-503",
                               deadline=deadline())
            self.assertEqual(str(caught.exception), "HTTP 503")
        self.assertNotIn("127.0.0.1", str(caught.exception))
        self.assertNotIn("http://", str(caught.exception))

        # A refused loopback connection must become a fixed network error.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            refused_port = probe.getsockname()[1]
        with self.assertRaises(benchmarks.SourceError) as network:
            client.request("GET", f"http://127.0.0.1:{refused_port}/feed",
                           deadline=deadline())
        self.assertEqual(str(network.exception), "network error")
        self.assertNotIn("127.0.0.1", str(network.exception))

    def test_body_budget_and_zero_deadline_are_enforced(self):
        client = _LocalHttpClient(max_bytes=10, sleep=lambda _seconds: None)
        with _LocalFeedServer(_transport_responder) as server:
            with self.assertRaises(benchmarks.SourceError) as caught:
                client.request("GET", server.base_url + "/big",
                               deadline=deadline())
        self.assertEqual(str(caught.exception), "response too large")
        self.assertEqual(benchmarks.MAX_BODY_BYTES, 4 * 1024 * 1024)
        with self.assertRaises(benchmarks.SourceError) as expired:
            client.request("GET", "http://127.0.0.1:1/feed", deadline=deadline(0))
        self.assertEqual(str(expired.exception), "deadline exceeded")

    def test_slow_drip_body_respects_the_absolute_deadline_and_cleans_up(self):
        closed = threading.Event()
        real_client = httpx.AsyncClient

        class SpyClient(real_client):
            async def __aexit__(self, *exc_info):
                try:
                    return await super().__aexit__(*exc_info)
                finally:
                    closed.set()

        client = _LocalHttpClient(sleep=lambda _seconds: None)
        with _LocalFeedServer(_transport_responder) as server, \
                patch.object(benchmarks.httpx, "AsyncClient", SpyClient):
            started = time.monotonic()
            with self.assertRaises(benchmarks.SourceError) as caught:
                client.request("GET", server.base_url + "/slowbody",
                               deadline=deadline(0.12))
            elapsed = time.monotonic() - started
        self.assertEqual(str(caught.exception), "deadline exceeded")
        # The body would take 2.4 s to finish; a bounded tolerance proves the
        # absolute clock, not an inactivity timeout or a larger chunk size,
        # stopped it.
        self.assertLess(elapsed, 0.6)
        self.assertTrue(closed.wait(2), "cancelled response/client was not closed")

    def test_slow_headers_respect_the_absolute_deadline(self):
        closed = threading.Event()
        real_client = httpx.AsyncClient

        class SpyClient(real_client):
            async def __aexit__(self, *exc_info):
                try:
                    return await super().__aexit__(*exc_info)
                finally:
                    closed.set()

        client = _LocalHttpClient(sleep=lambda _seconds: None)
        with _LocalFeedServer(_transport_responder) as server, \
                patch.object(benchmarks.httpx, "AsyncClient", SpyClient):
            started = time.monotonic()
            with self.assertRaises(benchmarks.SourceError) as caught:
                client.request("GET", server.base_url + "/slowheaders",
                               deadline=deadline(0.12))
            elapsed = time.monotonic() - started
        self.assertEqual(str(caught.exception), "deadline exceeded")
        self.assertLess(elapsed, 0.6)
        self.assertTrue(closed.wait(2), "cancelled response/client was not closed")

    def test_blocked_dns_lookup_cannot_extend_the_synchronous_deadline(self):
        release = threading.Event()
        entered = threading.Event()
        real_getaddrinfo = socket.getaddrinfo

        def blocked_getaddrinfo(*args, **kwargs):
            entered.set()
            release.wait(5)
            return real_getaddrinfo(*args, **kwargs)

        client = _LocalHttpClient(sleep=lambda _seconds: None)
        try:
            with patch("socket.getaddrinfo", side_effect=blocked_getaddrinfo):
                started = time.monotonic()
                with self.assertRaises(benchmarks.SourceError) as caught:
                    client.request("GET", "http://resolver.invalid/feed",
                                   deadline=deadline(0.15))
                elapsed = time.monotonic() - started
            self.assertTrue(entered.is_set(), "the resolver was never called")
            self.assertEqual(str(caught.exception), "deadline exceeded")
            # asyncio.run would wait for the blocked resolver during loop
            # shutdown; the shared daemon bridge must return at the deadline.
            self.assertLess(elapsed, 0.8)
        finally:
            release.set()
        loop_threads = [thread for thread in threading.enumerate()
                        if thread.name == "benchmark-http-loop"]
        self.assertEqual(len(loop_threads), 1)
        resolver_threads = [thread for thread in threading.enumerate()
                            if thread.name.startswith("benchmark-dns")]
        self.assertLessEqual(len(resolver_threads), benchmarks._DNS_WORKERS)

    def test_retry_backoff_is_capped_by_the_remaining_deadline(self):
        class Clock:
            now = 0.0

            def __call__(self):
                return self.now

        clock = Clock()
        delays = []

        def sleep(seconds):
            delays.append(seconds)
            clock.now += seconds

        client = benchmarks.HttpClient(sleep=sleep)
        asyncio.run(client._sleep_before_retry(
            4, benchmarks.Deadline(0.75, clock=clock)))
        self.assertEqual(len(delays), 1)
        self.assertLessEqual(delays[0], 0.750001)
        self.assertGreaterEqual(delays[0], 0)
        with self.assertRaises(benchmarks.SourceError) as expired:
            asyncio.run(client._sleep_before_retry(
                0, benchmarks.Deadline(0, clock=clock)))
        self.assertEqual(str(expired.exception), "deadline exceeded")

    def test_streaming_body_transport_error_is_sanitized(self):
        client = _LocalHttpClient(sleep=lambda _seconds: None)
        with _LocalFeedServer(_transport_responder) as server:
            with self.assertRaises(benchmarks.SourceError) as caught:
                client.request("GET", server.base_url + "/partial",
                               deadline=deadline())
        self.assertEqual(str(caught.exception), "network error")
        self.assertNotIn("127.0.0.1", str(caught.exception))
        self.assertNotIn("http://", str(caught.exception))

    def test_json_and_csv_parsers_reject_non_finite_and_duplicate_headers(self):
        with self.assertRaises(benchmarks.SourceError):
            benchmarks._load_json(b'{"value": NaN}')
        with self.assertRaises(benchmarks.SourceError):
            benchmarks._load_json(b"not json")
        duplicate = benchmarks.Response(200, {}, b"model,coding,coding\nx,1,2\n")
        with self.assertRaises(benchmarks.SourceError):
            benchmarks._parse_csv(duplicate.body, ("model", "coding"), maximum_rows=10)
        extra = benchmarks.Response(200, {}, b"model,coding\nx,1,2\n")
        with self.assertRaises(benchmarks.SourceError):
            benchmarks._parse_csv(extra.body, ("model", "coding"), maximum_rows=10)

    def test_provenance_links_are_allowlisted_https(self):
        provenance_hosts = benchmarks.ALLOWED_HOSTS | {"docs.harborframework.com"}
        for spec in benchmarks.SOURCES.values():
            with self.subTest(source=spec.key):
                parts = urlsplit(spec.url)
                self.assertEqual(parts.scheme, "https")
                self.assertIsNone(parts.username)
                self.assertIsNone(parts.password)
                self.assertIn(parts.hostname, provenance_hosts)
                # Any link a source warning carries must also be allowlisted.
                for url in re.findall(r"https://[^\s)]+", spec.warning):
                    self.assertIn(urlsplit(url).hostname, provenance_hosts)


# ── Persistence, staleness and failure isolation ────────────────────────────
class SnapshotPersistenceTests(unittest.TestCase):
    def test_successful_refresh_persists_versioned_snapshot_and_reloads(self):
        directory, store, path = temp_store(fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "GPT-6.1 Sol", 58.18)])),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench")},
        })
        try:
            self.assertTrue(store.refresh_source("harbor"))
            document = json.loads(Path(path).read_text())
            self.assertEqual(document["schema_version"], benchmarks.FILE_SCHEMA_VERSION)
            self.assertEqual(document["sources"]["harbor"]["schema_version"],
                             benchmarks.SOURCE_SCHEMA_VERSION)
            reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: NOW)
            state = reloaded.read_state(now=NOW)
            self.assertFalse(state["harbor"]["stale"])
            self.assertEqual(len(state["harbor"]["data"]["entries"]), 1)
            self.assertEqual(state["harbor"]["data"]["entries"][0]["value"], 58.18)
            self.assertEqual(state["harbor"]["data"]["entries"][0]["date_kind"],
                             benchmarks.DATE_KIND_MODEL_RELEASE)
        finally:
            directory.cleanup()

    def test_source_failure_keeps_last_good_and_persists_error_across_restart(self):
        good = snapshot([entry("tb4", "GPT-6.1 Sol", 58.18)])
        directory, store, path = temp_store(fetchers={
            "harbor": static_fetcher(good),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench")},
        }, clock=lambda: NOW)
        try:
            store.refresh_source("harbor")
            store._clock = lambda: NOW + 60
            store._fetchers["harbor"] = static_fetcher(benchmarks.SourceError("HTTP 503"))
            self.assertFalse(store.refresh_source("harbor"))
            state = store.read_state(now=NOW + 60)
            self.assertEqual(state["harbor"]["error"], "HTTP 503")
            # Last-good data is retained, but an unresolved error is stale.
            self.assertTrue(state["harbor"]["stale"])
            self.assertEqual(state["harbor"]["last_success"], NOW)
            self.assertEqual(len(state["harbor"]["data"]["entries"]), 1)
            reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: NOW + 60)
            reloaded_state = reloaded.read_state(now=NOW + 60)
            self.assertEqual(reloaded_state["harbor"]["error"], "HTTP 503")
            self.assertEqual(len(reloaded_state["harbor"]["data"]["entries"]), 1)
        finally:
            directory.cleanup()

    def test_stale_last_good_is_retained_and_flagged_after_restart(self):
        directory, store, path = temp_store(fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "Model", 10.0)])),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench")},
        })
        try:
            store.refresh_source("harbor")
            later = NOW + benchmarks.CACHE_TTL + 1
            reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: later)
            state = reloaded.read_state(now=later)
            self.assertTrue(state["harbor"]["stale"])
            self.assertEqual(len(state["harbor"]["data"]["entries"]), 1)
        finally:
            directory.cleanup()

    def test_legacy_artificial_analysis_cache_is_rejected(self):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "benchmarks.json"
        path.write_text(json.dumps({
            "fetched_at": 123.0,
            "source": "https://artificialanalysis.ai/leaderboards/models",
            "models": {"glm53": {"intelligence": 53}},
        }))
        store = benchmarks.BenchmarkStore(path=str(path), clock=lambda: NOW)
        try:
            state = store.read_state(now=NOW)
            self.assertIsNone(state["harbor"]["data"])
            self.assertIsNone(state["epoch"]["data"])
            store._fetchers["harbor"] = static_fetcher(snapshot([entry("tb4", "Model", 10.0)]))
            self.assertTrue(store.refresh_source("harbor"))
            document = json.loads(path.read_text())
            self.assertEqual(document["schema_version"], 2)
            self.assertNotIn("models", document)
            self.assertNotIn("artificialanalysis", path.read_text().lower())
        finally:
            directory.cleanup()

    def test_unknown_schema_versions_are_ignored_per_source(self):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "benchmarks.json"
        path.write_text(json.dumps({
            "schema_version": 2,
            "sources": {
                "harbor": {"schema_version": 99, "data": {"entries": [entry("tb4", "X", 1)]}},
                "epoch": {
                    "schema_version": benchmarks.SOURCE_SCHEMA_VERSION,
                    "version": benchmarks.SOURCES["epoch"].version,
                    "data": {"entries": [entry("eci", "Y", 100)]},
                    "last_success": NOW,
                },
            },
        }))
        store = benchmarks.BenchmarkStore(path=str(path), clock=lambda: NOW)
        try:
            state = store.read_state(now=NOW)
            self.assertIsNone(state["harbor"]["data"])
            self.assertEqual(state["epoch"]["data"]["entries"][0]["value"], 100)
        finally:
            directory.cleanup()

    def test_oversized_state_never_replaces_a_readable_cache(self):
        directory, store, path = temp_store()
        try:
            Path(path).write_text("old-cache")
            with patch.object(benchmarks, "MAX_CACHE_BYTES", 64):
                with self.assertLogs(benchmarks._log, level="WARNING") as logs:
                    self.assertFalse(store._persist())
            self.assertEqual(Path(path).read_text(), "old-cache")
            self.assertEqual([item.name for item in Path(path).parent.iterdir()],
                             ["benchmarks.json"])
            self.assertIn("benchmark cache exceeds size limit", logs.output[0])
        finally:
            directory.cleanup()

    def test_oversized_state_keeps_last_good_cache_reloadable(self):
        directory, store, path = temp_store(fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "Old", 1.0)])),
        })
        try:
            self.assertTrue(store.refresh_source("harbor"))
            old_bytes = Path(path).read_bytes()
            with patch.object(benchmarks, "MAX_CACHE_BYTES", 64):
                with self.assertLogs(benchmarks._log, level="WARNING"):
                    self.assertFalse(store._persist())
            self.assertEqual(Path(path).read_bytes(), old_bytes)
            reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: NOW)
            state = reloaded.read_state(now=NOW)
            self.assertEqual(state["harbor"]["data"]["entries"][0]["value"], 1.0)
        finally:
            directory.cleanup()

    def test_atomic_replace_permission_failure_reports_storage_error(self):
        directory, store, path = temp_store(fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "Model", 10.0)])),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench")},
        })
        try:
            Path(path).write_text("old-cache")
            with patch.object(benchmarks.os, "replace",
                              side_effect=PermissionError("fixture")):
                with self.assertLogs(benchmarks._log, level="WARNING") as logs:
                    self.assertFalse(store.refresh_source("harbor"))
            self.assertEqual(Path(path).read_text(), "old-cache")
            self.assertEqual([item.name for item in Path(path).parent.iterdir()],
                             ["benchmarks.json"])
            state = store.read_state(now=NOW)
            self.assertEqual(state["harbor"]["error"], "cache storage unavailable")
            self.assertTrue(state["harbor"]["stale"])
            # The fetched last-good snapshot is still available in memory.
            self.assertEqual(state["harbor"]["data"]["entries"][0]["model"], "Model")
            self.assertFalse(store.ensure_refresh_scheduled())
            self.assertIn("cache storage unavailable", "\n".join(logs.output))
        finally:
            directory.cleanup()

    def test_storage_failure_retains_new_data_in_memory_and_recovers(self):
        old = snapshot([entry("tb4", "Old Model", 10.0)])
        new = snapshot([entry("tb4", "New Model", 20.0)])
        responses = {"value": old}
        clock = {"now": NOW}

        def fetcher(_client, _previous, _deadline):
            return responses["value"]

        directory, store, path = temp_store(fetchers={
            "harbor": fetcher,
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench")},
        })
        store._clock = lambda: clock["now"]
        try:
            self.assertTrue(store.refresh_source("harbor"))
            old_bytes = Path(path).read_bytes()
            responses["value"] = new
            clock["now"] = NOW + 10
            with patch.object(benchmarks.os, "replace",
                              side_effect=PermissionError("fixture")):
                with self.assertLogs(benchmarks._log, level="WARNING"):
                    self.assertFalse(store.refresh_source("harbor"))
            state = store.read_state(now=NOW + 10)
            self.assertEqual(state["harbor"]["error"], "cache storage unavailable")
            self.assertTrue(state["harbor"]["stale"])
            # Newly fetched data lives in memory; the previous disk bytes and
            # its restart view are untouched.
            self.assertEqual(state["harbor"]["data"]["entries"][0]["model"],
                             "New Model")
            # last_success still means the upstream validation succeeded, but
            # the error/stale pair prevents a complete-fresh-success claim.
            self.assertEqual(state["harbor"]["last_success"], NOW + 10)
            self.assertEqual(Path(path).read_bytes(), old_bytes)
            reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: NOW + 10)
            self.assertEqual(
                reloaded.read_state(now=NOW + 10)["harbor"]["data"]["entries"][0]["model"],
                "Old Model")

            # Storage recovery clears the error and restores freshness.
            responses["value"] = snapshot([entry("tb4", "Recovered Model", 30.0)])
            self.assertTrue(store.refresh_source("harbor"))
            state = store.read_state(now=NOW + 10)
            self.assertIsNone(state["harbor"]["error"])
            self.assertFalse(state["harbor"]["stale"])
            self.assertEqual(state["harbor"]["data"]["entries"][0]["model"],
                             "Recovered Model")
            reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: NOW + 10)
            self.assertEqual(
                reloaded.read_state(now=NOW + 10)["harbor"]["data"]["entries"][0]["model"],
                "Recovered Model")
        finally:
            directory.cleanup()

    def test_304_persist_failure_reports_storage_and_recovers(self):
        directory, store, path = temp_store(fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "Model", 10.0)])),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench")},
        })
        try:
            self.assertTrue(store.refresh_source("harbor"))
            stable = Path(path).read_bytes()
            store._fetchers["harbor"] = static_fetcher({"not_modified": True})
            store._clock = lambda: NOW + 20
            with patch.object(benchmarks.os, "replace",
                              side_effect=PermissionError("fixture")):
                with self.assertLogs(benchmarks._log, level="WARNING"):
                    self.assertFalse(store.refresh_source("harbor"))
            state = store.read_state(now=NOW + 20)
            self.assertEqual(state["harbor"]["error"], "cache storage unavailable")
            self.assertTrue(state["harbor"]["stale"])
            self.assertEqual(len(state["harbor"]["data"]["entries"]), 1)
            self.assertEqual(Path(path).read_bytes(), stable)
            self.assertFalse(store.ensure_refresh_scheduled())

            # The cache is writable again: the same 304 becomes a fresh success.
            self.assertTrue(store.refresh_source("harbor"))
            state = store.read_state(now=NOW + 20)
            self.assertIsNone(state["harbor"]["error"])
            self.assertFalse(state["harbor"]["stale"])
        finally:
            directory.cleanup()

    def test_serialization_failure_is_reported_as_storage_error(self):
        directory, store, _ = temp_store(fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "Model", 10.0)])),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench")},
        })
        try:
            with patch.object(benchmarks.json, "dumps",
                              side_effect=TypeError("fixture")):
                with self.assertLogs(benchmarks._log, level="WARNING"):
                    self.assertFalse(store.refresh_source("harbor"))
            state = store.read_state(now=NOW)
            self.assertEqual(state["harbor"]["error"], "cache storage unavailable")
            self.assertTrue(state["harbor"]["stale"])
            self.assertEqual(state["harbor"]["data"]["entries"][0]["model"], "Model")
        finally:
            directory.cleanup()

    def test_byte_budget_persist_failure_is_reported_as_storage_error(self):
        directory, store, path = temp_store(fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "Model", 10.0)])),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench")},
        })
        try:
            Path(path).write_text("previous-cache")
            with patch.object(benchmarks, "MAX_CACHE_BYTES", 128):
                with self.assertLogs(benchmarks._log, level="WARNING"):
                    self.assertFalse(store.refresh_source("harbor"))
            self.assertEqual(Path(path).read_text(), "previous-cache")
            state = store.read_state(now=NOW)
            self.assertEqual(state["harbor"]["error"], "cache storage unavailable")
            self.assertTrue(state["harbor"]["stale"])
            self.assertEqual(state["harbor"]["data"]["entries"][0]["model"], "Model")
        finally:
            directory.cleanup()

    def test_disabled_persistence_refreshes_without_storage_error(self):
        directory, store, path = temp_store(persist=False, fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "Model", 10.0)])),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench")},
        })
        try:
            self.assertTrue(store.refresh_source("harbor"))
            state = store.read_state(now=NOW)
            self.assertIsNone(state["harbor"]["error"])
            self.assertFalse(state["harbor"]["stale"])
            self.assertTrue(store._persist())  # persistence disabled is a no-op
            self.assertFalse(Path(path).exists())
        finally:
            directory.cleanup()

    def test_successful_refresh_replaces_records_instead_of_merging(self):
        old = snapshot([entry("tb4", "Old Model", 10.0)])
        new = snapshot([entry("tb4", "New Model", 20.0)])
        directory, store, path = temp_store(fetchers={
            "harbor": static_fetcher(old),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench")},
        })
        try:
            store.refresh_source("harbor")
            store._fetchers["harbor"] = static_fetcher(new)
            store.refresh_source("harbor")
            state = store.read_state(now=NOW)
            models = [item["model"] for item in state["harbor"]["data"]["entries"]]
            self.assertEqual(models, ["New Model"])
            payload = benchmarks.build_payload([
                inventory_model("old-model", "x/Old Model"),
                inventory_model("new-model", "x/New Model"),
            ], state)
            self.assertEqual(payload["models"][0]["bench"], {})
            self.assertEqual(payload["models"][1]["bench"]["tb4"], 20.0)
        finally:
            directory.cleanup()

    def test_304_without_any_cached_body_is_rejected(self):
        directory, store, _ = temp_store(fetchers={
            "harbor": static_fetcher({"not_modified": True}),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench")},
        })
        try:
            self.assertFalse(store.refresh_source("harbor"))
            self.assertEqual(store.read_state(now=NOW)["harbor"]["error"], "invalid response schema")
        finally:
            directory.cleanup()

    def test_fresh_source_and_entry_versions_must_match_the_pinned_source(self):
        directory, store, path = temp_store(fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "Model", 10.0)])),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench")},
        })
        try:
            bad_source = snapshot([entry("tb4", "Model", 10.0)])
            bad_source["version"] = "0-0-0"
            store._fetchers["harbor"] = static_fetcher(bad_source)
            self.assertFalse(store.refresh_source("harbor"))
            state = store.read_state(now=NOW)
            self.assertEqual(state["harbor"]["error"], "invalid response schema")
            self.assertIsNone(state["harbor"]["data"])

            bad_entry = entry("tb4", "Model", 10.0)
            bad_entry["version"] = "0-0-0"
            store._fetchers["harbor"] = static_fetcher(snapshot([bad_entry]))
            self.assertFalse(store.refresh_source("harbor"))
            self.assertEqual(store.read_state(now=NOW)["harbor"]["error"],
                             "invalid response schema")
            self.assertIsNone(store.read_state(now=NOW)["harbor"]["data"])
        finally:
            directory.cleanup()

    def test_persisted_source_and_entry_versions_must_match_the_pinned_source(self):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "benchmarks.json"
        good_harbor = entry("tb4", "Model", 10.0)
        good_epoch = entry("eci", "Model", 100.0)
        bad_entry = dict(good_epoch, version="0-0-0")
        path.write_text(json.dumps({
            "schema_version": benchmarks.FILE_SCHEMA_VERSION,
            "sources": {
                "harbor": {
                    "schema_version": benchmarks.SOURCE_SCHEMA_VERSION,
                    "version": "0-0-0",
                    "last_success": NOW,
                    "data": {"entries": [good_harbor]},
                },
                "epoch": {
                    "schema_version": benchmarks.SOURCE_SCHEMA_VERSION,
                    "version": benchmarks.SOURCES["epoch"].version,
                    "last_success": NOW,
                    "data": {"entries": [bad_entry]},
                },
            },
        }))
        store = benchmarks.BenchmarkStore(path=str(path), clock=lambda: NOW)
        try:
            state = store.read_state(now=NOW)
            self.assertIsNone(state["harbor"]["data"])
            self.assertIsNone(state["epoch"]["data"])
        finally:
            directory.cleanup()

    def test_malformed_metric_types_fail_closed_per_source(self):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "benchmarks.json"

        def record(name, item):
            return {
                "schema_version": benchmarks.SOURCE_SCHEMA_VERSION,
                "version": benchmarks.SOURCES[name].version,
                "last_success": NOW,
                "last_checked": NOW,
                "data": {"entries": [item]},
            }

        valid_epoch = entry("eci", "Valid Model", 100.0)
        harbor_entry = entry("tb4", "Harbor Model", 10.0)
        for bad_metric in ([], {}, 3, None, True):
            with self.subTest(metric=bad_metric):
                path.write_text(json.dumps({
                    "schema_version": benchmarks.FILE_SCHEMA_VERSION,
                    "sources": {
                        "harbor": record(
                            "harbor", dict(harbor_entry, metric=bad_metric)),
                        "epoch": record("epoch", valid_epoch),
                    },
                }))
                # A malformed entry must not raise TypeError during _load.
                store = benchmarks.BenchmarkStore(path=str(path), clock=lambda: NOW)
                state = store.read_state(now=NOW)
                self.assertIsNone(state["harbor"]["data"])
                self.assertIsNone(state["harbor"]["last_success"])
                # The valid other source is preserved.
                self.assertEqual(state["epoch"]["data"]["entries"][0]["value"], 100.0)
        directory.cleanup()

    def test_make_entry_rejects_unhashable_metric_types(self):
        base = dict(evaluator="Fixture", version=benchmarks.SOURCES["harbor"].version)
        for bad_metric in ([], {}, 3, None, True):
            with self.subTest(metric=bad_metric):
                with self.assertRaises(benchmarks.SourceError):
                    benchmarks._make_entry(
                        bad_metric, "Model", 1.0, date=None, date_kind=None,
                        **base)

    def test_overflowing_cache_numbers_are_rejected_without_raising(self):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "benchmarks.json"
        path.write_text(json.dumps({
            "schema_version": benchmarks.FILE_SCHEMA_VERSION,
            "sources": {
                "epoch": {
                    "schema_version": benchmarks.SOURCE_SCHEMA_VERSION,
                    "version": benchmarks.SOURCES["epoch"].version,
                    "last_success": 10 ** 400,
                },
                "harbor": {
                    "schema_version": benchmarks.SOURCE_SCHEMA_VERSION,
                    "version": benchmarks.SOURCES["harbor"].version,
                    "last_success": NOW,
                    "data": {
                        "entries": [entry("tb4", "Model", 10.0)],
                        "meta": {"suite_weights": {"Coding": 10 ** 400}},
                    },
                },
            },
        }))
        store = benchmarks.BenchmarkStore(path=str(path), clock=lambda: NOW)
        try:
            state = store.read_state(now=NOW)
            self.assertIsNone(state["epoch"]["data"])
            self.assertIsNone(state["epoch"]["last_success"])
            self.assertIsNone(state["harbor"]["data"])
        finally:
            directory.cleanup()
        self.assertFalse(benchmarks._is_number(10 ** 400))
        with self.assertRaises(benchmarks.SourceError):
            benchmarks._sanitize_timestamp(10 ** 400)

    def test_error_marks_state_stale_even_with_recent_success(self):
        directory, store, _ = temp_store()
        try:
            state = {"error": None, "last_success": NOW}
            self.assertTrue(store._is_fresh(state, NOW))
            state["error"] = "HTTP 503"
            self.assertFalse(store._is_fresh(state, NOW))
            self.assertEqual(state["last_success"], NOW)
        finally:
            directory.cleanup()

    def test_concurrent_persists_never_replace_a_newer_snapshot(self):
        directory, store, path = temp_store()
        original_document = store._disk_document
        first_captured = threading.Event()
        release_first = threading.Event()
        second_captured = threading.Event()
        second_finished = threading.Event()
        calls = {"count": 0}
        calls_lock = threading.Lock()

        def wrapped_document():
            document = original_document()
            with calls_lock:
                calls["count"] += 1
                index = calls["count"]
            if index == 1:
                first_captured.set()
                release_first.wait(5)
            elif index == 2:
                second_captured.set()
            return document

        store._disk_document = wrapped_document
        errors = []

        def persist(*, second=False):
            try:
                store._persist()
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
            finally:
                if second:
                    second_finished.set()

        try:
            first = threading.Thread(target=persist)
            first.start()
            self.assertTrue(first_captured.wait(2))
            # This update happens after the first writer captured its document.
            with store._state_lock:
                state = store._state["epoch"]
                state["data"] = {"entries": [entry("eci", "Model", 100.0)], "meta": {}}
                state["last_checked"] = NOW
                state["last_success"] = NOW
                state["error"] = None
                state["source_updated"] = "2026-10-15"
            second = threading.Thread(target=persist, kwargs={"second": True})
            second.start()
            # A correct implementation blocks the second writer until the first
            # transaction is complete.  The old capture-before-lock ordering lets
            # the second writer finish first, after which the stale first writer
            # overwrites it.
            if second_captured.wait(0.25):
                second_finished.wait(1)
            release_first.set()
            first.join(5)
            second.join(5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            document = json.loads(Path(path).read_text())
            self.assertEqual(
                document["sources"]["epoch"]["data"]["entries"][0]["value"], 100.0)
            reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: NOW)
            self.assertEqual(
                reloaded.read_state(now=NOW)["epoch"]["data"]["entries"][0]["value"],
                100.0,
            )
        finally:
            release_first.set()
            directory.cleanup()

    def test_one_source_failure_never_erases_another(self):
        directory, store, path = temp_store(fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "Model", 10.0)])),
            "epoch": static_fetcher(benchmarks.SourceError("invalid JSON response")),
            "livebench": static_fetcher(benchmarks.SourceError("empty feed")),
        })
        try:
            store.refresh_now()
            state = store.read_state(now=NOW)
            self.assertEqual(len(state["harbor"]["data"]["entries"]), 1)
            self.assertIsNone(state["harbor"]["error"])
            self.assertEqual(state["epoch"]["error"], "invalid JSON response")
            self.assertIsNone(state["epoch"]["data"])
        finally:
            directory.cleanup()

    def test_raw_exception_text_never_reaches_state_or_payload(self):
        secret = "https://user:fixture-secret@internal.invalid/path"
        directory, store, path = temp_store(fetchers={
            "harbor": static_fetcher(RuntimeError(secret)),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench")},
        })
        try:
            store.refresh_source("harbor")
            state = store.read_state(now=NOW)
            self.assertNotIn("secret", json.dumps(state))
            payload = benchmarks.build_payload(
                [inventory_model("m", "openai/m")], state)
            serialized = json.dumps(payload)
            self.assertNotIn("fixture-secret", serialized)
            self.assertNotIn("internal.invalid", serialized)
            self.assertEqual(payload["sources"]["harbor"]["error"], "network error")
        finally:
            directory.cleanup()


# ── Cache file hardening ─────────────────────────────────────────────────────
class CacheReaderHardeningTests(unittest.TestCase):
    def valid_document(self):
        return {
            "schema_version": benchmarks.FILE_SCHEMA_VERSION,
            "sources": {
                "harbor": {
                    "schema_version": benchmarks.SOURCE_SCHEMA_VERSION,
                    "version": benchmarks.SOURCES["harbor"].version,
                    "last_success": NOW,
                    "last_checked": NOW,
                    "data": {"entries": [entry("tb4", "Model", 10.0)]},
                },
            },
        }

    def test_symlink_cache_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "real.json"
            target.write_text(json.dumps(self.valid_document()))
            link = Path(tmp) / "cache.json"
            try:
                os.symlink(target, link)
            except (AttributeError, OSError):
                self.skipTest("symlinks unavailable")
            store = benchmarks.BenchmarkStore(path=str(link), clock=lambda: NOW)
            self.assertIsNone(store.read_state(now=NOW)["harbor"]["data"])

    def test_fifo_cache_does_not_block_or_load(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "cache.fifo"
            os.mkfifo(fifo)
            result = {}

            def load():
                try:
                    result["store"] = benchmarks.BenchmarkStore(
                        path=str(fifo), clock=lambda: NOW)
                except Exception as exc:  # pragma: no cover - failure path
                    result["error"] = exc

            worker = threading.Thread(target=load)
            worker.daemon = True
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive(), "cache reader blocked on a FIFO")
            self.assertNotIn("error", result)
            self.assertIsNone(result["store"].read_state(now=NOW)["harbor"]["data"])


# ── Refresh concurrency ─────────────────────────────────────────────────────
class _FakeThread:
    def __init__(self, target, name):
        self.target = target
        self.name = name
        self.started = False
        self.joined = False

    def start(self):
        self.started = True

    def join(self, timeout=None):
        self.joined = True


class RefreshConcurrencyTests(unittest.TestCase):
    def test_manual_refresh_is_guarded_against_thread_storms(self):
        created = []

        def factory(target, name):
            thread = _FakeThread(target, name)
            created.append(thread)
            return thread

        directory, store, _ = temp_store(thread_factory=factory)
        try:
            first = store.request_refresh()
            second = store.request_refresh()
            self.assertTrue(first["ok"])
            self.assertTrue(first["refreshing"])
            self.assertEqual(second["refreshing"], True)
            self.assertFalse(second["started"])
            self.assertEqual(len(created), 1)
            self.assertTrue(created[0].started)
            self.assertTrue(store.refreshing)
        finally:
            directory.cleanup()

    def test_auto_refresh_never_starts_even_with_missing_or_stale_sources(self):
        created = []

        def factory(target, name):
            thread = _FakeThread(target, name)
            created.append(thread)
            return thread

        directory, store, _ = temp_store(thread_factory=factory)
        try:
            self.assertFalse(store.ensure_refresh_scheduled())
            self.assertFalse(store.ensure_refresh_scheduled())
            store._clock = lambda: NOW + benchmarks.CACHE_TTL + 1
            self.assertFalse(store.ensure_refresh_scheduled())
            self.assertEqual(created, [])
        finally:
            directory.cleanup()

    def test_fresh_cache_does_not_schedule_network_work(self):
        created = []
        directory = tempfile.TemporaryDirectory()
        path = str(Path(directory.name) / "benchmarks.json")
        try:
            document = {
                "schema_version": 2,
                "sources": {
                    name: {"schema_version": benchmarks.SOURCE_SCHEMA_VERSION,
                           "version": benchmarks.SOURCES[name].version,
                           "last_success": NOW, "last_checked": NOW,
                           "data": {"entries": [entry(
                               "tb4" if name == "harbor" else "eci", "X", 1.0)]}}
                    if name in ("harbor", "epoch") else
                    {"schema_version": benchmarks.SOURCE_SCHEMA_VERSION,
                     "version": benchmarks.SOURCES[name].version,
                     "last_success": NOW, "last_checked": NOW}
                    for name in benchmarks.SOURCE_NAMES
                },
            }
            Path(path).write_text(json.dumps(document))
            fresh = benchmarks.BenchmarkStore(
                path=path, clock=lambda: NOW,
                thread_factory=lambda t, n: created.append((t, n)) or _FakeThread(t, n))
            self.assertFalse(fresh.ensure_refresh_scheduled())
            self.assertEqual(created, [])
        finally:
            directory.cleanup()

    def test_same_source_never_refreshes_concurrently(self):
        entered = threading.Event()
        release = threading.Event()

        def blocking_fetcher(client, previous, deadline):
            entered.set()
            release.wait(timeout=5)
            return snapshot([entry("tb4", "Model", 10.0)])

        directory, store, _ = temp_store(fetchers={"harbor": blocking_fetcher})
        try:
            worker = threading.Thread(target=lambda: store.refresh_source("harbor"))
            worker.start()
            self.assertTrue(entered.wait(timeout=3))
            self.assertFalse(store.refresh_source("harbor"))
            release.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(store.read_state(now=NOW)["harbor"]["data"]["entries"]), 1)
        finally:
            release.set()
            directory.cleanup()

    def test_read_path_is_immediate_and_does_not_call_fetchers(self):
        called = []
        fetcher = lambda *args: called.append(True) or snapshot([])
        directory = tempfile.TemporaryDirectory()
        store = benchmarks.BenchmarkStore(path=str(Path(directory.name) / "b.json"),
                                          clock=lambda: NOW, fetchers={"harbor": fetcher})
        try:
            state = store.read_state(now=NOW)
            self.assertFalse(called)
            payload = benchmarks.build_payload(
                [inventory_model("m", "openai/m")], state)
            self.assertFalse(called)
            self.assertEqual(payload["models"][0]["missing"]["tb4"], "source unavailable")
        finally:
            directory.cleanup()

    def test_worker_refreshes_sources_independently(self):
        directory, store, _ = temp_store(fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "A", 10.0)])),
            "epoch": static_fetcher(benchmarks.SourceError("empty feed")),
            "livebench": static_fetcher(benchmarks.SourceError("empty feed")),
        })
        try:
            with patch.object(benchmarks, "LIVEBENCH_ENABLED", True):
                store._refresh_worker()
            state = store.read_state(now=NOW)
            self.assertFalse(state["harbor"]["stale"])
            self.assertEqual(state["epoch"]["error"], "empty feed")
        finally:
            directory.cleanup()


class ManualRefreshPolicyTests(unittest.TestCase):
    def make_store(self, **kwargs):
        directory, store, path = temp_store(
            thread_factory=lambda target, name: _FakeThread(target, name), **kwargs)
        self.addCleanup(directory.cleanup)
        return store, path

    def test_reservation_is_persisted_before_worker_creation(self):
        store, path = self.make_store()
        created = []

        def factory(target, name):
            self.assertEqual(json.loads(Path(path).read_text())["last_refresh_attempt"], NOW)
            created.append(name)
            return _FakeThread(target, name)

        store._thread_factory = factory
        result = store.request_refresh()
        self.assertTrue(result["started"])
        self.assertEqual(created, ["benchmark-refresh"])
        self.assertEqual(result["refresh_policy"]["next_allowed_at"], NOW + 86400)

    def test_cooldown_survives_restart_and_expires_at_exact_boundary(self):
        store, path = self.make_store()
        store.request_refresh()
        reloaded = benchmarks.BenchmarkStore(
            path=path, clock=lambda: NOW + 86399,
            thread_factory=lambda target, name: _FakeThread(target, name))
        denied = reloaded.request_refresh()
        self.assertTrue(denied["rate_limited"])
        self.assertEqual(denied["retry_after"], 1)
        reloaded._clock = lambda: NOW + 86400
        self.assertTrue(reloaded.request_refresh()["started"])

    def test_failed_sources_and_failed_worker_start_still_consume_the_day(self):
        store, path = self.make_store(fetchers={
            "harbor": static_fetcher(benchmarks.SourceError("HTTP 503"))})
        store.request_refresh()
        self.assertFalse(store.refresh_source("harbor"))
        store._end_coordinator()
        self.assertTrue(store.request_refresh()["rate_limited"])
        self.assertEqual(benchmarks.BenchmarkStore(path=path, clock=lambda: NOW)
                         .refresh_policy()["last_attempt"], NOW)

        store._clock = lambda: NOW + 86400
        store._thread_factory = Mock(side_effect=RuntimeError("private details"))
        denied = store.request_refresh()
        self.assertEqual(denied["error"], "unable to start refresh")
        self.assertNotIn("private details", json.dumps(denied))
        self.assertTrue(store.request_refresh()["rate_limited"])

    def test_coordinator_thread_start_failure_releases_guard_and_keeps_reservation(self):
        store, path = self.make_store()
        thread = Mock()
        thread.start.side_effect = RuntimeError("private details")
        store._thread_factory = Mock(return_value=thread)
        result = store.request_refresh()
        self.assertEqual(result["error"], "unable to start refresh")
        self.assertFalse(store.refreshing)
        self.assertNotIn("private details", json.dumps(result))
        self.assertTrue(store.request_refresh()["rate_limited"])
        reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: NOW)
        self.assertTrue(reloaded.request_refresh()["rate_limited"])

    def test_source_thread_startup_failures_keep_scores_and_run_other_sources(self):
        for stage in ("construct", "start"):
            for failed in ("harbor", "epoch"):
                with self.subTest(stage=stage, source=failed):
                    store, path = self.make_store(fetchers={
                        "harbor": static_fetcher(snapshot([entry("tb4", "A", 10.0)])),
                        "epoch": static_fetcher(snapshot([entry("eci", "A", 20.0)])),
                    })
                    store._clock = lambda: NOW - 60
                    self.assertTrue(store.refresh_source("harbor"))
                    self.assertTrue(store.refresh_source("epoch"))
                    store._clock = lambda: NOW
                    self.assertTrue(store.request_refresh()["started"])
                    started = []
                    testcase = self

                    class SourceThread(_FakeThread):
                        def start(self):
                            if self.name == f"benchmark-{failed}":
                                raise RuntimeError("private details")
                            super().start()
                            started.append(self)
                            self.target()

                        def join(self, timeout=None):
                            testcase.assertTrue(store._coordinator)
                            super().join(timeout)

                    def factory(target, name):
                        if stage == "construct" and name == f"benchmark-{failed}":
                            raise RuntimeError("private details")
                        return SourceThread(target, name)

                    store._thread_factory = factory
                    with patch.object(benchmarks, "LIVEBENCH_ENABLED", False):
                        store._refresh_worker()
                    state = store.read_state()
                    self.assertFalse(store.refreshing)
                    self.assertEqual(state[failed]["error"], "unable to start refresh")
                    self.assertTrue(state[failed]["stale"])
                    self.assertEqual(state[failed]["last_checked"], NOW)
                    self.assertEqual(state[failed]["last_success"], NOW - 60)
                    self.assertIsNotNone(state[failed]["data"])
                    other = "epoch" if failed == "harbor" else "harbor"
                    self.assertIsNone(state[other]["error"])
                    self.assertEqual(state[other]["last_success"], NOW)
                    self.assertEqual(len(started), 1)
                    self.assertTrue(started[0].joined)
                    self.assertTrue(store.request_refresh()["rate_limited"])
                    document = json.loads(Path(path).read_text())
                    self.assertNotIn("private details", json.dumps(document))
                    reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: NOW)
                    self.assertEqual(reloaded.read_state()[failed]["error"],
                                     "unable to start refresh")
                    self.assertTrue(reloaded.request_refresh()["rate_limited"])

    def test_parallel_tabs_share_one_reservation(self):
        store, path = self.make_store()
        results = []
        threads = [threading.Thread(target=lambda: results.append(store.request_refresh()))
                   for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(result["started"] for result in results), 1)
        self.assertEqual(json.loads(Path(path).read_text())["last_refresh_attempt"], NOW)

    def test_persistence_failure_prevents_all_outbound_work(self):
        store, _ = self.make_store()
        factory = Mock()
        store._thread_factory = factory
        with patch.object(store, "_persist", return_value=False):
            result = store.request_refresh()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "refresh history unavailable")
        self.assertFalse(store.refresh_policy()["can_refresh"])
        factory.assert_not_called()

    def test_disabled_persistence_cannot_bypass_restart_guarantee(self):
        store, _ = self.make_store(persist=False)
        self.assertFalse(store.refresh_policy()["can_refresh"])
        self.assertFalse(store.request_refresh()["ok"])
        self.assertFalse(store.refresh_policy()["can_refresh"])

    def test_source_version_change_does_not_reset_a_saved_reservation(self):
        store, path = self.make_store()
        store.request_refresh()
        document = json.loads(Path(path).read_text())
        document["sources"]["harbor"]["version"] = "retired-version"
        Path(path).write_text(json.dumps(document))
        reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: NOW + 60)
        self.assertTrue(reloaded.request_refresh()["rate_limited"])

    def test_unreadable_or_linked_history_cannot_silently_reset_the_limit(self):
        store, path = self.make_store()
        store.request_refresh()
        with patch.object(benchmarks, "_read_cache_bytes", return_value=None):
            reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: NOW)
        self.assertFalse(reloaded.request_refresh()["ok"])
        link = Path(path).with_name("linked.json")
        link.symlink_to(path)
        reloaded = benchmarks.BenchmarkStore(path=str(link), clock=lambda: NOW)
        self.assertFalse(reloaded.request_refresh()["ok"])

    def test_uninspectable_cache_path_is_not_treated_as_a_missing_history(self):
        store, path = self.make_store()
        factory = Mock()
        with patch.object(benchmarks.os, "lstat", side_effect=PermissionError("private path")):
            reloaded = benchmarks.BenchmarkStore(
                path=path, clock=lambda: NOW, thread_factory=factory)
        self.assertFalse(reloaded.refresh_policy()["can_refresh"])
        self.assertEqual(reloaded.request_refresh()["error"], "refresh history unavailable")
        self.assertFalse(Path(path).exists())
        factory.assert_not_called()

    def test_migration_rejects_malformed_source_history_without_discarding_scores(self):
        store, path = self.make_store(fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "A", 10.0)]))})
        store._clock = lambda: NOW - 86460
        self.assertTrue(store.refresh_source("harbor"))
        document = json.loads(Path(path).read_text())
        document.pop("last_refresh_attempt")
        for invalid in (None, [], "broken"):
            with self.subTest(record=invalid):
                document["sources"]["livebench"] = invalid
                Path(path).write_text(json.dumps(document))
                reloaded = benchmarks.BenchmarkStore(
                    path=path, clock=lambda: NOW,
                    thread_factory=lambda target, name: _FakeThread(target, name))
                self.assertFalse(reloaded.refresh_policy()["can_refresh"])
                self.assertEqual(reloaded.request_refresh()["error"], "refresh history unavailable")
                self.assertEqual(reloaded.read_state()["harbor"]["data"]["entries"][0]["value"], 10.0)

    def test_migration_counts_old_checks_including_failures_and_disabled_sources(self):
        store, path = self.make_store()
        document = store._disk_document()
        document.pop("last_refresh_attempt")
        document["sources"]["livebench"]["last_checked"] = NOW - 60
        document["sources"]["livebench"]["error"] = "HTTP 503"
        document["sources"]["livebench"]["version"] = "old-version"
        Path(path).write_text(json.dumps(document))
        reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: NOW)
        result = reloaded.request_refresh()
        self.assertTrue(result["rate_limited"])
        self.assertEqual(result["refresh_policy"]["last_attempt"], NOW - 60)

    def test_corrupt_history_fails_closed_without_discarding_scores(self):
        store, path = self.make_store(fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "A", 10.0)]))})
        store.refresh_source("harbor")
        document = json.loads(Path(path).read_text())
        document["last_refresh_attempt"] = "invalid"
        Path(path).write_text(json.dumps(document))
        reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: NOW)
        self.assertEqual(reloaded.read_state()["harbor"]["data"]["entries"][0]["value"], 10.0)
        self.assertEqual(reloaded.request_refresh()["error"], "refresh history unavailable")
        Path(path).write_text("broken JSON")
        reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: NOW)
        self.assertFalse(reloaded.request_refresh()["ok"])

    def test_clock_rollback_and_source_toggle_cannot_reset_the_limit(self):
        store, _ = self.make_store()
        store.request_refresh()
        store._end_coordinator()
        store._clock = lambda: NOW - 100
        with patch.object(benchmarks, "LIVEBENCH_ENABLED", True):
            result = store.request_refresh()
        self.assertTrue(result["rate_limited"])
        self.assertEqual(result["refresh_policy"]["last_attempt"], NOW)


# ── Payload contract and identities ─────────────────────────────────────────
class PayloadContractTests(unittest.TestCase):
    def setUp(self):
        self.livebench_patch = patch.object(benchmarks, "LIVEBENCH_ENABLED", True)
        self.livebench_patch.start()

    def tearDown(self):
        self.livebench_patch.stop()

    def state(self, entries):
        return make_state(entries)

    def test_top_level_contract_and_metric_metadata(self):
        state = self.state({
            "harbor": [entry("tb4", "GPT-6.1 Sol", 58.18)],
            "epoch": [entry("eci", "GPT-6.1 Sol", 168.37)],
            "livebench": [entry("livebench_coding", "gpt-6.1-sol", 96.0)],
        })
        payload = benchmarks.build_payload(
            [inventory_model("gpt-6.1-sol", "openai/gpt-6.1-sol")], state)
        self.assertEqual(set(payload), {"models", "metrics", "sources"})
        self.assertEqual([m["key"] for m in payload["metrics"]],
                         [spec.key for spec in benchmarks.METRICS])
        for metric in payload["metrics"]:
            self.assertEqual(set(metric), {"key", "label", "name", "unit", "source"})
            self.assertIn(metric["unit"], ("pct", "index"))
        model = payload["models"][0]
        self.assertEqual(set(model),
                         {"name", "upstream_model", "provider", "mode", "routes",
                          "bench", "results", "missing"})
        self.assertEqual(model["bench"]["tb4"], 58.18)
        self.assertEqual(model["bench"]["eci"], 168.37)
        self.assertEqual(model["bench"]["livebench_coding"], 96.0)
        result = model["results"]["tb4"][0]
        for key in ("model", "agent", "effort", "evaluator", "version", "date",
                    "date_kind", "value", "ci", "configuration", "source",
                    "selected", "metadata"):
            self.assertIn(key, result)
        self.assertEqual(result["value"], 58.18)
        self.assertEqual(result["date_kind"], benchmarks.DATE_KIND_MODEL_RELEASE)
        self.assertEqual(result["metadata"]["date_kind"],
                         benchmarks.DATE_KIND_MODEL_RELEASE)
        self.assertTrue(result["selected"])
        self.assertEqual(payload["sources"]["livebench"]["disabled"], False)
        source = payload["sources"]["harbor"]
        self.assertEqual(set(source), {"last_checked", "last_success", "source_updated",
                                       "stale", "error", "version", "url", "warning",
                                       "disabled"})
        self.assertEqual(source["version"], "4-0-0")
        self.assertTrue(source["url"].startswith("https://"))

    def test_each_configuration_retains_its_own_value(self):
        state = self.state({
            "harbor": [
                entry("tb4", "Model", 50.0, date="2026-09-01", effort="low"),
                entry("tb4", "Model", 48.0, date="2026-09-01", effort="max"),
            ],
            "epoch": [], "livebench": [],
        })
        model = benchmarks.build_payload(
            [inventory_model("model", "x/model")], state)["models"][0]
        values = {result["effort"]: result["value"]
                  for result in model["results"]["tb4"]}
        self.assertEqual(values, {"low": 50.0, "max": 48.0})
        self.assertEqual(model["bench"]["tb4"], 48.0)  # selected max effort, not max score

    def test_date_kinds_are_a_strict_known_enum_and_require_a_date(self):
        base = dict(evaluator="Fixture", version=benchmarks.SOURCES["harbor"].version)
        with self.assertRaises(benchmarks.SourceError):
            benchmarks._make_entry("tb4", "Model", 1.0, date="2026-09-01",
                                   date_kind="bogus", **base)
        with self.assertRaises(benchmarks.SourceError):
            benchmarks._make_entry("tb4", "Model", 1.0, date="2026-09-01", **base)
        with self.assertRaises(benchmarks.SourceError):
            benchmarks._make_entry("tb4", "Model", 1.0, date=None,
                                   date_kind=benchmarks.DATE_KIND_MODEL_RELEASE,
                                   **base)

    def test_result_rejects_a_non_finite_value_instead_of_emitting_it(self):
        with self.assertRaises(benchmarks.SourceError):
            benchmarks._result({"value": math.inf, "metric": "tb4",
                                "model": "X", "agent": None, "effort": None,
                                "evaluator": "Fixture", "version": "4-0-0",
                                "date": None, "date_kind": None, "ci": None,
                                "configuration": None, "metadata": {}}, "harbor")

    def test_latest_published_dated_result_wins_not_the_maximum(self):
        state = self.state({
            "harbor": [
                entry("tb4", "Model", 90.0, date="2026-08-01", effort="max"),
                entry("tb4", "Model", 70.0, date="2026-09-01", effort="max"),
            ],
            "epoch": [], "livebench": [],
        })
        payload = benchmarks.build_payload(
            [inventory_model("model", "x/model")], state)
        model = payload["models"][0]
        self.assertEqual(model["bench"]["tb4"], 70.0)
        self.assertEqual(len(model["results"]["tb4"]), 2)
        self.assertEqual([item["date"] for item in model["results"]["tb4"]],
                         ["2026-08-01", "2026-09-01"])
        self.assertTrue(model["results"]["tb4"][1]["selected"])

    def test_same_date_configuration_selection_is_deterministic_and_not_maxima(self):
        state = self.state({
            "harbor": [
                entry("tb4", "Model", 50.0, date="2026-09-01", effort="low"),
                entry("tb4", "Model", 52.0, date="2026-09-01", effort="medium"),
                entry("tb4", "Model", 48.0, date="2026-09-01", effort="max"),
            ],
            "epoch": [], "livebench": [],
        })
        payload = benchmarks.build_payload([inventory_model("model", "x/model")], state)
        model = payload["models"][0]
        self.assertEqual(model["bench"]["tb4"], 48.0)  # max effort, not max score
        selected = [item for item in model["results"]["tb4"] if item["selected"]]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["effort"], "max")

    def test_multiple_undated_configurations_never_get_a_summary(self):
        state = self.state({
            "harbor": [
                entry("tb4", "Model", 60.0, date=None, effort="max"),
                entry("tb4", "Model", 70.0, date=None, effort="low"),
            ],
            "epoch": [], "livebench": [],
        })
        model = benchmarks.build_payload(
            [inventory_model("model", "x/model")], state)["models"][0]
        self.assertNotIn("tb4", model["bench"])
        self.assertEqual(model["missing"]["tb4"], "not evaluated")
        self.assertEqual(len(model["results"]["tb4"]), 2)
        self.assertFalse(any(item["selected"] for item in model["results"]["tb4"]))

    def test_reviewed_exact_aliases_cover_verified_source_ids(self):
        state = self.state({
            "harbor": [
                entry("tb4", "GPT-6.1 Sol", 58.18),
                entry("tb4", "Opus 5.5", 64.85),
                entry("tb4", "GLM-5.3", 41.82),
            ],
            "epoch": [
                entry("eci", "GPT-6.1 Sol", 168.37),
                entry("eci", "Claude Opus 5.5", 168.83),
                entry("eci", "GLM-5.3", 155.99),
            ],
            "livebench": [
                entry("livebench_coding", "gpt-6.1-sol", 96.0),
                entry("livebench_coding", "claude-opus-5.5", 96.3),
            ],
        })
        models = benchmarks.build_payload([
            inventory_model("gpt-6.1-sol", "openai/gpt-6.1-sol"),
            inventory_model("claude-opus-5.5", "anthropic/claude-opus-5.5"),
            inventory_model("glm-5.3", "zai/glm-5.3"),
        ], state)["models"]
        self.assertEqual(models[0]["bench"]["tb4"], 58.18)
        self.assertEqual(models[0]["bench"]["eci"], 168.37)
        self.assertEqual(models[0]["bench"]["livebench_coding"], 96.0)
        self.assertEqual(models[1]["bench"]["tb4"], 64.85)
        self.assertEqual(models[1]["bench"]["eci"], 168.83)
        self.assertEqual(models[1]["bench"]["livebench_coding"], 96.3)
        self.assertEqual(models[2]["bench"]["tb4"], 41.82)
        self.assertEqual(models[2]["bench"]["eci"], 155.99)
        # LiveBench has no glm-5.3 row: missing, not silently borrowed.
        self.assertEqual(models[2]["missing"]["livebench_coding"], "unmatched identity")

    def test_documented_mistral_route_ids_gain_source_metrics(self):
        state = self.state({
            "harbor": [
                entry("tb4", "GLM-5.3", 41.82),
                entry("tb4", "GLM-5.3-Flash", 35.76),
            ],
            "epoch": [
                entry("eci", "GLM-5.3", 155.99),
                entry("eci", "GLM-5.3-Flash", 153.08),
                entry("eci", "Mistral Large 4", 150.03),
            ],
            "livebench": [
                entry("livebench_coding", "glm-5.3", 80.0),
                entry("livebench_coding", "glm-5.3-flash", 80.6),
                # LiveBench publishes this exact ID; exact matching is not a
                # curated alias and needs no source mapping.
                entry("livebench_coding", "mistral-large-4", 60.0),
            ],
        })
        models = benchmarks.build_payload([
            inventory_model("zai-glm-5-3", "mistral/zai-glm-5-3"),
            inventory_model("zai-glm-5-3-flash", "mistral/zai-glm-5-3-flash"),
            inventory_model("mistral-large-4", "mistral/mistral-large-4"),
        ], state)["models"]

        self.assertEqual(models[0]["bench"], {
            "tb4": 41.82, "livebench_coding": 80.0, "eci": 155.99,
        })
        self.assertEqual(models[1]["bench"], {
            "tb4": 35.76, "livebench_coding": 80.6, "eci": 153.08,
        })
        # Only the reviewed Epoch alias was added for Mistral Large 4.
        self.assertEqual(models[2]["bench"],
                         {"eci": 150.03, "livebench_coding": 60.0})
        self.assertEqual(models[2]["missing"]["tb4"], "unmatched identity")

    def test_mistral_rolling_latest_and_undocumented_variants_stay_unmatched(self):
        state = self.state({
            "harbor": [entry("tb4", "GLM-5.3", 41.82)],
            "epoch": [entry("eci", "Mistral Large 4", 150.03)],
            "livebench": [entry("livebench_coding", "glm-5.3", 80.0)],
        })
        candidates = [
            ("mistral-large-latest", "mistral/mistral-large-latest"),
            # The previous research suggested -4-0, but the documented API ID
            # is mistral-large-4; no version-prefix alias is invented.
            ("mistral-large-4-0", "mistral/mistral-large-4-0"),
            ("zai-glm-5-3-latest", "mistral/zai-glm-5-3-latest"),
            ("zai-glm-5-3-preview", "mistral/zai-glm-5-3-preview"),
        ]
        for name, upstream in candidates:
            with self.subTest(name=name):
                model = benchmarks.build_payload(
                    [inventory_model(name, upstream)], state)["models"][0]
                self.assertEqual(model["bench"], {})
                self.assertEqual(model["results"], {})
                self.assertEqual(model["missing"]["tb4"], "unmatched identity")

    def test_modified_route_alias_blocks_a_stock_looking_upstream(self):
        state = self.state({
            "harbor": [entry("tb4", "GLM-5.3", 41.82)],
            "epoch": [entry("eci", "GLM-5.3", 155.99)],
            "livebench": [entry("livebench_coding", "glm-5.3", 80.0)],
        })
        stock = benchmarks.build_payload([
            inventory_model(
                "zai-glm-5-3", "mistral/zai-glm-5-3",
                routes=[
                    {"name": "zai-glm-5-3", "provider": "mistral"},
                    {"name": "zai-glm-5-3-abliterated", "provider": "mistral"},
                ],
            ),
        ], state)["models"][0]
        self.assertEqual(stock["bench"], {})
        self.assertEqual(stock["results"], {})
        self.assertEqual(stock["missing"]["tb4"], "unmatched identity")

        row_name_only = benchmarks.build_payload([
            inventory_model("zai-glm-5-3-uncensored", "mistral/zai-glm-5-3"),
        ], state)["models"][0]
        self.assertEqual(row_name_only["bench"], {})
        self.assertEqual(row_name_only["missing"]["tb4"], "unmatched identity")

        normal = benchmarks.build_payload([
            inventory_model("zai-glm-5-3", "mistral/zai-glm-5-3"),
        ], state)["models"][0]
        self.assertEqual(normal["bench"]["tb4"], 41.82)
        self.assertEqual(normal["bench"]["eci"], 155.99)
        self.assertEqual(normal["bench"]["livebench_coding"], 80.0)

    def test_mistral_alias_routes_still_exclude_noncoding_and_abliterated(self):
        state = self.state({
            "harbor": [entry("tb4", "GLM-5.3", 41.82)],
            "epoch": [entry("eci", "GLM-5.3", 155.99)],
            "livebench": [],
        })
        models = benchmarks.build_payload([
            inventory_model("audio", "mistral/voxtral-mini-latest",
                            mode="audio_transcription"),
            inventory_model("embed", "mistral/mistral-embed", mode="embedding"),
            inventory_model("abliterated", "mistral/zai-glm-5-3-abliterated"),
        ], state)["models"]
        for model in models[:2]:
            self.assertEqual(model["bench"], {})
            self.assertEqual(model["missing"]["tb4"], "unsupported modality")
        self.assertEqual(models[2]["bench"], {})
        self.assertEqual(models[2]["results"], {})
        self.assertEqual(models[2]["missing"]["tb4"], "unmatched identity")

    def test_responses_api_mode_is_coding_comparable(self):
        # LiteLLM model_info.mode "responses" is the OpenAI Responses API
        # protocol, not a modality; such routes must keep matching benchmarks.
        state = self.state({
            "harbor": [entry("tb4", "GPT-6.1 Sol", 58.18)],
            "epoch": [entry("eci", "GPT-6.1 Sol", 168.37)],
            "livebench": [],
        })
        model = benchmarks.build_payload([
            inventory_model("chatgpt-gpt-6.1-sol", "chatgpt/gpt-6.1-sol",
                            mode="responses"),
        ], state)["models"][0]
        self.assertEqual(model["mode"], "responses")
        self.assertEqual(model["bench"]["tb4"], 58.18)
        self.assertEqual(model["bench"]["eci"], 168.37)
        self.assertNotIn("tb4", model["missing"])
        self.assertNotIn("eci", model["missing"])

    def test_responses_pseudo_provider_identity_is_resolved(self):
        # Gateway routes like openai/responses/<model> must resolve to the
        # model identity below the Responses-API pseudo provider segment.
        state = self.state({
            "harbor": [entry("tb4", "GPT-5.6 Luna", 52.12)],
            "epoch": [entry("eci", "GPT-5.6 Luna", 156.5)],
            "livebench": [],
        })
        model = benchmarks.build_payload([
            inventory_model("gpt-5.6-luna", "openai/responses/gpt-5.6-luna"),
        ], state)["models"][0]
        self.assertEqual(model["bench"]["tb4"], 52.12)
        self.assertEqual(model["bench"]["eci"], 156.5)

    def test_user_approved_identity_aliases_resolve(self):
        # Reviewed one-to-one identities approved by the operator on
        # 2026-10-29; the rule cascade reproduces them (see
        # ALIAS_PARITY_TRIPLES for the full parity fixture).
        state = self.state({
            "epoch": [
                entry("eci", "Kimi K3", 157.86),
                entry("eci", "Kimi K2.7 Code", 150.11),
                entry("eci", "Claude Haiku 5.5", 159.32),
                entry("eci", "DeepSeek V4.1 Flash", 155.93),
                entry("eci", "DeepSeek V4.1 Flash 1017", 158.83),
                entry("eci", "Qwen 3.8 Max", 157.4),
                entry("eci", "Qwen 3.8 27B", 142.0),
                entry("eci", "Muse Spark 1.3", 157.21),
            ],
            "harbor": [entry("tb4", "Muse Spark 1.3", 46.02)],
            "livebench": [],
        })
        cases = [
            ("kimi-k3", "openai/kimi-k3", 157.86),
            ("kimi-k2.7-code", "openai/kimi-k2.7-code", 150.11),
            ("claude-haiku-5-5", "anthropic/claude-haiku-5-5", 159.32),
            ("deepseek-v4.1-flash", "openai/deepseek-v4.1-flash", 155.93),
            # Documented divergence (brief §5): the route serving the pinned
            # 1017 snapshot resolves to the dated row, which wins where it
            # has entries (158.83), instead of the base row's 155.93.
            ("deepseek-v4.1-flash-mistral",
             "mistral/deepseek-v4-1-flash-1017", 158.83),
            ("qwen3.8-max", "openai/qwen3.8-max", 157.4),
            ("qwen3.8-27b", "openai/qwen3.8:27b", 142.0),
            ("muse-spark-1.3-contributor",
             "openai/responses/muse-spark-1.3-contributor", 157.21),
        ]
        for name, upstream, eci in cases:
            with self.subTest(name=name):
                model = benchmarks.build_payload(
                    [inventory_model(name, upstream)], state)["models"][0]
                self.assertEqual(model["bench"]["eci"], eci)
                self.assertNotIn("eci", model["missing"])
        contributor = benchmarks.build_payload([
            inventory_model("muse-spark-1.3-contributor",
                            "openai/responses/muse-spark-1.3-contributor"),
        ], state)["models"][0]
        self.assertEqual(contributor["bench"]["tb4"], 46.02)

    def test_qwen38_max_rolling_route_maps_to_the_reviewed_tb4_snapshot(self):
        # Terminal-Bench only evaluated the dated Qwen3.8-Max-0902 snapshot;
        # the operator accepted the variant risk on the rolling qwen3.8-max
        # route (2026-10-29) and considers it the same model.
        state = self.state({
            "harbor": [entry("tb4", "Qwen3.8-Max-0902", 51.6)],
            "epoch": [], "livebench": [],
        })
        model = benchmarks.build_payload(
            [inventory_model("qwen3.8-max", "openai/qwen3.8-max")], state)["models"][0]
        self.assertEqual(model["bench"]["tb4"], 51.6)
        self.assertNotIn("tb4", model["missing"])

    def test_livebench_exact_api_ids_resolve_for_reviewed_routes(self):
        # LiveBench publishes exact API-style IDs; these identities mirror the
        # user-approved epoch/harbor judgments (2026-10-29). The operator also
        # explicitly accepted rolling DeepSeek -> 1017 during branch review.
        state = self.state({
            "livebench": [
                entry("livebench_coding", "deepseek-v4.1-flash-1017", 96.0),
                entry("livebench_coding", "claude-haiku-5.5", 91.4),
                entry("livebench_coding", "qwen3.8-27b", 86.8),
                entry("livebench_coding", "muse-spark-1.3", 90.2),
            ],
            "harbor": [], "epoch": [],
        })
        cases = [
            ("deepseek-v4.1-flash-mistral", "mistral/deepseek-v4-1-flash-1017", 96.0),
            ("deepseek-v4.1-flash", "openai/deepseek-v4.1-flash", 96.0),
            ("claude-haiku-5-5", "anthropic/claude-haiku-5-5", 91.4),
            ("qwen3.8-27b", "openai/qwen3.8:27b", 86.8),
            ("muse-spark-1.3-contributor",
             "openai/responses/muse-spark-1.3-contributor", 90.2),
        ]
        for name, upstream, coding in cases:
            with self.subTest(name=name):
                model = benchmarks.build_payload(
                    [inventory_model(name, upstream)], state)["models"][0]
                self.assertEqual(model["bench"]["livebench_coding"], coding)
                self.assertNotIn("livebench_coding", model["missing"])

    def test_no_fuzzy_prefix_or_rolling_latest_matching(self):
        state = self.state({
            "harbor": [entry("tb4", "Opus 5.5", 64.85)],
            "epoch": [entry("eci", "Claude Opus 5.5", 168.83)],
            "livebench": [entry("livebench_coding", "claude-opus-5.5", 96.3)],
        })
        names = ["claude-opus-5.5-abliterated", "claude-opus-5.5-preview",
                 "claude-opus-5", "claude-opus-latest", "claudeopus55"]
        for name in names:
            with self.subTest(name=name):
                model = benchmarks.build_payload(
                    [inventory_model(name, f"x/{name}")], state)["models"][0]
                self.assertEqual(model["bench"], {})
        blocked = benchmarks.build_payload(
            [inventory_model("claude-opus-5.5-abliterated",
                             "anthropic/claude-opus-5.5-abliterated")], state)["models"][0]
        self.assertEqual(blocked["missing"]["tb4"], "unmatched identity")
        self.assertEqual(blocked["missing"]["livebench_coding"], "unmatched identity")

    def test_case_insensitive_exact_identity_is_supported_without_punctuation_collapsing(self):
        state = self.state({
            "harbor": [entry("tb4", "GLM-5.3", 41.82)],
            "epoch": [], "livebench": [],
        })
        upper = benchmarks.build_payload(
            [inventory_model("GLM-5.3", "zai/GLM-5.3")], state)["models"][0]
        self.assertEqual(upper["bench"]["tb4"], 41.82)
        collapsed = benchmarks.build_payload(
            [inventory_model("GLM53", "zai/GLM53")], state)["models"][0]
        self.assertEqual(collapsed["bench"], {})

    def test_unsupported_modalities_remain_rows_without_scores(self):
        state = self.state({
            "harbor": [entry("tb4", "Embed 1", 50.0)],
            "epoch": [], "livebench": [],
        })
        models = benchmarks.build_payload([
            inventory_model("audio", "mistral/voxtral-mini-latest", mode="audio_transcription"),
            inventory_model("embed", "mistral/mistral-embed", mode="embedding"),
        ], state)["models"]
        self.assertEqual([model["mode"] for model in models],
                         ["audio_transcription", "embedding"])
        for model in models:
            self.assertEqual(model["bench"], {})
            self.assertEqual(model["results"], {})
            self.assertEqual(model["missing"]["tb4"], "unsupported modality")

    def test_missing_reasons_are_specific_and_source_scoped(self):
        state = self.state({
            "livebench": [entry("livebench_coding", "matched", 80.0)],
            "harbor": [], "epoch": None,
        })
        models = benchmarks.build_payload([
            inventory_model("absent", "x/absent"),
            inventory_model("matched", "x/matched"),
        ], state)["models"]
        absent = models[0]
        self.assertEqual(absent["missing"]["tb4"], "source unavailable")
        self.assertEqual(absent["missing"]["eci"], "source unavailable")
        self.assertEqual(absent["missing"]["livebench_coding"], "unmatched identity")
        matched = models[1]
        self.assertEqual(matched["missing"]["tb4"], "source unavailable")
        self.assertEqual(matched["missing"]["eci"], "source unavailable")
        self.assertEqual(matched["bench"]["livebench_coding"], 80.0)

    def test_identity_match_but_missing_suite_is_not_evaluated(self):
        state = self.state({
            "livebench": [entry("livebench_reasoning", "model", 90.0)],
            "harbor": [], "epoch": [],
        })
        model = benchmarks.build_payload(
            [inventory_model("model", "x/model")], state)["models"][0]
        self.assertEqual(model["missing"]["livebench_coding"], "not evaluated")
        self.assertEqual(model["bench"]["livebench_reasoning"], 90.0)

    def test_openai_is_a_transport_provider_not_an_identity_source(self):
        state = self.state({
            "harbor": [entry("tb4", "GPT-6.1 Sol", 58.18)],
            "epoch": [], "livebench": [],
        })
        model = benchmarks.build_payload(
            [inventory_model("azure-route", "openai/gpt-6.1-sol")], state)["models"][0]
        self.assertEqual(model["provider"], "openai")
        self.assertEqual(model["bench"]["tb4"], 58.18)

    def test_disabled_livebench_definitions_remain_but_scores_are_hidden(self):
        with patch.object(benchmarks, "LIVEBENCH_ENABLED", False):
            state = self.state({
                "livebench": [entry("livebench_coding", "gpt-6.1-sol", 96.0)],
                "harbor": [], "epoch": [],
            })
            payload = benchmarks.build_payload(
                [inventory_model("gpt-6.1-sol", "openai/gpt-6.1-sol")], state)
        self.assertIn("livebench_coding", [m["key"] for m in payload["metrics"]])
        model = payload["models"][0]
        self.assertEqual(model["bench"], {})
        self.assertEqual(model["results"], {})
        self.assertEqual(model["missing"]["livebench_coding"], "source unavailable")
        source = payload["sources"]["livebench"]
        self.assertTrue(source["disabled"])
        self.assertIn("reuse terms unverified", source["warning"])
        self.assertNotIn("96.0", json.dumps(payload))

    def test_source_provenance_never_includes_model_supplied_urls(self):
        row = harbor_row(model_url="https://evil.invalid/secret-path")
        state = self.state({"harbor": [entry("tb4", "GPT-6.1 Sol", 1.0)]})
        payload = benchmarks.build_payload(
            [inventory_model("gpt-6.1-sol", "openai/gpt-6.1-sol")], state)
        self.assertNotIn("evil.invalid", json.dumps(payload))
        self.assertNotIn("secret-path", json.dumps(payload))

    def test_all_exposed_numbers_are_finite(self):
        state = self.state({
            "harbor": [entry("tb4", "Model", 10.0)],
            "epoch": [entry("eci", "Model", 120.0)],
            "livebench": [],
        })
        payload = benchmarks.build_payload([inventory_model("model", "x/model")], state)
        for model in payload["models"]:
            for value in model["bench"].values():
                self.assertTrue(math.isfinite(value))


# ── Rule-based identity cascade ─────────────────────────────────────────────
# Parity fixture: the reviewed alias triples that the removed SOURCE_ALIASES
# table encoded, kept here as test data only (the runtime resolves identities
# purely by the rule cascade in app/benchmarks.py).  Each source's synthetic
# index is built from the fixture's targets plus the dated 1017 models that
# source actually publishes (PARITY_EXTRA_MODELS).  The two documented
# divergences (PARITY_DIVERGENCES) are the 1017-snapshot triples: the route
# now resolves to the dated model itself, and the per-metric preference falls
# back to the base row only for metrics the dated row does not have.
ALIAS_PARITY_TRIPLES = {
    "harbor": {
        'claude-fable-5': 'Fable 5',
        'claude-fable-5.1': 'Fable 5.1',
        'glm-5.3': 'GLM-5.3',
        'glm-5.3-flash': 'GLM-5.3-Flash',
        'zai-glm-5-3': 'GLM-5.3',
        'zai-glm-5-3-flash': 'GLM-5.3-Flash',
        'gpt-5.6-luna': 'GPT-5.6 Luna',
        'gpt-5.6-sol': 'GPT-5.6 Sol',
        'gpt-5.6-terra': 'GPT-5.6 Terra',
        'gpt-6-astra': 'GPT-6 Astra',
        'gpt-6-luna': 'GPT-6 Luna',
        'gpt-6-sol': 'GPT-6 Sol',
        'gpt-6.1-sol': 'GPT-6.1 Sol',
        'gemini-3.7-flash': 'Gemini 3.7 Flash',
        'gemini-3.8-flash': 'Gemini 3.8 Flash',
        'grok-4.5': 'Grok 4.5',
        'grok-4.6': 'Grok 4.6',
        'grok-4.7': 'Grok 4.7',
        'muse-spark-1.3': 'Muse Spark 1.3',
        'muse-spark-1.3-contributor': 'Muse Spark 1.3',
        'claude-opus-4.8': 'Opus 4.8',
        'claude-opus-5': 'Opus 5',
        'claude-opus-5.5': 'Opus 5.5',
        'qwen3.8-max': 'Qwen3.8-Max-0902',
        'qwen3.8-max-0902': 'Qwen3.8-Max-0902',
        'claude-sonnet-5': 'Sonnet 5',
        'claude-sonnet-5.5': 'Sonnet 5.5',
    },
    "epoch": {
        'claude-fable-5': 'Claude Fable 5',
        'claude-fable-5.1': 'Claude Fable 5.1',
        'glm-5.3': 'GLM-5.3',
        'glm-5.3-flash': 'GLM-5.3-Flash',
        'zai-glm-5-3': 'GLM-5.3',
        'zai-glm-5-3-flash': 'GLM-5.3-Flash',
        'mistral-large-4': 'Mistral Large 4',
        'gpt-5.6-luna': 'GPT-5.6 Luna',
        'gpt-5.6-sol': 'GPT-5.6 Sol',
        'gpt-5.6-terra': 'GPT-5.6 Terra',
        'gpt-6-astra': 'GPT-6 Astra',
        'gpt-6-luna': 'GPT-6 Luna',
        'gpt-6-sol': 'GPT-6 Sol',
        'gpt-6.1-sol': 'GPT-6.1 Sol',
        'gemini-3.7-flash': 'Gemini 3.7 Flash',
        'gemini-3.8-flash': 'Gemini 3.8 Flash',
        'grok-4.5': 'Grok 4.5',
        'grok-4.6': 'Grok 4.6',
        'grok-4.7': 'Grok 4.7',
        'muse-spark-1.3': 'Muse Spark 1.3',
        'claude-opus-4.8': 'Claude Opus 4.8',
        'claude-opus-5': 'Claude Opus 5',
        'claude-opus-5.5': 'Claude Opus 5.5',
        'qwen3.8-max-0902': 'Qwen3.8 Max (0902)',
        'claude-sonnet-5': 'Claude Sonnet 5',
        'claude-sonnet-5.5': 'Claude Sonnet 5.5',
        'kimi-k3': 'Kimi K3',
        'kimi-k2.7-code': 'Kimi K2.7 Code',
        'claude-haiku-5-5': 'Claude Haiku 5.5',
        'deepseek-v4.1-flash': 'DeepSeek V4.1 Flash',
        'deepseek-v4-1-flash-1017': 'DeepSeek V4.1 Flash',
        'qwen3.8-max': 'Qwen 3.8 Max',
        'qwen3.8:27b': 'Qwen 3.8 27B',
        'muse-spark-1.3-contributor': 'Muse Spark 1.3',
    },
    "livebench": {
        'zai-glm-5-3': 'glm-5.3',
        'zai-glm-5-3-flash': 'glm-5.3-flash',
        'deepseek-v4-1-flash-1017': 'deepseek-v4.1-flash-1017',
        'claude-haiku-5-5': 'claude-haiku-5.5',
        'qwen3.8:27b': 'qwen3.8-27b',
        'muse-spark-1.3-contributor': 'muse-spark-1.3',
        'deepseek-v4.1-flash': 'deepseek-v4.1-flash-1017',
    },
    "benchlm": {
        'gpt-6.1-sol': 'gpt-6-1-sol',
        'zai-glm-5-3': 'glm-5-3',
        'zai-glm-5-3-flash': 'glm-5-3-flash',
        'deepseek-v4-1-flash-1017': 'deepseek-v4-1-flash',
        'qwen3.8:27b': 'qwen3-8-27b',
        'glm-5.3': 'glm-5-3',
        'glm-5.3-flash': 'glm-5-3-flash',
        'gpt-5.6-luna': 'gpt-5-6-luna',
        'kimi-k2.7-code': 'kimi-k2-7-code',
        'mimo-v2.6-flash': 'mimo-v2-6-flash',
        'mimo-v2.6-pro': 'mimo-v2-6-pro',
        'qwen3.8-max': 'qwen3-8-max',
        'deepseek-v4.1-flash': 'deepseek-v4-1-flash',
        'muse-spark-1.3-contributor': 'muse-spark-1-3',
    },
}
PARITY_EXTRA_MODELS = {
    "epoch": ("DeepSeek V4.1 Flash 1017",),
    "benchlm": ("deepseek-v4-1-flash-1017",),
}
# The one documented divergence per source: the 1017 route resolves to the
# dated snapshot model (itself), not to the base row the old alias named.
PARITY_DIVERGENCES = {
    ("epoch", "deepseek-v4-1-flash-1017"): "DeepSeek V4.1 Flash 1017",
    ("benchlm", "deepseek-v4-1-flash-1017"): "deepseek-v4-1-flash-1017",
}


def parity_index(source):
    """Synthetic index for one source, built from the parity fixture targets."""
    models = set(ALIAS_PARITY_TRIPLES[source].values())
    models.update(PARITY_EXTRA_MODELS.get(source, ()))
    entries = [{"metric": "tb4", "model": model} for model in sorted(models)]
    return benchmarks._build_index({"data": {"entries": entries}})


def resolve_identity(source, upstream, index=None):
    return benchmarks._resolve_identity(
        {"upstream_model": upstream}, source, index if index is not None
        else parity_index(source))


class RuleCascadeTests(unittest.TestCase):
    def _index(self, *models):
        entries = [{"metric": "tb4", "model": model} for model in models]
        return benchmarks._build_index({"data": {"entries": entries}})

    def test_canonical_form_equivalences(self):
        equal = [
            ("qwen3.8-max", "Qwen 3.8 Max"),
            ("qwen3.8:27b", "qwen3-8-27b"),
            ("claude-haiku-5-5", "claude-haiku-5.5"),
            ("27B", "27b"),
        ]
        for left, right in equal:
            with self.subTest(pair=(left, right)):
                self.assertEqual(benchmarks._canonical_tokens(left),
                                 benchmarks._canonical_tokens(right))

    def test_canonical_form_non_equalities(self):
        unequal = [
            ("kimi-k3", "kimi-3"),  # no k3<->3 equivalence
            ("qwen3-8-max", "qwen3-8-max-internal"),
        ]
        for left, right in unequal:
            with self.subTest(pair=(left, right)):
                self.assertNotEqual(benchmarks._canonical_tokens(left),
                                    benchmarks._canonical_tokens(right))

    def test_date_suffix_validation(self):
        valid = {"1017": 1, "0902": 1, "0731": 1, "0813": 1,
                 "20260317": 1, "2026-03-17": 3}
        for suffix, expected in valid.items():
            with self.subTest(suffix=suffix):
                tokens = benchmarks._canonical_tokens("model-" + suffix)
                self.assertEqual(benchmarks._date_suffix_length(tokens), expected)
        invalid = ["nvfp4", "internal", "next", "preview", "0", "128b",
                   "2025", "1-pro", "max", "thinking",
                   "1300", "2026-13-01", "2026-02-30"]
        for suffix in invalid:
            with self.subTest(suffix=suffix):
                tokens = benchmarks._canonical_tokens("model-" + suffix)
                self.assertEqual(benchmarks._date_suffix_length(tokens), 0)

    def test_candidate_generation_is_every_slash_suffix(self):
        self.assertEqual(
            benchmarks._identity_candidates({"upstream_model": "mistral/zai-glm-5-3"}),
            ["mistral/zai-glm-5-3", "zai-glm-5-3"])
        self.assertEqual(
            benchmarks._identity_candidates(
                {"upstream_model": "openai/responses/gpt-5.6-luna"}),
            ["openai/responses/gpt-5.6-luna", "responses/gpt-5.6-luna",
             "gpt-5.6-luna"])
        self.assertEqual(
            benchmarks._identity_candidates({"upstream_model": "plain"}), ["plain"])

    def test_r1_exact_and_casefold_beat_canonical(self):
        # "Alpha-1" and "alpha 1" share the canonical form "alpha-1", so the
        # canonical rule is ambiguous and can never fire; the exact and
        # case-folded exact rules still resolve the candidate.
        index = self._index("Alpha-1", "alpha 1")
        self.assertIsNone(index["canon"]["alpha-1"])  # ambiguity guard
        self.assertEqual(resolve_identity("harbor", "ALPHA-1", index), "Alpha-1")
        self.assertEqual(resolve_identity("harbor", "alpha-1", index), "Alpha-1")

    def test_r2_canonical_beats_r3_dated_extension(self):
        index = self._index("Base", "Base 0902")
        self.assertEqual(resolve_identity("epoch", "base", index), "Base")
        self.assertEqual(resolve_identity("epoch", "BASE", index), "Base")

    def test_r3_unique_dated_extension_wins(self):
        index = self._index("Only 0902")
        self.assertEqual(resolve_identity("harbor", "only", index), "Only 0902")
        self.assertEqual(resolve_identity("harbor", "only-0902", index), "Only 0902")

    def test_r3_two_snapshots_fails_closed(self):
        index = self._index("Snap 0902", "Snap 0731")
        self.assertIsNone(resolve_identity("epoch", "snap", index))
        # A dated candidate itself still resolves exactly/canonically.
        self.assertEqual(resolve_identity("epoch", "snap-0902", index), "Snap 0902")

    def test_r4_strips_only_the_contributor_qualifier(self):
        index = self._index("Muse Spark 1.3")
        self.assertEqual(
            resolve_identity("harbor", "muse-spark-1.3-contributor", index),
            "Muse Spark 1.3")
        self.assertIsNone(
            resolve_identity("harbor", "muse-spark-1.3-preview", index))

    def test_r5_head_drop_never_fires_before_r2(self):
        index = self._index("Head-Tail", "tail")
        self.assertEqual(resolve_identity("x", "head-tail", index), "Head-Tail")
        only_tail = self._index("tail")
        self.assertEqual(resolve_identity("x", "head-tail", only_tail), "tail")

    def test_ambiguity_guard_never_matches_a_shared_canonical_form(self):
        index = self._index("A B", "A-B")
        self.assertIsNone(index["canon"]["a-b"])
        # "a.b" matches neither exactly nor case-folded, only canonically —
        # and that form is ambiguous, so the resolution fails closed.
        self.assertIsNone(resolve_identity("epoch", "a.b", index))

    def test_tripwires_never_cross_match_variants(self):
        cases = [
            (("nemotron-super-vl-3.5", "nemotron-super-vl-3.5-nvfp4"),
             "x/nemotron-super-vl-3.5", "nemotron-super-vl-3.5"),
            (("nemotron-super-vl-3.5-nvfp4",),
             "x/nemotron-super-vl-3.5", None),
            (("step-5-preview",), "stepfun/step-5", None),
            (("mistral-small-3.1",), "mistral/mistral-small-latest", None),
            (("qwen3-6-max-preview",), "x/qwen3.6-max", None),
            (("mistral-large-4",), "x/mistral-large-4-0", None),
            (("mistral-large-4-0",), "mistral/mistral-large-4", None),
        ]
        for models, upstream, expected in cases:
            with self.subTest(upstream=upstream, models=models):
                self.assertEqual(resolve_identity("livebench", upstream,
                                                  self._index(*models)),
                                 expected)

    def test_parity_fixture_rules_reproduce_every_reviewed_triple(self):
        for source, triples in ALIAS_PARITY_TRIPLES.items():
            index = parity_index(source)
            with self.subTest(source=source):
                # No two fixture targets share a canonical form.
                self.assertEqual(
                    [key for key, model in index["canon"].items()
                     if model is None], [])
            for upstream, target in triples.items():
                with self.subTest(source=source, upstream=upstream):
                    expected = PARITY_DIVERGENCES.get((source, upstream), target)
                    self.assertEqual(
                        benchmarks._resolve_identity(
                            {"upstream_model": upstream}, source, index),
                        expected)

    def test_dated_route_prefers_dated_row_then_base_per_metric(self):
        # The dated key has tb4 and one composite; the base key has tb4 and
        # all composites.  The dated row wins where it exists (even when its
        # value is lower — never merged or maxed), and the base row supplies
        # only the metrics the dated row lacks.
        state = make_state({"benchlm": [
            entry("tb4", "deepseek-v4-1-flash-1017", 31.818, source="benchlm",
                  date="2026-10-29"),
            entry("tb4", "deepseek-v4-1-flash", 42.3, source="benchlm",
                  date="2026-10-29"),
            entry("benchalign_overall", "deepseek-v4-1-flash-1017", 60.0,
                  source="benchlm", date="2026-10-28"),
            entry("benchalign_overall", "deepseek-v4-1-flash", 65.22,
                  source="benchlm", date="2026-10-28"),
            entry("benchalign_coding", "deepseek-v4-1-flash", 62.91,
                  source="benchlm", date="2026-10-28"),
        ]})
        payload = benchmarks.build_payload(
            [inventory_model("deepseek-v4.1-flash-mistral",
                             "mistral/deepseek-v4-1-flash-1017")],
            state, benchlm_enabled=True)
        model = payload["models"][0]
        self.assertNotIn("tb4", model["missing"])
        # Present on both keys: the dated value wins, not the maximum.
        self.assertEqual(model["bench"]["tb4"], 31.818)
        self.assertEqual(model["results"]["tb4"][0]["model"],
                         "deepseek-v4-1-flash-1017")
        self.assertEqual(model["bench"]["benchalign_overall"], 60.0)
        self.assertEqual(model["results"]["benchalign_overall"][0]["model"],
                         "deepseek-v4-1-flash-1017")
        # Present only on the base key: per-metric fallback to the base row.
        self.assertEqual(model["bench"]["benchalign_coding"], 62.91)
        self.assertEqual(model["results"]["benchalign_coding"][0]["model"],
                         "deepseek-v4-1-flash")


# ── BenchLM payload and fallback contract ───────────────────────────────────
class BenchLMPayloadTests(unittest.TestCase):
    def benchlm_entry(self, metric, model, value, **kwargs):
        kwargs.setdefault("source", "benchlm")
        kwargs.setdefault("date", "2026-10-28")
        if metric == "tb4":
            kwargs.setdefault("evaluator", benchmarks.BENCHLM_TB4_EVALUATOR)
            kwargs.setdefault("version", benchmarks.BENCHLM_TB4_BOARDS[0][0])
            kwargs.setdefault("metadata",
                              {"board": benchmarks.BENCHLM_TB4_BOARDS[0][1]})
        else:
            kwargs.setdefault("evaluator", benchmarks.BENCHLM_COMPOSITE_EVALUATOR)
            kwargs.setdefault("version", "benchalign-2026.10.28")
        return entry(metric, model, value, **kwargs)

    def test_tb4_spec_keeps_harbor_primary_with_benchlm_fallback(self):
        spec = benchmarks.METRIC_BY_KEY["tb4"]
        self.assertEqual(spec.source, "harbor")
        self.assertEqual(spec.fallback, ("benchlm",))
        # tb4 merges candidates by maximum selected value; every other metric
        # keeps the first-candidate policy, and as_dict stays frontend-shaped.
        self.assertEqual(spec.merge, "max")
        for other in benchmarks.METRICS:
            if other.key != "tb4":
                self.assertEqual(other.merge, "first")
        self.assertEqual(set(spec.as_dict()),
                         {"key", "label", "name", "unit", "source"})
        # The BenchLM key normalization (gpt-6.1-sol -> gpt-6-1-sol) now comes
        # from the rule cascade, not an alias table.
        index = benchmarks._build_index(
            {"data": {"entries": [{"metric": "tb4", "model": "gpt-6-1-sol"}]}})
        self.assertEqual(benchmarks._resolve_identity(
            {"upstream_model": "openai/gpt-6.1-sol"}, "benchlm", index),
            "gpt-6-1-sol")

    def test_harbor_wins_tb4_when_both_sources_cover_the_model(self):
        state = make_state({
            "harbor": [entry("tb4", "GPT-6.1 Sol", 58.18)],
            "benchlm": [self.benchlm_entry("tb4", "gpt-6-1-sol", 55.5),
                        self.benchlm_entry("benchalign_overall", "gpt-6-1-sol", 91.5)],
        })
        payload = benchmarks.build_payload(
            [inventory_model("gpt-6.1-sol", "openai/gpt-6.1-sol")], state,
            benchlm_enabled=True)
        model = payload["models"][0]
        self.assertEqual(model["bench"]["tb4"], 58.18)
        self.assertEqual(model["results"]["tb4"][0]["source"], "harbor")
        self.assertEqual(model["bench"]["benchalign_overall"], 91.5)
        result = model["results"]["benchalign_overall"][0]
        self.assertEqual(result["source"], "benchlm")
        self.assertEqual(result["date_kind"], benchmarks.DATE_KIND_SOURCE_SNAPSHOT)

    def test_tb4_max_merge_picks_the_higher_candidate_across_sources(self):
        # merge="max": the candidate whose selected result value is highest
        # supplies the row; an exact tie keeps the earlier candidate (Harbor).
        cases = [
            ("benchlm higher", 41.0, 61.25, "benchlm", 61.25),
            ("harbor higher", 58.18, 55.5, "harbor", 58.18),
            ("exact tie", 50.0, 50.0, "harbor", 50.0),
        ]
        for label, harbor_value, benchlm_value, expected_source, expected in cases:
            with self.subTest(label=label):
                state = make_state({
                    "harbor": [entry("tb4", "GPT-6.1 Sol", harbor_value)],
                    "benchlm": [self.benchlm_entry("tb4", "gpt-6-1-sol", benchlm_value)],
                })
                payload = benchmarks.build_payload(
                    [inventory_model("gpt-6.1-sol", "openai/gpt-6.1-sol")], state,
                    benchlm_enabled=True)
                model = payload["models"][0]
                self.assertEqual(model["bench"]["tb4"], expected)
                self.assertNotIn("tb4", model["missing"])
                results = model["results"]["tb4"]
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["source"], expected_source)
                self.assertTrue(results[0]["selected"])
                if expected_source == "benchlm":
                    self.assertEqual(results[0]["version"],
                                     benchmarks.BENCHLM_TB4_BOARDS[0][0])
                    self.assertEqual(results[0]["metadata"]["board"],
                                     benchmarks.BENCHLM_TB4_BOARDS[0][1])
                else:
                    self.assertEqual(results[0]["version"],
                                     benchmarks.SOURCES["harbor"].version)

    def test_tb4_missing_reasons_are_unchanged_without_a_usable_value(self):
        # No candidate yields a numeric selected value: the selection falls
        # through to the first-candidate behaviour, so the missing reason and
        # the emitted results are exactly as before the max merge.
        state = make_state({
            "harbor": [entry("tb4", "GPT-6.1 Sol", 58.18, date=None)],
            "benchlm": [self.benchlm_entry("tb4", "gpt-6-1-sol", 55.5, date=None)],
        })
        payload = benchmarks.build_payload(
            [inventory_model("gpt-6.1-sol", "openai/gpt-6.1-sol")], state,
            benchlm_enabled=True)
        model = payload["models"][0]
        self.assertEqual(model["missing"]["tb4"], "not evaluated")
        self.assertNotIn("tb4", model["bench"])
        self.assertEqual(len(model["results"]["tb4"]), 1)  # harbor's row kept
        self.assertFalse(any(item["selected"] for item in model["results"]["tb4"]))
        # Primary outage with no usable fallback row still reports
        # "source unavailable" (the primary-outage rule), and with an undated
        # fallback row the reason stays "not evaluated" as before.
        for label, benchlm, expected in (
            ("primary outage, no tb4 row",
             [self.benchlm_entry("benchalign_overall", "gpt-6-1-sol", 91.5)],
             "source unavailable"),
            ("primary outage, undated tb4 row",
             [self.benchlm_entry("tb4", "gpt-6-1-sol", 55.5, date=None)],
             "not evaluated"),
        ):
            with self.subTest(label=label):
                state = make_state({"harbor": None, "benchlm": benchlm})
                payload = benchmarks.build_payload(
                    [inventory_model("gpt-6.1-sol", "openai/gpt-6.1-sol")], state,
                    benchlm_enabled=True)
                model = payload["models"][0]
                self.assertEqual(model["missing"]["tb4"], expected)
                self.assertNotIn("tb4", model["bench"])

    def test_benchlm_fills_tb4_when_harbor_has_no_identity_or_no_entries(self):
        benchlm = [self.benchlm_entry("tb4", "glm-5-3", 41.0),
                   self.benchlm_entry("benchalign_overall", "glm-5-3", 80.25)]
        for label, harbor in (("no identity", [entry("tb4", "GPT-6.1 Sol", 58.18)]),
                              ("no entries", None)):
            with self.subTest(label=label):
                state = make_state({"harbor": harbor, "benchlm": benchlm})
                payload = benchmarks.build_payload(
                    [inventory_model("zai-glm-5-3", "mistral/zai-glm-5-3")],
                    state, benchlm_enabled=True)
                model = payload["models"][0]
                self.assertEqual(model["bench"]["tb4"], 41.0)
                self.assertNotIn("tb4", model["missing"])
                result = model["results"]["tb4"][0]
                self.assertEqual(result["source"], "benchlm")
                self.assertEqual(result["version"], benchmarks.BENCHLM_TB4_BOARDS[0][0])
                self.assertEqual(result["metadata"]["board"],
                                 benchmarks.BENCHLM_TB4_BOARDS[0][1])
                self.assertEqual(result["evaluator"], benchmarks.BENCHLM_TB4_EVALUATOR)
                self.assertEqual(result["date_kind"],
                                 benchmarks.DATE_KIND_SOURCE_SNAPSHOT)
                self.assertEqual(model["bench"]["benchalign_overall"], 80.25)

    def test_missing_reason_is_the_furthest_progressed_candidate(self):
        state = make_state({
            "harbor": [entry("tb4", "GPT-6.1 Sol", 58.18)],
            "benchlm": [self.benchlm_entry("benchalign_overall", "glm-5-3", 80.25)],
        })
        # Harbor has data but no identity for this model (unmatched), while
        # BenchLM resolves the identity but has no tb4 row (not evaluated):
        # the deeper reason wins.
        payload = benchmarks.build_payload(
            [inventory_model("zai-glm-5-3", "mistral/zai-glm-5-3")], state,
            benchlm_enabled=True)
        self.assertEqual(payload["models"][0]["missing"]["tb4"], "not evaluated")
        # A disabled fallback (unavailable) never masks the primary's unmatched.
        payload = benchmarks.build_payload(
            [inventory_model("absent", "x/absent")], state, benchlm_enabled=False)
        self.assertEqual(payload["models"][0]["missing"]["tb4"], "unmatched identity")

    def test_primary_outage_is_never_reported_as_not_evaluated(self):
        # Harbor (the tb4 primary) is enabled but has no data (an outage),
        # while BenchLM covers the model yet has no tb4 row: the primary's
        # "source unavailable" must win over the fallback's deeper
        # "not evaluated".
        state = make_state({
            "harbor": None,
            "benchlm": [self.benchlm_entry("benchalign_overall", "gpt-6-1-sol", 91.5)],
        })
        payload = benchmarks.build_payload(
            [inventory_model("gpt-6.1-sol", "openai/gpt-6.1-sol")], state,
            benchlm_enabled=True)
        model = payload["models"][0]
        self.assertEqual(model["missing"]["tb4"], "source unavailable")
        self.assertEqual(model["bench"]["benchalign_overall"], 91.5)

    def test_benchalign_values_resolve_through_the_reviewed_aliases(self):
        state = make_state({
            "benchlm": [self.benchlm_entry("benchalign_overall", "gpt-6-1-sol", 91.5),
                        self.benchlm_entry("benchalign_coding", "gpt-6-1-sol", 88.0)],
        })
        payload = benchmarks.build_payload(
            [inventory_model("gpt-6.1-sol", "openai/gpt-6.1-sol")], state,
            benchlm_enabled=True)
        model = payload["models"][0]
        self.assertEqual(model["bench"]["benchalign_overall"], 91.5)
        self.assertEqual(model["bench"]["benchalign_coding"], 88.0)
        result = model["results"]["benchalign_overall"][0]
        self.assertEqual(result["source"], "benchlm")
        self.assertEqual(result["evaluator"], benchmarks.BENCHLM_COMPOSITE_EVALUATOR)
        self.assertEqual(result["version"], "benchalign-2026.10.28")
        self.assertEqual(result["date"], "2026-10-28")
        self.assertEqual(result["date_kind"], benchmarks.DATE_KIND_SOURCE_SNAPSHOT)
        self.assertEqual(result["metadata"]["date_kind"],
                         benchmarks.DATE_KIND_SOURCE_SNAPSHOT)
        self.assertTrue(result["selected"])
        source = payload["sources"]["benchlm"]
        self.assertFalse(source["disabled"])
        self.assertEqual(source["warning"], benchmarks.BENCHLM_WARNING)

    def test_disabled_benchlm_hides_cached_rows_and_reports_source_unavailable(self):
        state = make_state({
            "benchlm": [self.benchlm_entry("benchalign_overall", "gpt-6-1-sol", 91.5),
                        self.benchlm_entry("tb4", "gpt-6-1-sol", 55.5)],
        })
        cases = [
            ("module flag off", patch.object(benchmarks, "BENCHLM_ENABLED", False), {}),
            ("override off", patch.object(benchmarks, "BENCHLM_ENABLED", True),
             {"benchlm_enabled": False}),
        ]
        for label, patcher, kwargs in cases:
            with self.subTest(label=label), patcher:
                payload = benchmarks.build_payload(
                    [inventory_model("gpt-6.1-sol", "openai/gpt-6.1-sol")],
                    state, **kwargs)
            model = payload["models"][0]
            self.assertEqual(model["bench"], {})
            self.assertEqual(model["results"], {})
            self.assertEqual(model["missing"]["benchalign_overall"], "source unavailable")
            self.assertEqual(model["missing"]["benchalign_coding"], "source unavailable")
            self.assertEqual(model["missing"]["tb4"], "source unavailable")
            source = payload["sources"]["benchlm"]
            self.assertTrue(source["disabled"])
            self.assertEqual(source["warning"], benchmarks.BENCHLM_DISABLED_WARNING)
            serialized = json.dumps(payload)
            self.assertNotIn("91.5", serialized)
            self.assertNotIn("55.5", serialized)
            # Definitions stay in the metrics contract (like LiveBench).
            self.assertIn("benchalign_overall", [m["key"] for m in payload["metrics"]])

    def test_vals_shaped_tb4_row_flows_from_the_board_into_the_payload(self):
        # Regression: the Vals board's null modelKey used to abort the whole
        # BenchLM refresh with "invalid response schema"; the converted row
        # must now survive the fetch and reach the payload as a benchlm-
        # sourced tb4 result for the canonically-named inventory model.
        responses = benchlm_tb4_responses(
            [], [benchlm_item(None, 27.778,
                              source_model_id="mistralai/mistral-large-4")])
        with benchlm_env(BENCHLM_TEST_KEY):
            client = StubClient(responses)
            result = benchmarks.fetch_benchlm(client, None, deadline())
        state = make_state({"benchlm": result["entries"]})
        payload = benchmarks.build_payload(
            [inventory_model("mistral-large-4", "mistralai/mistral-large-4")],
            state, benchlm_enabled=True)
        model = payload["models"][0]
        self.assertEqual(model["bench"]["tb4"], 27.778)
        self.assertNotIn("tb4", model["missing"])
        selected = [item for item in model["results"]["tb4"] if item["selected"]]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["source"], "benchlm")
        self.assertEqual(selected[0]["model"], "mistral-large-4")
        self.assertEqual(selected[0]["value"], 27.778)
        self.assertEqual(selected[0]["metadata"]["board"],
                         benchmarks.BENCHLM_TB4_BOARDS[1][1])


# ── Inventory and API wrappers ──────────────────────────────────────────────
class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / "litellm.yaml"
        self.path_patch = patch.object(core, "LITELLM_CONFIG_FILE", str(self.config))
        self.path_patch.start()
        self.key_patch = patch.object(core, "_read_opencode_go_key", return_value=None)
        self.key_patch.start()

    def tearDown(self):
        self.key_patch.stop()
        self.path_patch.stop()
        self.temp.cleanup()

    def write(self, text):
        self.config.write_text(text, encoding="utf-8")

    def test_inventory_groups_routes_and_uses_safe_fields_only(self):
        self.write("""
model_list:
  - model_name: gpt-sol
    litellm_params:
      model: openai/gpt-6.1-sol
      api_key: config-secret
  - model_name: gpt-sol-backup
    litellm_params:
      model: openai/gpt-6.1-sol
      api_key: another-secret
  - model_name: embed
    litellm_params:
      model: openai/text-embedding-3-large
    model_info:
      mode: embedding
  - model_name: incomplete
    litellm_params:
      model: openai/gpt-6.1-sol
      api_base: https://secret.invalid/v1
""")
        inventory, error = core._read_litellm_inventory()
        self.assertIsNone(error)
        self.assertEqual(len(inventory), 2)
        gpt = inventory[0]
        self.assertEqual(gpt["name"], "gpt-sol")
        self.assertEqual(gpt["upstream_model"], "openai/gpt-6.1-sol")
        self.assertEqual(gpt["provider"], "openai")
        self.assertEqual(gpt["mode"], "chat")
        self.assertEqual(gpt["routes"], [
            {"name": "gpt-sol", "provider": "openai"},
            {"name": "gpt-sol-backup", "provider": "openai"},
            {"name": "incomplete", "provider": "openai"},
        ])
        self.assertEqual(inventory[1]["mode"], "embedding")
        serialized = json.dumps(inventory)
        self.assertNotIn("config-secret", serialized)
        self.assertNotIn("secret.invalid", serialized)
        self.assertNotIn("api_base", serialized)

    def test_modes_are_explicit_or_conservatively_inferred(self):
        self.write("""
model_list:
  - model_name: chat
    litellm_params:
      model: openai/gpt-6.1-sol
  - model_name: audio
    litellm_params:
      model: mistral/voxtral-mini-latest
  - model_name: embed
    litellm_params:
      model: mistral/mistral-embed
  - model_name: image
    litellm_params:
      model: openai/imagen-4
  - model_name: explicit
    litellm_params:
      model: custom/unknown-model
    model_info:
      mode: completion
""")
        inventory, _ = core._read_litellm_inventory()
        self.assertEqual([model["mode"] for model in inventory],
                         ["chat", "audio_transcription", "embedding",
                          "image_generation", "completion"])

    def test_invalid_configuration_is_reported_without_paths(self):
        for text in ("", "model_list: [", "[]", "model_list: {}",
                     "model_list:\n  - model_name: x\n",
                     "model_list:\n  - model_name: x\n    model_info: []\n    litellm_params:\n      model: x/y\n",
                     "model_list:\n  - model_name: x\n    model_info:\n      mode: 3\n    litellm_params:\n      model: x/y\n"):
            with self.subTest(text=text):
                self.write(text)
                self.assertEqual(core._read_litellm_inventory(), (None, "invalid configuration"))
        self.config.unlink()
        self.assertEqual(core._read_litellm_inventory(), (None, "configuration unavailable"))
        with patch.object(core, "LITELLM_CONFIG_FILE", ""):
            self.assertEqual(core._read_litellm_inventory(), (None, "not configured"))

    def test_legacy_mistral_wrapper_filters_and_keeps_shape(self):
        self.write("""
model_list:
  - model_name: glm53
    litellm_params:
      model: mistral/glm-5.3
      api_key: config-secret
  - model_name: other
    litellm_params:
      model: openai/gpt-6.1-sol
""")
        state = make_state({"harbor": [entry("tb4", "GLM-5.3", 41.82)]})
        with patch.object(benchmarks, "read_state", return_value=state), \
                patch.object(benchmarks, "ensure_refresh_scheduled") as ensure:
            result = core.get_mistral_models()
        ensure.assert_not_called()
        self.assertEqual(set(result), {"models", "bench_metrics", "bench_fetched_at"})
        self.assertEqual(result["models"][0]["name"], "glm53")
        self.assertEqual(result["models"][0]["bench"]["tb4"], 41.82)
        self.assertEqual(len(result["models"]), 1)
        self.assertEqual(result["bench_fetched_at"], NOW)
        self.assertNotIn("config-secret", json.dumps(result))

    def test_ai_services_reads_cache_without_scheduling_any_refresh(self):
        self.write("model_list:\n  - model_name: gpt\n    litellm_params:\n      model: openai/gpt-6.1-sol\n")
        state = make_state({"harbor": [entry("tb4", "GPT-6.1 Sol", 58.18)]})
        with patch.object(benchmarks, "read_state", return_value=state), \
                patch.object(benchmarks, "ensure_refresh_scheduled") as ensure, \
                patch.object(benchmarks, "is_refreshing", return_value=True):
            payload = core.get_ai_services()
        ensure.assert_not_called()
        self.assertTrue(payload["refreshing"])
        self.assertEqual(payload["models"][0]["bench"]["tb4"], 58.18)
        self.assertNotIn("error", payload)
        self.assertTrue(payload["refresh_policy"]["manual_only"])

    def test_ai_services_not_configured_stays_offline_and_safe(self):
        with patch.object(core, "LITELLM_CONFIG_FILE", ""), \
                patch.object(benchmarks, "ensure_refresh_scheduled") as ensure, \
                patch.object(benchmarks, "read_state", return_value=empty_state()), \
                patch.object(benchmarks, "is_refreshing", return_value=False):
            payload = core.get_ai_services()
        ensure.assert_not_called()
        self.assertEqual(payload["models"], [])
        self.assertEqual(payload["error"], "not configured")
        self.assertEqual(set(payload["sources"]), set(benchmarks.SOURCE_NAMES))

    def test_feature_flags_keep_legacy_keys_and_add_ai_services(self):
        features = core.get_features()
        self.assertTrue(features["mistral"])
        self.assertTrue(features["ai_services"])
        self.assertIn("opencode_go", features)
        with patch.object(core, "LITELLM_CONFIG_FILE", ""):
            features = core.get_features()
            self.assertFalse(features["mistral"])
            self.assertFalse(features["ai_services"])
        with patch.object(core, "_read_opencode_go_key", return_value="key"), \
                patch.object(core, "LITELLM_CONFIG_FILE", ""):
            self.assertTrue(core.get_features()["ai_services"])

    def test_legacy_fetch_wrappers_trigger_the_guarded_refresh(self):
        expected = {"ok": True, "refreshing": True, "started": True, "sources": []}
        with patch.object(benchmarks, "request_refresh", return_value=expected) as refresh:
            self.assertEqual(core.fetch_benchmarks(), expected)
            refresh.assert_called_once_with()
        with patch.object(benchmarks, "request_refresh") as refresh:
            self.assertEqual(core.fetch_opencode_go_benchmarks(),
                             {"ok": False, "error": "not configured"})
            refresh.assert_not_called()
        with patch.object(core, "_read_opencode_go_key", return_value="key"), \
                patch.object(benchmarks, "request_refresh", return_value=expected) as refresh:
            self.assertEqual(core.fetch_opencode_go_benchmarks(), expected)
            refresh.assert_called_once_with()
        with patch.object(core, "LITELLM_CONFIG_FILE", ""), \
                patch.object(benchmarks, "request_refresh") as refresh:
            self.assertEqual(core.fetch_benchmarks(),
                             {"ok": False, "error": "not configured"})
            refresh.assert_not_called()

    def test_no_artificial_analysis_code_or_constants_remain(self):
        core_source = (ROOT / "app" / "homelab_core.py").read_text()
        bench_source = (ROOT / "app" / "benchmarks.py").read_text()
        for text in (core_source, bench_source):
            self.assertNotIn("artificialanalysis", text.lower())
            self.assertNotIn("_BENCH_METRICS", text)
            self.assertNotIn("_BENCH_ALIASES", text)
            self.assertNotIn("OPENCODE_BENCH_URL", text)
        self.assertFalse(hasattr(core, "get_opencode_go_benchmarks"))
        self.assertFalse(hasattr(core, "_fetch_aa_benchmarks"))
        self.assertFalse(hasattr(core, "_MISTRAL_BENCH_ALIASES"))


class ApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(core, "prime_counters"), \
                patch.object(core, "start_energy_tracker"), \
                patch.object(core, "start_desktop_monitor"):
            import main
        cls.client = TestClient(main.app)
        cls.main = main

    def test_read_only_ai_services_route_returns_the_contract(self):
        payload = {
            "models": [], "metrics": [], "sources": {}, "refreshing": False,
        }
        with patch.object(core, "get_ai_services", return_value=payload):
            response = self.client.get("/api/ai-services")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_corrupt_cache_never_turns_ai_services_into_a_500(self):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "benchmarks.json"
        path.write_text(json.dumps({
            "schema_version": benchmarks.FILE_SCHEMA_VERSION,
            "sources": {
                "epoch": {
                    "schema_version": benchmarks.SOURCE_SCHEMA_VERSION,
                    "version": benchmarks.SOURCES["epoch"].version,
                    "last_success": 10 ** 400,
                },
            },
        }))
        store = benchmarks.BenchmarkStore(path=str(path), clock=lambda: NOW)
        benchmarks._reset_store_for_tests(store)
        try:
            with patch.object(core, "LITELLM_CONFIG_FILE", ""), \
                    patch.object(benchmarks, "ensure_refresh_scheduled",
                                 return_value=False):
                response = self.client.get("/api/ai-services")
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()["sources"]["epoch"]["last_success"])
            self.assertEqual(response.json()["error"], "not configured")
        finally:
            benchmarks._reset_store_for_tests(None)
            directory.cleanup()

    def test_unhashable_metric_cache_never_turns_ai_services_into_a_500(self):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "benchmarks.json"
        valid_epoch = entry("eci", "Valid Model", 100.0)
        malformed_harbor = dict(entry("tb4", "Harbor Model", 10.0), metric=[])
        path.write_text(json.dumps({
            "schema_version": benchmarks.FILE_SCHEMA_VERSION,
            "sources": {
                "harbor": {
                    "schema_version": benchmarks.SOURCE_SCHEMA_VERSION,
                    "version": benchmarks.SOURCES["harbor"].version,
                    "last_success": NOW,
                    "data": {"entries": [malformed_harbor]},
                },
                "epoch": {
                    "schema_version": benchmarks.SOURCE_SCHEMA_VERSION,
                    "version": benchmarks.SOURCES["epoch"].version,
                    "last_success": NOW,
                    "data": {"entries": [valid_epoch]},
                },
            },
        }))
        store = benchmarks.BenchmarkStore(path=str(path), clock=lambda: NOW)
        benchmarks._reset_store_for_tests(store)
        try:
            with patch.object(core, "LITELLM_CONFIG_FILE", ""), \
                    patch.object(benchmarks, "ensure_refresh_scheduled",
                                 return_value=False):
                response = self.client.get("/api/ai-services")
            self.assertEqual(response.status_code, 200)
            sources = response.json()["sources"]
            self.assertIsNone(sources["harbor"]["last_success"])
            self.assertEqual(sources["epoch"]["last_success"], NOW)
        finally:
            benchmarks._reset_store_for_tests(None)
            directory.cleanup()

    def test_manual_fetch_route_returns_immediately_and_keeps_guard(self):
        result = {"ok": True, "refreshing": True, "started": True,
                  "sources": ["harbor", "epoch"]}
        headers = {"Origin": "https://testserver", "X-Homelab-Request": "1"}
        with patch.object(core, "fetch_benchmarks", return_value=result):
            response = self.client.post("/api/benchmarks/fetch", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), result)
        # Same-origin guard still applies: no browser headers, no launch.
        with patch.object(core, "fetch_benchmarks") as fetch:
            self.assertEqual(self.client.post("/api/benchmarks/fetch").status_code, 403)
            fetch.assert_not_called()

    def test_both_post_routes_share_daily_limit_and_get_never_launches_work(self):
        directory, store, path = temp_store(
            thread_factory=lambda target, name: _FakeThread(target, name))
        benchmarks._reset_store_for_tests(store)
        headers = {"Origin": "https://testserver", "X-Homelab-Request": "1"}
        try:
            with patch.object(core, "LITELLM_CONFIG_FILE", "/absent/config.yaml"), \
                    patch.object(core, "_read_opencode_go_key", return_value="fixture"):
                for _ in range(3):
                    response = self.client.get("/api/ai-services")
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json()["refresh_policy"]["can_refresh"])
                    self.assertFalse(store.refreshing)
                self.assertEqual(self.client.post("/api/benchmarks/fetch").status_code, 403)
                self.assertIsNone(store.refresh_policy()["last_attempt"])
                first = self.client.post("/api/benchmarks/fetch", headers=headers)
                self.assertTrue(first.json()["started"])
                store._end_coordinator()  # fake worker has completed/crashed
                for route in ("/api/benchmarks/fetch", "/api/opencode-go-benchmarks/fetch"):
                    denied = self.client.post(route, headers=headers)
                    self.assertEqual(denied.status_code, 429)
                    self.assertEqual(denied.headers["retry-after"], "86400")
                    self.assertEqual(denied.headers["cache-control"], "no-store")
                    self.assertFalse(denied.json()["started"])
                # Restart and repeat GET: neither cooldown nor read behavior resets.
                reloaded = benchmarks.BenchmarkStore(path=path, clock=lambda: NOW + 60)
                benchmarks._reset_store_for_tests(reloaded)
                response = self.client.get("/api/ai-services")
                self.assertFalse(response.json()["refresh_policy"]["can_refresh"])
                self.assertFalse(reloaded.refreshing)
                denied = self.client.post("/api/benchmarks/fetch", headers=headers)
                self.assertEqual(denied.status_code, 429)
        finally:
            benchmarks._reset_store_for_tests(None)
            directory.cleanup()

    def test_unwritable_refresh_history_reports_503_without_launching_work(self):
        result = {"ok": False, "error": "refresh history unavailable",
                  "refresh_policy": {"error": "refresh history unavailable"}}
        headers = {"Origin": "https://testserver", "X-Homelab-Request": "1"}
        for route, function in (("/api/benchmarks/fetch", "fetch_benchmarks"),
                                ("/api/opencode-go-benchmarks/fetch", "fetch_opencode_go_benchmarks")):
            with patch.object(core, function, return_value=result):
                response = self.client.post(route, headers=headers)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), result)

    def test_legacy_and_new_routes_are_registered(self):
        routes = {route.path: route.methods for route in self.main.app.routes
                  if hasattr(route, "methods")}
        self.assertIn("GET", routes["/api/ai-services"])
        self.assertIn("GET", routes["/api/mistral-models"])
        self.assertIn("GET", routes["/api/opencode-go"])
        self.assertIn("GET", routes["/api/opencode-go-limits"])
        self.assertIn("POST", routes["/api/benchmarks/fetch"])
        self.assertIn("POST", routes["/api/opencode-go-benchmarks/fetch"])

    def test_disabled_sources_are_never_fetched_by_the_store(self):
        called = []
        fetcher = lambda *args: called.append(True)
        directory = tempfile.TemporaryDirectory()
        store = benchmarks.BenchmarkStore(
            path=str(Path(directory.name) / "b.json"), clock=lambda: NOW,
            fetchers={"livebench": fetcher})
        try:
            with patch.object(benchmarks, "LIVEBENCH_ENABLED", False):
                enabled = store._enabled_sources()
                self.assertEqual(set(enabled), {"harbor", "epoch"})
                self.assertFalse(store.refresh_source("livebench"))
            self.assertFalse(called)
        finally:
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
