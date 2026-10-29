"""Verify startup configuration in isolated processes, without device polling."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StartupConfigTests(unittest.TestCase):
    def startup_flags(self, dotenv_contents, **overrides):
        with tempfile.TemporaryDirectory() as tmp:
            dotenv_path = Path(tmp) / ".env"
            dotenv_path.write_text(dotenv_contents, encoding="utf-8")
            # Do not inherit real integration settings, opt-ins, or the test
            # runner's PYTHON_DOTENV_DISABLED: this tests actual dotenv parsing.
            env = {"PATH": os.defpath, "HOME": tmp,
                   "PYTHONPATH": str(ROOT / "app"), **overrides}
            script = """
import json
import sys
from unittest.mock import patch
with patch('dotenv.find_dotenv', return_value=sys.argv[1]):
    import homelab_core
import benchmarks
print(json.dumps([benchmarks.LIVEBENCH_ENABLED, benchmarks.DEEPSWE_ENABLED]))
"""
            result = subprocess.run(
                [sys.executable, "-c", script, str(dotenv_path)],
                env=env, cwd=tmp, capture_output=True, text=True, timeout=10,
                check=True,
            )
            return json.loads(result.stdout)

    def test_dotenv_opt_ins_are_loaded_before_benchmark_initialization(self):
        self.assertEqual(self.startup_flags(
            "HOMELAB_LIVEBENCH_ENABLED=true\nHOMELAB_DEEPSWE_ENABLED=true\n"
        ), [True, True])

    def test_process_environment_takes_precedence_over_dotenv(self):
        self.assertEqual(self.startup_flags(
            "HOMELAB_LIVEBENCH_ENABLED=true\nHOMELAB_DEEPSWE_ENABLED=true\n",
            HOMELAB_LIVEBENCH_ENABLED="false", HOMELAB_DEEPSWE_ENABLED="false",
        ), [False, False])

    def test_optional_sources_default_off(self):
        self.assertEqual(self.startup_flags(""), [False, False])
