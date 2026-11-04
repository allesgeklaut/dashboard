"""Read server-health's fixed local ``latest.json``; no network or Markdown.

Regular file only, at most :data:`MAX_REPORT_BYTES`, opened without symlink
following. Unknown fields are retained; invalid reports become a safe error.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat

MAX_REPORT_BYTES = 8 * 1024 * 1024
REPORT_FILENAME = "latest.json"
# Starlette renders with json.dumps(ensure_ascii=False).encode("utf-8").
# Reject values that cannot be encoded and cap nesting well below the JSON
# encoder's recursion limit so a corrupt report becomes a safe "invalid"
# response instead of a 500 during response rendering.
MAX_TRANSPORT_DEPTH = 100

_MESSAGES = {
    "not_configured": "Health report is not configured.",
    "missing": "No health report is available yet.",
    "unreadable": "The health report could not be read.",
    "invalid": "The health report is invalid.",
    "too_large": "The health report is too large to display.",
}

_STATUSES = ("ok", "warn", "crit")
_ISSUE_LEVELS = ("WARN", "CRIT")
_DETAIL_LEVELS = ("OK", "info")
_REQUIRED_REPORT_FIELDS = (
    "host", "time", "status", "warnings", "critical",
    "load", "cpus", "memory_percent", "disks", "issues",
)
# Host `date '+%Y-%m-%d %H:%M:%S'` — a fixed-width local timestamp, not ISO 8601.
_TIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
# /proc/loadavg's first three fields, e.g. "0.42 0.35 0.31".
_LOAD_PATTERN = re.compile(r"\d+(?:\.\d+)? \d+(?:\.\d+)? \d+(?:\.\d+)?")


class _InvalidReport(Exception):
    """Internal marker; no detail is ever exposed to API callers."""


def _fail() -> None:
    raise _InvalidReport


def _directory(value):
    """Return a nonblank string directory, or None when not configured."""
    if value is None:
        return None
    try:
        path = os.fspath(value)
    except TypeError:
        return None
    if not isinstance(path, str):
        return None
    path = path.strip()
    return path or None


def _error(code: str) -> dict:
    return {"error": _MESSAGES[code], "code": code}


def _reject_constant(_value: str):
    # NaN/Infinity are Python extensions, not JSON; they would also break the
    # dashboard's JSON response encoding.
    raise ValueError


def _finite_float(value: str) -> float:
    # A huge exponent such as 1e999 is valid JSON but parses to inf in Python,
    # which Starlette's strict JSON encoder cannot serve.
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return number


def read_report(directory) -> dict:
    """Read ``<directory>/latest.json`` with a hard 8 MiB limit.

    ``directory`` is the configured ``HEALTH_REPORT_DIR``.  Unset, empty,
    or whitespace-only values disable the feature rather than falling back to
    any default location.
    """
    configured = _directory(directory)
    if configured is None:
        return _error("not_configured")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    # O_NOFOLLOW refuses a final-component symlink; O_NONBLOCK prevents a FIFO
    # from stalling the request before fstat can reject the non-regular file.
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    path = os.path.join(configured, REPORT_FILENAME)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return _error("missing")
    except (OSError, ValueError):
        return _error("unreadable")

    try:
        try:
            info = os.fstat(fd)
        except OSError:
            return _error("unreadable")
        if not stat.S_ISREG(info.st_mode):
            return _error("unreadable")
        if info.st_size > MAX_REPORT_BYTES:
            return _error("too_large")
        try:
            handle = os.fdopen(fd, "rb")
            fd = -1
            try:
                raw = handle.read(MAX_REPORT_BYTES + 1)
            finally:
                handle.close()
        except OSError:
            return _error("unreadable")
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    # The on-disk size may have grown after fstat; bound the bytes we accept.
    if len(raw) > MAX_REPORT_BYTES:
        return _error("too_large")
    try:
        report = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant,
                            parse_float=_finite_float)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _error("invalid")
    try:
        _validate_report(report)
        _validate_transport_safe(report)
    except _InvalidReport:
        return _error("invalid")
    return {"report": report}


# ── Validation ────────────────────────────────────────────────────────────────
# Mirrors scripts/server-health.sh as of the current report format.  Only
# known, present fields are type-checked; unknown fields are retained untouched.

def _string(value, *, nonempty: bool) -> None:
    if not isinstance(value, str) or (nonempty and not value):
        _fail()


def _mapping(value) -> None:
    if not isinstance(value, dict):
        _fail()


def _int(value, *, minimum: int, maximum: int | None = None) -> None:
    # bool is an int subclass; the report's counters must never be booleans.
    if isinstance(value, bool) or not isinstance(value, int):
        _fail()
    if value < minimum or (maximum is not None and value > maximum):
        _fail()


def _optional_int(value, *, minimum: int, maximum: int | None = None) -> None:
    # The deadline variant reports null metrics instead of numbers.
    if value is None:
        return
    _int(value, minimum=minimum, maximum=maximum)


def _report_time(value) -> None:
    if not isinstance(value, str) or _TIME_PATTERN.fullmatch(value) is None:
        _fail()


def _load_average(value) -> None:
    if value is None:
        return
    if not isinstance(value, str) or _LOAD_PATTERN.fullmatch(value) is None:
        _fail()


def _utf8(value: str) -> None:
    if not value.isascii():
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            # Escaped lone surrogates are valid JSON text but cannot be
            # emitted by JSONResponse(ensure_ascii=False).encode("utf-8").
            _fail()


def _validate_transport_safe(value, depth: int = 0) -> None:
    """Reject data that JSONResponse cannot render, without changing it.

    The walk is recursive but depth-capped before Python's JSON encoder can
    raise RecursionError; every string, including unknown object keys and
    values, must be encodable as UTF-8.  Valid astral characters/emoji are
    preserved untouched.
    """
    if depth > MAX_TRANSPORT_DEPTH:
        _fail()
    if isinstance(value, str):
        _utf8(value)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail()
            _utf8(key)
            _validate_transport_safe(item, depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _validate_transport_safe(item, depth + 1)
        return
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail()
        return
    _fail()


def _validate_disk(disk) -> None:
    _mapping(disk)
    for field in ("mount", "used_percent", "free_kb"):
        if field not in disk:
            _fail()
    _string(disk["mount"], nonempty=True)
    _int(disk["used_percent"], minimum=0, maximum=100)
    _int(disk["free_kb"], minimum=0)


def _validate_entry(entry, levels) -> None:
    _mapping(entry)
    for field in ("level", "message"):
        if field not in entry:
            _fail()
    if entry["level"] not in levels:
        _fail()
    _string(entry["message"], nonempty=True)


def _validate_report(report) -> None:
    _mapping(report)
    for field in _REQUIRED_REPORT_FIELDS:
        if field not in report:
            _fail()

    _string(report["host"], nonempty=True)
    _report_time(report["time"])
    if report["status"] not in _STATUSES:
        _fail()
    _int(report["warnings"], minimum=0)
    _int(report["critical"], minimum=0)
    _load_average(report["load"])
    _optional_int(report["cpus"], minimum=1)
    _optional_int(report["memory_percent"], minimum=0, maximum=100)

    disks = report["disks"]
    if not isinstance(disks, list):
        _fail()
    for disk in disks:
        _validate_disk(disk)

    issues = report["issues"]
    if not isinstance(issues, list):
        _fail()
    for issue in issues:
        _validate_entry(issue, _ISSUE_LEVELS)

    if "details" in report:
        details = report["details"]
        if not isinstance(details, list):
            _fail()
        for detail in details:
            _validate_entry(detail, _DETAIL_LEVELS)
