import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runtime_healthcheck", ROOT / "docker" / "healthcheck.py")
healthcheck = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(healthcheck)


class HealthcheckTests(unittest.TestCase):
    def test_probe_uses_local_socket_and_configured_public_host(self):
        connection = Mock()
        connection.getresponse.return_value.status = 200
        with patch.dict(os.environ, {"PUBLIC_URL": "https://support.example.com:8443"}):
            with patch.object(healthcheck, "HTTPConnection", return_value=connection) as constructor:
                self.assertEqual(healthcheck.main(), 0)
        constructor.assert_called_once_with("127.0.0.1", 8000, timeout=3)
        connection.request.assert_called_once_with(
            "GET", "/health/ready",
            headers={"Host": "support.example.com:8443", "Connection": "close"},
        )
        connection.close.assert_called_once()

    def test_dependency_failure_and_network_error_are_unhealthy(self):
        for status in (400, 500, 503):
            with self.subTest(status=status):
                connection = Mock()
                connection.getresponse.return_value.status = status
                with patch.object(healthcheck, "HTTPConnection", return_value=connection):
                    self.assertEqual(healthcheck.main(), 1)
                connection.close.assert_called_once()
        with patch.object(healthcheck, "HTTPConnection", side_effect=OSError("unreachable")):
            self.assertEqual(healthcheck.main(), 1)

    def test_probe_rejects_invalid_origins_without_sending_credentials(self):
        for value in ("file:///etc/passwd", "https://user:password@support.example.com", "invalid"):
            with self.subTest(value=value), patch.dict(os.environ, {"PUBLIC_URL": value}):
                with patch.object(healthcheck, "HTTPConnection") as connection:
                    self.assertEqual(healthcheck.main(), 1)
                    connection.assert_not_called()


@unittest.skipUnless(shutil.which("sh"), "Container entrypoint coverage requires a POSIX shell")
class BootstrapTests(unittest.TestCase):
    def reject(self, script, values, message):
        result = subprocess.run(
            ["sh", str(ROOT / script)], env={"PATH": os.defpath, **values},
            text=True, capture_output=True, timeout=5, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)

    def test_redis_bootstrap_requires_valid_exclusive_secret(self):
        for values, message in (
            ({}, "at least 32"),
            ({"REDIS_PASSWORD": "short"}, "at least 32"),
            ({"REDIS_PASSWORD": "z" * 64}, "hexadecimal"),
            ({"REDIS_PASSWORD": "a" * 64, "REDIS_PASSWORD_FILE": "/nonexistent-test-secret"}, "not both"),
        ):
            with self.subTest(values=values):
                self.reject("docker/redis-entrypoint.sh", values, message)

    def test_database_bootstrap_rejects_missing_or_ambiguous_configuration(self):
        for values, message in (
            ({}, "APP_DB_PASSWORD"),
            ({"APP_DB_PASSWORD": "test", "APP_DB_PASSWORD_FILE": "/nonexistent-test-secret"}, "not both"),
            ({"APP_DB_PASSWORD": "test"}, "POSTGRES_USER"),
            ({"APP_DB_PASSWORD": "test", "POSTGRES_USER": "test-owner"}, "POSTGRES_DB"),
        ):
            with self.subTest(values=values):
                self.reject("docker/postgres/init-runtime-role.sh", values, message)

    def test_gateway_rejects_non_http_public_origin(self):
        self.reject("docker/gateway-entrypoint.sh", {"PUBLIC_URL": "file:///etc/passwd"}, "must use http:// or https://")
