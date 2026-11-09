"""Tests for the LAN media server that feeds audio to the speaker."""
from __future__ import annotations

import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from googlehome.audioserver import MediaServer, content_type_for


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MediaServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "clip.mp3"
        self.payload = bytes(range(256)) * 4  # 1024 bytes
        self.source.write_bytes(self.payload)
        self.media = MediaServer(self.root / "media", port=_free_port())
        self.port = self.media.start()
        self.addCleanup(self.media.stop)
        self.addCleanup(self.tmp.cleanup)

    def _get(self, path: str, headers: dict | None = None) -> tuple[int, bytes, dict]:
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                         headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def test_publish_and_fetch(self):
        name = self.media.publish(str(self.source))
        self.assertEqual(name, "clip.mp3")
        status, body, headers = self._get(f"/{name}")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.payload)
        self.assertEqual(headers.get("Content-Type"), "audio/mpeg")
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")
        self.assertEqual(int(headers.get("Content-Length")), len(self.payload))

    def test_url_uses_advertised_host(self):
        self.media.advertise_host = "192.168.1.50"
        url = self.media.url_for("clip.mp3")
        self.assertEqual(url, f"http://192.168.1.50:{self.port}/clip.mp3")

    def test_range_request(self):
        name = self.media.publish(str(self.source))
        status, body, headers = self._get(f"/{name}", {"Range": "bytes=10-19"})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.payload[10:20])
        self.assertEqual(headers.get("Content-Range"), f"bytes 10-19/{len(self.payload)}")

    def test_open_ended_range(self):
        name = self.media.publish(str(self.source))
        status, body, _ = self._get(f"/{name}", {"Range": "bytes=1000-"})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.payload[1000:])

    def test_head_request(self):
        name = self.media.publish(str(self.source))
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/{name}", method="HEAD")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(int(response.headers["Content-Length"]), len(self.payload))
            self.assertEqual(response.read(), b"")

    def test_missing_file_404(self):
        status, _, _ = self._get("/nope.mp3")
        self.assertEqual(status, 404)

    def test_path_traversal_blocked(self):
        for path in ("/../secret.txt", "/..%2Fsecret.txt", "/%2e%2e%2fsecret.txt"):
            status, _, _ = self._get(path)
            self.assertIn(status, (400, 404), path)

    def test_stats_record_fetches(self):
        name = self.media.publish(str(self.source))
        self._get(f"/{name}")
        stats = self.media.stats
        self.assertTrue(stats["serving"])
        self.assertGreaterEqual(stats["requests"], 1)
        self.assertEqual(stats["last_request_path"], "clip.mp3")

    def test_prune_removes_old_files(self):
        name = self.media.publish(str(self.source))
        target = self.media.media_root / name
        self.assertTrue(target.exists())
        self.assertEqual(self.media.prune(max_age_seconds=-1), 1)
        self.assertFalse(target.exists())


class BusyPortTests(unittest.TestCase):
    def test_second_server_on_a_busy_port_raises(self):
        """A busy port must fail loudly; silently sharing it splits device fetches."""
        with tempfile.TemporaryDirectory() as tmp:
            first = MediaServer(Path(tmp), port=0)
            port = first.start()
            self.addCleanup(first.stop)
            second = MediaServer(Path(tmp), port=port)
            with self.assertRaises(OSError):
                second.start()
            second.stop()


class ContentTypeTests(unittest.TestCase):
    def test_known_audio_types(self):
        self.assertEqual(content_type_for("a.mp3"), "audio/mpeg")
        self.assertEqual(content_type_for("a.wav"), "audio/wav")
        self.assertEqual(content_type_for("a.ogg"), "audio/ogg")
        self.assertEqual(content_type_for("a.m4a"), "audio/mp4")
        self.assertEqual(content_type_for("a.opus"), "audio/ogg")


if __name__ == "__main__":
    unittest.main()
