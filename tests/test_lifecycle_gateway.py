import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import homelab_core as core


class LifecycleClientTests(unittest.TestCase):
    def test_gateway_uses_only_restricted_key_and_verifies_tls(self):
        with patch.object(core, "LIFECYCLE_URL", "https://lifecycle-gateway:8443"), \
                patch.object(core, "LIFECYCLE_API_KEY", "restricted-dummy"), \
                patch.object(core, "LIFECYCLE_CA_FILE", "/fixture/ca.pem"), \
                patch.object(core.requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value
            core._portainer_request("GET", "/api/endpoints", 3)
        self.assertFalse(session.trust_env)
        session.request.assert_called_once_with(
            "GET", "https://lifecycle-gateway:8443/api/endpoints", headers={"X-API-Key": "restricted-dummy"},
            timeout=3, verify="/fixture/ca.pem", allow_redirects=False)

    def test_missing_gateway_credentials_never_fall_back(self):
        with patch.object(core, "LIFECYCLE_URL", "https://lifecycle-gateway:8443"), \
                patch.object(core, "LIFECYCLE_API_KEY", ""), \
                patch.object(core.requests, "Session") as factory:
            with self.assertRaises(ValueError):
                core._portainer_request("GET", "/api/endpoints", 3)
        factory.assert_not_called()

    def test_insecure_gateway_never_receives_credentials(self):
        with patch.object(core, "LIFECYCLE_URL", "http://lifecycle-gateway:8443"), \
                patch.object(core.requests, "Session") as factory:
            with self.assertRaises(ValueError):
                core._portainer_request("GET", "/api/endpoints", 3)
        factory.assert_not_called()

    def test_missing_gateway_does_not_use_legacy_admin_configuration(self):
        with patch.object(core, "LIFECYCLE_URL", ""), \
                patch.dict("os.environ", {"PORTAINER_URL": "https://portainer.invalid", "PORTAINER_API_KEY": "admin-fixture"}), \
                patch.object(core.requests, "Session") as factory:
            with self.assertRaises(ValueError):
                core._portainer_request("GET", "/api/endpoints", 3)
        factory.assert_not_called()

    def test_action_validation_before_network(self):
        with patch.object(core, "_portainer_request") as send:
            for cid, action in (("fixture", "exec"), ("../fixture", "stop"), ("name?x=1", "start")):
                self.assertEqual(core.container_action(cid, action, 3), (False, "invalid container action"))
        send.assert_not_called()

    def test_critical_marker_survives_projection(self):
        c = {"Id": "a" * 64, "Names": ["/portainer"], "State": "running", "Status": "Up", "Image": "portainer:lts", "LifecycleCritical": True}
        self.assertTrue(core._parse_container(c, "local", 3)["critical"])

    def test_gateway_failure_is_reported_not_empty_or_direct_fallback(self):
        with patch.object(core, "LIFECYCLE_URL", "https://lifecycle-gateway:8443"), \
                patch.object(core, "portainer_containers", return_value=([], False)):
            self.assertEqual(core.get_containers(), ([], "error"))


if __name__ == "__main__":
    unittest.main()
