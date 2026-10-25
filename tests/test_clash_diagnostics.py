"""Connection diagnostics with injected responses; no sockets or live controller."""

import errno
import io
import json
import unittest
from urllib.error import HTTPError, URLError

from flashmonitor.clash import ClashClient


class StubOpener:
    def __init__(self, *outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request.full_url, timeout))
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        body = outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode()
        return io.BytesIO(body)


class ClashDiagnosticsTest(unittest.TestCase):
    def client(self, *outcomes, base="http://127.0.0.1:9097", fallbacks=()):
        opener = StubOpener(*outcomes)
        return ClashClient(lambda: base, lambda: "private-secret", opener=opener, fallbacks=fallbacks), opener

    def test_authentication_statuses(self):
        for status in (401, 403):
            with self.subTest(status=status):
                client, _ = self.client(HTTPError("http://unused/private-token", status, "private-secret", {}, None))
                result = client.test_link()
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], "auth_failed")
                self.assertEqual(result["http_status"], status)
                self.assertNotIn("private-", json.dumps(result))

    def test_network_failure_categories(self):
        for failure, code in (
            (ConnectionRefusedError(errno.ECONNREFUSED, "private-secret"), "connection_refused"),
            (URLError(ConnectionRefusedError(10061, "private-secret")), "connection_refused"),
            (TimeoutError("private-secret"), "timeout"),
            (URLError(OSError(10060, "private-secret")), "timeout"),
            (URLError(OSError("private-secret")), "network_error"),
            (PermissionError("private-secret"), "network_error"),
            (HTTPError("http://unused", 503, "private-secret", {}, None), "http_error"),
        ):
            with self.subTest(code=code, failure=type(failure).__name__):
                client, _ = self.client(failure)
                result = client.test_link()
                self.assertEqual(result["error_code"], code)
                self.assertNotIn("private-secret", json.dumps(result))

    def test_invalid_responses_are_not_successful_empty_lists(self):
        for response in (b"<html>Not the controller</html>", {}, [], {"connections": {}}, {"connections": "bad"}):
            with self.subTest(response=response):
                client, _ = self.client(response)
                result = client.test_link()
                self.assertEqual(result["error_code"], "invalid_response")
                self.assertEqual(client.base_ok, "")

    def test_missing_connection_endpoint_reports_incompatible_service(self):
        for status in (404, 405):
            client, _ = self.client(HTTPError("http://unused", status, "private-secret", {}, None))
            result = client.test_link()
            self.assertEqual(result["error_code"], "invalid_response")
            self.assertEqual(result["http_status"], status)

    def test_empty_and_null_connections_remain_successful(self):
        for connections in ([], None):
            client, _ = self.client({"connections": connections})
            result = client.test_link()
            self.assertEqual((result["ok"], result["connections"], result["base"]),
                             (True, 0, "http://127.0.0.1:9097"))
            self.assertIsInstance(result["ms"], int)

    def test_configured_address_precedes_previously_working_fallback(self):
        configured = "http://127.0.0.1:9097"
        fallback = "http://127.0.0.1:9090"
        client, opener = self.client(
            HTTPError(configured, 401, "Unauthorized", {}, None),
            URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused")),
            fallbacks=(fallback,),
        )
        client.remember_base(fallback)
        result = client.test_link()
        self.assertEqual([call[0] for call in opener.calls], [configured + "/connections", fallback + "/connections"])
        self.assertEqual(result["error_code"], "auth_failed")
        self.assertEqual([a["error_code"] for a in result["attempts"]], ["auth_failed", "connection_refused"])

    def test_fallback_success_retains_failed_attempt(self):
        fallback = "http://127.0.0.1:9090"
        client, _ = self.client(TimeoutError(), {"connections": [{"id": "a"}]}, fallbacks=(fallback,))
        result = client.test_link()
        self.assertEqual((result["ok"], result["connections"], result["base"]), (True, 1, fallback))
        self.assertEqual(result["attempts"][0]["error_code"], "timeout")
        self.assertTrue(result["attempts"][1]["ok"])
        self.assertEqual(client.base_ok, fallback)

    def test_diagnostics_strip_credentials_path_query_and_fragment(self):
        base = "http://private-user:private-password@[::1]:9097/private-path?token=private-token#private-fragment"
        for outcome in (TimeoutError("private-secret"), {"connections": []}):
            client, _ = self.client(outcome, base=base)
            result = client.test_link()
            self.assertEqual(result["base"], "http://[::1]:9097")
            self.assertNotIn("private-", json.dumps(result))

    def test_invalid_address_reports_without_request(self):
        for base in ("ftp://example.com", "http://", "http://localhost:bad", "http://localhost:0"):
            with self.subTest(base=base):
                client, opener = self.client(base=base)
                result = client.test_link()
                self.assertEqual(result["error_code"], "invalid_address")
                self.assertEqual(opener.calls, [])

    def test_collection_payload_contract_is_unchanged(self):
        client, _ = self.client(
            HTTPError("http://unused", 401, "Unauthorized", {}, None),
            {"connections": None, "uploadTotal": "12", "downloadTotal": 34},
        )
        self.assertIsNone(client.payload())
        self.assertEqual(client.payload(), {"connections": [], "uploadTotal": 12, "downloadTotal": 34})


if __name__ == "__main__":
    unittest.main()
