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
import security_report  # noqa: E402

# Import the app with its startup side effects stubbed: these tests must never
# poll a Shelly plug, probe the desktop or create background threads.
with patch.object(core, "prime_counters"), \
        patch.object(core, "start_energy_tracker"), \
        patch.object(core, "start_desktop_monitor"):
    import main  # noqa: E402

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "security-report.json"
REPORT = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def mutated(mutator):
    report = copy.deepcopy(REPORT)
    mutator(report)
    return report


class SecurityReportReadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.report_dir = self.root / "reports"
        self.report_dir.mkdir()
        self.publisher = patch.object(core, "SECURITY_REPORT_DIR", str(self.report_dir))
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
        target = Path(directory or self.report_dir) / security_report.REPORT_FILENAME
        if raw is not None:
            target.write_bytes(raw)
        else:
            target.write_text(json.dumps(report if report is not None else REPORT), encoding="utf-8")

    def read(self):
        return security_report.read_report(self.report_dir)

    def assert_invalid(self, report):
        self.write_report(report)
        self.assertEqual(self.read(), {"error": security_report._MESSAGES["invalid"], "code": "invalid"})

    def test_complete_report_is_preserved_including_unknown_and_retained_fields(self):
        extended = copy.deepcopy(REPORT)
        extended["future_section"] = {"producer": "synthetic", "enabled": True}
        extended["findings"][1]["future_flag"] = 7
        extended["inventory"]["future_component_metadata"] = {"note": "synthetic"}
        extended["dependabot"]["future_field"] = True
        self.write_report(extended)

        self.assertEqual(self.read(), {"report": extended})
        self.assertTrue(extended["findings"][1]["not_rechecked"])
        self.assertEqual(extended["findings"][0]["sources"],
                         REPORT["findings"][0]["sources"])

    def test_optional_sections_absent_are_allowed(self):
        minimal = copy.deepcopy(REPORT)
        for key in ("assessments", "rejected_assessments", "inventory", "advisory_checks",
                    "source_checks_complete", "search_sources", "dependabot"):
            del minimal[key]
        self.write_report(minimal)
        self.assertEqual(self.read(), {"report": minimal})

    def test_merged_retained_findings_may_exceed_the_per_run_limit(self):
        merged = copy.deepcopy(REPORT)
        template = merged["findings"][1]
        merged["findings"] = []
        for index in range(30):
            finding = copy.deepcopy(template)
            finding["advisory"] = f"SAMPLE-2025-{index:04d}"
            if index == 29:
                finding.pop("not_rechecked", None)
            else:
                finding["not_rechecked"] = True
            merged["findings"].append(finding)
        self.write_report(merged)

        result = self.read()
        self.assertIn("report", result)
        self.assertEqual(len(result["report"]["findings"]), 30)

    def test_atomic_replacement_is_visible_to_the_next_read(self):
        first = self.read()
        self.assertEqual(first["report"], REPORT)

        replacement = copy.deepcopy(REPORT)
        replacement["checked_at"] = "2026-10-28T07:30:00+00:00"
        replacement["summary"] = "Synthetic replacement report."
        temporary = self.report_dir / ".latest.json.tmp"
        temporary.write_text(json.dumps(replacement), encoding="utf-8")
        os.replace(temporary, self.report_dir / security_report.REPORT_FILENAME)

        self.assertEqual(self.read()["report"], replacement)
        self.assertEqual(first["report"], REPORT)

    def test_only_the_fixed_latest_filename_is_consulted(self):
        (self.report_dir / "other.json").write_text("not a report", encoding="utf-8")
        (self.report_dir / "latest.md").write_text("markdown is not authoritative", encoding="utf-8")
        self.assertEqual(self.read()["report"], REPORT)

    def test_checked_at_requires_an_iso_timestamp_with_timezone(self):
        for value in (
            "2026-10-28T06:15:00",
            "2026-10-28",
            "not-a-timestamp",
            "",
            1720000000,
        ):
            with self.subTest(value=value):
                self.assert_invalid(mutated(lambda report: report.update(checked_at=value)))

    def test_malformed_known_report_sections_are_rejected(self):
        cases = {
            "root is not an object": [],
            "missing checked_at": mutated(lambda report: report.pop("checked_at")),
            "empty summary": mutated(lambda report: report.update(summary="")),
            "summary is not a string": mutated(lambda report: report.update(summary=1)),
            "checked_components is not a list": mutated(
                lambda report: report.update(checked_components="sample-web")),
            "limitations holds a non-string": mutated(
                lambda report: report.update(limitations=[1])),
            "findings is not a list": mutated(lambda report: report.update(findings={})),
            "finding is missing action": mutated(
                lambda report: report["findings"][0].pop("action")),
            "finding text field is empty": mutated(
                lambda report: report["findings"][0].update(fixed_version="")),
            "finding priority is unknown": mutated(
                lambda report: report["findings"][0].update(priority="urgent")),
            "finding status is unknown": mutated(
                lambda report: report["findings"][0].update(status="fixed")),
            "finding exploited is unknown": mutated(
                lambda report: report["findings"][0].update(exploited="maybe")),
            "finding confidence is unknown": mutated(
                lambda report: report["findings"][0].update(confidence="certain")),
            "finding sources is a string": mutated(
                lambda report: report["findings"][0].update(sources="https://sample.example")),
            "finding sources holds a non-string": mutated(
                lambda report: report["findings"][0].update(sources=[1])),
            "not_rechecked is not a bool": mutated(
                lambda report: report["findings"][1].update(not_rechecked="yes")),
            "searches is a string": mutated(lambda report: report.update(searches="4")),
            "searches is a bool": mutated(lambda report: report.update(searches=True)),
            "searches is negative": mutated(lambda report: report.update(searches=-1)),
            "usage is not an object": mutated(lambda report: report.update(usage=[])),
            "assessments is not a list": mutated(lambda report: report.update(assessments={})),
            "assessment finding is malformed": mutated(
                lambda report: report["assessments"][0].update(status="dismissed")),
            "rejected assessment has no reason": mutated(
                lambda report: report["rejected_assessments"][0].pop("reason")),
            "inventory components is not a list": mutated(
                lambda report: report["inventory"].update(components="sample-web")),
            "inventory component has no version": mutated(
                lambda report: report["inventory"]["components"][0].pop("version")),
            "inventory package has no ecosystem": mutated(
                lambda report: report["inventory"]["packages"][0].pop("ecosystem")),
            "inventory collected_at has no timezone": mutated(
                lambda report: report["inventory"].update(collected_at="2026-10-28T06:12:30")),
            "advisory checks errors is not a list": mutated(
                lambda report: report["advisory_checks"].update(errors="failed")),
            "source_checks_complete is not a bool": mutated(
                lambda report: report.update(source_checks_complete="false")),
            "search_sources holds a non-string": mutated(
                lambda report: report.update(search_sources=[1])),
            "dependabot is missing new": mutated(
                lambda report: report["dependabot"].pop("new")),
            "dependabot open is not a list": mutated(
                lambda report: report["dependabot"].update(open={})),
            "dependabot entry has no branch": mutated(
                lambda report: report["dependabot"]["open"][0].pop("branch")),
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
                    b'"total_tokens": 20643', b'"total_tokens": ' + literal)
                self.write_report(raw=raw)
                self.assertEqual(self.read()["code"], "invalid")

    def test_escaped_unpaired_surrogates_are_invalid_anywhere(self):
        cases = {
            "summary value": lambda report: report.update(summary="Synthetic \ud800"),
            "unknown nested value": lambda report: report.update(
                future_extension={"nested": ["\udfff"]}),
            "unknown nested key": lambda report: report.update(
                future_extension={"\ud800": "synthetic"}),
            "finding title": lambda report: report["findings"][0].update(
                title="Synthetic \udfff"),
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
            summary="Synthetic Unicode: café, 漢字, 😀, \u2028",
            future_extension={"clé": "valeur", "emoji😀": ["ok"]},
        ))
        self.write_report(report)
        self.assertEqual(self.read(), {"report": report})

    def test_excessive_nesting_is_invalid_without_a_serialization_error(self):
        nested = "synthetic"
        for _ in range(security_report.MAX_TRANSPORT_DEPTH + 5):
            nested = [nested]
        report = mutated(lambda item: item.update(future_extension=nested))
        # json.dumps() itself succeeds at this depth; only the API renderer
        # and response contract need a deliberately safe bound.
        self.write_report(report)
        self.assertEqual(self.read()["code"], "invalid")

    def test_missing_report_is_safe_and_does_not_register_as_configured_absence(self):
        absent = self.root / "absent-secret-directory"
        with patch.object(core, "SECURITY_REPORT_DIR", str(absent)):
            result = core.get_security_report()
            self.assertEqual(core.get_features()["security"], True)
        self.assertEqual(result["code"], "missing")
        self.assertNotIn(str(absent), result["error"])
        self.assertNotIn("latest.json", result["error"])

    def test_configured_path_can_be_pathlike_or_padded(self):
        for value in (self.report_dir, f"  {self.report_dir}  "):
            with self.subTest(value=value), patch.object(core, "SECURITY_REPORT_DIR", value):
                self.assertEqual(core.get_features()["security"], True)
                self.assertEqual(core.get_security_report()["report"], REPORT)

    def test_directory_at_latest_path_is_unreadable_not_followed(self):
        target = self.report_dir / security_report.REPORT_FILENAME
        target.unlink()
        target.mkdir()
        result = self.read()
        self.assertEqual(result["code"], "unreadable")
        self.assertNotIn(str(self.report_dir), result["error"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root bypasses mode-bit denial")
    def test_permission_denied_is_unreadable(self):
        target = self.report_dir / security_report.REPORT_FILENAME
        target.chmod(0)
        try:
            result = self.read()
        finally:
            target.chmod(0o600)
        self.assertEqual(result["code"], "unreadable")
        self.assertNotIn(str(self.report_dir), result["error"])

    def test_final_symlink_is_refused_and_target_is_not_served(self):
        target = self.report_dir / security_report.REPORT_FILENAME
        target.unlink()
        outside = self.root / "outside-report.json"
        outside.write_text(json.dumps(REPORT), encoding="utf-8")
        target.symlink_to(outside)

        result = self.read()
        self.assertEqual(result["code"], "unreadable")
        self.assertNotIn("report", result)
        self.assertEqual(outside.read_text(encoding="utf-8"), json.dumps(REPORT))

    def test_fifo_is_refused_without_blocking(self):
        target = self.report_dir / security_report.REPORT_FILENAME
        target.unlink()
        os.mkfifo(target)

        started = time.monotonic()
        result = self.read()
        elapsed = time.monotonic() - started

        self.assertEqual(result["code"], "unreadable")
        self.assertLess(elapsed, 2.0)

    def test_oversized_report_is_rejected_by_a_bounded_read(self):
        target = self.report_dir / security_report.REPORT_FILENAME
        with target.open("wb") as handle:
            handle.truncate(security_report.MAX_REPORT_BYTES + 1)
        self.assertEqual(self.read()["code"], "too_large")

        # Even if fstat under-reports the size, the read itself is capped and
        # the resulting byte count is still rejected.
        fake = os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, 0, 0, 10, 0, 0, 0))
        with patch.object(security_report.os, "fstat", return_value=fake):
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
                    target = self.report_dir / security_report.REPORT_FILENAME
                    target.unlink()
                    target.mkdir()
                elif state == "oversized":
                    target = self.report_dir / security_report.REPORT_FILENAME
                    if target.exists():
                        target.unlink()
                    with target.open("wb") as handle:
                        handle.truncate(security_report.MAX_REPORT_BYTES + 1)
                elif state is not None:
                    self.write_report(raw=state)

                with patch.object(core, "SECURITY_REPORT_DIR", directory):
                    result = core.get_security_report()
                self.assertEqual(set(result), {"error", "code"})
                self.assertEqual(result["code"], code)
                self.assertIsInstance(result["error"], str)
                self.assertTrue(result["error"])
                self.assertNotIn(str(self.report_dir), result["error"])
                self.assertNotIn("latest.json", result["error"])

                # Restore a valid report for the next iteration.
                target = self.report_dir / security_report.REPORT_FILENAME
                if target.is_dir():
                    target.rmdir()
                elif target.exists():
                    target.unlink()
                self.write_report(REPORT)


class SecurityReportApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.report_dir = self.root / "reports"
        self.report_dir.mkdir()
        self.publisher = patch.object(core, "SECURITY_REPORT_DIR", str(self.report_dir))
        self.publisher.start()
        self.addCleanup(self.publisher.stop)
        key_patch = patch.object(core, "_read_opencode_go_key", return_value=None)
        key_patch.start()
        self.addCleanup(key_patch.stop)
        config_patch = patch.object(core, "_litellm_config_path", return_value=None)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        (self.report_dir / security_report.REPORT_FILENAME).write_text(
            json.dumps(REPORT), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_api_returns_complete_report_with_no_store(self):
        response = self.client.get("/api/security")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), {"report": REPORT})

    def test_api_failure_uses_same_json_contract_with_no_store(self):
        with patch.object(core, "SECURITY_REPORT_DIR", ""):
            response = self.client.get("/api/security")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(set(response.json()), {"error", "code"})
        self.assertEqual(response.json()["code"], "not_configured")

    def test_api_rejects_escaped_surrogates_instead_of_raising(self):
        report = mutated(lambda item: (
            item.update(summary="Synthetic \ud800"),
            item.update(future_extension={"\udfff": "synthetic"}),
        ))
        target = self.report_dir / security_report.REPORT_FILENAME
        target.write_bytes(json.dumps(report).encode("ascii"))

        response = self.client.get("/api/security")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["code"], "invalid")

    def test_api_preserves_valid_unicode_and_emoji(self):
        report = mutated(lambda item: item.update(
            summary="Synthetic Unicode: café, 漢字, 😀, \u2028",
            future_extension={"clé": "valeur", "emoji😀": ["ok"]},
        ))
        target = self.report_dir / security_report.REPORT_FILENAME
        target.write_text(json.dumps(report), encoding="utf-8")

        response = self.client.get("/api/security")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), {"report": report})

    def test_api_rejects_excessive_nesting_instead_of_raising(self):
        nested = "synthetic"
        for _ in range(security_report.MAX_TRANSPORT_DEPTH + 5):
            nested = [nested]
        report = mutated(lambda item: item.update(future_extension=nested))
        target = self.report_dir / security_report.REPORT_FILENAME
        target.write_text(json.dumps(report), encoding="utf-8")

        response = self.client.get("/api/security")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["code"], "invalid")

    def test_feature_flag_is_true_without_a_report_and_false_without_configuration(self):
        empty = self.root / "empty"
        empty.mkdir()
        with patch.object(core, "SECURITY_REPORT_DIR", str(empty)):
            self.assertTrue(core.get_features()["security"])
            self.assertEqual(self.client.get("/api/security").json()["code"], "missing")
        for disabled in (None, "", "   "):
            with self.subTest(disabled=disabled), \
                    patch.object(core, "SECURITY_REPORT_DIR", disabled):
                self.assertFalse(core.get_features()["security"])
                self.assertEqual(core.get_security_report()["code"], "not_configured")

    def test_route_is_get_only_and_no_download_route_exists(self):
        matches = [route for route in main.app.routes
                   if getattr(route, "path", None) == "/api/security"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].methods, {"GET"})
        self.assertFalse([getattr(route, "path", "")
                          for route in main.app.routes
                          if "download" in getattr(route, "path", "").lower()])

        headers = {"Origin": "https://testserver", "X-Homelab-Request": "1"}
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)("/api/security", headers=headers)
                self.assertEqual(response.status_code, 405)

    def test_read_and_api_perform_no_network_calls(self):
        with patch("socket.create_connection", side_effect=AssertionError("network")), \
                patch.object(core.requests.Session, "request",
                             side_effect=AssertionError("network")), \
                patch.object(core.requests, "get", side_effect=AssertionError("network")):
            self.assertEqual(core.get_security_report()["report"], REPORT)
            response = self.client.get("/api/security")
        self.assertEqual(response.json(), {"report": REPORT})


if __name__ == "__main__":
    unittest.main()
