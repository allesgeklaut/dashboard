import hashlib
import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import homelab_core as core  # noqa: E402


class PortainerRequestTests(unittest.TestCase):
    def test_unpinned_requests_require_normal_tls_and_never_redirect(self):
        with patch.object(core, "PORTAINER_URL", "https://portainer.test:9443/"), \
                patch.object(core, "PORTAINER_CA_FILE", ""), \
                patch.object(core.requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value
            core._portainer_request("GET", "/api/endpoints", timeout=3)
        self.assertFalse(session.trust_env)
        session.mount.assert_not_called()
        session.request.assert_called_once_with(
            "GET", "https://portainer.test:9443/api/endpoints",
            headers=core._HDR, timeout=3, verify=True, allow_redirects=False,
        )

    def test_http_is_rejected_before_sending_credentials(self):
        with patch.object(core, "PORTAINER_URL", "http://portainer.test:9000"), \
                patch.object(core.requests, "Session") as factory:
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                core._portainer_request("GET", "/api/endpoints", timeout=3)
        factory.assert_not_called()

    def test_missing_pin_fails_closed_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing.pem")
            with patch.object(core, "PORTAINER_URL", "https://portainer.test"), \
                    patch.object(core, "PORTAINER_CA_FILE", missing), \
                    patch.object(core.requests, "Session") as factory:
                with self.assertRaises(FileNotFoundError):
                    core._portainer_request("GET", "/api/endpoints", timeout=3)
                factory.return_value.__enter__.return_value.request.assert_not_called()

    def test_all_portainer_operations_use_the_verified_helper(self):
        endpoints = Mock(ok=True, json=lambda: [{"Id": 1, "Name": "test"}])
        containers = Mock(ok=True, json=lambda: [])
        action = Mock(status_code=204)
        with patch.object(core, "_eids", None), \
                patch.object(core, "_env_names", {}), \
                patch.object(core, "PORTAINER_ENVS", None), \
                patch.object(core, "_portainer_request", side_effect=[endpoints, containers, action]) as send:
            self.assertEqual(core.get_eids(), [1])
            self.assertEqual(core.portainer_containers(), ([], True))
            self.assertEqual(core.container_action("fixture", "stop", 1), (True, "stop OK"))
        self.assertEqual(
            [(call.args, call.kwargs) for call in send.call_args_list],
            [
                (("GET", "/api/endpoints"), {"timeout": 3}),
                (("GET", "/api/endpoints/1/docker/containers/json?all=true"), {"timeout": 4}),
                (("POST", "/api/endpoints/1/docker/containers/fixture/stop"), {"timeout": 15}),
            ],
        )


class _TLSHandler(BaseHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            # Expected when the client rejects the peer fingerprint after TLS.
            pass

    def do_GET(self):
        self.server.paths.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/redirect-target")
            self.end_headers()
        else:
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *_args):
        pass


@unittest.skipUnless(shutil.which("openssl"), "TLS fixtures require openssl")
class PortainerPinnedTLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.certs = []
        for name in ("trusted", "other"):
            cert = Path(cls.tmp.name) / f"{name}.pem"
            key = Path(cls.tmp.name) / f"{name}.key"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(cert), "-days", "1",
                 "-subj", "/CN=portainer.test", "-addext", "subjectAltName=DNS:portainer.test",
                 "-addext", "basicConstraints=critical,CA:FALSE"],
                check=True, capture_output=True,
            )
            cls.certs.append((cert, key))

    def start_server(self, index=0):
        cert, key = self.certs[index]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _TLSHandler)
        server.paths = []
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, f"https://127.0.0.1:{server.server_port}"

    def test_exact_pin_accepts_trusted_server_despite_hostname_mismatch(self):
        _server, url = self.start_server()
        with patch.object(core, "PORTAINER_URL", url), \
                patch.object(core, "PORTAINER_CA_FILE", str(self.certs[0][0])), \
                patch.object(core, "_HDR", {"X-API-Key": "dummy-fixture"}):
            response = core._portainer_request("GET", "/status", timeout=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def test_wrong_certificate_is_rejected(self):
        server, url = self.start_server(index=1)
        with patch.object(core, "PORTAINER_URL", url), \
                patch.object(core, "PORTAINER_CA_FILE", str(self.certs[0][0])):
            with self.assertRaises(requests.exceptions.SSLError):
                core._portainer_request("GET", "/status", timeout=3)
        self.assertEqual(server.paths, [])

    def test_fingerprint_is_enforced_even_with_another_trusted_certificate(self):
        server, url = self.start_server(index=1)
        fingerprint = hashlib.sha256(
            ssl.PEM_cert_to_DER_cert(self.certs[0][0].read_text())
        ).hexdigest()
        with requests.Session() as session:
            session.trust_env = False
            session.mount("https://", core._PortainerPinnedAdapter(fingerprint))
            with self.assertRaises(requests.exceptions.SSLError):
                session.get(url, verify=str(self.certs[1][0]), timeout=3)
        self.assertEqual(server.paths, [])

    def test_redirect_is_not_followed(self):
        server, url = self.start_server()
        with patch.object(core, "PORTAINER_URL", url), \
                patch.object(core, "PORTAINER_CA_FILE", str(self.certs[0][0])):
            response = core._portainer_request("GET", "/redirect", timeout=3)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(server.paths, ["/redirect"])

    def test_certificate_bundle_is_rejected_before_network(self):
        bundle = Path(self.tmp.name) / "bundle.pem"
        bundle.write_text("".join(cert.read_text() for cert, _key in self.certs))
        with patch.object(core, "PORTAINER_URL", "https://portainer.test"), \
                patch.object(core, "PORTAINER_CA_FILE", str(bundle)), \
                patch.object(core.requests, "Session") as factory:
            with self.assertRaisesRegex(ValueError, "exactly one"):
                core._portainer_request("GET", "/status", timeout=3)
            factory.return_value.__enter__.return_value.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
