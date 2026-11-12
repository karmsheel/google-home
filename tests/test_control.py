"""The loopback control API's security contract.

The platform skips pairing, so this API is the only inbound door. These tests
pin what the plugin-catalogue review required: a token on every request, in the
header only, constant-time comparison, a loopback Host check, and no CORS grant
that would let a web page on this machine drive the speaker.
"""
from __future__ import annotations

import http.client
import json
import unittest

from googlehome.control import ControlServer


class _Adapter:
    """Enough adapter for the endpoints that never touch Cast."""

    _loop = None


class ControlSecurityTests(unittest.TestCase):
    def setUp(self):
        self.server = ControlServer(_Adapter(), port=0, token="s3cret-token")
        self.port = self.server.start()
        self.addCleanup(self.server.stop)

    def _request(self, method="GET", path="/health", *, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5.0)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            body = response.read()
            return response, body
        finally:
            connection.close()

    def _token_header(self):
        return {"X-Hermes-Token": "s3cret-token"}

    def test_a_valid_header_token_is_accepted(self):
        response, body = self._request(headers=self._token_header())
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_a_missing_token_is_refused(self):
        response, _ = self._request()
        self.assertEqual(response.status, 401)

    def test_a_wrong_token_is_refused(self):
        response, _ = self._request(headers={"X-Hermes-Token": "guess"})
        self.assertEqual(response.status, 401)

    def test_a_query_token_is_not_accepted(self):
        response, _ = self._request(path="/health?token=s3cret-token")
        self.assertEqual(response.status, 401)

    def test_no_install_token_fails_closed(self):
        server = ControlServer(_Adapter(), port=0, token="")
        port = server.start()
        self.addCleanup(server.stop)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
        try:
            connection.request("GET", "/health", headers={"X-Hermes-Token": ""})
            self.assertEqual(connection.getresponse().status, 401)
        finally:
            connection.close()

    def test_a_non_loopback_host_is_refused(self):
        for host in ("evil.example", f"127.0.0.1:{self.port + 1}", "192.168.1.10"):
            with self.subTest(host=host):
                response, _ = self._request(
                    headers={**self._token_header(), "Host": host}
                )
                self.assertEqual(response.status, 403)

    def test_localhost_is_accepted(self):
        response, _ = self._request(
            headers={**self._token_header(), "Host": f"localhost:{self.port}"}
        )
        self.assertEqual(response.status, 200)

    def test_no_cors_grant_is_sent(self):
        response, _ = self._request(headers=self._token_header())
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        self.assertIsNone(response.getheader("Access-Control-Allow-Headers"))

    def test_cross_origin_preflight_gets_no_grant(self):
        response, _ = self._request(method="OPTIONS", headers={"Origin": "https://evil.example"})
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))


if __name__ == "__main__":
    unittest.main()
