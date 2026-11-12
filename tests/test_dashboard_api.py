"""The Desktop half's backend: manifest wiring and the loopback proxy.

The proxy tests need FastAPI (the plugin host imports this router), so a
stdlib-only checkout skips them; run them inside the Hermes venv to exercise
the real proxy against a stand-in bridge.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "googlehome" / "dashboard"
API_FILE = DASHBOARD / "plugin_api.py"

try:
    import fastapi  # noqa: F401

    FASTAPI_AVAILABLE = True
except Exception:  # noqa: BLE001
    FASTAPI_AVAILABLE = False


def _load_api():
    spec = importlib.util.spec_from_file_location("googlehome_plugin_api", API_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ManifestTests(unittest.TestCase):
    def test_the_backend_is_declared_beside_the_desktop_half(self):
        manifest = json.loads((DASHBOARD / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "googlehome")
        self.assertEqual(manifest["api"], "plugin_api.py")
        self.assertTrue(API_FILE.is_file())
        self.assertTrue((DASHBOARD / "dist" / "index.js").is_file())
        self.assertTrue(manifest["tab"]["hidden"], "no web-dashboard page is offered")

    def test_the_desktop_half_never_reads_the_control_file_itself(self):
        source = (ROOT / "googlehome" / "desktop" / "plugin.js").read_text(encoding="utf-8")
        for forbidden in ("hermesDesktop", "readFileText", "control.json", "X-Hermes-Token"):
            self.assertNotIn(forbidden, source)


class _BridgeHandler(BaseHTTPRequestHandler):
    """A stand-in control API that records the token it was sent."""

    protocol_version = "HTTP/1.1"
    seen_tokens: list[str] = []
    fail_status = 0

    def log_message(self, *args):  # noqa: A003 - silence the test's stderr
        pass

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen_tokens.append(self.headers.get("X-Hermes-Token", ""))
        if type(self).fail_status:
            self._send(type(self).fail_status, {"ok": False, "error": "token rejected"})
            return
        self._send(200, {"ok": True, "path": self.path})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        type(self).seen_tokens.append(self.headers.get("X-Hermes-Token", ""))
        self._send(200, {"ok": True, "echo": json.loads(raw or b"{}")})


class _Bridge:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _BridgeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def port(self) -> int:
        return self.server.server_port


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI (the plugin host) is not importable")
class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.api = _load_api()
        self.api._CACHE.update({"at": 0.0, "rows": []})
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "googlehome").mkdir(parents=True)
        self._env = mock.patch.dict(
            os.environ, {"HERMES_HOME": str(self.home)}, clear=False
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        _BridgeHandler.seen_tokens = []
        _BridgeHandler.fail_status = 0

    def _write_control(self, port: int, token: str = "bridge-token") -> None:
        (self.home / "googlehome" / "control.json").write_text(
            json.dumps({"port": port, "token": token, "updated_at": 1.0}),
            encoding="utf-8",
        )

    def test_a_missing_control_file_is_a_readable_503(self):
        with self.assertRaises(Exception) as raised:
            self.api._call("status", "GET", "/status")
        self.assertEqual(getattr(raised.exception, "status_code", 0), 503)
        self.assertIn("not running", str(raised.exception.detail))

    def test_get_and_post_are_proxied_with_the_token(self):
        with _Bridge() as bridge:
            self._write_control(bridge.port)
            status = self.api._call("status", "GET", "/status")
            echoed = self.api._call("ask", "POST", "/ask", {"text": "hello"})
        self.assertTrue(status["ok"])
        self.assertEqual(echoed["echo"], {"text": "hello"})
        self.assertEqual(_BridgeHandler.seen_tokens, ["bridge-token", "bridge-token"])

    def test_a_bridge_error_is_relayed_not_masked(self):
        _BridgeHandler.fail_status = 401
        with _Bridge() as bridge:
            self._write_control(bridge.port)
            with self.assertRaises(Exception) as raised:
                self.api._call("status", "GET", "/status")
        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("token rejected", str(raised.exception.detail))

    def test_a_stale_candidate_falls_through_to_the_live_bridge(self):
        with _Bridge() as bridge:
            self._write_control(bridge.port)
            stale = {
                "port": bridge.port + 1,
                "token": "old",
                "updated_at": 99.0,
            }
            # Newest first: a dead port must not hide the working one.
            self.api._CACHE.update({
                "at": self.api.time.time(),
                "rows": [stale, json.loads(
                    (self.home / "googlehome" / "control.json").read_text(encoding="utf-8")
                )],
            })
            self.api._homes = lambda: [self.home]
            result = self.api._call("status", "GET", "/status")
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
