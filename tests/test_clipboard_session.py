import struct
import unittest

from radmin_viewer.clipboard import ClipboardFormatError, encode_unicode_text
from radmin_viewer.clipboard_session import (
    ClipboardSession, encode_receive_request, encode_send_text,
    split_clipboard_message,
)
from radmin_viewer.desktop import DesktopCompression, DesktopDecoder, encode_tlv
from radmin_viewer.protocol import ProtocolError


class Transport:
    mode = "control"

    def __init__(self):
        self.messages = []

    def send_message(self, data):
        self.messages.append(data)


class ClipboardSessionTests(unittest.TestCase):
    def test_native_literal_wire_fixtures(self):
        # Viewer serializer: outer packed header; inner BE length/type/UTF-16BE.
        self.assertEqual(encode_send_text("A🙂"), bytes.fromhex(
            "60000010 00000008 20000000 0041 d83d de42 0000"))
        self.assertEqual(encode_receive_request(), bytes.fromhex("70000001 13"))
        self.assertEqual(encode_send_text(""), bytes.fromhex(
            "6000000a 00000002 20000000 0000"))

    def test_mixed_image_and_clipboard_shared_compression(self):
        tx, rx = DesktopCompression(), DesktopCompression()
        decoder = DesktopDecoder()
        transport = Transport()
        clipboard = ClipboardSession(transport)
        clipboard.request_receive()
        image = (encode_tlv(0x10000000, struct.pack(">4I", 24, 0xff0000, 0xff00, 0xff))
                 + encode_tlv(0x30000000, struct.pack(">II", 1, 1))
                 + encode_tlv(0x40000000, b"\x01\x02\x03\x00"))
        message = image + encode_tlv(0x90000000, encode_unicode_text("café 中文 🙂"))
        remaining, update = clipboard.consume_message(rx.decode(tx.encode(message)))
        self.assertEqual(remaining, image)
        self.assertEqual(decoder.decode(remaining).rgb, b"\x03\x02\x01")
        self.assertEqual(update.text, "café 中文 🙂")
        self.assertFalse(clipboard.receive_pending)
        clipboard.request_receive()
        remaining, update = clipboard.consume_message(rx.decode(tx.encode(
            encode_tlv(0x90000000, encode_unicode_text("")))))
        self.assertEqual(remaining, b"")
        self.assertEqual(update.text, "")

    def test_no_response_unsolicited_cancel_and_teardown(self):
        transport = Transport()
        session = ClipboardSession(transport)
        reply = encode_tlv(0x90000000, encode_unicode_text("private"))
        self.assertEqual(session.consume_message(reply), (b"", None))
        session.request_receive()
        with self.assertRaises(ProtocolError):
            session.request_receive()
        cursor = encode_tlv(0x70000000, b"\0\0\0\0")
        self.assertEqual(session.consume_message(cursor), (cursor, None))
        self.assertTrue(session.receive_pending)
        self.assertTrue(session.cancel_receive())
        self.assertFalse(session.cancel_receive())
        self.assertEqual(session.consume_message(reply), (b"", None))
        session.request_receive()
        session.close()
        session.close()
        self.assertFalse(session.receive_pending)
        self.assertEqual(session.consume_message(reply), (b"", None))
        with self.assertRaises(ProtocolError):
            session.send_text("x")
        with self.assertRaises(ProtocolError):
            session.request_receive()
        self.assertEqual(transport.messages, [bytes.fromhex("70000001 13")] * 2)

    def test_modes_and_failed_send(self):
        transport = Transport()
        transport.mode = "view"
        session = ClipboardSession(transport)
        for action in (lambda: session.send_text("x"), session.request_receive):
            with self.assertRaises(ProtocolError):
                action()
        self.assertEqual(transport.messages, [])
        transport.mode = "control"
        session.send_text("x")
        self.assertEqual(transport.messages, [encode_send_text("x")])
        def fail(data):
            raise ProtocolError("transport failed")
        transport.send_message = fail
        with self.assertRaises(ProtocolError):
            session.request_receive()
        self.assertFalse(session.receive_pending)

    def test_unknown_ansi_duplicate_and_first_unicode(self):
        ansi = bytes.fromhex("00000002 10000000 4100")
        unknown = bytes.fromhex("00000001 f0000000 aa")
        raw = ansi + unknown + encode_unicode_text("first") + encode_unicode_text("second")
        other = bytes.fromhex("b0000001 ff")
        remainder, update = split_clipboard_message(
            other + encode_tlv(0x90000000, raw))
        self.assertEqual(remainder, other)
        self.assertEqual(update.payload, raw)
        self.assertEqual(len(update.records), 4)
        self.assertEqual(update.text, "first")
        _, update = split_clipboard_message(encode_tlv(0x90000000, ansi))
        self.assertIsNone(update.text)

    def test_reject_malformed_before_delivery(self):
        session = ClipboardSession(Transport())
        session.request_receive()
        raw = encode_unicode_text("hello")
        for length in range(1, len(raw)):
            with self.subTest(length=length), self.assertRaises(ProtocolError):
                session.consume_message(encode_tlv(0x90000000, raw[:length]))
        for malformed in (bytes.fromhex("90000000"), bytes.fromhex("90000002 00"),
                          bytes.fromhex("9000000a 00000002 20000000 0041"),
                          encode_tlv(0x90000000, raw) * 2):
            with self.assertRaises(ProtocolError):
                session.consume_message(malformed)
        self.assertTrue(session.receive_pending)
        for invalid in ("bad\0text", "\ud800"):
            with self.assertRaises(ClipboardFormatError):
                encode_send_text(invalid)


if __name__ == "__main__":
    unittest.main()
