import copy
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import homelab_core as core  # noqa: E402
import health_report  # noqa: E402

# Import the app with its startup side effects stubbed: these tests must never
# poll a Shelly plug, probe the desktop or create background threads.
with patch.object(core, "prime_counters"), \
        patch.object(core, "start_energy_tracker"), \
        patch.object(core, "start_desktop_monitor"):
    import main  # noqa: E402

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "health-report.json"
REPORT = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def mutated(mutator):
    report = copy.deepcopy(REPORT)
    mutator(report)
    return report


class HealthReportReadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.report_dir = self.root / "reports"
        self.report_dir.mkdir()
        self.publisher = patch.object(core, "HEALTH_REPORT_DIR", str(self.report_dir))
        self.publisher.start()
        self.addCleanup(self.publisher.stop)
        # get_features() also reports unrelated integrations; stub their local
        # configuration reads so tests never touch a real auth/config store.
        key_patch = patch.object(core, "_read_opencode_go_key", return_value=None)
        key_patch.start()
        self.addCleanup(key_patch.stop)
        config_patch = patch.object(core, "_litellm_config_path", return_value=None)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.write_report(REPORT)

    def tearDown(self):
        self.temporary.cleanup()

    def write_report(self, report=None, *, raw=None, directory=None):
        target = Path(directory or self.report_dir) / health_report.REPORT_FILENAME
        if raw is not None:
            target.write_bytes(raw)
        else:
            target.write_text(json.dumps(report if report is not None else REPORT), encoding="utf-8")

    def read(self):
        return health_report.read_report(self.report_dir)

    def assert_invalid(self, report):
        self.write_report(report)
        self.assertEqual(self.read(), {"error": health_report._MESSAGES["invalid"], "code": "invalid"})

    def test_complete_report_is_preserved_including_unknown_and_retained_fields(self):
        extended = copy.deepcopy(REPORT)
        extended["future_section"] = {"producer": "synthetic", "enabled": True}
        extended["disks"][0]["future_flag"] = 7
        extended["issues"][0]["future_note"] = "synthetic"
        extended["details"][1]["future_field"] = True
        self.write_report(extended)

        self.assertEqual(self.read(), {"report": extended})
        self.assertEqual(extended["disks"][0]["free_kb"], REPORT["disks"][0]["free_kb"])
        self.assertEqual(extended["issues"][0]["message"], REPORT["issues"][0]["message"])

    def test_details_absent_is_allowed(self):
        minimal = copy.deepcopy(REPORT)
        del minimal["details"]
        self.write_report(minimal)
        self.assertEqual(self.read(), {"report": minimal})

    def test_deadline_variant_with_null_metrics_is_valid(self):
        deadline = {
            "host": "sample-host",
            "time": "2026-11-04 07:00:03",
            "status": "crit",
            "warnings": 0,
            "critical": 1,
            "load": None,
            "cpus": None,
            "memory_percent": None,
            "disks": [],
            "issues": [
                {"level": "CRIT",
                 "message": "health check deadline exceeded: load, cpu and memory readings unavailable"}
            ],
            "details": [],
        }
        self.write_report(deadline)
        self.assertEqual(self.read(), {"report": deadline})

    def test_boundary_values_are_valid(self):
        variants = [
            lambda report: (report.update(cpus=1, memory_percent=100),
                            report["disks"][0].update(used_percent=100, free_kb=0)),
            lambda report: (report.update(memory_percent=0),
                            report["disks"][0].update(used_percent=0)),
            lambda report: report.update(load="0 0 0"),
            lambda report: report.update(warnings=10 ** 9, critical=10 ** 6),
        ]
        for index, mutate in enumerate(variants):
            with self.subTest(index=index):
                report = mutated(mutate)
                self.write_report(report)
                self.assertEqual(self.read(), {"report": report})

    def test_atomic_replacement_is_visible_to_the_next_read(self):
        first = self.read()
        self.assertEqual(first["report"], REPORT)

        replacement = copy.deepcopy(REPORT)
        replacement["time"] = "2026-11-04 08:00:00"
        replacement["status"] = "warn"
        replacement["warnings"] = 2
        temporary = self.report_dir / ".latest.json.tmp"
        temporary.write_text(json.dumps(replacement), encoding="utf-8")
        os.replace(temporary, self.report_dir / health_report.REPORT_FILENAME)

        self.assertEqual(self.read()["report"], replacement)
        self.assertEqual(first["report"], REPORT)

    def test_only_the_fixed_latest_filename_is_consulted(self):
        (self.report_dir / "other.json").write_text("not a report", encoding="utf-8")
        (self.report_dir / "latest.md").write_text("markdown is not authoritative", encoding="utf-8")
        self.assertEqual(self.read()["report"], REPORT)

    def test_malformed_known_report_fields_are_rejected(self):
        cases = {
            "root is not an object": [],
            "missing host": mutated(lambda report: report.pop("host")),
            "missing time": mutated(lambda report: report.pop("time")),
            "missing status": mutated(lambda report: report.pop("status")),
            "missing warnings": mutated(lambda report: report.pop("warnings")),
            "missing critical": mutated(lambda report: report.pop("critical")),
            "missing load": mutated(lambda report: report.pop("load")),
            "missing cpus": mutated(lambda report: report.pop("cpus")),
            "missing memory_percent": mutated(lambda report: report.pop("memory_percent")),
            "missing disks": mutated(lambda report: report.pop("disks")),
            "missing issues": mutated(lambda report: report.pop("issues")),
            "empty host": mutated(lambda report: report.update(host="")),
            "host is not a string": mutated(lambda report: report.update(host=7)),
            "time is ISO 8601 instead": mutated(
                lambda report: report.update(time="2026-11-04T07:00:03")),
            "time is only a date": mutated(lambda report: report.update(time="2026-11-04")),
            "time has no seconds": mutated(lambda report: report.update(time="2026-11-04 07:00")),
            "time is not a string": mutated(lambda report: report.update(time=1762245603)),
            "empty time": mutated(lambda report: report.update(time="")),
            "status is unknown": mutated(lambda report: report.update(status="healthy")),
            "status is wrong case": mutated(lambda report: report.update(status="OK")),
            "status is null": mutated(lambda report: report.update(status=None)),
            "status is a bool": mutated(lambda report: report.update(status=True)),
            "warnings is negative": mutated(lambda report: report.update(warnings=-1)),
            "warnings is a bool": mutated(lambda report: report.update(warnings=True)),
            "warnings is a string": mutated(lambda report: report.update(warnings="0")),
            "warnings is a float": mutated(lambda report: report.update(warnings=1.0)),
            "critical is negative": mutated(lambda report: report.update(critical=-1)),
            "critical is a bool": mutated(lambda report: report.update(critical=False)),
            "load is a number": mutated(lambda report: report.update(load=0.42)),
            "load has two fields": mutated(lambda report: report.update(load="0.42 0.35")),
            "load has four fields": mutated(lambda report: report.update(load="0.42 0.35 0.31 0.20")),
            "load is not numeric": mutated(lambda report: report.update(load="high medium low")),
            "load is empty": mutated(lambda report: report.update(load="")),
            "load has a double space": mutated(lambda report: report.update(load="0.42  0.35 0.31")),
            "cpus is zero": mutated(lambda report: report.update(cpus=0)),
            "cpus is negative": mutated(lambda report: report.update(cpus=-2)),
            "cpus is a bool": mutated(lambda report: report.update(cpus=True)),
            "cpus is a string": mutated(lambda report: report.update(cpus="8")),
            "cpus is a float": mutated(lambda report: report.update(cpus=8.0)),
            "memory_percent is negative": mutated(lambda report: report.update(memory_percent=-1)),
            "memory_percent is over 100": mutated(lambda report: report.update(memory_percent=101)),
            "memory_percent is a bool": mutated(lambda report: report.update(memory_percent=True)),
            "memory_percent is a string": mutated(lambda report: report.update(memory_percent="43")),
            "disks is not a list": mutated(lambda report: report.update(disks={})),
            "disk is not an object": mutated(lambda report: report["disks"].append("/")),
            "disk is missing mount": mutated(lambda report: report["disks"][0].pop("mount")),
            "disk mount is empty": mutated(lambda report: report["disks"][0].update(mount="")),
            "disk is missing used_percent": mutated(
                lambda report: report["disks"][0].pop("used_percent")),
            "disk used_percent is 101": mutated(
                lambda report: report["disks"][0].update(used_percent=101)),
            "disk used_percent is negative": mutated(
                lambda report: report["disks"][0].update(used_percent=-1)),
            "disk used_percent is a bool": mutated(
                lambda report: report["disks"][0].update(used_percent=True)),
            "disk is missing free_kb": mutated(lambda report: report["disks"][0].pop("free_kb")),
            "disk free_kb is negative": mutated(
                lambda report: report["disks"][0].update(free_kb=-1)),
            "disk free_kb is a bool": mutated(
                lambda report: report["disks"][0].update(free_kb=False)),
            "issues is not a list": mutated(lambda report: report.update(issues={})),
            "issue is not an object": mutated(lambda report: report["issues"].append("warn")),
            "issue level info is rejected": mutated(
                lambda report: report["issues"][0].update(level="info")),
            "issue level OK is rejected": mutated(
                lambda report: report["issues"][0].update(level="OK")),
            "issue level is lowercase": mutated(
                lambda report: report["issues"][0].update(level="warn")),
            "issue is missing level": mutated(lambda report: report["issues"][0].pop("level")),
            "issue message is empty": mutated(
                lambda report: report["issues"][0].update(message="")),
            "issue is missing message": mutated(lambda report: report["issues"][0].pop("message")),
            "details is not a list": mutated(lambda report: report.update(details="none")),
            "details is null": mutated(lambda report: report.update(details=None)),
            "detail level WARN is rejected": mutated(
                lambda report: report["details"][0].update(level="WARN")),
            "detail level crit is rejected": mutated(
                lambda report: report["details"][0].update(level="crit")),
            "detail message is empty": mutated(
                lambda report: report["details"][0].update(message="")),
            "detail is missing message": mutated(
                lambda report: report["details"][0].pop("message")),
        }
        for label, report in cases.items():
            with self.subTest(label):
                self.assert_invalid(report)

    def test_malformed_json_and_non_utf8_are_invalid(self):
        for raw in (b"", b"{", b"[]", b"not json", b"\xff\xfe\x00", b"NaN",
                    b"[" * 2000 + b"]" * 2000):
            with self.subTest(raw=raw[:20]):
                self.write_report(raw=raw)
                self.assertEqual(self.read()["code"], "invalid")

    def test_non_finite_json_numbers_are_invalid(self):
        for literal in (b"NaN", b"Infinity", b"1e999"):
            with self.subTest(literal=literal):
                raw = FIXTURE_PATH.read_bytes().replace(
                    b'"free_kb": 104857600', b'"free_kb": ' + literal)
                self.write_report(raw=raw)
                self.assertEqual(self.read()["code"], "invalid")

    def test_escaped_unpaired_surrogates_are_invalid_anywhere(self):
        cases = {
            "host value": lambda report: report.update(host="Synthetic \ud800"),
            "unknown nested value": lambda report: report.update(
                future_extension={"nested": ["\udfff"]}),
            "unknown nested key": lambda report: report.update(
                future_extension={"\ud800": "synthetic"}),
            "issue message": lambda report: report["issues"][0].update(
                message="Synthetic \udfff"),
        }
        for label, mutator in cases.items():
            with self.subTest(label):
                report = mutated(mutator)
                # The on-disk file is plain ASCII with a JSON \uXXXX escape;
                # the reader must reject the decoded lone surrogate.
                raw = json.dumps(report).encode("ascii")
                self.assertIn(b"\\ud", raw)
                self.write_report(raw=raw)
                self.assertEqual(self.read()["code"], "invalid")

    def test_valid_unicode_and_emoji_are_preserved(self):
        report = mutated(lambda item: item.update(
            host="sample-höst 😀",
            future_extension={"clé": "valeur", "emoji😀": ["ok"]},
        ))
        self.write_report(report)
        self.assertEqual(self.read(), {"report": report})

    def test_excessive_nesting_is_invalid_without_a_serialization_error(self):
        nested = "synthetic"
        for _ in range(health_report.MAX_TRANSPORT_DEPTH + 5):
            nested = [nested]
        report = mutated(lambda item: item.update(future_extension=nested))
        # json.dumps() itself succeeds at this depth; only the API renderer
        # and response contract need a deliberately safe bound.
        self.write_report(report)
        self.assertEqual(self.read()["code"], "invalid")

    def test_missing_report_is_safe_and_does_not_register_as_configured_absence(self):
        absent = self.root / "absent-health-directory"
        with patch.object(core, "HEALTH_REPORT_DIR", str(absent)):
            result = core.get_health_report()
            self.assertEqual(core.get_features()["health"], True)
        self.assertEqual(result["code"], "missing")
        self.assertNotIn(str(absent), result["error"])
        self.assertNotIn("latest.json", result["error"])

    def test_configured_path_can_be_pathlike_or_padded(self):
        for value in (self.report_dir, f"  {self.report_dir}  "):
            with self.subTest(value=value), patch.object(core, "HEALTH_REPORT_DIR", value):
                self.assertEqual(core.get_features()["health"], True)
                self.assertEqual(core.get_health_report()["report"], REPORT)

    def test_directory_at_latest_path_is_unreadable_not_followed(self):
        target = self.report_dir / health_report.REPORT_FILENAME
        target.unlink()
        target.mkdir()
        result = self.read()
        self.assertEqual(result["code"], "unreadable")
        self.assertNotIn(str(self.report_dir), result["error"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root bypasses mode-bit denial")
    def test_permission_denied_is_unreadable(self):
        target = self.report_dir / health_report.REPORT_FILENAME
        target.chmod(0)
        try:
            result = self.read()
        finally:
            target.chmod(0o600)
        self.assertEqual(result["code"], "unreadable")
        self.assertNotIn(str(self.report_dir), result["error"])

    def test_final_symlink_is_refused_and_target_is_not_served(self):
        target = self.report_dir / health_report.REPORT_FILENAME
        target.unlink()
        outside = self.root / "outside-report.json"
        outside.write_text(json.dumps(REPORT), encoding="utf-8")
        target.symlink_to(outside)

        result = self.read()
        self.assertEqual(result["code"], "unreadable")
        self.assertNotIn("report", result)
        self.assertEqual(outside.read_text(encoding="utf-8"), json.dumps(REPORT))

    def test_fifo_is_refused_without_blocking(self):
        target = self.report_dir / health_report.REPORT_FILENAME
        target.unlink()
        os.mkfifo(target)

        started = time.monotonic()
        result = self.read()
        elapsed = time.monotonic() - started

        self.assertEqual(result["code"], "unreadable")
        self.assertLess(elapsed, 2.0)

    def test_oversized_report_is_rejected_by_a_bounded_read(self):
        target = self.report_dir / health_report.REPORT_FILENAME
        with target.open("wb") as handle:
            handle.truncate(health_report.MAX_REPORT_BYTES + 1)
        self.assertEqual(self.read()["code"], "too_large")

        # Even if fstat under-reports the size, the read itself is capped and
        # the resulting byte count is still rejected.
        fake = os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, 0, 0, 10, 0, 0, 0))
        with patch.object(health_report.os, "fstat", return_value=fake):
            self.assertEqual(self.read()["code"], "too_large")

    def test_every_failure_uses_exactly_the_safe_error_contract(self):
        cases = {
            "not_configured": (None, None),
            "missing": (str(self.root / "missing-dir"), None),
            "unreadable": (str(self.report_dir), "directory"),
            "invalid": (str(self.report_dir), b"{"),
            "too_large": (str(self.report_dir), "oversized"),
        }
        for code, (directory, state) in cases.items():
            with self.subTest(code=code):
                if state == "directory":
                    target = self.report_dir / health_report.REPORT_FILENAME
                    target.unlink()
                    target.mkdir()
                elif state == "oversized":
                    target = self.report_dir / health_report.REPORT_FILENAME
                    if target.exists():
                        target.unlink()
                    with target.open("wb") as handle:
                        handle.truncate(health_report.MAX_REPORT_BYTES + 1)
                elif state is not None:
                    self.write_report(raw=state)

                with patch.object(core, "HEALTH_REPORT_DIR", directory):
                    result = core.get_health_report()
                self.assertEqual(set(result), {"error", "code"})
                self.assertEqual(result["code"], code)
                self.assertIsInstance(result["error"], str)
                self.assertTrue(result["error"])
                self.assertNotIn(str(self.report_dir), result["error"])
                self.assertNotIn("latest.json", result["error"])

                # Restore a valid report for the next iteration.
                target = self.report_dir / health_report.REPORT_FILENAME
                if target.is_dir():
                    target.rmdir()
                elif target.exists():
                    target.unlink()
                self.write_report(REPORT)


class HealthReportApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.report_dir = self.root / "reports"
        self.report_dir.mkdir()
        self.publisher = patch.object(core, "HEALTH_REPORT_DIR", str(self.report_dir))
        self.publisher.start()
        self.addCleanup(self.publisher.stop)
        key_patch = patch.object(core, "_read_opencode_go_key", return_value=None)
        key_patch.start()
        self.addCleanup(key_patch.stop)
        config_patch = patch.object(core, "_litellm_config_path", return_value=None)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        (self.report_dir / health_report.REPORT_FILENAME).write_text(
            json.dumps(REPORT), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_api_returns_complete_report_with_no_store(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), {"report": REPORT})

    def test_api_failure_uses_same_json_contract_with_no_store(self):
        with patch.object(core, "HEALTH_REPORT_DIR", ""):
            response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(set(response.json()), {"error", "code"})
        self.assertEqual(response.json()["code"], "not_configured")

    def test_api_rejects_escaped_surrogates_instead_of_raising(self):
        report = mutated(lambda item: (
            item.update(host="Synthetic \ud800"),
            item.update(future_extension={"\udfff": "synthetic"}),
        ))
        target = self.report_dir / health_report.REPORT_FILENAME
        target.write_bytes(json.dumps(report).encode("ascii"))

        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["code"], "invalid")

    def test_api_preserves_valid_unicode_and_emoji(self):
        report = mutated(lambda item: item.update(
            host="sample-höst 😀",
            future_extension={"clé": "valeur", "emoji😀": ["ok"]},
        ))
        target = self.report_dir / health_report.REPORT_FILENAME
        target.write_text(json.dumps(report), encoding="utf-8")

        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), {"report": report})

    def test_api_rejects_excessive_nesting_instead_of_raising(self):
        nested = "synthetic"
        for _ in range(health_report.MAX_TRANSPORT_DEPTH + 5):
            nested = [nested]
        report = mutated(lambda item: item.update(future_extension=nested))
        target = self.report_dir / health_report.REPORT_FILENAME
        target.write_text(json.dumps(report), encoding="utf-8")

        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["code"], "invalid")

    def test_feature_flag_is_true_without_a_report_and_false_without_configuration(self):
        empty = self.root / "empty"
        empty.mkdir()
        with patch.object(core, "HEALTH_REPORT_DIR", str(empty)):
            self.assertTrue(core.get_features()["health"])
            self.assertEqual(self.client.get("/api/health").json()["code"], "missing")
        for disabled in (None, "", "   "):
            with self.subTest(disabled=disabled), \
                    patch.object(core, "HEALTH_REPORT_DIR", disabled):
                self.assertFalse(core.get_features()["health"])
                self.assertEqual(core.get_health_report()["code"], "not_configured")

    def test_route_is_get_only_and_no_download_route_exists(self):
        matches = [route for route in main.app.routes
                   if getattr(route, "path", None) == "/api/health"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].methods, {"GET"})
        self.assertFalse([getattr(route, "path", "")
                          for route in main.app.routes
                          if "download" in getattr(route, "path", "").lower()])

        headers = {"Origin": "https://testserver", "X-Homelab-Request": "1"}
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)("/api/health", headers=headers)
                self.assertEqual(response.status_code, 405)

    def test_read_and_api_perform_no_network_calls(self):
        with patch("socket.create_connection", side_effect=AssertionError("network")), \
                patch.object(core.requests.Session, "request",
                             side_effect=AssertionError("network")), \
                patch.object(core.requests, "get", side_effect=AssertionError("network")):
            self.assertEqual(core.get_health_report()["report"], REPORT)
            response = self.client.get("/api/health")
        self.assertEqual(response.json(), {"report": REPORT})


if __name__ == "__main__":
    unittest.main()
