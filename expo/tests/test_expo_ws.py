"""WebSocket framing: what the laptop and the local sim server speak."""

import pytest

from expotool.ws import (OP_CLOSE, OP_CONT, OP_PING, OP_TEXT, FrameDecoder, ProtocolError,
                         accept_key, encode_frame)


def test_accept_key_matches_rfc_example():
    # RFC 6455 section 1.3
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.parametrize("n", [0, 1, 125, 126, 127, 1000, 65535, 65536, 70000])
@pytest.mark.parametrize("mask", [False, True])
def test_roundtrip_every_length_form(n, mask):
    payload = bytes(i % 251 for i in range(n))
    assert FrameDecoder().feed(encode_frame(OP_TEXT, payload, mask=mask)) == [(OP_TEXT, payload)]


def test_masked_frames_are_actually_masked():
    payload = b"START\n" * 10
    frame = encode_frame(OP_TEXT, payload, mask=True)
    assert payload not in frame


def test_decoder_handles_byte_at_a_time_and_back_to_back_frames():
    data = encode_frame(OP_TEXT, b"one", mask=True) + encode_frame(OP_PING, b"", mask=False) \
        + encode_frame(OP_TEXT, b"two" * 100, mask=False)
    dec, out = FrameDecoder(), []
    for b in data:
        out += dec.feed(bytes([b]))
    assert out == [(OP_TEXT, b"one"), (OP_PING, b""), (OP_TEXT, b"two" * 100)]


def test_fragmented_message_is_reassembled_around_a_control_frame():
    data = (encode_frame(OP_TEXT, b"S,1,", mask=False, fin=False)
            + encode_frame(OP_PING, b"x", mask=False)
            + encode_frame(OP_CONT, b"2,3", mask=False, fin=True))
    assert FrameDecoder().feed(data) == [(OP_PING, b"x"), (OP_TEXT, b"S,1,2,3")]


def test_stray_continuation_is_an_error():
    with pytest.raises(ProtocolError):
        FrameDecoder().feed(encode_frame(OP_CONT, b"x", mask=False))


def test_close_frame_is_reported():
    assert FrameDecoder().feed(encode_frame(OP_CLOSE, b"\x03\xe8", mask=True)) == [(OP_CLOSE, b"\x03\xe8")]
