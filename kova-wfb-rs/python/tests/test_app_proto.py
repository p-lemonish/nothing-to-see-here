import pytest

from wfb_rs_py.app_proto import (
    AppFrameError,
    HEADER_SIZE,
    MSG_HELLO,
    VERSION,
    decode_frame,
    encode_frame,
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
