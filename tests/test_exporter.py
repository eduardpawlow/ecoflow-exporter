import json
import unittest
from queue import Queue
from unittest.mock import Mock, patch
from urllib.request import urlopen

import ecoflow_exporter as exporter
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode
from prometheus_client import CollectorRegistry, Gauge, Counter, generate_latest


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.registry = CollectorRegistry()
        self.gauge_patch = patch.object(exporter, 'Gauge', side_effect=lambda *a, **kw: Gauge(*a, registry=self.registry, **kw))
        self.counter_patch = patch.object(exporter, 'Counter', side_effect=lambda *a, **kw: Counter(*a, registry=self.registry, **kw))
        self.gauge_patch.start()
        self.counter_patch.start()
        self.addCleanup(self.gauge_patch.stop)
        self.addCleanup(self.counter_patch.stop)
        self.worker = exporter.Worker(Queue(), 'test-device')

    def metric(self, name):
        return self.registry.get_sample_value('ecoflow_' + name, {'device': 'test-device'})

    def test_dashboard_names_and_nested_paths(self):
        self.worker.process_message(json.dumps({'params': {
            'energyInfos': [{'batteryPercentage': 70}, {'batteryPercentage': 80}],
            'backupCmd': {'chCtrlInfos': [{'ctrlSta': 1}]},
            'infoList': [{'chWatt': 42}],
            'bms_bmsStatus.minCellTemp': 25,
            'matrix': [[1, 2], [3, 4]],
        }}))
        for name, value in {'energy_infos_battery_percentage_0': 70,
                            'energy_infos_battery_percentage_1': 80,
                            'backup_cmd_ch_ctrl_infos_ctrl_sta_0': 1,
                            'info_list_ch_watt_0': 42,
                            'bms_bms_status_min_cell_temp': 25,
                            'matrix_1_0': 3}.items():
            self.assertEqual(self.metric(name), value)

    def test_invalid_messages_do_not_prevent_next_update(self):
        for payload in (None, b'\xff', '{', '{}', '[]', 'null', '{"params":null}', '{"params":[]}'):
            with self.subTest(payload=payload):
                self.worker.process_message(payload)
        self.worker.process_message(b'{"params":{"charge":55}}')
        self.assertEqual(self.metric('charge'), 55)

    def test_invalid_values_names_and_collisions_are_skipped(self):
        self.worker.process_payload({'': 1, 'bad-key': 2, 'values': [None, 'text', float('inf'), 4],
                                     'huge': 10 ** 500, 'nan': float('nan'),
                                     'a.b': 1, 'a_b': 2, 'charge': 80}, '', '')
        self.assertEqual(self.metric('values_3'), 4)
        self.assertIsNone(self.metric('values_0'))
        self.assertIsNone(self.metric('huge'))
        self.assertEqual(self.metric('a_b'), 1)
        self.assertEqual(self.metric('charge'), 80)

    def test_mqtt_v2_callbacks_and_resubscription(self):
        with patch.object(exporter.mqtt.Client, 'connect'), patch.object(exporter.mqtt.Client, 'loop_start'):
            connection = exporter.EcoflowMQTT(Queue(), 'test-device', 'user', 'password', 'localhost', 8883, 'client-id')
        self.assertEqual(connection.client.callback_api_version, exporter.mqtt.CallbackAPIVersion.VERSION2)
        client = Mock()
        client.subscribe.return_value = (exporter.mqtt.MQTT_ERR_SUCCESS, 1)
        ok = ReasonCode(PacketTypes.CONNACK, 'Success')
        connection.on_connect(client, None, None, ok, None)
        connection.on_connect(client, None, None, ok, None)
        self.assertEqual(client.subscribe.call_count, 2)
        connection.on_connect(client, None, None, ReasonCode(PacketTypes.CONNACK, 'Not authorized'), None)
        self.assertEqual(client.subscribe.call_count, 2)
        with patch.object(exporter.time, 'sleep') as sleep:
            connection.on_disconnect(client, None, None, ReasonCode(PacketTypes.DISCONNECT, 'Unspecified error'), None)
            sleep.assert_not_called()
        connection.on_message(client, None, Mock(payload=b'\xff'))
        self.assertEqual(connection.message_queue.get_nowait(), b'\xff')

    def test_quota_keepalive_disabled_by_default(self):
        # Without user_id / QUOTA_REQUEST_INTERVAL the client only ever
        # subscribes to the regular property topic, same as before this feature.
        with patch.object(exporter.mqtt.Client, 'connect'), patch.object(exporter.mqtt.Client, 'loop_start'):
            connection = exporter.EcoflowMQTT(Queue(), 'sn', 'user', 'password', 'localhost', 8883, 'client-id')
        self.assertIsNone(connection.get_topic)
        client = Mock()
        client.subscribe.return_value = (exporter.mqtt.MQTT_ERR_SUCCESS, 1)
        ok = ReasonCode(PacketTypes.CONNACK, 'Success')
        connection.on_connect(client, None, None, ok, None)
        self.assertEqual(client.subscribe.call_count, 1)
        client.publish.assert_not_called()

    def test_quota_keepalive_requests_latest_quotas_snapshot(self):
        with patch.object(exporter.mqtt.Client, 'connect'), patch.object(exporter.mqtt.Client, 'loop_start'):
            connection = exporter.EcoflowMQTT(Queue(), 'sn', 'user', 'password', 'localhost', 8883, 'client-id',
                                               user_id='42', quota_request_interval=30)
        self.assertEqual(connection.get_topic, '/app/42/sn/thing/property/get')
        self.assertEqual(connection.get_reply_topic, '/app/42/sn/thing/property/get_reply')

        client = Mock()
        client.subscribe.return_value = (exporter.mqtt.MQTT_ERR_SUCCESS, 1)
        ok = ReasonCode(PacketTypes.CONNACK, 'Success')

        # The "get" request is published via connection.client (the long-lived
        # MQTT client), not the `client` argument on_connect receives, because
        # the periodic Timer callback runs outside any paho callback context.
        with patch.object(connection.client, 'publish', return_value=(exporter.mqtt.MQTT_ERR_SUCCESS, 1)) as publish, \
             patch.object(exporter.threading, 'Timer') as timer:
            connection.on_connect(client, None, None, ok, None)

        # Regular property topic + get_reply topic.
        self.assertEqual(client.subscribe.call_count, 2)
        client.subscribe.assert_any_call(connection.get_reply_topic)

        publish.assert_called_once()
        topic, payload = publish.call_args[0][:2]
        self.assertEqual(topic, connection.get_topic)
        self.assertEqual(json.loads(payload), {
            'version': '1.1', 'moduleType': 0, 'operateType': 'latestQuotas', 'params': {},
        })
        timer.assert_called_once_with(30, connection._schedule_quota_request)

    def test_quota_keepalive_reply_is_processed_like_a_normal_message(self):
        # get_reply arrives on the same client/queue as the regular property
        # topic, so no special-casing is needed on the Worker side.
        self.worker.process_message(json.dumps({'params': {'bmsMaster.soc': 87}}))
        self.assertEqual(self.metric('bms_master_soc'), 87)

    def test_auth_errors_do_not_expose_response_body(self):
        auth = exporter.EcoflowAuthentication.__new__(exporter.EcoflowAuthentication)
        for response in (Mock(status_code=500, text='SECRET'),
                         Mock(status_code=200, json=Mock(return_value={'message': 'SECRET'})),
                         Mock(status_code=200, json=Mock(side_effect=ValueError('SECRET')))):
            with self.assertRaises(RuntimeError) as error:
                auth.get_json_response(response)
            self.assertNotIn('SECRET', str(error.exception))

    def test_http_metrics(self):
        self.worker.process_message('{"params":{"charge":75}}')
        server, thread = exporter.start_http_server(0, addr='127.0.0.1', registry=self.registry)
        try:
            with urlopen(f'http://127.0.0.1:{server.server_port}/metrics', timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b'ecoflow_charge{device="test-device"} 75.0', response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
