import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import homelab_core as core


class MutationSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(core, "prime_counters"), patch.object(core, "start_energy_tracker"), \
                patch.object(core, "start_desktop_monitor"):
            import main
        cls.client = TestClient(main.app)
        cls.headers = {"Origin": "https://testserver", "X-Homelab-Request": "1"}

    def test_same_host_browser_mutation_is_allowed_behind_tls_terminating_proxy(self):
        with patch.object(core, "poweroff_desktop", return_value=(True, "mock")) as action:
            response = self.client.post("/api/desktop/power", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        action.assert_called_once()

    def test_foreign_missing_null_and_malformed_origins_are_rejected(self):
        for origin in (None, "https://evil.invalid", "https://sibling.testserver", "null", "https://["):
            with self.subTest(origin=origin), patch.object(core, "poweroff_desktop") as action:
                headers = {"X-Homelab-Request": "1"}
                if origin is not None:
                    headers["Origin"] = origin
                response = self.client.post("/api/desktop/power", headers=headers)
                self.assertEqual(response.status_code, 403)
                action.assert_not_called()

    def test_custom_header_and_fetch_metadata_are_enforced(self):
        for headers in (
            {"Origin": "https://testserver"},
            {**self.headers, "Sec-Fetch-Site": "cross-site"},
            {"Origin": "https://evil.invalid", "Content-Type": "application/x-www-form-urlencoded"},
        ):
            with patch.object(core, "poweroff_desktop") as action:
                self.assertEqual(self.client.post("/api/desktop/power", headers=headers).status_code, 403)
                action.assert_not_called()

    def test_all_control_and_benchmark_mutations_are_guarded(self):
        paths = ["/api/containers/1/fixture/stop", "/api/shelly/powercycle", "/api/shelly2/powercycle",
                 "/api/stream/up", "/api/stream/up-1440p", "/api/stream/down", "/api/wake",
                 "/api/desktop/power", "/api/benchmarks/fetch", "/api/opencode-go-benchmarks/fetch"]
        # No handler can be reached by these requests, even if route code changes.
        with patch.object(core, "container_action", side_effect=AssertionError("real action")), \
                patch.object(core, "shelly_power_cycle", side_effect=AssertionError("real action")), \
                patch.object(core, "request_stream", side_effect=AssertionError("real action")), \
                patch.object(core, "wake_desktop", side_effect=AssertionError("real action")), \
                patch.object(core, "poweroff_desktop", side_effect=AssertionError("real action")), \
                patch.object(core, "fetch_benchmarks", side_effect=AssertionError("upstream")), \
                patch.object(core, "fetch_opencode_go_benchmarks", side_effect=AssertionError("upstream")):
            for path in paths:
                with self.subTest(path=path):
                    self.assertEqual(self.client.post(path).status_code, 403)

    def test_cross_origin_preflight_is_not_authorized(self):
        response = self.client.options("/api/desktop/power", headers={
            "Origin": "https://evil.invalid", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-Homelab-Request",
        })
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_read_only_routes_do_not_require_mutation_headers(self):
        with patch.object(core, "get_features", return_value={}):
            self.assertEqual(self.client.get("/api/features").status_code, 200)


class HostBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("spool_reader", ROOT / "host/read-spool-action.py")
        cls.reader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.reader)

    def test_spool_reader_accepts_only_bounded_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = root / "valid.request"
            request.write_text("up\n")
            self.assertEqual(self.reader.read_action(request, {"up"}), "up")
            request.write_text("x" * 33)
            with self.assertRaises(ValueError):
                self.reader.read_action(request, {"up"})
            request.write_text("$(not-a-command)")
            with self.assertRaises(ValueError):
                self.reader.read_action(request, {"up"})
            link = root / "link.request"
            link.symlink_to(request)
            with self.assertRaises(OSError):
                self.reader.read_action(link, {"up"})
            fifo = root / "fifo.request"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                self.reader.read_action(fifo, {"up"})

    def test_unknown_spool_entries_cannot_append_through_shared_log_symlinks(self):
        for handler, prefix in (("stream-handler.sh", "STREAM"), ("desktop-power-handler.sh", "DESKTOP")):
            with self.subTest(handler=handler), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                spool = root / "spool"
                spool.mkdir()
                target = root / "host-file"
                target.write_text("unchanged")
                log = root / "shared.log"
                log.symlink_to(target)
                (spool / "bad.request").write_text("$(UNTRUSTED_MARKER)")
                (spool / "link.request").symlink_to(target)
                env = dict(os.environ, HOME=tmp, DESKTOP_ENV_FILE=str(root / "absent.env"))
                env[f"{prefix}_SPOOL_DIR"] = str(spool)
                env[f"{prefix}_LOG_FILE"] = str(log)
                result = subprocess.run(["bash", str(ROOT / "host" / handler)], env=env,
                                        capture_output=True, text=True, timeout=5, check=True)
                self.assertEqual(target.read_text(), "unchanged")
                self.assertNotIn("UNTRUSTED_MARKER", result.stdout)
                self.assertEqual(list(spool.iterdir()), [])

    def test_ssh_255_is_reported_as_unconfirmed_using_only_stubbed_ssh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spool = root / "spool"
            spool.mkdir()
            (spool / "test.request").write_text("poweroff")
            ssh = root / "ssh"
            ssh.write_text("#!/bin/sh\nexit 255\n")
            ssh.chmod(0o755)
            env = dict(os.environ, HOME=tmp, PATH=f"{root}:{os.environ['PATH']}",
                       DESKTOP_SPOOL_DIR=str(spool), DESKTOP_ENV_FILE=str(root / "absent.env"),
                       DESKTOP_SSH="fixture.invalid")
            result = subprocess.run(["bash", str(ROOT / "host/desktop-power-handler.sh")],
                                    env=env, capture_output=True, text=True, timeout=5, check=True)
            self.assertIn("shutdown unconfirmed", result.stdout)
            self.assertNotIn("poweroff in progress", result.stdout)

    def test_installers_never_execute_reboot_or_permission_test_target_switches(self):
        script = (ROOT / "host/setup-stream.sh").read_text()
        commands = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(commands, r"\bsudo\s+[^\n]*\breboot\b")
        self.assertNotRegex(commands, r"\bsudo\s+[^\n]*\bisolate\b")

    def test_empty_spool_flags_disable_features_but_unset_values_keep_defaults(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        for name, default in (("STREAM_SPOOL_DIR", "/stream-spool"), ("DESKTOP_SPOOL_DIR", "/desktop-spool")):
            expression = re.search(rf"{name}=(\$\{{[^}}]+\}})", compose).group(1)
            for value, expected in ((None, default), ("", ""), ("/custom", "/custom")):
                env = dict(os.environ)
                env.pop(name, None)
                if value is not None:
                    env[name] = value
                result = subprocess.run(["bash", "-c", f'printf "%s" "{expression}"'],
                                        env=env, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout, expected)

    def test_main_compose_includes_fail_closed_read_only_mistral_mount(self):
        service = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["homelab-ctrl"]
        self.assertIn("LITELLM_CONFIG_FILE=/run/litellm/config.yaml", service["environment"])
        mount = next(v for v in service["volumes"] if isinstance(v, dict) and v["target"] == "/run/litellm/config.yaml")
        self.assertEqual(mount["source"], "${LITELLM_CONFIG_HOST_PATH:-../litellm/config.yaml}")
        self.assertTrue(mount["read_only"])
        self.assertFalse(mount["bind"]["create_host_path"])
        self.assertFalse((ROOT / "docker-compose.mistral.yml").exists())
