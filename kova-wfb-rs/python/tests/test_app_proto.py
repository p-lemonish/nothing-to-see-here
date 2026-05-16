import pytest

from wfb_rs_py.app_proto import (
    AppFrameError,
    HEADER_SIZE,
    MSG_HELLO,
    MSG_ROUTE_DATA,
    MSG_STATUS,
    MSG_SYNC,
    SYNC_PAYLOAD_SIZE,
    ROUTE_DATA_PAYLOAD_SIZE,
    VERSION,
    decode_frame,
    decode_route_data_payload,
    decode_sync_payload,
    encode_frame,
    encode_route_data_payload,
    encode_sync_payload,
    message_type_name,
    message_type_value,
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
