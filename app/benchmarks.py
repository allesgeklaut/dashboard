"""Benchmark source adapters, snapshots, and AI Services payload assembly.

Four fixed, public, no-auth sources replace the removed Artificial Analysis
scraping:

* Harbor (Terminal-Bench 4.0), a documented public JSON leaderboard read.
* Epoch AI's ECI CSV (abstract composite index; CC BY 4.0 attribution).
* LiveBench, pinned to the explicitly released ``20261025`` table.
* DeepSWE (optional, disabled by default because of documented task grading
  defects; see ``DEEPSWE_WARNING``).

Security posture: only the module's fixed HTTPS URLs are ever requested, TLS
verification is on, redirects are rejected, sessions ignore proxy/netrc
configuration (``trust_env = False``), no credentials are attached, response
bodies are streamed with a byte budget under one absolute wall-clock source
deadline that also bounds connection setup and header receipt, and every
upstream error is mapped to a fixed sanitized message.  No upstream content is
executed and no upstream URLs are copied into API responses.

Nothing here is a database or a job queue: per-source state is held in memory
and persisted as one versioned, atomically replaced JSON document at
``HOMELAB_BENCH_FILE``.  A failed source keeps its last good snapshot.

Default source availability: Harbor and Epoch are enabled.  LiveBench and
DeepSWE are opt-in (``HOMELAB_LIVEBENCH_ENABLED`` / ``HOMELAB_DEEPSWE_ENABLED``,
both false by default) because their scores' reuse terms could not be verified
here; disabled sources are never fetched and their cached rows are never
exposed, although the metric/source definitions stay visible so the UI can
explain the missing column.

Dates are semantic, never inferred: every dated entry carries ``date_kind``
(``model_release`` for Harbor/Epoch, ``benchmark_release`` for LiveBench, or
``evaluation`` only when a source actually supplies an eval date).  Summary
selection uses the latest published dated result and never relabels it as an
evaluation.  ``source_updated`` is feed mutation metadata only (Harbor
leaderboard/row timestamps, Epoch Last-Modified, DeepSWE generated_at), never a
model-release or evaluation date.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import csv
import inspect
import io
import json
import logging
import math
import os
import queue
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import date as _date
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

_log = logging.getLogger(__name__)

# ── Fixed endpoints ──────────────────────────────────────────────────────────
# These are the only URLs this module will ever request.  They are public,
# documented, credential-free feeds; no user or config supplied URL is used.
HARBOR_ENDPOINT = "https://api.harborframework.com/functions/v1/leaderboard-read"
HARBOR_PACKAGE = "terminal-bench/terminal-bench"
HARBOR_LEADERBOARD = "4-0-0"
HARBOR_PAGE_SIZE = 100
HARBOR_MAX_PAGES = 50
HARBOR_DOC_URL = "https://docs.harborframework.com/harbor-hub/leaderboards"

EPOCH_CSV_URL = "https://epoch.ai/data/eci_scores.csv"
EPOCH_PROVENANCE_URL = "https://epoch.ai/eci"

LIVEBENCH_VERSION = "20261025"
LIVEBENCH_CSV_URL = f"https://livebench.ai/data/{LIVEBENCH_VERSION}/table.csv"
LIVEBENCH_CATEGORIES_URL = f"https://livebench.ai/data/{LIVEBENCH_VERSION}/categories.json"

DEEPSWE_VERSION = "v1.1"
DEEPSWE_JSON_URL = (
    "https://deepswe.datacurve.ai/artifacts/v1.1/leaderboard-live.json"
)
DEEPSWE_URL = "https://deepswe.datacurve.ai/"
DEEPSWE_TASKS = 113
DEEPSWE_KNOWN_DEFECTIVE_TASKS = 23
DEEPSWE_REVIEW_URL = "https://epoch.ai/benchmarks/deepswe/review"

ALLOWED_HOSTS = frozenset({
    "api.harborframework.com",
    "epoch.ai",
    "livebench.ai",
    "deepswe.datacurve.ai",
})

# ── Budgets ─────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = 10.0          # connect/read timeout per upstream request
SOURCE_DEADLINE = 30.0          # wall-clock budget for one source refresh
MAX_BODY_BYTES = 4 * 1024 * 1024
HTTP_ATTEMPTS = 3
RETRY_BACKOFF = 0.5
RETRY_BACKOFF_MAX = 4.0
MAX_ENTRIES = 6000
MAX_CACHE_BYTES = 16 * 1024 * 1024

# ── Snapshots ───────────────────────────────────────────────────────────────
FILE_SCHEMA_VERSION = 2         # deliberately different from the old AA cache
SOURCE_SCHEMA_VERSION = 2
CACHE_TTL = 24 * 60 * 60        # per-source freshness target
AUTO_RETRY_BASE = 300
AUTO_RETRY_MAX = 3600

# Opt-in flags.  LiveBench 2.0 score reuse terms and DeepSWE artifact terms
# could not be verified; both sources stay disabled (no fetching, no scores
# exposed) until an operator reviews them and enables the flag deliberately.
def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


LIVEBENCH_ENABLED = _env_flag("HOMELAB_LIVEBENCH_ENABLED")
DEEPSWE_ENABLED = _env_flag("HOMELAB_DEEPSWE_ENABLED")

USER_AGENT = "homelab-ctrl-benchmarks/1.0"

# ── Metric/source metadata ──────────────────────────────────────────────────
MISSING_NOT_EVALUATED = "not evaluated"
MISSING_UNMATCHED = "unmatched identity"
MISSING_UNSUPPORTED = "unsupported modality"
MISSING_UNAVAILABLE = "source unavailable"

# The result's date is never renamed into an evaluation date.  Known values are
# deliberately narrow and are persisted/revalidated with the entry.
DATE_KIND_MODEL_RELEASE = "model_release"
DATE_KIND_BENCHMARK_RELEASE = "benchmark_release"
DATE_KIND_EVALUATION = "evaluation"
DATE_KINDS = frozenset({
    DATE_KIND_MODEL_RELEASE, DATE_KIND_BENCHMARK_RELEASE, DATE_KIND_EVALUATION,
})


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    name: str
    unit: str
    source: str

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "name": self.name,
                "unit": self.unit, "source": self.source}


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("tb4", "TB4", "Terminal-Bench 4.0", "pct", "harbor"),
    MetricSpec("livebench_coding", "LB-CODE", "LiveBench Coding", "pct", "livebench"),
    MetricSpec("eci", "ECI", "Epoch Capabilities Index", "index", "epoch"),
    MetricSpec("livebench_long_agentic", "LB-AGENT", "LiveBench Long Agentic", "pct", "livebench"),
    MetricSpec("livebench_reasoning", "LB-REASON", "LiveBench Reasoning", "pct", "livebench"),
    MetricSpec("livebench_browser", "LB-BROWSER", "LiveBench Browser", "pct", "livebench"),
    MetricSpec("livebench_data_analysis", "LB-DATA", "LiveBench Data Analysis", "pct", "livebench"),
    MetricSpec("livebench_research", "LB-RESEARCH", "LiveBench Research", "pct", "livebench"),
    MetricSpec("livebench_design", "LB-DESIGN", "LiveBench Design", "pct", "livebench"),
    MetricSpec("deepswe", "DSWE", "DeepSWE pass@1", "pct", "deepswe"),
)
METRIC_BY_KEY = {spec.key: spec for spec in METRICS}
LIVEBENCH_COLUMNS = {
    "long_agentic": "livebench_long_agentic",
    "reasoning": "livebench_reasoning",
    "coding": "livebench_coding",
    "browser": "livebench_browser",
    "data_analysis": "livebench_data_analysis",
    "research": "livebench_research",
    "design": "livebench_design",
}
# Each CSV row expands to at most seven entries.  Cap advertised rows so the
# expansion can never exceed the shared entry limit (858 rows would produce
# 6006 entries and be rejected after a full fetch).
MAX_LIVEBENCH_ROWS = MAX_ENTRIES // len(LIVEBENCH_COLUMNS)  # 857 at 6000/7


@dataclass(frozen=True)
class SourceSpec:
    key: str
    version: str
    url: str
    warning: str


HARBOR_WARNING = (
    "Runs use each project's native agent and harness; compare the labelled "
    "configurations rather than treating the value as a pure-model score."
)
EPOCH_WARNING = (
    "ECI is an abstract composite index, not a percentage. It mixes evaluations "
    "and some vendor-reported results. Epoch AI ECI data, CC BY 4.0; cite Epoch "
    "AI and the ECI authors."
)
LIVEBENCH_WARNING = (
    "Pinned LiveBench release 20261025. Coding scores can saturate near 100; "
    "research and design involve rubric or visual grading."
)
DEEPSWE_WARNING = (
    f"Datacurve DeepSWE (2026; Wenqi Huang, Charley Lee, Leonard Tng, Serena Ge). "
    f"Artifact reuse terms are unverified and this source is opt-in. Epoch AI "
    f"documents grading defects in at least {DEEPSWE_KNOWN_DEFECTIVE_TASKS} of "
    f"{DEEPSWE_TASKS} tasks (mainly agent/hidden-test collisions); scores are not "
    f"corrected here. Review: {DEEPSWE_REVIEW_URL}"
)
LIVEBENCH_DISABLED_WARNING = (
    "LiveBench 2.0 reuse terms unverified; opt-in after review "
    "(HOMELAB_LIVEBENCH_ENABLED). Cached scores are hidden while disabled."
)
DEEPSWE_DISABLED_WARNING = (
    f"{DEEPSWE_WARNING} Disabled: set HOMELAB_DEEPSWE_ENABLED only after reviewing "
    "the artifact terms."
)
SOURCE_DISABLED_WARNINGS = {
    "livebench": LIVEBENCH_DISABLED_WARNING,
    "deepswe": DEEPSWE_DISABLED_WARNING,
}

SOURCES: dict[str, SourceSpec] = {
    "harbor": SourceSpec("harbor", HARBOR_LEADERBOARD, HARBOR_DOC_URL, HARBOR_WARNING),
    "epoch": SourceSpec("epoch", "eci_scores", EPOCH_PROVENANCE_URL, EPOCH_WARNING),
    "livebench": SourceSpec("livebench", LIVEBENCH_VERSION, LIVEBENCH_CSV_URL, LIVEBENCH_WARNING),
    "deepswe": SourceSpec("deepswe", DEEPSWE_VERSION, DEEPSWE_URL, DEEPSWE_WARNING),
}
SOURCE_NAMES = tuple(SOURCES)

# ── Reviewed exact identities ───────────────────────────────────────────────
# Keys are provider-stripped LiteLLM upstream IDs (case-insensitive exact
# lookup); values are the exact identifiers published by that source.  These
# are curated one-to-one identities, never produced by dropping punctuation,
# matching prefixes/version ranges, or mapping a rolling "latest" alias.  The
# LiveBench table publishes exact API-style IDs (for example claude-opus-5.5),
# so it only needs the reviewed Mistral-hosted ZAI aliases below.
SOURCE_ALIASES: dict[str, dict[str, str]] = {
    "harbor": {
        "claude-fable-5": "Fable 5",
        "claude-fable-5.1": "Fable 5.1",
        "glm-5.3": "GLM-5.3",
        "glm-5.3-flash": "GLM-5.3-Flash",
        # Mistral publishes these as hosted ZAI API IDs and says the model is
        # "served without Mistral modifications" (v5.3), reviewed 2026-10-29:
        # https://docs.mistral.ai/models/zai-glm-5-3
        # https://docs.mistral.ai/models/zai-glm-5-3-flash
        "zai-glm-5-3": "GLM-5.3",
        "zai-glm-5-3-flash": "GLM-5.3-Flash",
        "gpt-5.6-luna": "GPT-5.6 Luna",
        "gpt-5.6-sol": "GPT-5.6 Sol",
        "gpt-5.6-terra": "GPT-5.6 Terra",
        "gpt-6-astra": "GPT-6 Astra",
        "gpt-6-luna": "GPT-6 Luna",
        "gpt-6-sol": "GPT-6 Sol",
        "gpt-6.1-sol": "GPT-6.1 Sol",
        "gemini-3.7-flash": "Gemini 3.7 Flash",
        "gemini-3.8-flash": "Gemini 3.8 Flash",
        "grok-4.5": "Grok 4.5",
        "grok-4.6": "Grok 4.6",
        "grok-4.7": "Grok 4.7",
        "muse-spark-1.3": "Muse Spark 1.3",
        # User accepted the variant risk on this contributor route (2026-10-29).
        "muse-spark-1.3-contributor": "Muse Spark 1.3",
        "claude-opus-4.8": "Opus 4.8",
        "claude-opus-5": "Opus 5",
        "claude-opus-5.5": "Opus 5.5",
        # Terminal-Bench only evaluated the dated Qwen3.8-Max-0902 snapshot.
        # User accepted the variant risk on the rolling qwen3.8-max route
        # (2026-10-29): considered the same model even if the route may serve
        # a newer snapshot than the 0902 evaluation.
        "qwen3.8-max": "Qwen3.8-Max-0902",
        "qwen3.8-max-0902": "Qwen3.8-Max-0902",
        "claude-sonnet-5": "Sonnet 5",
        "claude-sonnet-5.5": "Sonnet 5.5",
    },
    "epoch": {
        "claude-fable-5": "Claude Fable 5",
        "claude-fable-5.1": "Claude Fable 5.1",
        "glm-5.3": "GLM-5.3",
        "glm-5.3-flash": "GLM-5.3-Flash",
        # Mistral's hosted ZAI API IDs, served without Mistral modifications
        # (v5.3), reviewed 2026-10-29:
        # https://docs.mistral.ai/models/zai-glm-5-3
        # https://docs.mistral.ai/models/zai-glm-5-3-flash
        "zai-glm-5-3": "GLM-5.3",
        "zai-glm-5-3-flash": "GLM-5.3-Flash",
        # Mistral Large 4 Public Preview v26.10 (2026-10-06) documents this API
        # ID; Epoch's row is exactly "Mistral Large 4", reviewed 2026-10-29:
        # https://docs.mistral.ai/models/mistral-large-4-0
        "mistral-large-4": "Mistral Large 4",
        "gpt-5.6-luna": "GPT-5.6 Luna",
        "gpt-5.6-sol": "GPT-5.6 Sol",
        "gpt-5.6-terra": "GPT-5.6 Terra",
        "gpt-6-astra": "GPT-6 Astra",
        "gpt-6-luna": "GPT-6 Luna",
        "gpt-6-sol": "GPT-6 Sol",
        "gpt-6.1-sol": "GPT-6.1 Sol",
        "gemini-3.7-flash": "Gemini 3.7 Flash",
        "gemini-3.8-flash": "Gemini 3.8 Flash",
        "grok-4.5": "Grok 4.5",
        "grok-4.6": "Grok 4.6",
        "grok-4.7": "Grok 4.7",
        "muse-spark-1.3": "Muse Spark 1.3",
        "claude-opus-4.8": "Claude Opus 4.8",
        "claude-opus-5": "Claude Opus 5",
        "claude-opus-5.5": "Claude Opus 5.5",
        "qwen3.8-max-0902": "Qwen3.8 Max (0902)",
        "claude-sonnet-5": "Claude Sonnet 5",
        "claude-sonnet-5.5": "Claude Sonnet 5.5",
        # Reviewed one-to-one identities (user-approved 2026-10-29): these are
        # punctuation/spelling-only differences against a single matching row.
        "kimi-k3": "Kimi K3",
        "kimi-k2.7-code": "Kimi K2.7 Code",
        "claude-haiku-5-5": "Claude Haiku 5.5",
        "deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
        # User accepted the version/variant risk on these rolling, pinned and
        # local routes (2026-10-29): qwen3.8-max maps to the unsuffixed
        # "Qwen 3.8 Max" row (2026-08-02), not "Qwen3.8 Max (0902)" — the
        # route ID carries no date suffix; Mistral's pinned 1017 snapshot maps
        # to Epoch's single V4.1 Flash row; the local qwen3.8:27b is accepted
        # as the stock "Qwen 3.8 27B"; the contributor route is accepted as
        # stock Muse Spark 1.3.
        "deepseek-v4-1-flash-1017": "DeepSeek V4.1 Flash",
        "qwen3.8-max": "Qwen 3.8 Max",
        "qwen3.8:27b": "Qwen 3.8 27B",
        "muse-spark-1.3-contributor": "Muse Spark 1.3",
    },
    # LiveBench's own identifiers are exact API-style IDs.  The entries here
    # are reviewed one-to-one identities only; there is no punctuation or
    # version-prefix normalization.
    "livebench": {
        "zai-glm-5-3": "glm-5.3",
        "zai-glm-5-3-flash": "glm-5.3-flash",
        # Reviewed one-to-one identities (user-approved 2026-10-29), mirroring
        # the same judgments already approved for the epoch/harbor tables:
        # - exact Mistral-hosted snapshot ID, punctuation only;
        # - stock claude-haiku-5-5 == LiveBench's claude-haiku-5.5;
        # - local qwen3.8:27b == LiveBench's qwen3.8-27b;
        # - contributor route accepted as stock Muse Spark 1.3.
        "deepseek-v4-1-flash-1017": "deepseek-v4.1-flash-1017",
        "claude-haiku-5-5": "claude-haiku-5.5",
        "qwen3.8:27b": "qwen3.8-27b",
        "muse-spark-1.3-contributor": "muse-spark-1.3",
        # User explicitly accepted this rolling -> dated snapshot mapping on
        # 2026-10-29 during branch review. The route may serve a newer snapshot
        # than LiveBench's 1017 evaluation; no other rolling alias is inferred.
        "deepseek-v4.1-flash": "deepseek-v4.1-flash-1017",
    },
    "deepswe": {
        "claude-opus-4.8": "claude-opus-4-8",
        "glm-5.3": "glm-5-3",
        "glm-5.3-flash": "glm-5-3-flash",
        # Mistral-hosted ZAI API IDs -> DeepSWE's published exact keys.
        "zai-glm-5-3": "glm-5-3",
        "zai-glm-5-3-flash": "glm-5-3-flash",
        "gpt-5.6-luna": "gpt-5-6-luna",
        "gpt-5.6-sol": "gpt-5-6-sol",
        "gpt-5.6-terra": "gpt-5-6-terra",
        "gemini-3.7-flash": "gemini-3-7-flash",
        "gemini-3.8-flash": "gemini-3-8-flash",
        "grok-4.5": "grok-4-5",
        "grok-4.6": "grok-4-6",
    },
}

# The summary is chosen by explicit date first, then by this published
# reasoning-effort ordering.  It never ranks by score value, so it cannot pick
# a per-metric maximum.  Undated evaluations never produce a summary.
EFFORT_PRIORITY = {
    "max": 0, "xhigh": 1, "high": 2, "medium": 3, "low": 4,
    "minimal": 5, "none": 6,
}

# Modified variants must never inherit a stock model's score, even through an
# alias table, so these identities are excluded up front.
_MODIFIED_TOKENS = ("abliterated", "uncensored", "lobotomized", "heretic")

_SOURCE_ERROR_NETWORK = "network error"
_SOURCE_ERROR_TIMEOUT = "request timeout"
_SOURCE_ERROR_DEADLINE = "deadline exceeded"
_SOURCE_ERROR_TOO_LARGE = "response too large"
_SOURCE_ERROR_REDIRECT = "upstream redirect rejected"
_SOURCE_ERROR_JSON = "invalid JSON response"
_SOURCE_ERROR_CSV = "invalid CSV response"
_SOURCE_ERROR_SCHEMA = "invalid response schema"
_SOURCE_ERROR_PAGINATION = "invalid pagination"
_SOURCE_ERROR_EMPTY = "empty feed"
_CACHE_ERROR_TOO_LARGE = "benchmark cache exceeds size limit"
_CACHE_ERROR_STORAGE = "cache storage unavailable"


class SourceError(Exception):
    """A sanitized, user-safe source failure (never contains URLs/secrets)."""


# ── Small validation helpers ────────────────────────────────────────────────
def _is_number(value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(number)


def _number(value, *, low: float, high: float) -> float:
    if not _is_number(value):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    number = float(value)
    if number < low or number > high:
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    return number


def _optional_number(value, *, low: float, high: float) -> float | None:
    if value is None or value == "":
        return None
    return _number(value, low=low, high=high)


def _csv_number(value, *, low: float, high: float) -> float:
    if not isinstance(value, str):
        raise SourceError(_SOURCE_ERROR_CSV)
    try:
        number = float(value.strip())
    except ValueError as exc:
        raise SourceError(_SOURCE_ERROR_CSV) from exc
    return _number(number, low=low, high=high)


def _csv_optional_number(value, *, low: float, high: float) -> float | None:
    if value is None or not str(value).strip():
        return None
    return _csv_number(value, low=low, high=high)


def _integer(value, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    if value < minimum or value > maximum:
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    return value


def _text(value, *, maximum: int = 200, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise SourceError(_SOURCE_ERROR_SCHEMA)
        return None
    if not isinstance(value, str):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    value = value.strip()
    if not value:
        if required:
            raise SourceError(_SOURCE_ERROR_SCHEMA)
        return None
    if len(value) > maximum or any(ord(char) < 32 for char in value):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    return value


def _iso_date(value) -> str | None:
    """Strict YYYY-MM-DD date, or None when the value is empty."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    if not (value[:4] + value[5:7] + value[8:]).isdigit():
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    try:
        _date.fromisoformat(value)
    except ValueError as exc:
        raise SourceError(_SOURCE_ERROR_SCHEMA) from exc
    return value


def _date_from_iso(value) -> str | None:
    """Date part of an ISO timestamp or date; invalid values are rejected."""
    if value is None or value == "":
        return None
    if isinstance(value, str) and len(value) >= 10:
        return _iso_date(value[:10])
    raise SourceError(_SOURCE_ERROR_SCHEMA)


def _optional_date_from_iso(value) -> str | None:
    """Optional feed metadata: use it only when it is a valid date, else null."""
    try:
        return _date_from_iso(value)
    except SourceError:
        return None


def _http_date(value) -> str | None:
    """Date part of an HTTP Last-Modified header, or None when absent/invalid."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        return parsed.date().isoformat()
    except (AttributeError, ValueError):
        return None


def _clean_ci(value) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    clean = {}
    for key in ("low", "high", "half_width"):
        if key in value:
            clean[key] = _number(value[key], low=0.0, high=1000.0)
    if not clean:
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    if "low" in clean and "high" in clean and clean["low"] > clean["high"]:
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    return clean


_METADATA_KEYS = {
    "n_trials", "rank", "ci_method", "n_runs", "task_count",
    "status",
}


def _clean_metadata(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    clean: dict = {}
    for key, item in value.items():
        if key not in _METADATA_KEYS:
            continue
        if isinstance(item, str):
            clean[key] = _text(item, maximum=200) or ""
        elif isinstance(item, bool):
            clean[key] = item
        elif isinstance(item, int):
            if abs(item) > 10**9:
                raise SourceError(_SOURCE_ERROR_SCHEMA)
            clean[key] = item
        elif isinstance(item, float):
            if not math.isfinite(item) or abs(item) > 1e9:
                raise SourceError(_SOURCE_ERROR_SCHEMA)
            clean[key] = item
        elif item is None:
            clean[key] = None
        else:
            raise SourceError(_SOURCE_ERROR_SCHEMA)
    return clean


def _clean_source_meta(value) -> dict:
    """Snapshot metadata: a few fixed fields, no arbitrary nested payloads."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    clean: dict = {}
    for key, item in value.items():
        if key == "suite_weights":
            if not isinstance(item, dict):
                raise SourceError(_SOURCE_ERROR_SCHEMA)
            weights = {}
            for name, weight in item.items():
                if not _is_number(weight) or float(weight) <= 0:
                    raise SourceError(_SOURCE_ERROR_SCHEMA)
                weights[_text(name, maximum=40) or "unknown"] = float(weight)
            clean[key] = weights
            continue
        if key not in {"pages", "rows", "task_count", "known_defective_tasks"}:
            continue
        if isinstance(item, int) and not isinstance(item, bool):
            if abs(item) > 10**7:
                raise SourceError(_SOURCE_ERROR_SCHEMA)
            clean[key] = item
        elif _is_number(item):
            clean[key] = float(item)
        elif item is None:
            clean[key] = None
        else:
            raise SourceError(_SOURCE_ERROR_SCHEMA)
    return clean


def _make_entry(metric: str, model: str, value: float, *, evaluator: str,
                version: str, date: str | None = None,
                date_kind: str | None = None, agent: str | None = None,
                effort: str | None = None, configuration: str | None = None,
                ci: dict | None = None, metadata: dict | None = None) -> dict:
    # Validate the type before the hash lookup: persisted entries with a list
    # or dict "metric" must fail closed with SourceError, not TypeError.
    if not isinstance(metric, str) or metric not in METRIC_BY_KEY:
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    clean_date = _iso_date(date)
    clean_kind = _text(date_kind, maximum=32)
    if clean_kind is not None and clean_kind not in DATE_KINDS:
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    # A date always says what kind of date it is; an absent date has no kind.
    if (clean_date is None) != (clean_kind is None):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    entry = {
        "metric": metric,
        "model": _text(model, required=True),
        "agent": _text(agent),
        "effort": _text(effort, maximum=40),
        "evaluator": _text(evaluator, maximum=100),
        "version": _text(version, maximum=64),
        "date": clean_date,
        "date_kind": clean_kind,
        "value": _number(value, low=-1e6, high=1e6),
        "ci": _clean_ci(ci),
        "configuration": _text(configuration, maximum=200),
        "metadata": _clean_metadata(metadata),
    }
    # Per-unit range checks are deliberately conservative but reject obvious
    # drift (for example a 0-1 pass_at_1 leaking into a percent field).
    if METRIC_BY_KEY[metric].unit == "pct" and not 0.0 <= entry["value"] <= 100.0:
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    if metric == "eci" and not 0.0 <= entry["value"] <= 1000.0:
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    return entry


def _sanitize_entry(entry) -> dict:
    """Re-validate an entry loaded from disk or produced by an adapter."""
    if not isinstance(entry, dict):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    return _make_entry(
        entry.get("metric"), entry.get("model"), entry.get("value"),
        evaluator=entry.get("evaluator") or "unknown",
        version=entry.get("version") or "unknown",
        date=entry.get("date"), date_kind=entry.get("date_kind"),
        agent=entry.get("agent"),
        effort=entry.get("effort"), configuration=entry.get("configuration"),
        ci=entry.get("ci"), metadata=entry.get("metadata"),
    )


def _validated_entries(entries, *, source: str) -> list[dict]:
    if not isinstance(entries, list) or not entries:
        raise SourceError(_SOURCE_ERROR_EMPTY)
    if len(entries) > MAX_ENTRIES:
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    expected_version = SOURCES[source].version
    clean = []
    for entry in entries:
        item = _sanitize_entry(entry)
        spec = METRIC_BY_KEY[item["metric"]]
        if spec.source != source or item["version"] != expected_version:
            raise SourceError(_SOURCE_ERROR_SCHEMA)
        clean.append(item)
    return clean


def _clean_validators(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    clean = {}
    for key in ("etag", "last_modified"):
        item = value.get(key)
        if item is None:
            continue
        clean[key] = _text(item, maximum=300) or None
    return clean


def _nested_validators(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    clean = {}
    for key in ("table", "categories"):
        if key in value:
            clean[key] = _clean_validators(value[key])
    return clean


def _clean_validators_any(value) -> dict:
    """Validators are flat for single-file sources and nested for LiveBench."""
    if not isinstance(value, dict):
        return {}
    if "table" in value or "categories" in value:
        return _nested_validators(value)
    return _clean_validators(value)


# ── HTTP client ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Response:
    status_code: int
    headers: dict
    body: bytes


class Deadline:
    """Monotonic wall-clock budget shared by every request for one source."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self._clock = clock
        self._end = clock() + max(0.0, seconds)

    def remaining(self) -> float:
        remaining = self._end - self._clock()
        if remaining <= 0:
            raise SourceError(_SOURCE_ERROR_DEADLINE)
        return remaining


_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_DNS_WORKERS = 4


class _DaemonThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """Bounded resolver pool whose workers never hold up process shutdown.

    ``asyncio.run`` shuts down its loop's default executor and waits for it,
    so one blocked ``getaddrinfo`` call could otherwise keep a synchronous
    request past its deadline.  The shared benchmark loop is never shut down;
    this pool uses daemon workers that are intentionally not registered with
    ``concurrent.futures``' interpreter-exit join hook.  A stalled resolver can
    occupy at most ``max_workers`` threads, and cancelled queued work is
    skipped by the standard ``_WorkItem`` cancellation check.
    """

    def _adjust_thread_count(self) -> None:
        if self._idle_semaphore.acquire(timeout=0):
            return
        num_threads = len(self._threads)
        if num_threads >= self._max_workers:
            return
        thread = threading.Thread(
            target=self._daemon_worker,
            name=f"{self._thread_name_prefix}_{num_threads}",
            daemon=True,
        )
        thread.start()
        self._threads.add(thread)

    def _daemon_worker(self) -> None:
        while True:
            try:
                work_item = self._work_queue.get_nowait()
            except queue.Empty:
                self._idle_semaphore.release()
                work_item = self._work_queue.get(block=True)
            if work_item is not None:
                work_item.run()
                continue
            if self._shutdown:
                self._work_queue.put(None)  # wake other workers
                return

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        # Never join: a blocked resolver must not delay shutdown.  The shared
        # loop is process-wide, so this is only an interface-completeness path.
        super().shutdown(wait=False, cancel_futures=cancel_futures)


_HTTP_LOOP_LOCK = threading.Lock()
_HTTP_LOOP: asyncio.AbstractEventLoop | None = None
_HTTP_LOOP_THREAD: threading.Thread | None = None


def _http_event_loop() -> asyncio.AbstractEventLoop:
    """Return the one lazy, daemon, process-wide loop for HTTP coroutines.

    A per-request ``asyncio.run`` would also create a fresh default executor
    and wait for it during shutdown.  A stalled DNS lookup could therefore
    delay the synchronous caller beyond the source deadline even after the
    coroutine was cancelled.  The shared loop stays alive for the process
    lifetime (it is a daemon thread), while every request still creates and
    closes its own ``httpx.AsyncClient`` inside its own coroutine.
    """
    global _HTTP_LOOP, _HTTP_LOOP_THREAD
    with _HTTP_LOOP_LOCK:
        loop = _HTTP_LOOP
        thread = _HTTP_LOOP_THREAD
        if (loop is not None and loop.is_running()
                and thread is not None and thread.is_alive()):
            return loop
        new_loop = asyncio.new_event_loop()
        new_loop.set_default_executor(
            _DaemonThreadPoolExecutor(
                max_workers=_DNS_WORKERS,
                thread_name_prefix="benchmark-dns"))
        started = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(new_loop)
            new_loop.call_soon(started.set)
            try:
                new_loop.run_forever()
            finally:
                new_loop.close()

        new_thread = threading.Thread(
            target=run, name="benchmark-http-loop", daemon=True)
        new_thread.start()
        if not started.wait(5.0):
            raise RuntimeError("benchmark HTTP loop failed to start")
        _HTTP_LOOP = new_loop
        _HTTP_LOOP_THREAD = new_thread
        return new_loop


def _consume_future(future: concurrent.futures.Future) -> None:
    """Retrieve a completed runner future so no exception goes unobserved."""
    if future.cancelled():
        return
    try:
        future.exception()
    except BaseException:
        pass


def _run_async(coro, deadline: Deadline):
    """Run one coroutine on the shared loop, cancelling at the deadline.

    ``future.result`` is a bounded blocking wait.  If it expires, the loop
    task is cancelled and its ``async with`` cleanup closes the response and
    client; cleanup runs on the daemon loop thread and never holds up the
    synchronous caller.
    """
    try:
        timeout = deadline.remaining()
    except SourceError:
        coro.close()
        raise
    try:
        loop = _http_event_loop()
    except Exception:
        coro.close()
        raise
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
    except BaseException:
        coro.close()
        raise
    future.add_done_callback(_consume_future)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise SourceError(_SOURCE_ERROR_DEADLINE) from None


class HttpClient:
    """Minimal fixed-endpoint HTTPS client with strict safety properties."""

    def __init__(self, *, timeout: float = REQUEST_TIMEOUT,
                 max_bytes: int = MAX_BODY_BYTES, sleep=None):
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._sleep = sleep

    @staticmethod
    def _validate_url(url: str) -> None:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS
                or parts.username or parts.password
                or parts.port not in (None, 443)):
            raise SourceError(_SOURCE_ERROR_SCHEMA)

    async def _sleep_before_retry(self, attempt: int, deadline: Deadline) -> None:
        """Back off for at most the remaining source deadline."""
        remaining = deadline.remaining()
        delay = min(RETRY_BACKOFF_MAX, RETRY_BACKOFF * 2 ** attempt)
        delay = min(delay, remaining)
        if self._sleep is None:
            await asyncio.sleep(delay)
            return
        result = self._sleep(delay)
        if inspect.isawaitable(result):
            await result

    def request(self, method: str, url: str, *, json_body=None,
                headers: dict | None = None, deadline: Deadline) -> Response:
        if method not in ("GET", "POST"):
            raise SourceError(_SOURCE_ERROR_SCHEMA)
        self._validate_url(url)
        request_headers = dict(headers or {})
        return _run_async(
            self._request_async(
                method, url, json_body=json_body, headers=request_headers,
                deadline=deadline),
            deadline,
        )

    async def _request_async(self, method: str, url: str, *, json_body,
                             headers: dict, deadline: Deadline) -> Response:
        # One absolute timeout owns the whole coroutine: retries, backoff,
        # connection/headers/body reads and closing the client/response below.
        try:
            async with asyncio.timeout(deadline.remaining()):
                return await self._request_in_client(
                    method, url, json_body=json_body, headers=headers,
                    deadline=deadline)
        except TimeoutError:
            raise SourceError(_SOURCE_ERROR_DEADLINE) from None

    async def _request_in_client(self, method: str, url: str, *, json_body,
                                 headers: dict, deadline: Deadline) -> Response:
        # trust_env=False ignores proxies/netrc; follow_redirects=False never
        # follows a Location; verify=True keeps certificate validation on.
        # The client is scoped to this one synchronous request and is closed
        # by the context manager even when the source deadline cancels it.
        async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, verify=True,
                timeout=None,
                headers={"User-Agent": USER_AGENT,
                         "Accept": "application/json, text/csv;q=0.9"}) as client:
            for attempt in range(HTTP_ATTEMPTS):
                last_attempt = attempt + 1 >= HTTP_ATTEMPTS
                try:
                    # The per-attempt timeout still wraps connection
                    # establishment, header receipt, every body read and the
                    # stream context cleanup; the outer timeout owns the total.
                    async with asyncio.timeout(
                            min(self._timeout, deadline.remaining())):
                        async with client.stream(
                                method, url, json=json_body,
                                headers=headers) as response:
                            status = response.status_code
                            response_headers = _headers_dict(response.headers)
                            if status == 304:
                                return Response(304, response_headers, b"")
                            if 300 <= status < 400:
                                # Never follow redirects: a redirect could
                                # target an internal address or another origin.
                                raise SourceError(_SOURCE_ERROR_REDIRECT)
                            if status == 200:
                                body = await self._read_body(response, deadline)
                                return Response(200, response_headers, body)
                            if status not in _RETRY_STATUSES or last_attempt:
                                raise SourceError(f"HTTP {status}")
                except SourceError:
                    raise
                except TimeoutError:
                    # Distinguish the absolute source deadline from a single
                    # request's timeout.  The former must never be retried.
                    try:
                        deadline.remaining()
                    except SourceError:
                        raise SourceError(_SOURCE_ERROR_DEADLINE) from None
                    if last_attempt:
                        raise SourceError(_SOURCE_ERROR_TIMEOUT) from None
                except httpx.TimeoutException:
                    try:
                        deadline.remaining()
                    except SourceError:
                        raise SourceError(_SOURCE_ERROR_DEADLINE) from None
                    if last_attempt:
                        raise SourceError(_SOURCE_ERROR_TIMEOUT) from None
                except (httpx.HTTPError, OSError):
                    if last_attempt:
                        raise SourceError(_SOURCE_ERROR_NETWORK) from None
                # A retryable status or a transport/timeout failure with
                # attempts left: bounded backoff, then another request.
                await self._sleep_before_retry(attempt, deadline)
        raise SourceError(_SOURCE_ERROR_NETWORK)

    async def _read_body(self, response, deadline: Deadline) -> bytes:
        length = response.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > self._max_bytes:
                    raise SourceError(_SOURCE_ERROR_TOO_LARGE)
            except (TypeError, ValueError):
                pass
        chunks = []
        total = 0
        async for chunk in response.aiter_bytes():
            # Check even for empty chunks: a stalled stream must still hit the
            # source deadline, and sleeps/reads are bounded by it.
            deadline.remaining()
            if not chunk:
                continue
            total += len(chunk)
            if total > self._max_bytes:
                raise SourceError(_SOURCE_ERROR_TOO_LARGE)
            chunks.append(chunk)
        deadline.remaining()
        return b"".join(chunks)


def _headers_dict(headers) -> dict:
    result = {}
    try:
        for key, value in headers.items():
            result[str(key).lower()] = str(value)
    except Exception:
        return {}
    return result


def _load_json(body: bytes):
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceError(_SOURCE_ERROR_JSON) from exc
    try:
        return json.loads(text, parse_constant=_reject_json_constant)
    except (ValueError, TypeError) as exc:
        raise SourceError(_SOURCE_ERROR_JSON) from exc


def _reject_json_constant(value):
    raise ValueError(f"non-finite JSON constant {value}")


def _parse_csv(body: bytes, required: tuple[str, ...], *, maximum_rows: int) -> list[dict]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError(_SOURCE_ERROR_CSV) from exc
    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames
        if not fieldnames or len(fieldnames) != len(set(fieldnames)):
            raise SourceError(_SOURCE_ERROR_CSV)
        missing = [name for name in required if name not in fieldnames]
        if missing:
            raise SourceError(_SOURCE_ERROR_CSV)
        rows = []
        for index, row in enumerate(reader):
            if index >= maximum_rows:
                raise SourceError(_SOURCE_ERROR_SCHEMA)
            if None in row:
                raise SourceError(_SOURCE_ERROR_CSV)
            rows.append(row)
        return rows
    except csv.Error as exc:
        raise SourceError(_SOURCE_ERROR_CSV) from exc


def _conditional_headers(validators: dict) -> dict:
    headers = {}
    if validators.get("etag"):
        headers["If-None-Match"] = validators["etag"]
    if validators.get("last_modified"):
        headers["If-Modified-Since"] = validators["last_modified"]
    return headers


def _validators_from(response: Response) -> dict:
    validators = {}
    if response.headers.get("etag"):
        validators["etag"] = response.headers["etag"]
    if response.headers.get("last-modified"):
        validators["last_modified"] = response.headers["last-modified"]
    return validators


# ── Harbor (Terminal-Bench 4.0) ─────────────────────────────────────────────
def _harbor_pagination(payload: dict, *, requested_page: int):
    if not isinstance(payload, dict):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    pagination = payload.get("pagination")
    if not isinstance(pagination, dict):
        raise SourceError(_SOURCE_ERROR_PAGINATION)
    page = _integer(pagination.get("page"), minimum=1, maximum=HARBOR_MAX_PAGES)
    page_size = _integer(pagination.get("page_size"), minimum=1, maximum=1000)
    total = _integer(pagination.get("total"), minimum=0, maximum=MAX_ENTRIES)
    total_pages = _integer(pagination.get("total_pages"), minimum=0,
                           maximum=HARBOR_MAX_PAGES)
    if page != requested_page:
        raise SourceError(_SOURCE_ERROR_PAGINATION)
    if total_pages > HARBOR_MAX_PAGES:
        raise SourceError(_SOURCE_ERROR_PAGINATION)
    if total == 0:
        raise SourceError(_SOURCE_ERROR_EMPTY)
    expected_pages = max(1, math.ceil(total / page_size))
    if total_pages != expected_pages:
        raise SourceError(_SOURCE_ERROR_PAGINATION)
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    rows_on_page = page_size if page < total_pages else total - page_size * (page - 1)
    if len(rows) != rows_on_page:
        raise SourceError(_SOURCE_ERROR_PAGINATION)
    return total, page_size, total_pages


def _harbor_configuration(agent: str, effort: str | None) -> str:
    """Keep the combined label within the shared 200-char configuration bound."""
    if not effort:
        return agent
    suffix = f" / {effort}"
    if len(agent) + len(suffix) <= 200:
        return agent + suffix
    # Preserve the full agent/effort fields and keep the effort suffix visible.
    return agent[: 200 - len(suffix)].rstrip() + suffix


def _harbor_row(row, *, seen_ids: set) -> dict | None:
    if not isinstance(row, dict):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    row_id = _text(row.get("id"), maximum=128, required=True)
    if row_id in seen_ids:
        raise SourceError(_SOURCE_ERROR_PAGINATION)  # repeated page/row
    seen_ids.add(row_id)

    status = _text(row.get("status"), maximum=40)
    if status != "display":
        return None  # verified public rows use "display"; never expose drafts

    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    model_display = metadata.get("model_display")
    agent_display = metadata.get("agent_display")
    if not isinstance(model_display, dict) or not isinstance(agent_display, dict):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    model = _text(model_display.get("label"), maximum=200, required=True)
    agent = _text(agent_display.get("label"), maximum=200, required=True)
    effort = _text(metadata.get("reasoning_effort"), maximum=40)
    row_date = _iso_date(metadata.get("date"))

    metrics = row.get("metrics")
    if not isinstance(metrics, dict):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    accuracy = _number(metrics.get("accuracy"), low=0.0, high=100.0)
    half_width = _optional_number(metrics.get("accuracy_ci95_half_width"),
                                  low=0.0, high=100.0)
    trials = metrics.get("n_trials")
    if trials is None:
        trials = row.get("n_trials")
    clean_trials = None
    if trials is not None:
        clean_trials = _integer(trials, minimum=1, maximum=10**7)

    rank = row.get("rank")
    clean_rank = None
    if rank is not None:
        clean_rank = _integer(rank, minimum=1, maximum=MAX_ENTRIES)

    configuration = _harbor_configuration(agent, effort)
    metadata_out = {}
    if clean_trials is not None:
        metadata_out["n_trials"] = clean_trials
    if clean_rank is not None:
        metadata_out["rank"] = clean_rank
    return _make_entry(
        "tb4", model, accuracy, evaluator="Terminal-Bench 4.0",
        version=HARBOR_LEADERBOARD, date=row_date,
        # Harbor's column is literally "Release Date"; it is not an eval date.
        date_kind=DATE_KIND_MODEL_RELEASE if row_date else None,
        agent=agent, effort=effort, configuration=configuration,
        ci={"half_width": half_width} if half_width is not None else None,
        metadata=metadata_out,
    )


def fetch_harbor(client: HttpClient, _previous: dict | None, deadline: Deadline) -> dict:
    """Read every Harbor page; a partial page set is never accepted."""
    entries: list[dict] = []
    seen_ids: set[str] = set()
    expected_total = expected_page_size = expected_pages = None
    leaderboard_updated = None
    row_updated_dates: list[str] = []
    page = 1
    while True:
        response = client.request(
            "POST", HARBOR_ENDPOINT,
            json_body={"package": HARBOR_PACKAGE, "name": HARBOR_LEADERBOARD,
                       "page": page, "page_size": HARBOR_PAGE_SIZE},
            deadline=deadline,
        )
        payload = _load_json(response.body)
        leaderboard = payload.get("leaderboard") if isinstance(payload, dict) else None
        if not isinstance(leaderboard, dict):
            raise SourceError(_SOURCE_ERROR_SCHEMA)
        if (leaderboard.get("package") != HARBOR_PACKAGE
                or leaderboard.get("name") != HARBOR_LEADERBOARD):
            raise SourceError(_SOURCE_ERROR_SCHEMA)
        if leaderboard_updated is None:
            leaderboard_updated = _optional_date_from_iso(leaderboard.get("updated_at"))

        total, page_size, total_pages = _harbor_pagination(payload, requested_page=page)
        if expected_total is None:
            expected_total, expected_page_size, expected_pages = total, page_size, total_pages
        elif (total, page_size, total_pages) != (
                expected_total, expected_page_size, expected_pages):
            raise SourceError(_SOURCE_ERROR_PAGINATION)

        rows = payload.get("rows")
        for row in rows:
            entry = _harbor_row(row, seen_ids=seen_ids)
            if entry is not None:
                entries.append(entry)
            if isinstance(row, dict):
                row_updated = _optional_date_from_iso(row.get("updated_at"))
                if row_updated is not None:
                    row_updated_dates.append(row_updated)
        if page >= total_pages:
            break
        page += 1
    if len(entries) > MAX_ENTRIES:
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    if not entries:
        raise SourceError(_SOURCE_ERROR_EMPTY)
    # source_updated describes when the feed/leaderboard changed, never the
    # model's release date.  Prefer the leaderboard timestamp; fall back to the
    # newest per-row mutation timestamp only when that is all the feed supplies.
    source_updated = leaderboard_updated or (
        max(row_updated_dates) if row_updated_dates else None)
    return {
        "entries": entries,
        "version": HARBOR_LEADERBOARD,
        "source_updated": source_updated,
        "validators": {},
        "meta": {"pages": page, "rows": len(entries)},
    }


# ── Epoch ECI ───────────────────────────────────────────────────────────────
def fetch_epoch(client: HttpClient, previous: dict | None, deadline: Deadline) -> dict:
    validators = _clean_validators((previous or {}).get("validators"))
    response = client.request("GET", EPOCH_CSV_URL,
                              headers=_conditional_headers(validators),
                              deadline=deadline)
    if response.status_code == 304:
        return {"not_modified": True}
    rows = _parse_csv(response.body, ("Model", "eci", "date"), maximum_rows=MAX_ENTRIES)
    entries = []
    for row in rows:
        model = _text(row.get("Model"), required=True)
        _text(row.get("Display name"))  # validated, never exposed separately
        value = _csv_number(row.get("eci"), low=0.0, high=1000.0)
        low = _csv_optional_number(row.get("eci_ci_low"), low=0.0, high=1000.0)
        high = _csv_optional_number(row.get("eci_ci_high"), low=0.0, high=1000.0)
        if low is not None and high is not None and not low <= value <= high:
            raise SourceError(_SOURCE_ERROR_SCHEMA)
        if low is not None and high is not None and low > high:
            raise SourceError(_SOURCE_ERROR_SCHEMA)
        row_date = _iso_date(row.get("date"))
        ci = None
        if low is not None or high is not None:
            ci = {}
            if low is not None:
                ci["low"] = low
            if high is not None:
                ci["high"] = high
            if low is not None and high is not None:
                ci["half_width"] = round((high - low) / 2, 4)
        entries.append(_make_entry(
            "eci", model, value, evaluator="Epoch AI ECI", version="eci_scores",
            date=row_date,
            # The CSV's date column is the model release date, not an eval date.
            date_kind=DATE_KIND_MODEL_RELEASE if row_date else None,
            configuration=None, ci=ci, metadata={},
        ))
    if not entries:
        raise SourceError(_SOURCE_ERROR_EMPTY)
    return {
        "entries": entries,
        "version": "eci_scores",
        # Epoch publishes no reliable feed-update date in the CSV.  The HTTP
        # Last-Modified header is the only genuine upstream mutation metadata.
        "source_updated": _http_date(response.headers.get("last-modified")),
        "validators": _validators_from(response),
        "meta": {"rows": len(entries)},
    }


# ── LiveBench (pinned release) ─────────────────────────────────────────────
def _parse_livebench_categories(body: bytes) -> dict:
    data = _load_json(body)
    if not isinstance(data, dict) or not data:
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    mapping = {}
    weights = {}
    for key, value in data.items():
        if key == "_overall_weights":
            if not isinstance(value, dict):
                raise SourceError(_SOURCE_ERROR_SCHEMA)
            for name, weight in value.items():
                if not _is_number(weight) or float(weight) <= 0:
                    raise SourceError(_SOURCE_ERROR_SCHEMA)
                weights[str(name)] = float(weight)
            continue
        if not isinstance(value, list) or not value:
            raise SourceError(_SOURCE_ERROR_SCHEMA)
        columns = []
        for column in value:
            if not isinstance(column, str) or column not in LIVEBENCH_COLUMNS:
                raise SourceError(_SOURCE_ERROR_SCHEMA)
            columns.append(column)
        mapping[str(key)] = tuple(columns)
    flattened = [column for columns in mapping.values() for column in columns]
    if not mapping or len(flattened) != len(set(flattened)):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    if set(flattened) != set(LIVEBENCH_COLUMNS):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    return {"categories": mapping, "weights": weights}


def _parse_livebench_table(body: bytes, categories: dict) -> list[dict]:
    rows = _parse_csv(body, ("model", *LIVEBENCH_COLUMNS),
                      maximum_rows=MAX_LIVEBENCH_ROWS)
    allowed_columns = {"model"} | {
        column for columns in categories["categories"].values() for column in columns
    }
    if rows:
        header = set(rows[0].keys())
        if header != allowed_columns:
            raise SourceError(_SOURCE_ERROR_SCHEMA)
    release_date = f"{LIVEBENCH_VERSION[0:4]}-{LIVEBENCH_VERSION[4:6]}-{LIVEBENCH_VERSION[6:8]}"
    _iso_date(release_date)
    entries = []
    for row in rows:
        model = _text(row.get("model"), required=True)
        values = {}
        for column, metric in LIVEBENCH_COLUMNS.items():
            value = _csv_optional_number(row.get(column), low=0.0, high=100.0)
            if value is not None:
                values[metric] = value
        if not values:
            continue
        for metric, value in values.items():
            entries.append(_make_entry(
                metric, model, value, evaluator="LiveBench",
                version=LIVEBENCH_VERSION, date=release_date,
                date_kind=DATE_KIND_BENCHMARK_RELEASE, metadata={},
            ))
    if not entries:
        raise SourceError(_SOURCE_ERROR_EMPTY)
    return entries


def fetch_livebench(client: HttpClient, previous: dict | None, deadline: Deadline) -> dict:
    """Fetch the pinned table + categories pair without mixing schemas.

    Both files are requested conditionally.  If exactly one reports 304, the
    unchanged file is re-requested unconditionally so the accepted pair is
    always internally consistent; if that re-request still reports 304 the
    source is rejected rather than mixing old and new metadata.
    """
    previous = previous or {}
    validators = _nested_validators(previous.get("validators"))
    table_headers = _conditional_headers(validators.get("table", {}))
    category_headers = _conditional_headers(validators.get("categories", {}))
    table_response = client.request("GET", LIVEBENCH_CSV_URL,
                                    headers=table_headers, deadline=deadline)
    categories_response = client.request("GET", LIVEBENCH_CATEGORIES_URL,
                                         headers=category_headers, deadline=deadline)
    if table_response.status_code == 304 and categories_response.status_code == 304:
        return {"not_modified": True}
    if table_response.status_code == 304:
        table_response = client.request("GET", LIVEBENCH_CSV_URL, deadline=deadline)
        if table_response.status_code == 304:
            raise SourceError(_SOURCE_ERROR_SCHEMA)
    if categories_response.status_code == 304:
        categories_response = client.request("GET", LIVEBENCH_CATEGORIES_URL,
                                             deadline=deadline)
        if categories_response.status_code == 304:
            raise SourceError(_SOURCE_ERROR_SCHEMA)
    categories = _parse_livebench_categories(categories_response.body)
    entries = _parse_livebench_table(table_response.body, categories)
    return {
        "entries": entries,
        "version": LIVEBENCH_VERSION,
        "source_updated": f"{LIVEBENCH_VERSION[0:4]}-{LIVEBENCH_VERSION[4:6]}-{LIVEBENCH_VERSION[6:8]}",
        "validators": {
            "table": _validators_from(table_response),
            "categories": _validators_from(categories_response),
        },
        "meta": {"rows": len({entry["model"] for entry in entries}),
                 "suite_weights": categories["weights"]},
    }


# ── DeepSWE (optional) ──────────────────────────────────────────────────────
def fetch_deepswe(client: HttpClient, previous: dict | None, deadline: Deadline) -> dict:
    validators = _clean_validators((previous or {}).get("validators"))
    response = client.request("GET", DEEPSWE_JSON_URL,
                              headers=_conditional_headers(validators),
                              deadline=deadline)
    if response.status_code == 304:
        return {"not_modified": True}
    payload = _load_json(response.body)
    if not isinstance(payload, dict):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    task_count = _integer(payload.get("n_tasks_in_set"), minimum=1, maximum=10000)
    if task_count != DEEPSWE_TASKS:
        raise SourceError(_SOURCE_ERROR_SCHEMA)  # v1.1 is pinned at 113 tasks
    generated_at = _date_from_iso(payload.get("generated_at"))
    if generated_at is None:
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise SourceError(_SOURCE_ERROR_EMPTY)
    if len(rows) > MAX_ENTRIES:
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    entries = []
    for row in rows:
        if not isinstance(row, dict):
            raise SourceError(_SOURCE_ERROR_SCHEMA)
        model = _text(row.get("model"), required=True)
        harness = _text(row.get("harness"), maximum=100)
        effort = _text(row.get("reasoning_effort"), maximum=40)
        config = _text(row.get("config"), maximum=200)
        # pass_at_1 is a 0..1 attempt pass rate; store/display percent.
        pass_at_1 = _number(row.get("pass_at_1"), low=0.0, high=1.0) * 100.0
        low = _optional_number(row.get("ci_lo"), low=0.0, high=1.0)
        high = _optional_number(row.get("ci_hi"), low=0.0, high=1.0)
        half = _optional_number(row.get("ci_half"), low=0.0, high=1.0)
        if low is not None and high is not None and low > high:
            raise SourceError(_SOURCE_ERROR_SCHEMA)
        ci = None
        if low is not None or high is not None or half is not None:
            ci = {}
            if low is not None:
                ci["low"] = round(low * 100.0, 4)
            if high is not None:
                ci["high"] = round(high * 100.0, 4)
            if half is not None:
                ci["half_width"] = round(half * 100.0, 4)
        metadata = {}
        n_runs = row.get("n_runs")
        if n_runs is not None:
            metadata["n_runs"] = _integer(n_runs, minimum=1, maximum=10**6)
        ci_method = _text(row.get("ci_method"), maximum=100)
        if ci_method:
            metadata["ci_method"] = ci_method
        metadata["task_count"] = task_count
        # The current artifact has no per-row date field.  Only a date the row
        # actually supplies is used, and it is labelled as an evaluation date;
        # the feed-wide generated_at is never copied onto rows.
        row_date = None
        for key in ("evaluated_at", "eval_date", "date"):
            if key in row:
                row_date = _date_from_iso(row.get(key))
                break
        entries.append(_make_entry(
            "deepswe", model, round(pass_at_1, 4), evaluator="DeepSWE",
            version=DEEPSWE_VERSION, date=row_date,
            date_kind=DATE_KIND_EVALUATION if row_date else None,
            agent=harness, effort=effort, configuration=config,
            ci=ci, metadata=metadata,
        ))
    return {
        "entries": entries,
        "version": DEEPSWE_VERSION,
        # generated_at describes when the artifact was produced, not a per-row
        # evaluation date; rows carry no eval date, so results stay undated.
        "source_updated": generated_at,
        "validators": _validators_from(response),
        "meta": {"rows": len(entries), "task_count": task_count,
                 "known_defective_tasks": DEEPSWE_KNOWN_DEFECTIVE_TASKS},
    }


FETCHERS = {
    "harbor": fetch_harbor,
    "epoch": fetch_epoch,
    "livebench": fetch_livebench,
    "deepswe": fetch_deepswe,
}


# ── Persistence and refresh orchestration ──────────────────────────────────
def _default_cache_path() -> str:
    return os.getenv("HOMELAB_BENCH_FILE",
                     os.path.expanduser("~/.homelab-ctrl/benchmarks.json"))


def _empty_state(name: str) -> dict:
    return {
        "schema_version": SOURCE_SCHEMA_VERSION,
        "data": None,
        "last_checked": None,
        "last_success": None,
        "error": None,
        "version": SOURCES[name].version,
        "source_updated": None,
        "validators": {},
        "meta": {},
    }


def _read_cache_bytes(path: str) -> bytes | None:
    """Read a bounded regular cache file without following links or blocking."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CACHE_BYTES:
            return None
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CACHE_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _sanitize_timestamp(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise SourceError(_SOURCE_ERROR_SCHEMA) from exc
    if not math.isfinite(number) or number < 0:
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    return number


def _sanitize_source_record(name: str, record) -> dict | None:
    if not isinstance(record, dict):
        return None
    if record.get("schema_version") != SOURCE_SCHEMA_VERSION:
        return None
    data = record.get("data")
    if data is not None:
        if not isinstance(data, dict):
            return None
        try:
            entries = _validated_entries(data.get("entries"), source=name)
            meta = _clean_source_meta(data.get("meta"))
        except SourceError:
            return None
        data = {"entries": entries, "meta": meta}
    state = _empty_state(name)
    try:
        state["last_checked"] = _sanitize_timestamp(record.get("last_checked"))
        state["last_success"] = _sanitize_timestamp(record.get("last_success"))
        version = _text(record.get("version"), maximum=64)
        if version != SOURCES[name].version:
            return None
        state["version"] = version
        state["source_updated"] = _date_from_iso(record.get("source_updated"))
        error = record.get("error")
        if error is not None:
            state["error"] = _text(error, maximum=300)
        state["validators"] = _clean_validators_any(record.get("validators"))
        state["data"] = data
    except SourceError:
        return None
    return state


def _default_thread_factory(target, name):
    return threading.Thread(target=target, name=name, daemon=True)


class BenchmarkStore:
    """Holds independent per-source snapshots and refreshes them in the background."""

    def __init__(self, path: str | None = None, *, ttl: float = CACHE_TTL,
                 fetchers: dict | None = None, client_factory=None,
                 clock=time.time, thread_factory=None,
                 persist: bool = True):
        self.path = _default_cache_path() if path is None else path
        self.ttl = ttl
        self._fetchers = dict(FETCHERS if fetchers is None else fetchers)
        self._client_factory = client_factory or HttpClient
        self._clock = clock
        self._thread_factory = thread_factory or _default_thread_factory
        self._persist_enabled = bool(persist and self.path)
        self._state_lock = threading.RLock()
        self._persist_lock = threading.Lock()
        self._coord_lock = threading.Lock()
        self._source_locks = {name: threading.Lock() for name in SOURCE_NAMES}
        self._state = {name: _empty_state(name) for name in SOURCE_NAMES}
        self._inflight: set[str] = set()
        self._coordinator = False
        self._next_attempt: dict[str, float] = {}
        self._failures = {name: 0 for name in SOURCE_NAMES}
        self._load()

    # ── loading / persistence ──
    def _load(self) -> None:
        if not self._persist_enabled:
            return
        raw = _read_cache_bytes(self.path)
        if not raw:
            return
        try:
            document = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
            return
        # Reject the legacy Artificial Analysis cache and any unknown format.
        if not isinstance(document, dict) or document.get("schema_version") != FILE_SCHEMA_VERSION:
            return
        sources = document.get("sources")
        if not isinstance(sources, dict):
            return
        with self._state_lock:
            for name in SOURCE_NAMES:
                if name not in sources:
                    continue
                state = _sanitize_source_record(name, sources[name])
                if state is not None:
                    self._state[name] = state

    def _disk_document(self) -> dict:
        with self._state_lock:
            sources = {}
            for name in SOURCE_NAMES:
                state = self._state[name]
                sources[name] = {
                    "schema_version": SOURCE_SCHEMA_VERSION,
                    "data": state["data"],
                    "last_checked": state["last_checked"],
                    "last_success": state["last_success"],
                    "error": state["error"],
                    "version": state["version"],
                    "source_updated": state["source_updated"],
                    "validators": state["validators"],
                }
        return {"schema_version": FILE_SCHEMA_VERSION, "sources": sources}

    def _persist(self) -> bool:
        if not self._persist_enabled:
            # Persistence is deliberately disabled (for example an in-memory
            # test store): refresh completion must not look like a storage
            # failure.
            return True
        # Capture, write and replace under one lock.  If the snapshot were
        # captured before the lock, an older writer could replace a newer file.
        with self._persist_lock:
            try:
                document = self._disk_document()
                blob = json.dumps(document, ensure_ascii=True, allow_nan=False,
                                  separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError):
                _log.warning("benchmark cache serialization failed")
                return False
            if len(blob) > MAX_CACHE_BYTES:
                # The reader refuses files above this limit; never replace a
                # readable last-good file with one that would be unreadable.
                _log.warning(_CACHE_ERROR_TOO_LARGE)
                return False
            directory = os.path.dirname(self.path) or "."
            handle = None
            temporary = None
            try:
                os.makedirs(directory, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(
                    prefix=".benchmarks-", suffix=".tmp", dir=directory)
                handle = os.fdopen(descriptor, "wb")
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
                handle = None
                os.replace(temporary, self.path)
                temporary = None
                return True
            except OSError as exc:
                _log.warning("benchmark cache write failed (%s)", type(exc).__name__)
                return False
            finally:
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:
                        pass
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass

    # ── state views ──
    def _is_fresh(self, state: dict, now: float) -> bool:
        # A failed refresh keeps last_success (last-good data) but must not
        # report freshness while the current error is unresolved.
        return (state["error"] is None
                and state["last_success"] is not None
                and 0 <= now - state["last_success"] <= self.ttl)

    def read_state(self, *, now: float | None = None) -> dict:
        """Immediate in-memory snapshot with computed staleness (no I/O)."""
        current = self._clock() if now is None else now
        with self._state_lock:
            result = {}
            for name in SOURCE_NAMES:
                state = self._state[name]
                result[name] = {
                    "data": state["data"],
                    "last_checked": state["last_checked"],
                    "last_success": state["last_success"],
                    "error": state["error"],
                    "version": state["version"],
                    "source_updated": state["source_updated"],
                    "validators": state["validators"],
                    "stale": not self._is_fresh(state, current),
                }
            return result

    @property
    def refreshing(self) -> bool:
        with self._coord_lock:
            coordinator = self._coordinator
        with self._state_lock:
            inflight = bool(self._inflight)
        return coordinator or inflight

    # ── refresh ──
    def _enabled_sources(self) -> tuple[str, ...]:
        return tuple(name for name in SOURCE_NAMES if _source_enabled(name))

    def _auto_due(self, name: str, now: float) -> bool:
        with self._state_lock:
            stale = not self._is_fresh(self._state[name], now)
        return stale and now >= self._next_attempt.get(name, 0.0)

    def refresh_source(self, name: str) -> bool:
        """Refresh one source, retaining its last good data on any failure."""
        if name not in SOURCES or name not in self._enabled_sources():
            return False
        source_lock = self._source_locks[name]
        if not source_lock.acquire(blocking=False):
            return False  # another refresh for this source is already running
        with self._state_lock:
            self._inflight.add(name)
        try:
            now = self._clock()
            previous = self.read_state(now=now)[name]
            fetcher = self._fetchers.get(name)
            deadline = Deadline(SOURCE_DEADLINE)
            try:
                client = self._client_factory()
                snapshot = fetcher(client, previous, deadline) if fetcher else None
                if not isinstance(snapshot, dict):
                    raise SourceError(_SOURCE_ERROR_SCHEMA)
                if snapshot.get("not_modified"):
                    completed = self._clock()
                    with self._state_lock:
                        state = self._state[name]
                        if state["data"] is None:
                            # A 304 without any cached body cannot be used.
                            raise SourceError(_SOURCE_ERROR_SCHEMA)
                        state["last_checked"] = completed
                        state["last_success"] = completed
                        state["error"] = None
                    if not self._persist():
                        self._record_storage_failure(name, completed)
                        return False
                    self._failures[name] = 0
                    self._next_attempt.pop(name, None)
                    return True
                entries = _validated_entries(snapshot.get("entries"), source=name)
                version = _text(snapshot.get("version"), maximum=64)
                if version != SOURCES[name].version:
                    raise SourceError(_SOURCE_ERROR_SCHEMA)
                source_updated = _date_from_iso(snapshot.get("source_updated"))
                validators = _clean_validators_any(snapshot.get("validators"))
                meta = _clean_source_meta(snapshot.get("meta"))
                completed = self._clock()
                with self._state_lock:
                    self._state[name] = {
                        "schema_version": SOURCE_SCHEMA_VERSION,
                        "data": {"entries": entries, "meta": meta},
                        "last_checked": completed,
                        "last_success": completed,
                        "error": None,
                        "version": version,
                        "source_updated": source_updated,
                        "validators": validators,
                        "meta": meta,
                    }
                if not self._persist():
                    # The newly validated snapshot stays usable in memory, but
                    # the disk still holds the previous readable bytes.  Report
                    # a fixed storage error so callers/UI cannot claim a
                    # complete fresh success.
                    self._record_storage_failure(name, completed)
                    return False
                self._failures[name] = 0
                self._next_attempt.pop(name, None)
                return True
            except SourceError as exc:
                self._record_failure(name, self._clock(), str(exc))
            except Exception:
                # Never propagate exception text: it can contain a URL or
                # credentials from an underlying transport.
                self._record_failure(name, self._clock(), _SOURCE_ERROR_NETWORK)
            self._persist()
            return False
        finally:
            with self._state_lock:
                self._inflight.discard(name)
            source_lock.release()

    def _schedule_retry(self, name: str, now: float) -> None:
        failures = self._failures.get(name, 0) + 1
        self._failures[name] = failures
        delay = min(AUTO_RETRY_MAX, AUTO_RETRY_BASE * (2 ** min(failures - 1, 8)))
        self._next_attempt[name] = now + delay

    def _record_failure(self, name: str, now: float, message: str) -> None:
        safe = message if message in {
            _SOURCE_ERROR_NETWORK, _SOURCE_ERROR_TIMEOUT, _SOURCE_ERROR_DEADLINE,
            _SOURCE_ERROR_TOO_LARGE, _SOURCE_ERROR_REDIRECT, _SOURCE_ERROR_JSON,
            _SOURCE_ERROR_CSV, _SOURCE_ERROR_SCHEMA, _SOURCE_ERROR_PAGINATION,
            _SOURCE_ERROR_EMPTY,
        } or (isinstance(message, str) and message.startswith("HTTP ")
              and message[5:].isdigit()) else _SOURCE_ERROR_NETWORK
        with self._state_lock:
            state = self._state[name]
            state["last_checked"] = now
            state["error"] = safe
        self._schedule_retry(name, now)

    def _record_storage_failure(self, name: str, now: float) -> None:
        """Persistence failed after a valid network refresh.

        The freshly validated snapshot is intentionally retained in memory and
        the previous disk bytes are untouched by ``_persist``.  ``last_success``
        still describes the successful upstream validation, while the fixed
        error and the stale flag prevent this from being reported as a complete
        fresh success.  Retry backoff prevents an active poll from turning a
        broken cache path into a fetch/write storm.
        """
        with self._state_lock:
            state = self._state[name]
            state["last_checked"] = now
            state["error"] = _CACHE_ERROR_STORAGE
        _log.warning("benchmark cache storage unavailable")
        self._schedule_retry(name, now)

    def refresh_now(self, *, names=None, force: bool = True) -> dict:
        """Synchronous refresh (tests and the coordinator worker)."""
        results = {}
        for name in (self._enabled_sources() if names is None else names):
            if not force and not self._auto_due(name, self._clock()):
                continue
            results[name] = self.refresh_source(name)
        return results

    def _begin_coordinator(self) -> bool:
        with self._coord_lock:
            if self._coordinator:
                return False
            self._coordinator = True
            return True

    def _end_coordinator(self) -> None:
        with self._coord_lock:
            self._coordinator = False

    def _spawn(self, target, name: str):
        thread = self._thread_factory(target, name)
        try:
            thread.start()
        except Exception:
            self._end_coordinator()
            raise
        return thread

    def _refresh_worker(self, force: bool) -> None:
        try:
            threads = []
            for name in self._enabled_sources():
                if not force and not self._auto_due(name, self._clock()):
                    continue
                thread = self._thread_factory(
                    lambda source=name: self.refresh_source(source),
                    f"benchmark-{name}",
                )
                threads.append(thread)
                thread.start()
            for thread in threads:
                # Leave room for retries/backoff beyond the per-request deadline.
                thread.join(SOURCE_DEADLINE + 15)
        finally:
            self._end_coordinator()

    def request_refresh(self) -> dict:
        """Launch one guarded background refresh; never overlaps another."""
        if not self._begin_coordinator():
            return {"ok": True, "refreshing": True, "started": False,
                    "sources": list(self._enabled_sources())}
        try:
            self._spawn(lambda: self._refresh_worker(force=True), "benchmark-refresh")
        except Exception:
            return {"ok": False, "error": "unable to start refresh",
                    "refreshing": self.refreshing}
        return {"ok": True, "refreshing": True, "started": True,
                "sources": list(self._enabled_sources())}

    def ensure_refresh_scheduled(self) -> bool:
        """Best-effort auto refresh for stale sources (used by the read path)."""
        if self.refreshing:
            return False
        now = self._clock()
        if not any(self._auto_due(name, now) for name in self._enabled_sources()):
            return False
        if not self._begin_coordinator():
            return False
        try:
            self._spawn(lambda: self._refresh_worker(force=False), "benchmark-auto-refresh")
        except Exception:
            return False
        return True


_store_lock = threading.Lock()
_store: BenchmarkStore | None = None


def get_store() -> BenchmarkStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = BenchmarkStore()
        return _store


def _reset_store_for_tests(store: BenchmarkStore | None) -> None:
    """Test helper: replace the process-wide store (never used in production)."""
    global _store
    with _store_lock:
        _store = store


def request_refresh() -> dict:
    return get_store().request_refresh()


def ensure_refresh_scheduled() -> bool:
    return get_store().ensure_refresh_scheduled()


def read_state() -> dict:
    return get_store().read_state()


def is_refreshing() -> bool:
    return get_store().refreshing


# ── Payload assembly ───────────────────────────────────────────────────────
def _source_enabled(name: str, *, deepswe_enabled: bool | None = None,
                    livebench_enabled: bool | None = None) -> bool:
    if name == "harbor" or name == "epoch":
        return True
    if name == "livebench":
        return LIVEBENCH_ENABLED if livebench_enabled is None else livebench_enabled
    if name == "deepswe":
        return DEEPSWE_ENABLED if deepswe_enabled is None else deepswe_enabled
    return False


def _enabled_metrics(deepswe_enabled: bool | None = None) -> list[MetricSpec]:
    # Keep LiveBench definitions in the API contract even while disabled;
    # clients use sources.livebench.disabled to omit them from the dropdown.
    # DeepSWE is fully optional and only appears once enabled.
    enabled = DEEPSWE_ENABLED if deepswe_enabled is None else deepswe_enabled
    return [spec for spec in METRICS if spec.source != "deepswe" or enabled]


def _build_index(source_state: dict | None) -> dict:
    entries = ((source_state or {}).get("data") or {}).get("entries") or []
    by_metric: dict[str, dict[str, list[dict]]] = {}
    models = set()
    for entry in entries:
        model = entry["model"]
        by_metric.setdefault(entry["metric"], {}).setdefault(model, []).append(entry)
        models.add(model)
    lower: dict[str, str | None] = {}
    for model in models:
        key = model.lower()
        lower[key] = model if key not in lower else None  # ambiguous case fold
    return {"by_metric": by_metric, "models": models, "lower": lower}


def _identity_candidates(row: dict) -> list[str]:
    upstream = row.get("upstream_model") or ""
    candidates = [upstream]
    if "/" in upstream:
        remainder = upstream.split("/", 1)[1]
        if remainder and remainder != upstream:
            candidates.append(remainder)
            # LiteLLM gateway routes may sit behind the Responses-API pseudo
            # provider (openai/responses/<model>); the model identity is below
            # it. Only this exact segment is stripped — every other spelling
            # stays verbatim so no punctuation normalization can sneak in.
            if remainder.startswith("responses/"):
                inner = remainder.split("/", 1)[1]
                if inner and inner != remainder:
                    candidates.append(inner)
    return candidates


def _is_modified_variant(row: dict) -> bool:
    """Block stock scores for any variant marker on the model or its routes.

    The upstream model ID is the primary evidence, but a grouped route's public
    name is also explicit evidence.  If the gateway fronts a stock-looking
    upstream ID under a modified alias, the whole grouped model is conservative
    and gets no stock score.
    """
    values = list(_identity_candidates(row))
    name = row.get("name")
    if isinstance(name, str):
        values.append(name)
    routes = row.get("routes")
    if isinstance(routes, list):
        for route in routes:
            if isinstance(route, dict) and isinstance(route.get("name"), str):
                values.append(route["name"])
    haystack = " ".join(values).lower()
    return any(token in haystack for token in _MODIFIED_TOKENS)


def _resolve_identity(row: dict, source: str, index: dict) -> str | None:
    aliases = SOURCE_ALIASES.get(source, {})
    for candidate in _identity_candidates(row):
        for key in (candidate, candidate.lower()):
            target = aliases.get(key)
            if target is not None and target in index["models"]:
                return target
            if key in index["models"]:
                return key
        folded = candidate.lower()
        if index["lower"].get(folded):
            return index["lower"][folded]
    return None


def _result(entry: dict, source: str, *, selected: bool = False) -> dict:
    value = entry.get("value")
    if not _is_number(value):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    date_kind = entry.get("date_kind")
    if date_kind is not None and date_kind not in DATE_KINDS:
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    if (entry.get("date") is None) != (date_kind is None):
        raise SourceError(_SOURCE_ERROR_SCHEMA)
    metadata = dict(entry["metadata"])
    if date_kind is not None:
        # Also expose the semantic date label with the result's metadata so a
        # consumer that only renders metadata still cannot call it an eval date.
        metadata["date_kind"] = date_kind
    return {
        "source": source,
        "metric": entry["metric"],
        "model": entry["model"],
        "agent": entry["agent"],
        "effort": entry["effort"],
        "evaluator": entry["evaluator"],
        "version": entry["version"],
        "date": entry["date"],
        "date_kind": date_kind,
        "value": float(value),
        "ci": dict(entry["ci"]) if entry["ci"] else None,
        "configuration": entry["configuration"],
        "metadata": metadata,
        "selected": selected,
    }


def _select_latest(results: list[dict]) -> int | None:
    """Summarize by latest published dated result, then configuration order.

    ``date_kind`` describes what the date means (model release, benchmark
    release, or a genuine evaluation date); this selector never relabels it as
    an evaluation date.  The key never includes the score, so it is
    deterministic and cannot become a per-metric maximum.  Undated results are
    retained but never summarized.
    """
    dated = [item for item in results if item["date"]]
    if not dated:
        return None
    latest = max(item["date"] for item in dated)
    candidates = [(index, item) for index, item in enumerate(results)
                  if item["date"] == latest]
    if not candidates:
        return None
    candidates.sort(key=lambda pair: (
        EFFORT_PRIORITY.get((pair[1]["effort"] or "").lower(), 7),
        pair[1]["agent"] or "",
        pair[1]["configuration"] or "",
        pair[1]["model"] or "",
        pair[0],
    ))
    return candidates[0][0]


def build_payload(inventory: list[dict], state: dict, *,
                  deepswe_enabled: bool | None = None,
                  include_sources: bool = True) -> dict:
    """Assemble the AI Services contract from inventory rows and cache state."""
    metrics = _enabled_metrics(deepswe_enabled)
    enabled_sources = {source for source in SOURCE_NAMES
                       if _source_enabled(source, deepswe_enabled=deepswe_enabled)}
    # Disabled sources never contribute cached rows, even if an earlier run
    # (or another deployment) left data in the cache file.
    indexes = {
        source: (_build_index(state.get(source)) if source in enabled_sources
                 else _build_index(None))
        for source in SOURCE_NAMES
    }
    source_has_data = {
        source: bool(((state.get(source) or {}).get("data") or {}).get("entries"))
        for source in enabled_sources
    }
    models = []
    for row in inventory:
        mode = row.get("mode") or "chat"
        # "responses" is the OpenAI Responses API protocol, not a modality;
        # routes using it are ordinary text models and stay coding-comparable.
        unsupported = mode not in ("chat", "completion", "responses")
        model = {
            "name": row["name"],
            "upstream_model": row["upstream_model"],
            "provider": row["provider"],
            "mode": mode,
            "routes": [dict(route) for route in row.get("routes", [])],
            "bench": {},
            "results": {},
            "missing": {},
        }
        blocked = unsupported or _is_modified_variant(row)
        resolved = {}
        if not blocked:
            for source in SOURCE_NAMES:
                resolved[source] = _resolve_identity(row, source, indexes[source])
        for spec in metrics:
            if unsupported:
                model["missing"][spec.key] = MISSING_UNSUPPORTED
                continue
            if blocked:
                model["missing"][spec.key] = MISSING_UNMATCHED
                continue
            source = spec.source
            if not source_has_data.get(source):
                model["missing"][spec.key] = MISSING_UNAVAILABLE
                continue
            identity = resolved.get(source)
            if identity is None:
                model["missing"][spec.key] = MISSING_UNMATCHED
                continue
            entries = indexes[source]["by_metric"].get(spec.key, {}).get(identity, [])
            if not entries:
                model["missing"][spec.key] = MISSING_NOT_EVALUATED
                continue
            results = [_result(entry, source) for entry in entries]
            selected_index = _select_latest(results)
            if selected_index is None:
                model["missing"][spec.key] = MISSING_NOT_EVALUATED
            else:
                results[selected_index]["selected"] = True
                value = entries[selected_index]["value"]
                if _is_number(value):
                    model["bench"][spec.key] = float(value)
                else:
                    model["missing"][spec.key] = MISSING_NOT_EVALUATED
            model["results"][spec.key] = results
        models.append(model)

    payload = {"models": models, "metrics": [spec.as_dict() for spec in metrics]}
    if include_sources:
        sources = {}
        for name in SOURCE_NAMES:
            source_state = state.get(name) or {}
            disabled = name not in enabled_sources
            warning = (SOURCE_DISABLED_WARNINGS.get(name, SOURCES[name].warning)
                       if disabled else SOURCES[name].warning)
            sources[name] = {
                "last_checked": source_state.get("last_checked"),
                "last_success": source_state.get("last_success"),
                "source_updated": source_state.get("source_updated"),
                "stale": True if disabled else bool(source_state.get("stale", True)),
                "error": source_state.get("error"),
                "version": source_state.get("version") or SOURCES[name].version,
                "url": SOURCES[name].url,
                "warning": warning,
                "disabled": disabled,
            }
        payload["sources"] = sources
    return payload


def metrics_metadata(deepswe_enabled: bool | None = None) -> list[dict]:
    return [spec.as_dict() for spec in _enabled_metrics(deepswe_enabled)]


def last_success(state: dict) -> float | None:
    """Latest successful refresh among sources currently enabled for display."""
    values = [entry.get("last_success") for name, entry in state.items()
              if _source_enabled(name)
              and isinstance(entry.get("last_success"), (int, float))
              and not isinstance(entry.get("last_success"), bool)]
    return max(values) if values else None


__all__ = [
    "BenchmarkStore", "Deadline", "HttpClient", "METRICS", "SOURCES",
    "SOURCE_ALIASES", "DEEPSWE_ENABLED", "DEEPSWE_WARNING", "DEEPSWE_REVIEW_URL",
    "build_payload", "ensure_refresh_scheduled", "get_store", "is_refreshing",
    "last_success", "metrics_metadata", "read_state", "request_refresh",
]
