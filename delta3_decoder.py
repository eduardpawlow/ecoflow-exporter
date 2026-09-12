"""Read-only DELTA 3 Plus telemetry decoder. See THIRD_PARTY_NOTICES.md."""
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError

import delta3_pb2 as proto

MESSAGES = {
    (254, 21): ('display', proto.DisplayPropertyUpload),
    (254, 22): ('runtime', proto.RuntimePropertyUpload),
    (32, 50): ('bms', proto.BMSHeartBeatReport),
    (32, 2): ('cms', proto.CMSHeartBeatReport),
}


def decode_delta3(payload):
    """Return recognized reports; absent protobuf fields remain absent (delta updates)."""
    envelope = proto.HeaderMessage()
    envelope.ParseFromString(payload)
    if not envelope.header:
        raise DecodeError('Missing EcoFlow envelope')
    reports = []
    for header in envelope.header:
        definition = MESSAGES.get((header.cmd_func, header.cmd_id))
        if definition is None or not header.pdata:
            continue
        if header.enc_type not in (0, 1):
            raise DecodeError('Unsupported EcoFlow encoding')
        data = header.pdata
        if header.data_len and header.data_len != len(data):
            raise DecodeError('EcoFlow payload length mismatch')
        if header.enc_type == 1 and header.src != 32:
            key = header.seq & 0xff
            data = bytes(byte ^ key for byte in data)
        name, message_type = definition
        message = message_type()
        message.ParseFromString(data)
        values = MessageToDict(message, preserving_proto_field_name=True, use_integers_for_enums=True)
        if not values:
            continue
        if name == 'bms':
            # Different battery packs must not overwrite one another.
            name += '_' + str(message.num)
        reports.append({name: values})
    return reports
