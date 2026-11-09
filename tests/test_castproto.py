"""Tests for the hand-rolled Cast v2 protobuf framing."""
from __future__ import annotations

import io
import json
import unittest

from googlehome import castproto as p


class EncodeDecodeTests(unittest.TestCase):
    def test_dict_payload_round_trip(self):
        wire = p.encode_message("sender-0", "receiver-0", p.NS_RECEIVER, {"type": "GET_STATUS", "requestId": 7})
        decoded = p.decode_message(wire)
        self.assertEqual(decoded["source_id"], "sender-0")
        self.assertEqual(decoded["destination_id"], "receiver-0")
        self.assertEqual(decoded["namespace"], p.NS_RECEIVER)
        self.assertEqual(decoded["payload_type"], 0)
        self.assertEqual(decoded["payload"], {"type": "GET_STATUS", "requestId": 7})

    def test_string_payload_is_passed_through(self):
        wire = p.encode_message("a", "b", p.NS_MEDIA, '{"type":"STOP"}')
        self.assertEqual(p.decode_message(wire)["payload"], {"type": "STOP"})

    def test_unicode_payload(self):
        text = "héllo — 世界 🎙"
        wire = p.encode_message("s", "d", p.NS_MEDIA, {"say": text})
        self.assertEqual(p.decode_message(wire)["payload"]["say"], text)

    def test_field_numbers_follow_cast_channel_proto(self):
        wire = p.encode_message("src", "dst", "ns", {})
        # protocol_version(1), source_id(2), destination_id(3), namespace(4),
        # payload_type(5), payload_utf8(6) — check the first tag byte of each
        # string field: (field << 3) | 2.
        tags = {wire[0], wire[0]}
        self.assertEqual(wire[0], (1 << 3) | 0)  # field 1 varint
        self.assertIn(bytes([(2 << 3) | 2]), wire)
        self.assertIn(bytes([(3 << 3) | 2]), wire)
        self.assertIn(bytes([(4 << 3) | 2]), wire)
        self.assertIn(bytes([(6 << 3) | 2]), wire)
        self.assertTrue(tags)

    def test_unknown_field_is_skipped(self):
        base = p.encode_message("a", "b", "ns", {"type": "X"})
        # append an unknown field number 9, wire type 2
        extra = p.encode_varint((9 << 3) | 2) + p.encode_varint(3) + b"xyz"
        decoded = p.decode_message(base + extra)
        self.assertEqual(decoded["payload"], {"type": "X"})

    def test_non_json_payload_is_preserved(self):
        wire = p.encode_message("a", "b", "ns", "not json at all")
        self.assertEqual(p.decode_message(wire)["payload"], {"_raw": "not json at all"})


class VarintTests(unittest.TestCase):
    def test_multibyte_values(self):
        for value in (0, 1, 127, 128, 300, 16384, 2**31 - 1):
            encoded = p.encode_varint(value)
            decoded, offset = p._read_varint(encoded, 0)
            self.assertEqual(decoded, value)
            self.assertEqual(offset, len(encoded))

    def test_negative_rejected(self):
        with self.assertRaises(p.CastProtocolError):
            p.encode_varint(-1)


class FramingTests(unittest.TestCase):
    def test_frame_prefixes_be_length(self):
        body = p.encode_message("a", "b", "ns", {"x": 1})
        wire = p.frame(body)
        self.assertEqual(int.from_bytes(wire[:4], "big"), len(body))
        self.assertEqual(wire[4:], body)

    def test_read_frame_round_trip(self):
        body = p.encode_message("a", "b", p.NS_HEARTBEAT, {"type": "PING"})
        stream = io.BytesIO(p.frame(body) + p.frame(body))
        self.assertEqual(p.read_frame(stream)["payload"], {"type": "PING"})
        self.assertEqual(p.read_frame(stream)["payload"], {"type": "PING"})

    def test_truncated_prefix_raises(self):
        with self.assertRaises(p.CastProtocolError):
            p.read_frame(io.BytesIO(b"\x00\x00"))

    def test_truncated_body_raises(self):
        with self.assertRaises(p.CastProtocolError):
            p.read_frame(io.BytesIO(b"\x00\x00\x00\x10" + b"short"))

    def test_absurd_length_raises(self):
        with self.assertRaises(p.CastProtocolError):
            p.read_frame(io.BytesIO(b"\xff\xff\xff\xff" + b"x"))


if __name__ == "__main__":
    unittest.main()
