"""End-to-end tests for CastClient against a fake TLS Cast device."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from googlehome import castproto as p  # noqa: E402
from googlehome.castdevice import CastClient, CastConnectionError, probe  # noqa: E402
from tests.mock_cast_device import MockCastDevice  # noqa: E402


class CastClientTests(unittest.TestCase):
    def setUp(self):
        self.device = MockCastDevice(finish_after=0.1).start()
        self.addCleanup(self.device.stop)
        self.client = CastClient("127.0.0.1", self.device.port, timeout=5.0)
        self.addCleanup(self.client.close)

    def test_second_sender_gets_a_channel_before_loading(self):
        """An already-running receiver still needs a channel to its transport.

        This is the real-device failure mode: the first sender launches the app
        and leaves it up, the next sender sees it running, skips the CONNECT,
        and the receiver closes its connection on the LOAD.
        """
        self.client.connect()
        self.client.play_url("http://192.168.1.5:8765/a.mp3", content_type="audio/mpeg")
        self.client.close()

        second = CastClient("127.0.0.1", self.device.port, timeout=5.0)
        self.addCleanup(second.close)
        second.connect()
        result = second.play_url("http://192.168.1.5:8765/b.mp3", content_type="audio/mpeg")
        self.assertEqual(result["player_state"], "PLAYING")
        self.assertIn("http://192.168.1.5:8765/b.mp3", self.device.loaded_urls)

    def test_clients_do_not_share_a_sender_id(self):
        """Two sessions to one device must present distinct sender ids.

        The receiver closes the duplicate connection when a second client reuses
        an id, which is exactly what happened to ``googlehome check`` while the
        bridge was running.
        """
        other = CastClient("127.0.0.1", self.device.port, timeout=5.0)
        self.addCleanup(other.close)
        self.assertNotEqual(self.client.source_id, other.source_id)
        self.assertTrue(self.client.source_id.startswith("sender-"))
        self.assertNotEqual(self.client.source_id, "sender-0")

    def test_connect_and_status(self):
        self.client.connect()
        info = CastClient.summarize_status(self.client.get_status())
        self.assertAlmostEqual(info["volume"], 0.5)
        self.assertIsNone(info["app_id"])
        self.assertFalse(info["muted"])

    def test_set_volume(self):
        self.client.connect()
        self.client.set_volume(0.8)
        self.assertAlmostEqual(self.device.volume, 0.8)
        self.assertAlmostEqual(self.client.get_volume(), 0.8)

    def test_volume_is_clamped(self):
        self.client.connect()
        self.client.set_volume(4.0)
        self.assertAlmostEqual(self.device.volume, 1.0)
        self.client.set_volume(-2)
        self.assertAlmostEqual(self.device.volume, 0.0)

    def test_play_url_launches_receiver_and_loads(self):
        self.client.connect()
        result = self.client.play_url("http://192.168.1.5:8765/a.mp3", content_type="audio/mpeg",
                                      title="hello")
        self.assertEqual(result["player_state"], "PLAYING")
        self.assertEqual(self.device.loaded_urls, ["http://192.168.1.5:8765/a.mp3"])
        self.assertEqual(self.device.loaded_content_types, ["audio/mpeg"])
        self.assertTrue(self.device.app_running)

    def test_second_play_reuses_running_receiver(self):
        self.client.connect()
        self.client.play_url("http://x/1.mp3")
        transport = self.client._transport_id
        self.client.play_url("http://x/2.mp3")
        self.assertEqual(self.client._transport_id, transport)
        self.assertEqual(self.device.loaded_urls, ["http://x/1.mp3", "http://x/2.mp3"])

    def test_play_and_wait_sees_clip_finish(self):
        self.client.connect()
        result = self.client.play_and_wait("http://x/3.mp3", timeout=5.0, max_wait=5.0)
        self.assertTrue(result["finished"])
        self.assertEqual(result["idle_reason"], "FINISHED")

    def test_stop_current_cuts_a_long_clip_short(self):
        device = MockCastDevice(finish_after=None).start()
        self.addCleanup(device.stop)
        client = CastClient("127.0.0.1", device.port, timeout=5.0)
        self.addCleanup(client.close)
        client.connect()

        started = client.play_url("http://x/long.mp3", timeout=5.0)
        self.assertEqual(device.current_state, "PLAYING")
        self.assertIsNotNone(client.media_session_id, "LOAD must remember its media session")
        self.assertTrue(client.stop_current())

        result = client.wait_until_done(started, max_wait=10.0)
        self.assertTrue(result["finished"])
        self.assertEqual(device.current_state, "IDLE")

    def test_stop_current_without_a_loaded_clip_is_a_no_op(self):
        self.client.connect()
        self.assertFalse(self.client.stop_current())

    def test_probe_helper(self):
        info = probe("127.0.0.1", self.device.port, timeout=5.0)
        self.assertEqual(info["host"], "127.0.0.1")
        self.assertAlmostEqual(info["volume"], 0.5)

    def test_connection_refused_is_wrapped(self):
        client = CastClient("127.0.0.1", 9, timeout=1.0)
        with self.assertRaises(CastConnectionError):
            client.connect()
        client.close()

    def test_heartbeat_thread_keeps_session_alive(self):
        self.client.connect()
        self.assertTrue(self.client._heartbeat_thread.is_alive())
        self.client.close()
        self.assertFalse(self.client.connected)

    def test_reconnect_after_close(self):
        self.client.connect()
        self.client.get_status()
        self.client.close()
        self.client.connect()
        self.assertAlmostEqual(CastClient.summarize_status(self.client.get_status())["volume"], 0.5)


class FrameCompatibilityTests(unittest.TestCase):
    def test_mock_and_client_agree_on_framing(self):
        body = p.encode_message("sender-0", "receiver-0", p.NS_HEARTBEAT, {"type": "PING"})
        self.assertEqual(body[0], 0x08)
        self.assertEqual(int.from_bytes(p.frame(body)[:4], "big"), len(body))


if __name__ == "__main__":
    unittest.main()
