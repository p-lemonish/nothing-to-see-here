import pytest

from wfb_rs_py.app_proto import (
    ADDR_C2,
    ADDR_NODE,
    AppFrameError,
    CHACHA20_POLY1305_TAG_SIZE,
    HEADER_SIZE,
    MSG_DATA,
    MSG_HELLO,
    MSG_ROUTE_DATA,
    MSG_ROUTE_V2,
    MSG_STATUS,
    MSG_SYNC,
    ROUTE_SECURE_ASSOCIATED_DATA_SIZE,
    ROUTE_V2_E2E_ASSOCIATED_DATA_SIZE,
    ROUTE_V2_PAYLOAD_SIZE,
    SECURE_PAYLOAD_SIZE,
    SEC_DOMAIN_MESH_GROUP,
    SEC_DOMAIN_NODE_TO_C2,
    STATUS_BATTERY_UNKNOWN,
    STATUS_FLAG_CRYPTO_ENABLED,
    STATUS_FLAG_DEGRADED_LINK,
    STATUS_FLAG_FORWARDING_ENABLED,
    STATUS_FLAG_LOW_BATTERY,
    STATUS_PAYLOAD_SIZE,
    STATUS_VERSION,
    SYNC_PAYLOAD_SIZE,
    ROUTE_DATA_PAYLOAD_SIZE,
    TRAFFIC_C2_UPLINK,
    VERSION,
    address_type_name,
    address_type_value,
    decode_frame,
    decode_route_data_payload,
    decode_route_v2_payload,
    decode_secure_payload,
    decode_status_payload,
    decode_sync_payload,
    decrypt_secure_payload,
    encode_frame,
    encode_route_data_payload,
    encode_route_v2_e2e_associated_data,
    encode_route_v2_payload,
    encode_route_secure_associated_data,
    encode_secure_payload,
    encode_status_payload,
    encode_sync_payload,
    message_type_name,
    message_type_value,
    security_domain_name,
    security_domain_value,
    traffic_class_name,
    traffic_class_value,
)


def test_encode_decode_roundtrip():
    encoded = encode_frame(
        sender_id=7,
        message_type="hello",
        app_seq=42,
        payload=b"hello world",
    )

    frame = decode_frame(encoded)

    assert frame.version == VERSION
    assert frame.message_type == MSG_HELLO
    assert frame.message_type_name == "hello"
    assert frame.sender_id == 7
    assert frame.flags == 0
    assert frame.app_seq == 42
    assert frame.payload == b"hello world"


def test_header_size_is_fixed():
    encoded = encode_frame(sender_id=1, message_type="text", app_seq=1, payload=b"")

    assert HEADER_SIZE == 10
    assert len(encoded) == HEADER_SIZE


def test_max_sender_id_and_sequence_are_valid():
    encoded = encode_frame(
        sender_id=255,
        message_type=0x04,
        app_seq=0xFFFF_FFFF,
        payload=b"ok",
        flags=255,
    )

    frame = decode_frame(encoded)

    assert frame.sender_id == 255
    assert frame.app_seq == 0xFFFF_FFFF
    assert frame.flags == 255


def test_sender_id_zero_is_invalid():
    with pytest.raises(AppFrameError, match="sender_id 0"):
        encode_frame(sender_id=0, message_type="hello", app_seq=1, payload=b"")


def test_payload_length_mismatch_is_invalid():
    encoded = encode_frame(sender_id=1, message_type="hello", app_seq=1, payload=b"abc")
    truncated = encoded[:-1]

    with pytest.raises(AppFrameError, match="payload_len mismatch"):
        decode_frame(truncated)


def test_short_frame_is_invalid():
    with pytest.raises(AppFrameError, match="frame too short"):
        decode_frame(b"short")


def test_unknown_message_type_is_rejected_by_default():
    encoded = encode_frame(sender_id=1, message_type=0x99, app_seq=1, payload=b"")

    with pytest.raises(AppFrameError, match="unknown message type"):
        decode_frame(encoded)


def test_unknown_message_type_can_be_allowed():
    encoded = encode_frame(sender_id=1, message_type=0x99, app_seq=1, payload=b"")

    frame = decode_frame(encoded, allow_unknown_message_type=True)

    assert frame.message_type == 0x99
    assert frame.message_type_name == "0x99"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hello", MSG_HELLO),
        ("0x01", MSG_HELLO),
        ("1", MSG_HELLO),
    ],
)
def test_message_type_value(raw, expected):
    assert message_type_value(raw) == expected


def test_message_type_name():
    assert message_type_name(MSG_HELLO) == "hello"
    assert message_type_name(0x99) == "0x99"


def test_encode_decode_route_data_payload():
    encoded = encode_route_data_payload(
        origin_sender_id=42,
        destination_id=0,
        ttl=3,
        origin_seq=500,
        inner_type="status",
        inner_payload=b"battery=91",
    )

    route = decode_route_data_payload(encoded)

    assert len(encoded) == ROUTE_DATA_PAYLOAD_SIZE + len(b"battery=91")
    assert route.origin_sender_id == 42
    assert route.destination_id == 0
    assert route.ttl == 3
    assert route.origin_seq == 500
    assert route.inner_type == MSG_STATUS
    assert route.inner_type_name == "status"
    assert route.inner_payload == b"battery=91"
    assert route.dedupe_key == (42, 500)


def test_route_data_ttl_decrement():
    route = decode_route_data_payload(
        encode_route_data_payload(
            origin_sender_id=42,
            destination_id=7,
            ttl=2,
            origin_seq=10,
            inner_type="data",
            inner_payload=b"x",
        )
    )

    forwarded = route.decremented_ttl()

    assert forwarded.ttl == 1
    assert forwarded.origin_sender_id == route.origin_sender_id
    assert forwarded.destination_id == route.destination_id
    assert forwarded.origin_seq == route.origin_seq
    assert forwarded.inner_type == route.inner_type
    assert forwarded.inner_payload == route.inner_payload


def test_route_data_ttl_decrement_zero_is_invalid():
    route = decode_route_data_payload(
        encode_route_data_payload(
            origin_sender_id=42,
            destination_id=0,
            ttl=0,
            origin_seq=10,
            inner_type="data",
            inner_payload=b"x",
        )
    )

    with pytest.raises(AppFrameError, match="ttl below zero"):
        route.decremented_ttl()


def test_route_data_sender_zero_is_invalid():
    with pytest.raises(AppFrameError, match="origin_sender_id 0"):
        encode_route_data_payload(
            origin_sender_id=0,
            destination_id=0,
            ttl=1,
            origin_seq=1,
            inner_type="status",
            inner_payload=b"",
        )


def test_route_data_short_payload_is_invalid():
    with pytest.raises(AppFrameError, match="payload too short"):
        decode_route_data_payload(b"short")


def test_route_data_inner_length_mismatch_is_invalid():
    encoded = encode_route_data_payload(
        origin_sender_id=1,
        destination_id=0,
        ttl=1,
        origin_seq=1,
        inner_type="status",
        inner_payload=b"abc",
    )

    with pytest.raises(AppFrameError, match="inner_payload_len mismatch"):
        decode_route_data_payload(encoded[:-1])


def test_route_data_cannot_nest_route_data():
    with pytest.raises(AppFrameError, match="nested route_data"):
        encode_route_data_payload(
            origin_sender_id=1,
            destination_id=0,
            ttl=1,
            origin_seq=1,
            inner_type=MSG_ROUTE_DATA,
            inner_payload=b"",
        )


def test_route_data_message_type_is_known():
    assert message_type_value("route_data") == MSG_ROUTE_DATA
    assert message_type_name(MSG_ROUTE_DATA) == "route_data"


def test_encode_decode_route_v2_payload():
    encoded = encode_route_v2_payload(
        origin_type="node",
        origin_id=42,
        destination_type="c2",
        destination_id=1,
        ttl=3,
        origin_seq=500,
        traffic_class="c2_uplink",
        inner_type="data",
        inner_payload=b"opaque",
    )

    route = decode_route_v2_payload(encoded)

    assert len(encoded) == ROUTE_V2_PAYLOAD_SIZE + len(b"opaque")
    assert route.origin_type == ADDR_NODE
    assert route.origin_id == 42
    assert route.destination_type == ADDR_C2
    assert route.destination_id == 1
    assert route.ttl == 3
    assert route.origin_seq == 500
    assert route.traffic_class == TRAFFIC_C2_UPLINK
    assert route.traffic_class_name == "c2_uplink"
    assert route.inner_type == MSG_DATA
    assert route.inner_payload == b"opaque"
    assert route.dedupe_key == (ADDR_NODE, 42, 500)


def test_route_v2_ttl_decrement_keeps_opaque_payload():
    route = decode_route_v2_payload(
        encode_route_v2_payload(
            origin_type="node",
            origin_id=42,
            destination_type="c2",
            destination_id=1,
            ttl=2,
            origin_seq=10,
            traffic_class="c2_uplink",
            inner_type="data",
            inner_payload=b"ciphertext",
        )
    )

    forwarded = route.decremented_ttl()

    assert forwarded.ttl == 1
    assert forwarded.origin_type == route.origin_type
    assert forwarded.origin_id == route.origin_id
    assert forwarded.destination_type == route.destination_type
    assert forwarded.destination_id == route.destination_id
    assert forwarded.origin_seq == route.origin_seq
    assert forwarded.traffic_class == route.traffic_class
    assert forwarded.inner_payload == b"ciphertext"


def test_route_v2_rejects_broadcast_origin():
    with pytest.raises(AppFrameError, match="origin_type broadcast"):
        encode_route_v2_payload(
            origin_type="broadcast",
            origin_id=0,
            destination_type="c2",
            destination_id=1,
            ttl=1,
            origin_seq=1,
            traffic_class="c2_uplink",
            inner_type="data",
            inner_payload=b"",
        )


def test_route_v2_message_type_is_known():
    assert message_type_value("route_v2") == MSG_ROUTE_V2
    assert message_type_name(MSG_ROUTE_V2) == "route_v2"


def test_encode_decode_sync_payload():
    encoded = encode_sync_payload(
        utc_ms=1_789_549_123_456,
        slot=357_909_824,
        channel=36,
        next_hop_ms=4321,
    )

    sync = decode_sync_payload(encoded)

    assert len(encoded) == SYNC_PAYLOAD_SIZE
    assert sync.utc_ms == 1_789_549_123_456
    assert sync.slot == 357_909_824
    assert sync.channel == 36
    assert sync.next_hop_ms == 4321


def test_sync_payload_length_mismatch_is_invalid():
    with pytest.raises(AppFrameError, match="sync payload length mismatch"):
        decode_sync_payload(b"short")


def test_sync_message_type_is_known():
    assert message_type_value("sync") == MSG_SYNC
    assert message_type_name(MSG_SYNC) == "sync"


def test_encode_decode_status_payload():
    encoded = encode_status_payload(
        uptime_s=12_345,
        battery_pct=91,
        peer_count=4,
        flags=(
            STATUS_FLAG_DEGRADED_LINK
            | STATUS_FLAG_LOW_BATTERY
            | STATUS_FLAG_CRYPTO_ENABLED
            | STATUS_FLAG_FORWARDING_ENABLED
        ),
    )
    status = decode_status_payload(encoded)

    assert len(encoded) == STATUS_PAYLOAD_SIZE
    assert status.status_version == STATUS_VERSION
    assert status.uptime_s == 12_345
    assert status.battery_pct == 91
    assert status.peer_count == 4
    assert status.flags == 0x0F
    assert status.degraded_link is True
    assert status.low_battery is True
    assert status.crypto_enabled is True
    assert status.forwarding_enabled is True


def test_status_payload_allows_unknown_battery():
    encoded = encode_status_payload(
        uptime_s=1,
        battery_pct=None,
        peer_count=0,
        flags=STATUS_FLAG_CRYPTO_ENABLED,
    )
    status = decode_status_payload(encoded)

    assert encoded[5] == STATUS_BATTERY_UNKNOWN
    assert status.battery_pct is None
    assert status.crypto_enabled is True


def test_status_payload_length_mismatch_is_invalid():
    with pytest.raises(AppFrameError, match="status payload length mismatch"):
        decode_status_payload(b"short")


def test_status_payload_rejects_invalid_battery_pct():
    with pytest.raises(AppFrameError, match="battery_pct must be in range"):
        encode_status_payload(
            uptime_s=1,
            battery_pct=101,
            peer_count=0,
        )


def test_status_payload_rejects_invalid_encoded_battery_pct():
    tampered = bytearray(
        encode_status_payload(
            uptime_s=1,
            battery_pct=50,
            peer_count=0,
        )
    )
    tampered[5] = 200

    with pytest.raises(AppFrameError, match="status battery_pct must be in range"):
        decode_status_payload(bytes(tampered))


def test_status_payload_rejects_unsupported_version():
    with pytest.raises(AppFrameError, match="unsupported status_version"):
        encode_status_payload(
            uptime_s=1,
            battery_pct=90,
            peer_count=1,
            status_version=2,
        )


def test_encode_route_secure_associated_data():
    aad = encode_route_secure_associated_data(
        origin_sender_id=42,
        destination_id=0,
        ttl=2,
        origin_seq=500,
        inner_type="status",
        inner_plaintext_len=STATUS_PAYLOAD_SIZE,
    )
    assert len(aad) == ROUTE_SECURE_ASSOCIATED_DATA_SIZE


def test_encode_decode_decrypt_secure_payload():
    key = bytes(range(32))
    nonce = bytes(range(12))
    aad = b"route-metadata"

    encoded = encode_secure_payload(
        key=key,
        security_domain="mesh_group",
        key_id=1001,
        key_epoch=7,
        plaintext=b"node 1 online",
        associated_data=aad,
        nonce=nonce,
    )
    secure = decode_secure_payload(encoded)
    plaintext = decrypt_secure_payload(secure, key=key, associated_data=aad)

    assert len(encoded) == SECURE_PAYLOAD_SIZE + len(b"node 1 online") + 16
    assert secure.security_domain == SEC_DOMAIN_MESH_GROUP
    assert secure.security_domain_name == "mesh_group"
    assert secure.key_id == 1001
    assert secure.key_epoch == 7
    assert secure.nonce == nonce
    assert secure.plaintext_len == len(b"node 1 online")
    assert plaintext == b"node 1 online"


def test_secure_payload_authenticates_associated_data():
    key = bytes(range(32))
    encoded = encode_secure_payload(
        key=key,
        security_domain="mesh_group",
        key_id=1001,
        key_epoch=7,
        plaintext=b"node 1 online",
        associated_data=b"good-aad",
        nonce=bytes(range(12)),
    )
    secure = decode_secure_payload(encoded)

    with pytest.raises(AppFrameError, match="authentication failed"):
        decrypt_secure_payload(secure, key=key, associated_data=b"bad-aad")


def test_secure_status_roundtrip_with_route_associated_data():
    key = bytes(range(32))
    nonce = bytes(range(12))
    status_plaintext = encode_status_payload(
        uptime_s=999,
        battery_pct=88,
        peer_count=3,
        flags=STATUS_FLAG_CRYPTO_ENABLED,
    )
    aad = encode_route_secure_associated_data(
        origin_sender_id=7,
        destination_id=0,
        ttl=2,
        origin_seq=100,
        inner_type=MSG_STATUS,
        inner_plaintext_len=len(status_plaintext),
    )

    encoded = encode_secure_payload(
        key=key,
        security_domain="mesh_group",
        key_id=101,
        key_epoch=1,
        plaintext=status_plaintext,
        associated_data=aad,
        nonce=nonce,
    )
    secure = decode_secure_payload(encoded)
    plaintext = decrypt_secure_payload(secure, key=key, associated_data=aad)
    status = decode_status_payload(plaintext)

    assert status.uptime_s == 999
    assert status.battery_pct == 88
    assert status.crypto_enabled is True


def test_secure_status_rejects_tampered_route_associated_data():
    key = bytes(range(32))
    plaintext = encode_status_payload(
        uptime_s=10,
        battery_pct=90,
        peer_count=2,
        flags=STATUS_FLAG_CRYPTO_ENABLED,
    )
    aad = encode_route_secure_associated_data(
        origin_sender_id=1,
        destination_id=0,
        ttl=2,
        origin_seq=10,
        inner_type="status",
        inner_plaintext_len=len(plaintext),
    )
    tampered_aad = encode_route_secure_associated_data(
        origin_sender_id=1,
        destination_id=0,
        ttl=1,
        origin_seq=10,
        inner_type="status",
        inner_plaintext_len=len(plaintext),
    )
    encoded = encode_secure_payload(
        key=key,
        security_domain="mesh_group",
        key_id=1,
        key_epoch=1,
        plaintext=plaintext,
        associated_data=aad,
        nonce=bytes(range(12)),
    )
    secure = decode_secure_payload(encoded)

    with pytest.raises(AppFrameError, match="authentication failed"):
        decrypt_secure_payload(secure, key=key, associated_data=tampered_aad)


def test_node_to_c2_e2e_roundtrip_ignores_mutable_ttl():
    key = bytes(range(32))
    nonce = bytes(range(12))
    plaintext = b"node 42 report"
    aad = encode_route_v2_e2e_associated_data(
        origin_type="node",
        origin_id=42,
        destination_type="c2",
        destination_id=1,
        origin_seq=700,
        traffic_class="c2_uplink",
        inner_type="data",
        inner_plaintext_len=len(plaintext),
    )

    secure_payload = encode_secure_payload(
        key=key,
        security_domain="node_to_c2",
        key_id=4201,
        key_epoch=7,
        plaintext=plaintext,
        associated_data=aad,
        nonce=nonce,
    )
    route = decode_route_v2_payload(
        encode_route_v2_payload(
            origin_type="node",
            origin_id=42,
            destination_type="c2",
            destination_id=1,
            ttl=2,
            origin_seq=700,
            traffic_class="c2_uplink",
            inner_type="data",
            inner_payload=secure_payload,
        )
    )
    forwarded = route.decremented_ttl()
    secure = decode_secure_payload(forwarded.inner_payload)

    assert len(aad) == ROUTE_V2_E2E_ASSOCIATED_DATA_SIZE
    assert secure.security_domain == SEC_DOMAIN_NODE_TO_C2
    assert decrypt_secure_payload(secure, key=key, associated_data=aad) == plaintext


def test_node_to_c2_e2e_rejects_redirected_destination():
    key = bytes(range(32))
    plaintext = b"node 42 report"
    aad = encode_route_v2_e2e_associated_data(
        origin_type="node",
        origin_id=42,
        destination_type="c2",
        destination_id=1,
        origin_seq=700,
        traffic_class="c2_uplink",
        inner_type="data",
        inner_plaintext_len=len(plaintext),
    )
    redirected_aad = encode_route_v2_e2e_associated_data(
        origin_type="node",
        origin_id=42,
        destination_type="c2",
        destination_id=2,
        origin_seq=700,
        traffic_class="c2_uplink",
        inner_type="data",
        inner_plaintext_len=len(plaintext),
    )
    encoded = encode_secure_payload(
        key=key,
        security_domain="node_to_c2",
        key_id=4201,
        key_epoch=7,
        plaintext=plaintext,
        associated_data=aad,
        nonce=bytes(range(12)),
    )
    secure = decode_secure_payload(encoded)

    with pytest.raises(AppFrameError, match="authentication failed"):
        decrypt_secure_payload(secure, key=key, associated_data=redirected_aad)


def test_secure_payload_rejects_short_wrapper():
    with pytest.raises(AppFrameError, match="secure payload too short"):
        decode_secure_payload(b"x" * (SECURE_PAYLOAD_SIZE + CHACHA20_POLY1305_TAG_SIZE - 1))


def test_security_domain_value_and_name():
    assert security_domain_value("mesh_group") == SEC_DOMAIN_MESH_GROUP
    assert security_domain_value("0x01") == SEC_DOMAIN_MESH_GROUP
    assert security_domain_name(SEC_DOMAIN_MESH_GROUP) == "mesh_group"
    assert security_domain_name(0x99) == "0x99"


def test_address_type_and_traffic_class_value_and_name():
    assert address_type_value("node") == ADDR_NODE
    assert address_type_name(ADDR_C2) == "c2"
    assert traffic_class_value("c2_uplink") == TRAFFIC_C2_UPLINK
    assert traffic_class_name(TRAFFIC_C2_UPLINK) == "c2_uplink"
