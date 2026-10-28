"""Read security-watch's fixed local ``latest.json``; no network or Markdown.

Regular file only, at most :data:`MAX_REPORT_BYTES`, opened without symlink
following. Unknown fields are retained; invalid reports become a safe error.
"""
from __future__ import annotations

import json
import math
import os
import stat
from datetime import datetime

MAX_REPORT_BYTES = 8 * 1024 * 1024
REPORT_FILENAME = "latest.json"
# Starlette renders with json.dumps(ensure_ascii=False).encode("utf-8").
# Reject values that cannot be encoded and cap nesting well below the JSON
# encoder's recursion limit so a corrupt report becomes a safe "invalid"
# response instead of a 500 during response rendering.
MAX_TRANSPORT_DEPTH = 100

_MESSAGES = {
    "not_configured": "Security reports are not configured.",
    "missing": "No security report is available yet.",
    "unreadable": "The security report could not be read.",
    "invalid": "The security report is invalid.",
    "too_large": "The security report is too large to display.",
}

_FINDING_TEXT_FIELDS = (
    "component", "advisory", "title", "installed_version",
    "affected_versions", "fixed_version", "reason", "action",
)
_FINDING_ENUMS = {
    "priority": ("act_now", "update_soon", "monitor"),
    "status": ("confirmed", "possible", "unknown", "not_affected"),
    "exploited": ("yes", "no", "unknown"),
    "confidence": ("high", "medium", "low"),
}
_REQUIRED_REPORT_FIELDS = (
    "checked_at", "summary", "checked_components", "limitations",
    "findings", "searches", "usage",
)


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

    ``directory`` is the configured ``SECURITY_REPORT_DIR``.  Unset, empty,
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
# Mirrors scripts/security-watch.py as of the current report format.  Only
# known, present fields are type-checked; unknown fields are retained untouched.

def _string(value, *, nonempty: bool) -> None:
    if not isinstance(value, str) or (nonempty and not value):
        _fail()


def _string_list(value, *, nonempty_items: bool) -> None:
    if not isinstance(value, list):
        _fail()
    for item in value:
        _string(item, nonempty=nonempty_items)


def _timestamp(value) -> None:
    if not isinstance(value, str) or not value:
        _fail()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _fail()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail()


def _mapping(value) -> None:
    if not isinstance(value, dict):
        _fail()


def _bool(value) -> None:
    if not isinstance(value, bool):
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


def _validate_finding(finding) -> None:
    _mapping(finding)
    for field in _FINDING_TEXT_FIELDS:
        if field not in finding:
            _fail()
        _string(finding[field], nonempty=True)
    for field, allowed in _FINDING_ENUMS.items():
        if finding.get(field) not in allowed:
            _fail()
    sources = finding.get("sources")
    if not isinstance(sources, list):
        _fail()
    for source in sources:
        _string(source, nonempty=True)
    if "not_rechecked" in finding:
        _bool(finding["not_rechecked"])


def _validate_findings(findings) -> None:
    if not isinstance(findings, list):
        _fail()
    for finding in findings:
        _validate_finding(finding)


def _validate_named_versions(rows) -> None:
    if not isinstance(rows, list):
        _fail()
    for row in rows:
        _mapping(row)
        for field in ("name", "version"):
            if field not in row:
                _fail()
            _string(row[field], nonempty=True)


def _validate_inventory(inventory) -> None:
    _mapping(inventory)
    if "collected_at" in inventory:
        _timestamp(inventory["collected_at"])
    if "host" in inventory:
        host = inventory["host"]
        _mapping(host)
        for field in ("alias", "distribution", "release", "kernel"):
            if field in host:
                _string(host[field], nonempty=False)
        if "packages" in host:
            _validate_named_versions(host["packages"])
        if "apt_last_successful_refresh" in host:
            _string(host["apt_last_successful_refresh"], nonempty=True)
        if "pending_updates_from_local_cache" in host:
            pending = host["pending_updates_from_local_cache"]
            if not isinstance(pending, list):
                _fail()
            for row in pending:
                _mapping(row)
                for field in ("name", "candidate", "installed"):
                    if field not in row:
                        _fail()
                    _string(row[field], nonempty=True)
    if "components" in inventory:
        components = inventory["components"]
        if not isinstance(components, list):
            _fail()
        for component in components:
            _mapping(component)
            for field in ("id", "image", "state", "version", "version_source", "exposure"):
                if field not in component:
                    _fail()
                _string(component[field], nonempty=field == "id")
            if "revision" in component:
                _string(component["revision"], nonempty=False)
    if "packages" in inventory:
        packages = inventory["packages"]
        if not isinstance(packages, list):
            _fail()
        for package in packages:
            _mapping(package)
            for field in ("component", "ecosystem", "name", "version"):
                if field not in package:
                    _fail()
                _string(package[field], nonempty=True)
            if "source" in package:
                _string(package["source"], nonempty=False)
    if "coverage_gaps" in inventory:
        _string_list(inventory["coverage_gaps"], nonempty_items=False)
    if "collection_errors" in inventory:
        _string_list(inventory["collection_errors"], nonempty_items=False)
    if "remote_hosts" in inventory:
        remote_hosts = inventory["remote_hosts"]
        if not isinstance(remote_hosts, list):
            _fail()
        for remote in remote_hosts:
            _mapping(remote)
            if "alias" not in remote:
                _fail()
            _string(remote["alias"], nonempty=True)
            if "collected_at" in remote:
                _timestamp(remote["collected_at"])
            if "host" in remote:
                _mapping(remote["host"])


def _validate_advisory_checks(checks) -> None:
    _mapping(checks)
    if "checked_at" in checks:
        _timestamp(checks["checked_at"])
    if "osv_matches" in checks:
        matches = checks["osv_matches"]
        if not isinstance(matches, list):
            _fail()
        for match in matches:
            _mapping(match)
            for field in ("component", "ecosystem", "name", "version", "advisory", "url"):
                if field in match:
                    _string(match[field], nonempty=field in ("advisory", "url", "component"))
            if "advisory" not in match or "url" not in match:
                _fail()
    if "kev_candidates" in checks:
        candidates = checks["kev_candidates"]
        if not isinstance(candidates, list):
            _fail()
        for candidate in candidates:
            _mapping(candidate)
            if "cveID" in candidate:
                _string(candidate["cveID"], nonempty=True)
            if "product" in candidate:
                _string(candidate["product"], nonempty=False)
    if "errors" in checks:
        _string_list(checks["errors"], nonempty_items=False)
    for field in ("kev_source", "kev_catalog_version"):
        if field in checks:
            _string(checks[field], nonempty=True)


def _validate_dependabot(dependabot) -> None:
    _mapping(dependabot)
    for key in ("open", "new"):
        if key not in dependabot:
            _fail()
        entries = dependabot[key]
        if not isinstance(entries, list):
            _fail()
        for entry in entries:
            _mapping(entry)
            for field in ("branch", "opened", "tip"):
                if field not in entry:
                    _fail()
                _string(entry[field], nonempty=field == "branch")


def _validate_rejected(rows) -> None:
    if not isinstance(rows, list):
        _fail()
    for row in rows:
        _mapping(row)
        for field in ("component", "advisory", "reason"):
            if field not in row:
                _fail()
            _string(row[field], nonempty=False)


def _validate_report(report) -> None:
    _mapping(report)
    for field in _REQUIRED_REPORT_FIELDS:
        if field not in report:
            _fail()

    _timestamp(report["checked_at"])
    _string(report["summary"], nonempty=True)
    _string_list(report["checked_components"], nonempty_items=False)
    _string_list(report["limitations"], nonempty_items=False)
    _validate_findings(report["findings"])

    searches = report["searches"]
    if isinstance(searches, bool) or not isinstance(searches, int) or searches < 0:
        _fail()
    _mapping(report["usage"])

    if "assessments" in report:
        _validate_findings(report["assessments"])
    if "rejected_assessments" in report:
        _validate_rejected(report["rejected_assessments"])
    if "inventory" in report:
        _validate_inventory(report["inventory"])
    if "advisory_checks" in report:
        _validate_advisory_checks(report["advisory_checks"])
    if "source_checks_complete" in report:
        _bool(report["source_checks_complete"])
    if "search_sources" in report:
        _string_list(report["search_sources"], nonempty_items=True)
    if "dependabot" in report:
        _validate_dependabot(report["dependabot"])
