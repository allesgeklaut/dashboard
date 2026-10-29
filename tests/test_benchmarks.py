"""Offline tests for benchmark adapters, snapshots, identities and the API contract."""
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests
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


def deepswe_response(rows=None, *, task_count=113, generated_at="2026-09-22T06:27:15.860279+00:00"):
    rows = rows if rows is not None else [
        {"model": "gpt-6-astra", "harness": "mini-swe-agent", "reasoning_effort": "xhigh",
         "config": "mini_swe_agent_gpt_6_astra_xhigh", "pass_at_1": 0.74115,
         "pass_at_4": 0.8053, "ci_lo": 0.7125, "ci_hi": 0.7698, "ci_half": 0.0287,
         "n_runs": 4, "ci_method": "fixture"},
        {"model": "gpt-6-astra", "harness": "mini-swe-agent", "reasoning_effort": "max",
         "config": "mini_swe_agent_gpt_6_astra_max", "pass_at_1": 0.7323,
         "ci_lo": 0.70, "ci_hi": 0.76, "ci_half": 0.03, "n_runs": 4, "ci_method": "fixture"},
    ]
    payload = {"scope": "fixture", "unit": "pass@1", "generated_at": generated_at,
               "n_tasks_in_set": task_count,
               "latest_job": {"name": "fixture", "finished_at": "2026-09-01T07:35:13Z"},
               "rows": rows}
    return benchmarks.Response(200, {}, json.dumps(payload).encode())


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
    assert source is None or spec.source == source
    if date is _UNSET:
        # DeepSWE carries no eval date; other sources default to a dated fixture.
        date = None if spec.source == "deepswe" else "2026-09-01"
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


# ── DeepSWE ─────────────────────────────────────────────────────────────────
class DeepSweAdapterTests(unittest.TestCase):
    def test_pass_at_1_is_converted_to_percent_and_configs_are_retained(self):
        response = deepswe_response()
        response = benchmarks.Response(200, {"etag": 'W/"d"'}, response.body)
        result = benchmarks.fetch_deepswe(StubClient([response]), None, deadline())
        self.assertEqual(len(result["entries"]), 2)
        self.assertEqual(result["entries"][0]["value"], 74.115)
        self.assertEqual(result["entries"][0]["ci"]["low"], 71.25)
        self.assertIsNone(result["entries"][0]["date"])
        self.assertIsNone(result["entries"][0]["date_kind"])
        self.assertEqual(result["source_updated"], "2026-09-22")
        self.assertEqual(result["entries"][0]["metadata"]["task_count"], 113)
        self.assertIn("23 of 113", benchmarks.DEEPSWE_WARNING)
        self.assertIn(benchmarks.DEEPSWE_REVIEW_URL, benchmarks.DEEPSWE_WARNING)

    def test_actual_row_eval_date_is_used_when_the_row_supplies_one(self):
        rows = [{
            "model": "gpt-6-astra", "harness": "mini-swe-agent",
            "reasoning_effort": "max", "config": "fixture",
            "pass_at_1": 0.5, "evaluated_at": "2026-09-05T10:11:12Z",
        }]
        result = benchmarks.fetch_deepswe(
            StubClient([deepswe_response(rows)]), None, deadline())
        self.assertEqual(result["entries"][0]["date"], "2026-09-05")
        self.assertEqual(result["entries"][0]["date_kind"],
                         benchmarks.DATE_KIND_EVALUATION)

    def test_ranges_tasks_and_timestamps_are_validated(self):
        bad = [
            deepswe_response(task_count=112),
            deepswe_response(generated_at="not-a-date"),
            deepswe_response([{"model": "x", "pass_at_1": 1.1}]),
            deepswe_response([{"model": "x", "pass_at_1": 0.5, "ci_lo": 0.8, "ci_hi": 0.2}]),
            deepswe_response([]),
        ]
        for response in bad:
            with self.subTest(body=response.body[:80]):
                with self.assertRaises(benchmarks.SourceError):
                    benchmarks.fetch_deepswe(StubClient([response]), None, deadline())

    def test_conditional_request_and_304(self):
        previous = {"validators": {"last_modified": "Mon, 26 Oct 2026 08:25:02 GMT"}}
        client = StubClient([benchmarks.Response(304, {}, b"")])
        result = benchmarks.fetch_deepswe(client, previous, deadline())
        self.assertTrue(result["not_modified"])
        self.assertEqual(client.calls[0]["headers"]["If-Modified-Since"],
                         "Mon, 26 Oct 2026 08:25:02 GMT")


# ── HTTP and provenance security ────────────────────────────────────────────
class _FakeResponse:
    def __init__(self, status=200, body=b"{}", headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks if chunks is not None else [body]
        self.closed = False

    def iter_content(self, chunk_size=8192):
        for chunk in self._chunks:
            yield chunk

    def close(self):
        self.closed = True


class HttpSecurityTests(unittest.TestCase):
    def test_only_fixed_public_https_endpoints_are_accepted(self):
        for url in (benchmarks.HARBOR_ENDPOINT, benchmarks.EPOCH_CSV_URL,
                    benchmarks.LIVEBENCH_CSV_URL, benchmarks.LIVEBENCH_CATEGORIES_URL,
                    benchmarks.DEEPSWE_JSON_URL):
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

    def test_client_ignores_environment_auth_and_never_follows_redirects(self):
        client = benchmarks.HttpClient(sleep=lambda _seconds: None)
        self.assertFalse(client._session.trust_env)
        self.assertIsNone(client._session.auth)
        self.assertNotIn("Authorization", client._session.headers)
        fake = _FakeResponse(status=302, headers={"location": "https://evil.invalid/"})
        with patch.object(client._session, "request", return_value=fake) as request:
            with self.assertRaises(benchmarks.SourceError):
                client.request("GET", benchmarks.EPOCH_CSV_URL, deadline=deadline())
        kwargs = request.call_args.kwargs
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["verify"])
        self.assertTrue(kwargs["stream"])
        self.assertNotIn("auth", kwargs)

    def test_retries_use_bounded_backoff_and_sanitized_errors(self):
        client = benchmarks.HttpClient(sleep=lambda _seconds: None)
        ok = _FakeResponse(body=b'{"ok":true}')
        with patch.object(client._session, "request",
                          side_effect=[requests.ConnectionError("https://secret.invalid/?token=x"),
                                       requests.ConnectionError("https://secret.invalid/?token=x"),
                                       ok]) as request:
            result = client.request("GET", benchmarks.EPOCH_CSV_URL, deadline=deadline())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(request.call_count, 3)
        with patch.object(client._session, "request",
                          side_effect=requests.ConnectionError("https://secret.invalid/?token=x")):
            with self.assertRaises(benchmarks.SourceError) as caught:
                client.request("GET", benchmarks.EPOCH_CSV_URL, deadline=deadline())
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("http", str(caught.exception).lower())

    def test_body_budget_and_deadline_are_enforced(self):
        client = benchmarks.HttpClient(max_bytes=10, sleep=lambda _seconds: None)
        with patch.object(client._session, "request",
                          return_value=_FakeResponse(body=b"x" * 11)):
            with self.assertRaises(benchmarks.SourceError):
                client.request("GET", benchmarks.EPOCH_CSV_URL, deadline=deadline())
        with self.assertRaises(benchmarks.SourceError):
            client.request("GET", benchmarks.EPOCH_CSV_URL, deadline=deadline(0))

    def test_empty_stream_chunks_still_honor_the_source_deadline(self):
        class Clock:
            now = 0.0

            def __call__(self):
                return self.now

        clock = Clock()
        client = benchmarks.HttpClient(sleep=lambda _seconds: None)
        source_deadline = benchmarks.Deadline(1.0, clock=clock)
        clock.now = 2.0
        with self.assertRaises(benchmarks.SourceError):
            client._read_body(_FakeResponse(chunks=[b""]), source_deadline)

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
        with patch.object(client._session, "request",
                          side_effect=requests.ConnectionError("fixture")):
            with self.assertRaises(benchmarks.SourceError):
                client.request("GET", benchmarks.EPOCH_CSV_URL,
                               deadline=benchmarks.Deadline(0.75, clock=clock))
        self.assertTrue(delays)
        self.assertLessEqual(sum(delays), 0.750001)
        self.assertTrue(all(delay >= 0 for delay in delays))

    def test_streaming_body_transport_error_is_sanitized(self):
        class BrokenStream(_FakeResponse):
            def iter_content(self, chunk_size=8192):
                raise requests.ConnectionError(
                    "https://user:fixture-secret@internal.invalid/")
                yield b""  # pragma: no cover

        client = benchmarks.HttpClient(sleep=lambda _seconds: None)
        with patch.object(client._session, "request",
                          return_value=BrokenStream()):
            with self.assertRaises(benchmarks.SourceError) as caught:
                client.request("GET", benchmarks.EPOCH_CSV_URL, deadline=deadline())
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertEqual(str(caught.exception), "network error")

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
        urls = re.findall(r"https://[^\s)]+", benchmarks.SOURCES["deepswe"].warning)
        self.assertTrue(urls)
        for url in urls:
            self.assertIn(urlsplit(url).hostname, provenance_hosts)


# ── Persistence, staleness and failure isolation ────────────────────────────
class SnapshotPersistenceTests(unittest.TestCase):
    def test_successful_refresh_persists_versioned_snapshot_and_reloads(self):
        directory, store, path = temp_store(fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "GPT-6.1 Sol", 58.18)])),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench", "deepswe")},
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
               for name in ("epoch", "livebench", "deepswe")},
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
               for name in ("epoch", "livebench", "deepswe")},
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

    def test_atomic_replace_failure_preserves_previous_file(self):
        directory, store, path = temp_store(fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "Model", 10.0)])),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench", "deepswe")},
        })
        try:
            Path(path).write_text("old-cache")
            with patch.object(benchmarks.os, "replace", side_effect=OSError("fixture")):
                self.assertTrue(store.refresh_source("harbor"))
            self.assertEqual(Path(path).read_text(), "old-cache")
            self.assertEqual([item.name for item in Path(path).parent.iterdir()],
                             ["benchmarks.json"])
        finally:
            directory.cleanup()

    def test_successful_refresh_replaces_records_instead_of_merging(self):
        old = snapshot([entry("tb4", "Old Model", 10.0)])
        new = snapshot([entry("tb4", "New Model", 20.0)])
        directory, store, path = temp_store(fetchers={
            "harbor": static_fetcher(old),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench", "deepswe")},
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
            ], state, deepswe_enabled=False)
            self.assertEqual(payload["models"][0]["bench"], {})
            self.assertEqual(payload["models"][1]["bench"]["tb4"], 20.0)
        finally:
            directory.cleanup()

    def test_304_without_any_cached_body_is_rejected(self):
        directory, store, _ = temp_store(fetchers={
            "harbor": static_fetcher({"not_modified": True}),
            **{name: static_fetcher(benchmarks.SourceError("empty feed"))
               for name in ("epoch", "livebench", "deepswe")},
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
               for name in ("epoch", "livebench", "deepswe")},
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
            "deepswe": static_fetcher(benchmarks.SourceError("empty feed")),
        })
        try:
            store.refresh_now(force=True)
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
               for name in ("epoch", "livebench", "deepswe")},
        })
        try:
            store.refresh_source("harbor")
            state = store.read_state(now=NOW)
            self.assertNotIn("secret", json.dumps(state))
            payload = benchmarks.build_payload(
                [inventory_model("m", "openai/m")], state, deepswe_enabled=False)
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

    def test_auto_refresh_starts_once_and_only_for_stale_sources(self):
        created = []

        def factory(target, name):
            thread = _FakeThread(target, name)
            created.append(thread)
            return thread

        directory, store, _ = temp_store(thread_factory=factory)
        try:
            self.assertTrue(store.ensure_refresh_scheduled())
            self.assertFalse(store.ensure_refresh_scheduled())
            self.assertEqual(len(created), 1)
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
                [inventory_model("m", "openai/m")], state, deepswe_enabled=False)
            self.assertFalse(called)
            self.assertEqual(payload["models"][0]["missing"]["tb4"], "source unavailable")
        finally:
            directory.cleanup()

    def test_worker_refreshes_sources_independently(self):
        directory, store, _ = temp_store(fetchers={
            "harbor": static_fetcher(snapshot([entry("tb4", "A", 10.0)])),
            "epoch": static_fetcher(benchmarks.SourceError("empty feed")),
            "livebench": static_fetcher(benchmarks.SourceError("empty feed")),
            "deepswe": static_fetcher(benchmarks.SourceError("empty feed")),
        })
        try:
            with patch.object(benchmarks, "LIVEBENCH_ENABLED", True), \
                    patch.object(benchmarks, "DEEPSWE_ENABLED", True):
                store._refresh_worker(force=True)
            state = store.read_state(now=NOW)
            self.assertFalse(state["harbor"]["stale"])
            self.assertEqual(state["epoch"]["error"], "empty feed")
        finally:
            directory.cleanup()


# ── Payload contract and identities ─────────────────────────────────────────
class PayloadContractTests(unittest.TestCase):
    def setUp(self):
        self.livebench_patch = patch.object(benchmarks, "LIVEBENCH_ENABLED", True)
        self.deepswe_patch = patch.object(benchmarks, "DEEPSWE_ENABLED", True)
        self.livebench_patch.start()
        self.deepswe_patch.start()

    def tearDown(self):
        self.deepswe_patch.stop()
        self.livebench_patch.stop()

    def state(self, entries):
        return make_state(entries)

    def test_top_level_contract_and_metric_metadata(self):
        state = self.state({
            "harbor": [entry("tb4", "GPT-6.1 Sol", 58.18)],
            "epoch": [entry("eci", "GPT-6.1 Sol", 168.37)],
            "livebench": [entry("livebench_coding", "gpt-6.1-sol", 96.0)],
            "deepswe": [entry("deepswe", "gpt-6-astra", 74.1)],
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
        self.assertEqual(model["missing"]["deepswe"], "unmatched identity")
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
            "epoch": [], "livebench": [], "deepswe": [],
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
            "epoch": [], "livebench": [], "deepswe": [],
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
            "epoch": [], "livebench": [], "deepswe": [],
        })
        payload = benchmarks.build_payload([inventory_model("model", "x/model")], state)
        model = payload["models"][0]
        self.assertEqual(model["bench"]["tb4"], 48.0)  # max effort, not max score
        selected = [item for item in model["results"]["tb4"] if item["selected"]]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["effort"], "max")

    def test_undated_deepswe_scores_are_retained_but_never_summarized(self):
        state = self.state({
            "deepswe": [entry("deepswe", "gpt-6-astra", 74.1)],
            "harbor": [], "epoch": [], "livebench": [],
        })
        model = benchmarks.build_payload(
            [inventory_model("gpt-6-astra", "openai/gpt-6-astra")], state)["models"][0]
        self.assertNotIn("deepswe", model["bench"])
        self.assertEqual(model["missing"]["deepswe"], "not evaluated")
        result = model["results"]["deepswe"][0]
        self.assertEqual(result["value"], 74.1)
        self.assertIsNone(result["date"])
        self.assertIsNone(result["date_kind"])
        self.assertNotIn("date_kind", result["metadata"])

    def test_multiple_undated_configurations_never_get_a_summary(self):
        state = self.state({
            "harbor": [
                entry("tb4", "Model", 60.0, date=None, effort="max"),
                entry("tb4", "Model", 70.0, date=None, effort="low"),
            ],
            "epoch": [], "livebench": [], "deepswe": [],
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
            "deepswe": [entry("deepswe", "glm-5-3", 68.96)],
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
        # DeepSWE rows are undated, so no summary is invented; the result value
        # remains available in the per-configuration list.
        self.assertNotIn("deepswe", models[2]["bench"])
        self.assertEqual(models[2]["missing"]["deepswe"], "not evaluated")
        self.assertEqual(models[2]["results"]["deepswe"][0]["value"], 68.96)
        self.assertIsNone(models[2]["results"]["deepswe"][0]["date"])
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
            "deepswe": [
                entry("deepswe", "glm-5-3", 68.96),
                entry("deepswe", "glm-5-3-flash", 63.39),
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
        # DeepSWE aliases resolve, but its rows are undated, so the values stay
        # in results and the summary remains "not evaluated" by contract.
        self.assertEqual(models[0]["results"]["deepswe"][0]["value"], 68.96)
        self.assertEqual(models[1]["results"]["deepswe"][0]["value"], 63.39)
        self.assertEqual(models[0]["missing"]["deepswe"], "not evaluated")
        # Only the reviewed Epoch alias was added for Mistral Large 4.
        self.assertEqual(models[2]["bench"],
                         {"eci": 150.03, "livebench_coding": 60.0})
        self.assertEqual(models[2]["missing"]["tb4"], "unmatched identity")
        self.assertEqual(models[2]["missing"]["deepswe"], "unmatched identity")

    def test_mistral_rolling_latest_and_undocumented_variants_stay_unmatched(self):
        state = self.state({
            "harbor": [entry("tb4", "GLM-5.3", 41.82)],
            "epoch": [entry("eci", "Mistral Large 4", 150.03)],
            "livebench": [entry("livebench_coding", "glm-5.3", 80.0)],
            "deepswe": [entry("deepswe", "glm-5-3", 68.96)],
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
            "deepswe": [entry("deepswe", "glm-5-3", 68.96)],
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
        self.assertEqual(normal["results"]["deepswe"][0]["value"], 68.96)

    def test_mistral_alias_routes_still_exclude_noncoding_and_abliterated(self):
        state = self.state({
            "harbor": [entry("tb4", "GLM-5.3", 41.82)],
            "epoch": [entry("eci", "GLM-5.3", 155.99)],
            "livebench": [], "deepswe": [],
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

    def test_no_fuzzy_prefix_or_rolling_latest_matching(self):
        state = self.state({
            "harbor": [entry("tb4", "Opus 5.5", 64.85)],
            "epoch": [entry("eci", "Claude Opus 5.5", 168.83)],
            "livebench": [entry("livebench_coding", "claude-opus-5.5", 96.3)],
            "deepswe": [],
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
            "epoch": [], "livebench": [], "deepswe": [],
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
            "epoch": [], "livebench": [], "deepswe": [],
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
            "harbor": [], "epoch": None, "deepswe": [],
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
            "harbor": [], "epoch": [], "deepswe": [],
        })
        model = benchmarks.build_payload(
            [inventory_model("model", "x/model")], state)["models"][0]
        self.assertEqual(model["missing"]["livebench_coding"], "not evaluated")
        self.assertEqual(model["bench"]["livebench_reasoning"], 90.0)

    def test_openai_is_a_transport_provider_not_an_identity_source(self):
        state = self.state({
            "harbor": [entry("tb4", "GPT-6.1 Sol", 58.18)],
            "epoch": [], "livebench": [], "deepswe": [],
        })
        model = benchmarks.build_payload(
            [inventory_model("azure-route", "openai/gpt-6.1-sol")], state)["models"][0]
        self.assertEqual(model["provider"], "openai")
        self.assertEqual(model["bench"]["tb4"], 58.18)

    def test_disabled_livebench_definitions_remain_but_scores_are_hidden(self):
        with patch.object(benchmarks, "LIVEBENCH_ENABLED", False):
            state = self.state({
                "livebench": [entry("livebench_coding", "gpt-6.1-sol", 96.0)],
                "harbor": [], "epoch": [], "deepswe": [],
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

    def test_disabled_deepswe_is_optional_and_hides_cached_rows(self):
        with patch.object(benchmarks, "DEEPSWE_ENABLED", False):
            state = self.state({
                "deepswe": [entry("deepswe", "gpt-6-astra", 74.1)],
                "harbor": [], "epoch": [], "livebench": [],
            })
            payload = benchmarks.build_payload(
                [inventory_model("gpt-6-astra", "openai/gpt-6-astra")], state)
        self.assertNotIn("deepswe", [m["key"] for m in payload["metrics"]])
        self.assertTrue(payload["sources"]["deepswe"]["disabled"])
        self.assertEqual(payload["models"][0]["bench"], {})
        self.assertNotIn("deepswe", payload["models"][0]["missing"])

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
            "livebench": [], "deepswe": [],
        })
        payload = benchmarks.build_payload([inventory_model("model", "x/model")], state)
        for model in payload["models"]:
            for value in model["bench"].values():
                self.assertTrue(math.isfinite(value))


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

    def test_ai_services_reads_cache_and_schedules_guarded_refresh(self):
        self.write("model_list:\n  - model_name: gpt\n    litellm_params:\n      model: openai/gpt-6.1-sol\n")
        state = make_state({"harbor": [entry("tb4", "GPT-6.1 Sol", 58.18)]})
        with patch.object(benchmarks, "read_state", return_value=state), \
                patch.object(benchmarks, "ensure_refresh_scheduled") as ensure, \
                patch.object(benchmarks, "is_refreshing", return_value=True):
            payload = core.get_ai_services()
        ensure.assert_called_once_with()
        self.assertTrue(payload["refreshing"])
        self.assertEqual(payload["models"][0]["bench"]["tb4"], 58.18)
        self.assertNotIn("error", payload)

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
            fetchers={"livebench": fetcher, "deepswe": fetcher})
        try:
            with patch.object(benchmarks, "LIVEBENCH_ENABLED", False), \
                    patch.object(benchmarks, "DEEPSWE_ENABLED", False):
                enabled = store._enabled_sources()
                self.assertEqual(set(enabled), {"harbor", "epoch"})
                self.assertFalse(store.refresh_source("livebench"))
                self.assertFalse(store.refresh_source("deepswe"))
            self.assertFalse(called)
        finally:
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
