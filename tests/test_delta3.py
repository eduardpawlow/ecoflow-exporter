import unittest
from unittest.mock import patch

import delta3_pb2 as proto
from delta3_decoder import decode_delta3
from google.protobuf.message import DecodeError
import test_exporter


def packet(message, func, cmd, seq=91, src=2, encoding=1):
    data = message.SerializeToString()
    if encoding == 1 and src != 32:
        data = bytes(byte ^ (seq & 255) for byte in data)
    header = proto.Header(pdata=data, cmd_func=func, cmd_id=cmd, seq=seq,
                          src=src, enc_type=encoding, data_len=len(data))
    return proto.HeaderMessage(header=[header]).SerializeToString()


class DecoderTests(unittest.TestCase):
    def test_runtime_report(self):
        decoded = decode_delta3(packet(proto.RuntimePropertyUpload(temp_pcs_dc=32.5), 254, 22))
        self.assertEqual(decoded, [{'runtime': {'temp_pcs_dc': 32.5}}])

    def test_encrypted_and_zero_fields(self):
        message = proto.DisplayPropertyUpload(pow_in_sum_w=0, pow_out_sum_w=42)
        decoded = decode_delta3(packet(message, 254, 21))
        self.assertEqual(decoded, [{'display': {'pow_in_sum_w': 0.0, 'pow_out_sum_w': 42.0}}])
        self.assertNotIn('bms_batt_soc', decoded[0]['display'])

    def test_battery_identity_signed_and_repeated_values(self):
        message = proto.BMSHeartBeatReport(num=1, amp=-250, cell_vol=[3300, 3301])
        decoded = decode_delta3(packet(message, 32, 50))
        self.assertEqual(decoded[0]['bms_1']['amp'], -250)
        self.assertEqual(decoded[0]['bms_1']['cell_vol'], [3300, 3301])

    def test_multiple_headers_and_unknown_command(self):
        one = packet(proto.DisplayPropertyUpload(pow_in_sum_w=1), 254, 21)
        two = packet(proto.CMSHeartBeatReport(), 99, 99)
        self.assertEqual(len(decode_delta3(one + two)), 1)
        with self.assertRaises(DecodeError):
            decode_delta3(b'\x0a\xff')

    def test_source_32_is_not_xored(self):
        decoded = decode_delta3(packet(proto.BMSHeartBeatReport(soc=50), 32, 50, src=32))
        self.assertEqual(decoded[0]['bms_0']['soc'], 50)


class WorkerDeltaTests(unittest.TestCase):
    setUp = test_exporter.ExporterTests.setUp
    metric = test_exporter.ExporterTests.metric

    def test_partial_updates_and_offline_timeout(self):
        self.worker.device_model = 'delta3_plus'
        self.worker.message_queue.put(packet(proto.DisplayPropertyUpload(pow_in_sum_w=10), 254, 21))
        with patch('ecoflow_exporter.time.monotonic', return_value=100), patch('ecoflow_exporter.time.sleep', side_effect=[None, InterruptedError]):
            with self.assertRaises(InterruptedError):
                self.worker.loop()
        self.assertEqual(self.metric('display_pow_in_sum_w'), 10)
        for now, online in ((110, 1), (221, 0)):
            with patch('ecoflow_exporter.time.monotonic', return_value=now), patch('ecoflow_exporter.time.sleep', side_effect=[None, InterruptedError]):
                with self.assertRaises(InterruptedError):
                    self.worker.loop()
            self.assertEqual(self.metric('online'), online)
        self.assertIsNone(self.metric('display_pow_in_sum_w'))
