"""Protocol codec tests - every vendor frame example, scaling, and parser edge case.

Hardware-free: the codec never touches a serial port.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import kuaiqus_psu_protocol as protocol  # noqa: E402


class EncodeTests(unittest.TestCase):
    """Frames the PC sends, checked against the vendor manual's examples."""

    def test_set_voltage_matches_vendor_example(self):
        # 4.58 V -> 4580 mV -> "004580" + "000"
        self.assertEqual(protocol.encode_set_voltage(4.58), "<01004580000>")

    def test_set_voltage_twelve_volts(self):
        self.assertEqual(protocol.encode_set_voltage(12), "<01012000000>")

    def test_set_current_matches_vendor_example(self):
        # 6.92 A -> 6920 mA
        self.assertEqual(protocol.encode_set_current(6.92), "<03006920000>")

    def test_set_current_sub_amp(self):
        self.assertEqual(protocol.encode_set_current(0.183), "<03000183000>")

    def test_queries_are_zero_filled(self):
        self.assertEqual(protocol.encode_read_voltage(), "<02000000000>")
        self.assertEqual(protocol.encode_read_current(), "<04000000000>")
        self.assertEqual(protocol.encode_read_version(), "<06000000000>")

    def test_power_and_mode_frames(self):
        self.assertEqual(protocol.encode_power_on(), "<07000000000>")
        self.assertEqual(protocol.encode_power_off(), "<08000000000>")
        self.assertEqual(protocol.encode_connect(), "<09100000000>")
        self.assertEqual(protocol.encode_disconnect(), "<09200000000>")

    def test_every_frame_has_the_fixed_width(self):
        for frame in (
            protocol.encode_set_voltage(25),
            protocol.encode_set_current(10),
            protocol.encode_read_voltage(),
            protocol.encode_power_on(),
            protocol.encode_connect(),
        ):
            self.assertEqual(len(frame), protocol.PAYLOAD_LENGTH + 2, frame)
            self.assertTrue(frame.startswith(protocol.START))
            self.assertTrue(frame.endswith(protocol.END))


class ScalingTests(unittest.TestCase):
    """Milli-unit conversion, rounding, and range rejection."""

    def test_volts_round_to_nearest_millivolt(self):
        self.assertEqual(protocol.volts_to_millivolts(4.5804), 4580)
        self.assertEqual(protocol.volts_to_millivolts(4.5806), 4581)

    def test_amps_round_to_nearest_milliamp(self):
        self.assertEqual(protocol.amps_to_milliamps(0.9999), 1000)

    def test_round_trip(self):
        self.assertAlmostEqual(protocol.millivolts_to_volts(4580), 4.58, places=6)
        self.assertAlmostEqual(protocol.milliamps_to_amps(183), 0.183, places=6)

    def test_none_passes_through(self):
        self.assertIsNone(protocol.millivolts_to_volts(None))
        self.assertIsNone(protocol.milliamps_to_amps(None))

    def test_negative_is_rejected(self):
        with self.assertRaises(ValueError):
            protocol.encode_set_voltage(-1)

    def test_above_range_is_rejected(self):
        with self.assertRaises(ValueError):
            protocol.encode_set_voltage(1000)

    def test_non_numeric_is_rejected(self):
        with self.assertRaises(ValueError):
            protocol.encode_set_current("abc")


class DecodeTests(unittest.TestCase):
    """Responses the device sends, including CV/CC state and the OK acknowledgement."""

    def test_cv_voltage_response(self):
        frame = protocol.decode_frame("<12004580000>")
        self.assertIsNotNone(frame)
        self.assertTrue(frame.from_mcu)
        self.assertEqual(frame.cv_cc, "CV")
        self.assertEqual(frame.value_milli, 4580)
        self.assertFalse(frame.is_ack)

    def test_cc_voltage_response(self):
        frame = protocol.decode_frame("<C2012000000>")
        self.assertEqual(frame.cv_cc, "CC")
        self.assertEqual(frame.value_milli, 12000)

    def test_current_response(self):
        frame = protocol.decode_frame("<14000183000>")
        self.assertEqual(frame.value_milli, 183)
        self.assertAlmostEqual(protocol.milliamps_to_amps(frame.value_milli), 0.183, places=6)

    def test_zero_value_is_a_value_not_a_miss(self):
        frame = protocol.decode_frame("<12000000000>")
        self.assertEqual(frame.value_milli, 0)

    def test_set_acknowledgement(self):
        for raw in ("<11OK0000000>", "<13OK0000000>"):
            frame = protocol.decode_frame(raw)
            self.assertTrue(frame.is_ack)
            self.assertIsNone(frame.value_milli)

    def test_version_response_is_text(self):
        frame = protocol.decode_frame("<16V1.300000>")
        self.assertEqual(frame.text, "V1.300000")
        self.assertIsNone(frame.value_milli)
        self.assertEqual(protocol.parse_version_text(frame.text), "1.3")

    def test_pc_direction_is_not_from_mcu(self):
        frame = protocol.decode_frame("<02000000000>")
        self.assertFalse(frame.from_mcu)
        self.assertIsNone(frame.cv_cc)

    def test_malformed_frames_return_none(self):
        for raw in ("", "<>", "<123>", "12004580000", "<1200458000>", "<120045800000>", None, 42):
            self.assertIsNone(protocol.decode_frame(raw), repr(raw))


class VersionTextTests(unittest.TestCase):
    """Firmware-version normalisation."""

    def test_trailing_zeros_are_trimmed(self):
        self.assertEqual(protocol.parse_version_text("V1.300000"), "1.3")

    def test_plain_number(self):
        self.assertEqual(protocol.parse_version_text("2"), "2")

    def test_unparsable_text_is_returned_as_is(self):
        self.assertEqual(protocol.parse_version_text("garbage"), "garbage")

    def test_empty_is_none(self):
        self.assertIsNone(protocol.parse_version_text(""))
        self.assertIsNone(protocol.parse_version_text(None))


class FrameParserTests(unittest.TestCase):
    """Streaming parser: split, concatenated and noisy input."""

    def test_single_frame(self):
        parser = protocol.FrameParser()
        frames = parser.push("<12004580000>")
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].value_milli, 4580)

    def test_frame_split_across_chunks(self):
        parser = protocol.FrameParser()
        self.assertEqual(parser.push("<1200458"), [])
        frames = parser.push("0000>")
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].value_milli, 4580)

    def test_two_frames_in_one_chunk(self):
        parser = protocol.FrameParser()
        frames = parser.push("<12004580000><14000183000>")
        self.assertEqual([f.value_milli for f in frames], [4580, 183])

    def test_noise_before_frame_is_dropped(self):
        parser = protocol.FrameParser()
        frames = parser.push("garbage<12004580000>")
        self.assertEqual(len(frames), 1)

    def test_bytes_input(self):
        parser = protocol.FrameParser()
        frames = parser.push(b"<12004580000>")
        self.assertEqual(len(frames), 1)

    def test_reset_discards_partial_frame(self):
        parser = protocol.FrameParser()
        parser.push("<1200458")
        parser.reset()
        self.assertEqual(parser.push("0000>"), [])

    def test_iter_frames_helper(self):
        frames = protocol.iter_frames([b"<11OK0000000>", b"<12004580000>"])
        self.assertEqual(len(frames), 2)
        self.assertTrue(frames[0].is_ack)


if __name__ == "__main__":
    unittest.main()
