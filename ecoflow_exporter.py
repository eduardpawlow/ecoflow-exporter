# Based on vbash/ecoflow_exporter (GPL-3.0).
# Modified 2026-09-13: Paho callback API v2 and resilient payload processing.
import logging as log
import sys
import os
import ssl
import time
import json
import re
import math
import requests
import base64
import uuid
import paho.mqtt.client as mqtt
from queue import Queue
from google.protobuf.message import DecodeError
from delta3_decoder import decode_delta3
from prometheus_client import start_http_server, REGISTRY, Gauge, Counter


class EcoflowMetricException(Exception):
    pass


class EcoflowAuthentication:
    def __init__(self, ecoflow_username, ecoflow_password):
        self.ecoflow_username = ecoflow_username
        self.ecoflow_password = ecoflow_password
        self.mqtt_url = "mqtt.ecoflow.com"
        self.mqtt_port = 8883
        self.mqtt_username = None
        self.mqtt_password = None
        self.mqtt_client_id = None
        self.authorize()

    def authorize(self):
        url = "https://api.ecoflow.com/auth/login"
        headers = {"lang": "en_US", "content-type": "application/json"}
        data = {"email": self.ecoflow_username,
                "password": base64.b64encode(self.ecoflow_password.encode()).decode(),
                "scene": "IOT_APP",
                "userType": "ECOFLOW"}

        log.info(f"Login to EcoFlow API {url}")
        request = requests.post(url, json=data, headers=headers, timeout=30)
        response = self.get_json_response(request)

        try:
            token = response["data"]["token"]
            user_id = response["data"]["user"]["userId"]
            user_name = response["data"]["user"]["name"]
        except KeyError as key:
            raise RuntimeError(f"Login response is missing {key}") from None

        log.info(f"Successfully logged in: {user_name}")

        url = "https://api.ecoflow.com/iot-auth/app/certification"
        headers = {"lang": "en_US", "authorization": f"Bearer {token}"}
        data = {"userId": user_id}

        log.info(f"Requesting IoT MQTT credentials {url}")
        request = requests.get(url, data=data, headers=headers, timeout=30)
        response = self.get_json_response(request)

        try:
            self.mqtt_url = response["data"]["url"]
            self.mqtt_port = int(response["data"]["port"])
            self.mqtt_username = response["data"]["certificateAccount"]
            self.mqtt_password = response["data"]["certificatePassword"]
            self.mqtt_client_id = f"ANDROID_{str(uuid.uuid4()).upper()}_{user_id}"
        except KeyError as key:
            raise RuntimeError(f"MQTT credential response is missing {key}") from None

        log.info(f"Successfully extracted account: {self.mqtt_username}")

    def get_json_response(self, request):
        if request.status_code != 200:
            raise RuntimeError(f"EcoFlow API returned HTTP {request.status_code}")
        try:
            response = request.json()
        except ValueError:
            raise RuntimeError("EcoFlow API returned invalid JSON") from None
        if not isinstance(response, dict):
            raise RuntimeError("EcoFlow API returned an invalid response object")
        message = response.get("message")
        if not isinstance(message, str) or message.lower() != "success":
            raise RuntimeError("EcoFlow API request was not successful; check account credentials")
        if not isinstance(response.get("data"), dict):
            raise RuntimeError("EcoFlow API response is missing a data object")
        return response


class EcoflowMQTT():

    def __init__(self, message_queue, device_sn, username, password, addr, port, client_id):
        self.message_queue = message_queue
        self.addr = addr
        self.port = port
        self.username = username
        self.password = password
        self.client_id = client_id
        self.topic = f"/app/device/property/{device_sn}"

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id)
        self.client.username_pw_set(self.username, self.password)
        self.client.tls_set(certfile=None, keyfile=None, cert_reqs=ssl.CERT_REQUIRED)
        self.client.tls_insecure_set(False)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

        log.info(f"Connecting to MQTT Broker {self.addr}:{self.port} using client id {self.client_id}")
        self.client.connect(self.addr, self.port)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.loop_start()

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("Failed to connect to MQTT: %s", reason_code)
            return
        result, _ = client.subscribe(self.topic)
        if result != mqtt.MQTT_ERR_SUCCESS:
            log.error("Failed to subscribe to MQTT topic %s: %s", self.topic, result)
        else:
            log.info("Requested subscription to MQTT topic %s", self.topic)

    def on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("Unexpected MQTT disconnection: %s. Will auto-reconnect", reason_code)

    def on_message(self, client, userdata, message):
        # Decode in the worker so malformed UTF-8 cannot terminate the MQTT thread.
        self.message_queue.put(message.payload)

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()


class EcoflowMetric:
    def __init__(self, ecoflow_payload_key, device_name):
        self.ecoflow_payload_key = ecoflow_payload_key
        self.device_name = device_name
        self.name = f"ecoflow_{self.convert_ecoflow_key_to_prometheus_name()}"
        self.metric = Gauge(self.name, f"value from MQTT object key {ecoflow_payload_key}", labelnames=["device"])

    def convert_ecoflow_key_to_prometheus_name(self):
        # bms_bmsStatus.maxCellTemp -> bms_bms_status_max_cell_temp
        # pd.ext4p8Port -> pd_ext4p8_port
        if not self.ecoflow_payload_key:
            raise EcoflowMetricException('Empty metric key')
        key = self.ecoflow_payload_key.replace('.', '_')
        new = key[0].lower()
        for character in key[1:]:
            if character.isupper() and not new[-1] == '_':
                new += '_'
            new += character.lower()
        # Check that metric name complies with the data model for valid characters
        # https://prometheus.io/docs/concepts/data_model/#metric-names-and-labels
        if not re.fullmatch("[a-zA-Z_:][a-zA-Z0-9_:]*", new):
            raise EcoflowMetricException(f"Cannot convert payload key {self.ecoflow_payload_key} to comply with the Prometheus data model. Please, raise an issue!")
        return new

    def set(self, value):
        log.debug(f"Set {self.name} = {value}")
        self.metric.labels(device=self.device_name).set(value)

    def clear(self):
        log.debug(f"Clear {self.name}")
        self.metric.clear()


class Worker:
    def __init__(self, message_queue, device_name, collecting_interval_seconds=10, device_model="json", offline_timeout=120):
        self.message_queue = message_queue
        self.device_name = device_name
        self.collecting_interval_seconds = collecting_interval_seconds
        self.device_model = device_model
        self.offline_timeout = offline_timeout
        self.last_valid_message = None
        self.metrics_collector = []
        self.online = Gauge("ecoflow_online", "1 if device is online", labelnames=["device"])
        self.mqtt_messages_receive_total = Counter("ecoflow_mqtt_messages_receive_total", "total MQTT messages", labelnames=["device"])

    def loop(self):
        time.sleep(self.collecting_interval_seconds)
        while True:
            queue_size = self.message_queue.qsize()
            if queue_size:
                log.info("Processing %s event(s) from the message queue", queue_size)
                self.mqtt_messages_receive_total.labels(device=self.device_name).inc(queue_size)
            while not self.message_queue.empty():
                if self.process_message(self.message_queue.get()):
                    self.last_valid_message = time.monotonic()
            online = self.last_valid_message is not None and time.monotonic() - self.last_valid_message < self.offline_timeout
            self.online.labels(device=self.device_name).set(int(online))
            if not online:
                for metric in self.metrics_collector:
                    metric.clear()

            time.sleep(self.collecting_interval_seconds)

    def process_message(self, payload):
        try:
            message = json.loads(payload)
        except (ValueError, TypeError, UnicodeError):
            if self.device_model == "delta3_plus" and isinstance(payload, bytes):
                try:
                    reports = decode_delta3(payload)
                except DecodeError as error:
                    log.warning("Skipping invalid DELTA 3 Plus packet: %s", error)
                    return False
                for report in reports:
                    self.process_payload(report, "", "")
                return bool(reports)
            log.warning("Skipping non-JSON MQTT message (DEVICE_MODEL=%s)", self.device_model)
            return False
        if not isinstance(message, dict) or not isinstance(message.get("params"), dict):
            log.warning("Skipping MQTT message without a params object")
            return
        self.process_payload(message["params"], "", "")
        return True

    def get_metric_by_ecoflow_payload_key(self, ecoflow_payload_key):
        for metric in self.metrics_collector:
            if metric.ecoflow_payload_key == ecoflow_payload_key:
                log.debug(f"Found metric {metric.name} linked to {ecoflow_payload_key}")
                return metric
        log.debug(f"Cannot find metric linked to {ecoflow_payload_key}")
        return False

    def process_payload(self, params, prefix, postfix):
        for key, value in params.items():
            self.process_value(value, prefix + key, postfix)

    def process_value(self, value, key, postfix):
        # Keep the upstream array naming used by the Smart Home Panel dashboard:
        # energyInfos[0].batteryPercentage -> energy_infos_battery_percentage_0.
        if isinstance(value, dict):
            self.process_payload(value, key + "_", postfix)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self.process_value(item, key, postfix + "_" + str(index))
        else:
            self.process_parameter(key + postfix, value)

    def process_parameter(self, ecoflow_payload_key, ecoflow_payload_value):
        if not isinstance(ecoflow_payload_value, (int, float)):
            return
        try:
            value = float(ecoflow_payload_value)
        except (OverflowError, ValueError):
            return
        if not math.isfinite(value):
            return
        metric = self.get_metric_by_ecoflow_payload_key(ecoflow_payload_key)
        if not metric:
            try:
                metric = EcoflowMetric(ecoflow_payload_key, self.device_name)
            except (EcoflowMetricException, ValueError) as error:
                log.error(error)
                return

            log.info(f"Created new metric from payload key {metric.ecoflow_payload_key} -> {metric.name}")
            self.metrics_collector.append(metric)

        metric.set(value)

        if ecoflow_payload_key == 'inv.acInVol' and ecoflow_payload_value == 0:
            ac_in_current = self.get_metric_by_ecoflow_payload_key('inv.acInAmp')
            if ac_in_current:
                log.debug("Set AC inverter input current to zero because of zero inverter voltage")
                ac_in_current.set(0)


def main():

    # Disable Process and Platform collectors
    for coll in list(REGISTRY._collector_to_names.keys()):
        REGISTRY.unregister(coll)

    log_level = os.getenv("LOG_LEVEL", "INFO")

    match log_level:
        case "DEBUG":
            log_level = log.DEBUG
        case "INFO":
            log_level = log.INFO
        case "WARNING":
            log_level = log.WARNING
        case "ERROR":
            log_level = log.ERROR
        case _:
            log_level = log.INFO

    log.basicConfig(stream=sys.stdout, level=log_level, format='%(asctime)s %(levelname)-7s %(message)s')

    device_sn = os.getenv("DEVICE_SN")
    device_name = os.getenv("DEVICE_NAME") or device_sn
    ecoflow_username = os.getenv("ECOFLOW_USERNAME")
    ecoflow_password = os.getenv("ECOFLOW_PASSWORD")
    try:
        exporter_port = int(os.getenv("EXPORTER_PORT", "9090"))
        collecting_interval_seconds = int(os.getenv("COLLECTING_INTERVAL", "10"))
        offline_timeout = int(os.getenv("OFFLINE_TIMEOUT", "120"))
        if not 1 <= exporter_port <= 65535 or collecting_interval_seconds <= 0 or offline_timeout <= 0:
            raise ValueError
    except ValueError:
        log.error("EXPORTER_PORT must be 1..65535 and COLLECTING_INTERVAL/OFFLINE_TIMEOUT positive integers")
        sys.exit(1)

    if (not device_sn or not ecoflow_username or not ecoflow_password):
        log.error("Please, provide all required environment variables: DEVICE_SN, ECOFLOW_USERNAME, ECOFLOW_PASSWORD")
        sys.exit(1)

    try:
        auth = EcoflowAuthentication(ecoflow_username, ecoflow_password)
    except Exception as error:
        log.error(error)
        sys.exit(1)

    message_queue = Queue()

    connection = EcoflowMQTT(message_queue, device_sn, auth.mqtt_username, auth.mqtt_password, auth.mqtt_url, auth.mqtt_port, auth.mqtt_client_id)

    metrics = Worker(message_queue, device_name, collecting_interval_seconds,
                     device_model=os.getenv("DEVICE_MODEL", "json"), offline_timeout=offline_timeout)

    try:
        start_http_server(exporter_port)
        metrics.loop()
    finally:
        connection.close()


if __name__ == '__main__':
    main()
