"""Discovery tests: interface fan-out, mDNS merging, and device selection.

No real network traffic: ``_query_interface`` and ``local_ipv4_addresses`` are
replaced, so these pin the behaviour rather than the environment.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from googlehome import discovery


def _device(address: str, name: str, model: str = "google home mini", ca: str = "4") -> dict:
    return {
        "service": f"{name}._googlecast._tcp.local",
        "address": address,
        "host": f"{name}.local",
        "port": 8009,
        "txt": {"id": f"id-{name}", "fn": name, "md": model, "ca": ca},
    }


class DiscoveryFanoutTests(unittest.TestCase):
    def test_all_interfaces_are_queried_concurrently(self):
        interfaces = ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"]
        seen: list[str] = []

        def slow_query(iface_ip, _timeout):
            seen.append(iface_ip)
            time.sleep(0.5)
            return []

        with mock.patch.object(discovery, "local_ipv4_addresses", return_value=interfaces), \
                mock.patch.object(discovery, "_query_interface", side_effect=slow_query):
            started = time.monotonic()
            devices = discovery.discover(timeout=0.6)
            elapsed = time.monotonic() - started

        self.assertEqual(devices, [])
        self.assertCountEqual(seen, interfaces)
        # Sequential would be 4 x 0.5s = 2.0s; concurrent should stay near one window.
        self.assertLess(elapsed, 1.5, f"discovery took {elapsed:.2f}s — interfaces are serialised")

    def test_devices_from_different_interfaces_are_merged_and_deduplicated(self):
        def fake_query(iface_ip, _timeout):
            if iface_ip == "10.0.0.1":
                return [_device("10.0.0.50", "kitchen")]
            return [_device("10.0.0.50", "kitchen"), _device("10.0.0.51", "bed")]

        with mock.patch.object(discovery, "local_ipv4_addresses", return_value=["10.0.0.1", "10.0.0.2"]), \
                mock.patch.object(discovery, "_query_interface", side_effect=fake_query):
            devices = discovery.discover(timeout=0.2)

        self.assertEqual(len(devices), 2, "the same device seen twice should appear once")
        by_name = {device["name"]: device for device in devices}
        self.assertEqual(by_name["kitchen"]["address"], "10.0.0.50")
        self.assertTrue(by_name["kitchen"]["audio_only"])

    def test_capability_bitmask_decides_audio_only(self):
        def fake_query(_iface, _timeout):
            return [_device("10.0.0.60", "speaker", ca="4"),
                    _device("10.0.0.61", "tv", model="chromecast", ca="5")]

        with mock.patch.object(discovery, "local_ipv4_addresses", return_value=["10.0.0.1"]), \
                mock.patch.object(discovery, "_query_interface", side_effect=fake_query):
            devices = {device["name"]: device for device in discovery.discover(timeout=0.1)}

        self.assertTrue(devices["speaker"]["audio_only"])
        self.assertFalse(devices["tv"]["audio_only"])
        self.assertEqual(devices["speaker"]["capabilities"], 4)

    def test_real_firmware_bitmask_is_masked_not_compared(self):
        """A Home Mini reports ca=199172: high bits set, audio-out still bit 2."""
        def fake_query(_iface, _timeout):
            return [_device("10.0.0.62", "living room", model="Google Home Mini", ca="199172")]

        with mock.patch.object(discovery, "local_ipv4_addresses", return_value=["10.0.0.1"]), \
                mock.patch.object(discovery, "_query_interface", side_effect=fake_query):
            device = discovery.discover(timeout=0.1)[0]

        self.assertEqual(device["capabilities"], 199172)
        self.assertTrue(device["audio_only"], "a speaker with high ca bits must still be audio-only")

    def test_txt_without_id_is_skipped(self):
        def fake_query(_iface, _timeout):
            return [{"service": "x", "address": "10.0.0.9", "txt": {}}]

        with mock.patch.object(discovery, "local_ipv4_addresses", return_value=["10.0.0.1"]), \
                mock.patch.object(discovery, "_query_interface", side_effect=fake_query):
            self.assertEqual(discovery.discover(timeout=0.1), [])


class ResolveDeviceTests(unittest.TestCase):
    def test_ip_is_used_without_discovery(self):
        with mock.patch.object(discovery, "discover") as mocked:
            resolved = discovery.resolve_device("192.168.1.42")
        self.assertEqual(resolved["host"], "192.168.1.42")
        self.assertEqual(resolved["port"], 8009)
        mocked.assert_not_called()

    def test_audio_only_device_is_preferred(self):
        devices = [_device("10.0.0.61", "tv", model="chromecast", ca="5"),
                   _device("10.0.0.60", "speaker")]
        with mock.patch.object(discovery, "discover", return_value=devices):
            resolved = discovery.resolve_device("")
        self.assertEqual(resolved["name"], "speaker")

    def test_friendly_name_matches(self):
        devices = [_device("10.0.0.61", "kitchen"), _device("10.0.0.60", "bedroom")]
        with mock.patch.object(discovery, "discover", return_value=devices):
            resolved = discovery.resolve_device("bedroom")
        self.assertEqual(resolved["name"], "bedroom")

    def test_unmatched_name_falls_back_to_first_device(self):
        devices = [_device("10.0.0.60", "bedroom")]
        with mock.patch.object(discovery, "discover", return_value=devices):
            resolved = discovery.resolve_device("nonexistent")
        self.assertEqual(resolved["name"], "bedroom")

    def test_nothing_found_returns_empty(self):
        with mock.patch.object(discovery, "discover", return_value=[]):
            self.assertEqual(discovery.resolve_device(""), {})

    def test_unresolvable_name_is_returned_as_a_host(self):
        """An explicit spec must still be attempted even if mDNS finds nothing."""
        with mock.patch.object(discovery, "discover", return_value=[]):
            resolved = discovery.resolve_device("speaker.lan")
        self.assertEqual(resolved["host"], "speaker.lan")


class IdentifyTests(unittest.TestCase):
    """A device pinned by IP still has to say what hardware it is."""

    EUREKA = {
        "name": "Living Room speaker",
        "device_info": {"model_name": "Google Home Mini", "product_name": "mushroom"},
    }

    def test_identity_comes_from_the_device_info_block(self):
        with mock.patch.object(discovery, "eureka_info", return_value=self.EUREKA):
            self.assertEqual(
                discovery.identify("192.168.1.145"),
                {"name": "Living Room speaker", "model": "Google Home Mini"},
            )

    def test_identity_is_empty_when_the_device_does_not_answer(self):
        with mock.patch.object(discovery, "eureka_info", return_value={}):
            self.assertEqual(discovery.identify("192.168.1.145"), {})

    def test_ip_spec_is_enriched_without_waiting_for_multicast(self):
        with mock.patch.object(discovery, "eureka_info", return_value=self.EUREKA), \
                mock.patch.object(discovery, "discover") as mocked_discover:
            resolved = discovery.resolve_device("192.168.1.145", enrich=True)

        mocked_discover.assert_not_called()
        self.assertEqual(resolved["host"], "192.168.1.145")
        self.assertEqual(resolved["address"], "192.168.1.145", "the dial address must not move")
        self.assertEqual(resolved["name"], "Living Room speaker")
        self.assertEqual(resolved["model"], "Google Home Mini")

    def test_a_device_mdns_already_described_is_not_probed_again(self):
        devices = [_device("10.0.0.60", "bedroom", model="Google Home Mini")]
        with mock.patch.object(discovery, "discover", return_value=devices), \
                mock.patch.object(discovery, "eureka_info") as mocked_eureka:
            resolved = discovery.resolve_device("", enrich=True)

        mocked_eureka.assert_not_called()
        self.assertEqual(resolved["model"], "Google Home Mini")

    def test_an_unreachable_device_keeps_the_configured_address(self):
        with mock.patch.object(discovery, "eureka_info", return_value={}), \
                mock.patch.object(discovery, "discover"):
            resolved = discovery.resolve_device("192.168.1.200", enrich=True)

        self.assertEqual(resolved["host"], "192.168.1.200")
        self.assertEqual(resolved["name"], "192.168.1.200")


class DisplayTests(unittest.TestCase):
    def test_model_names_the_speaker_and_the_address_goes_in_brackets(self):
        device = discovery.normalize(
            _device("192.168.1.145", "Living Room speaker", model="Google Home Mini")
        )
        self.assertEqual(discovery.display_name(device), "Google Home Mini")
        self.assertEqual(discovery.display_label(device), "Google Home Mini (192.168.1.145)")

    def test_friendly_name_stands_in_when_the_model_is_unknown(self):
        device = discovery.normalize(_device("10.0.0.50", "kitchen", model=""))
        self.assertEqual(discovery.display_name(device), "kitchen")
        self.assertEqual(discovery.display_label(device), "kitchen (10.0.0.50)")

    def test_an_unnamed_device_uses_its_address_once(self):
        device = discovery.normalize({"name": "10.0.0.50", "address": "10.0.0.50"})
        self.assertEqual(discovery.display_label(device), "10.0.0.50")


class DialHostTests(unittest.TestCase):
    """The SRV target is ``<uuid>.local``; Windows cannot resolve that name."""

    def test_ip_address_wins_over_the_mdns_name(self):
        device = discovery.normalize(_device("192.168.1.42", "living room"))
        self.assertEqual(device["host"], "living room.local")
        self.assertEqual(discovery.dial_host(device), "192.168.1.42")

    def test_hostname_is_used_when_no_ip_was_resolved(self):
        self.assertEqual(discovery.dial_host({"host": "speaker.lan", "address": ""}), "speaker.lan")

    def test_ip_spec_dials_itself(self):
        self.assertEqual(discovery.dial_host(discovery.resolve_device("192.168.1.42")), "192.168.1.42")


class DescribeTests(unittest.TestCase):
    def test_describe_includes_model(self):
        self.assertIn("kitchen", discovery.describe(_device("10.0.0.50", "kitchen")))
        self.assertIn("google home mini", discovery.describe(_device("10.0.0.50", "kitchen")))


if __name__ == "__main__":
    unittest.main()
