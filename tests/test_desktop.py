"""Record fixtures come from emulating the recovered native padding routine."""
import struct
import socket
import threading
import select
import unittest
from unittest.mock import patch
import zlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from radmin_viewer.channel import EncryptedChannel, MAX_RECORD, _pad, _unpad
from radmin_viewer.desktop import (
    DesktopCompression, DesktopDecoder, DesktopFrame, DesktopStream,
    MAX_DESKTOP_MESSAGE, encode_tlv, parse_tlvs,
)
from radmin_viewer.protocol import ProtocolError, TransportError, UnsupportedProtocolError


class Socket:
    def __init__(self, incoming):
        self.incoming = bytearray(incoming)
        self.sent = bytearray()

    def settimeout(self, timeout):
        assert timeout > 0

    def recv(self, length):
        result = self.incoming[:min(length, 3)]
        del self.incoming[:len(result)]
        return bytes(result)

    def sendall(self, data):
        self.sent.extend(data)


class Session:
    authenticated = True
    timeout = 5

    def __init__(self, incoming):
        self._session_key = bytes(range(40))
        self._socket = Socket(incoming)

    def close(self):
        self._socket = self._session_key = None
        self.authenticated = False


def frame(data):
    return struct.pack('>I', len(data)) + data


class Records(unittest.TestCase):
    def test_native_padding_fixtures(self):
        fixtures = {
            '00': '00ccccccccccccc508283e781d39ee0f',
            '001f3e5d7c': '001f3e5d7cccccb4932804e1aa3bb40b',
            '001f3e5d7c9bbad9':
                '001f3e5d7c9bbad9ccccccccccccccccccccccccccccccc0faa7475889eb7d18',
        }
        for plain, encoded in fixtures.items():
            with self.subTest(plain=plain):
                self.assertEqual(_pad(bytes.fromhex(plain)), bytes.fromhex(encoded))
                self.assertEqual(_unpad(bytes.fromhex(encoded)), bytes.fromhex(plain))

    def test_corruption_and_length_rejected(self):
        valid = bytes.fromhex('00ccccccccccccc508283e781d39ee0f')
        for invalid in (b'', valid[:-1], valid[:-1] + b'\x08',
                        valid[:-1] + b'\x19', b'\x01' + valid[1:]):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProtocolError):
                    _unpad(invalid)

    def test_directional_iv_continuity_and_fragmented_transport(self):
        ivs = bytes(range(32, 64))
        server_encrypt = Cipher(algorithms.AES(bytes(range(32))), modes.CBC(ivs[:16])).encryptor()
        fixture = bytes.fromhex('00ccccccccccccc508283e781d39ee0f')
        wire = frame(b'\x2e' + ivs)
        wire += frame(server_encrypt.update(fixture)) + frame(server_encrypt.update(fixture))
        session = Session(wire)
        sock = session._socket
        channel = EncryptedChannel(session).start()
        self.assertEqual(channel.receive(), b'\0')
        self.assertEqual(channel.receive(), b'\0')
        channel.send(b'\0')
        channel.send(b'\0')
        client_cipher = Cipher(algorithms.AES(bytes(range(32))), modes.CBC(ivs[16:])).decryptor()
        self.assertEqual(sock.sent[:5], frame(b'\x2e'))
        self.assertEqual(client_cipher.update(bytes(sock.sent[9:25])), fixture)
        self.assertEqual(client_cipher.update(bytes(sock.sent[29:45])), fixture)
        channel.close()
        self.assertFalse(session.authenticated)

    def test_bad_record_closes_session(self):
        for bad in (struct.pack('>I', MAX_RECORD + 1), frame(b'bad'),
                    struct.pack('>I', 16) + bytes(3)):
            session = Session(frame(b'\x2e' + bytes(32)) + bad)
            channel = EncryptedChannel(session).start()
            with self.assertRaises((ProtocolError, TransportError)):
                channel.receive()
            self.assertFalse(session.authenticated)


class DesktopMessages(unittest.TestCase):
    def test_large_tlv_length_uses_27_bits(self):
        value = bytes(0x240000)
        self.assertEqual(parse_tlvs(b'\x40\x24\0\0' + value), {0x40000000: value})
        for bad in (b'\x10\0', b'\x10\0\0\x08abc',
                    bytes.fromhex('10000001011000000102')):
            with self.assertRaises(ProtocolError):
                parse_tlvs(bad)

    def test_persistent_raw_deflate_and_bounded_output(self):
        sender = zlib.compressobj(wbits=-15)
        receiver = DesktopCompression()
        data = b'a long repeated desktop record' * 100
        for _ in range(2):
            wire = frame(data)[:4] + sender.compress(data) + sender.flush(zlib.Z_SYNC_FLUSH)
            self.assertEqual(receiver.decode(wire), data)
        bad = DesktopCompression()
        with self.assertRaises(ProtocolError):
            bad.decode(struct.pack('>I', MAX_DESKTOP_MESSAGE + 1) + b'x')
        with self.assertRaises(ProtocolError):
            bad.decode(wire)
        small = DesktopCompression()
        bomb = zlib.compressobj(wbits=-15)
        with self.assertRaises(ProtocolError):
            small.decode(struct.pack('>I', 1) + bomb.compress(data) + bomb.flush(zlib.Z_SYNC_FLUSH))

    def test_full_frame_bgr_rows_and_png(self):
        # Native wire rows are top-down BGR, with DWORD row alignment.
        fields = encode_tlv(0x10000000, struct.pack('>4I', 24, 0xff0000, 0xff00, 0xff))
        fields += encode_tlv(0x30000000, struct.pack('>II', 1, 2))
        fields += encode_tlv(0x20000000, struct.pack('>I', 1))
        fields += encode_tlv(0x40000000, bytes.fromhex('ff0000000000ff00'))
        decoded = DesktopDecoder().decode(fields)
        self.assertEqual(decoded.rgb, bytes.fromhex('0000ffff0000'))
        png = decoded.to_png()
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack_from('>II', png, 16), (1, 2))
        pos = 8
        while pos < len(png):
            length = struct.unpack_from('>I', png, pos)[0]
            chunk = png[pos + 4:pos + 8 + length]
            self.assertEqual(zlib.crc32(chunk) & 0xffffffff,
                             struct.unpack_from('>I', png, pos + 8 + length)[0])
            pos += length + 12

    def test_unrecognized_pixels_fail_explicitly(self):
        decoder = DesktopDecoder()
        with self.assertRaises(ProtocolError):
            decoder.decode(encode_tlv(0x40000000, b'x'))
        with self.assertRaises(UnsupportedProtocolError):
            decoder.decode(encode_tlv(0x90000000, b'unknown update'))
        descriptor = encode_tlv(0x10000000, struct.pack('>4I', 16, 0xf800, 0x7e0, 0x1f))
        descriptor += encode_tlv(0x30000000, struct.pack('>II', 1, 1))
        with self.assertRaises(UnsupportedProtocolError):
            decoder.decode(descriptor + encode_tlv(0x40000000, bytes(4)))
        with self.assertRaises(ValueError):
            DesktopFrame(0, 0, b'')

    def test_view_only_handshake_and_no_input(self):
        class Channel:
            def __init__(self):
                self.replies = iter([b'\x1a', b'\x32' + bytes.fromhex('2000000400000003'), b'\x28'])
                self.sent = []
            def send(self, data): self.sent.append(data)
            def receive(self): return next(self.replies)
            def close(self): pass
        channel = Channel()
        DesktopStream(channel).start()
        self.assertEqual(channel.sent[:3], [bytes.fromhex('1a00000006'), b'\x32', b'\x28'])
        fields = parse_tlvs(DesktopCompression().decode(channel.sent[3]))
        self.assertEqual(set(fields), {0x10000000, 0x30000000, 0x40000000})
        self.assertEqual(fields[0x40000000], struct.pack('>I', 1))

    @staticmethod
    def initial(width=4, height=3):
        return (encode_tlv(0x10000000, struct.pack('>4I', 24, 0xff0000, 0xff00, 0xff))
                + encode_tlv(0x30000000, struct.pack('>II', width, height))
                + encode_tlv(0x20000000, struct.pack('>I', 1))
                + encode_tlv(0x40000000, bytes(((width * 3 + 3) & ~3) * height)))

    @staticmethod
    def delta(commands, pixels):
        return encode_tlv(0x50000000, commands) + encode_tlv(0x60000000, pixels)

    def test_incremental_spans_preserve_prior_pixels(self):
        d = DesktopDecoder()
        original = d.decode(self.initial())
        # Skip row zero, replace [0,1) and [3,4) in row one, skip row two.
        commands = bytes.fromhex('80000000010003800480')
        first = d.decode(self.delta(commands, bytes.fromhex('0000ffff0000')))
        self.assertEqual(first.rgb[12:15], bytes.fromhex('ff0000'))
        self.assertEqual(first.rgb[21:24], bytes.fromhex('0000ff'))
        self.assertEqual(first.rgb[:12], original.rgb[:12])
        second = d.decode(self.delta(bytes.fromhex('8100018002'), bytes.fromhex('00ff00')))
        self.assertEqual(second.rgb[12:24], first.rgb[12:24])
        self.assertEqual(second.rgb[27:30], bytes.fromhex('00ff00'))
        self.assertEqual(original.rgb, bytes(36))  # returned snapshots do not mutate

    def test_native_long_row_skip_and_exact_delta_extent(self):
        # Native 0x1468260/0x14682e0 use high-bit run counts: FF = 128 rows.
        d = DesktopDecoder()
        d.decode(self.initial(1, 130))
        f = d.decode(self.delta(bytes.fromhex('ff8000008001'), bytes.fromhex('010203')))
        self.assertEqual(f.rgb[-3:], bytes.fromhex('030201'))
        self.assertEqual(f.rgb[:-3], bytes(129 * 3))

    def test_malformed_delta_is_atomic(self):
        d = DesktopDecoder()
        d.decode(self.initial())
        bad_commands = [
            '00008005',             # span outside width
            '83',                   # skip past final row
            '8000008001',           # does not account for final row
            '80000200040001800380', # overlapping spans
            '800000000180',         # skip inside unterminated row
        ]
        for text in bad_commands:
            with self.subTest(text=text), self.assertRaises(ProtocolError):
                d.decode(self.delta(bytes.fromhex(text), bytes(3)))
        with self.assertRaises(ProtocolError):
            d.decode(self.delta(bytes.fromhex('80000080'), bytes(3)))
        with self.assertRaises(ProtocolError):
            d.decode(self.delta(bytes.fromhex('800000800180'), bytes(4)))
        self.assertEqual(d._pixels, bytes(36))
        with self.assertRaises(ProtocolError):
            DesktopDecoder().decode(self.delta(bytes.fromhex('800000800180'), bytes(3)))
        with self.assertRaises(ProtocolError):
            d.decode(encode_tlv(0x50000000, b'\x82'))

    def test_cursor_only_update_retains_framebuffer(self):
        d = DesktopDecoder()
        d.decode(self.initial())
        metadata = encode_tlv(0x70000000, struct.pack('>hh', -1, 50))
        metadata += encode_tlv(0x80000000, encode_tlv(0x10000000, struct.pack('>I', 2)))
        self.assertIsNone(d.decode(metadata))
        self.assertEqual(d.cursor_position, (-1, 50))
        self.assertEqual(d._pixels, bytes(36))
        with self.assertRaises(ProtocolError):
            d.decode(encode_tlv(0x70000000, b'bad'))

    def test_resize_invalidates_delta_base(self):
        d = DesktopDecoder()
        d.decode(self.initial())
        d.decode(encode_tlv(0x30000000, struct.pack('>II', 2, 1)))
        with self.assertRaises(ProtocolError):
            d.decode(self.delta(bytes.fromhex('00008001'), bytes(3)))
        f = d.decode(encode_tlv(0x40000000, bytes.fromhex('0000ff00ff000000')))
        self.assertEqual((f.width, f.height), (2, 1))
        self.assertEqual(f.rgb, bytes.fromhex('ff000000ff00'))

    def test_idle_wait_does_not_close_channel_and_close_cancels(self):
        left, right = socket.socketpair()
        class Channel:
            def __init__(self): self._socket = left; self.closed = False
            def receive(self): raise AssertionError('Idle socket must not be read')
            def close(self): self.closed = True; left.close()
        channel = Channel()
        stream = DesktopStream(channel)
        stream._started = True
        try:
            self.assertIsNone(stream.next_frame(wait_timeout=0.01))
            self.assertFalse(channel.closed)
            result = []
            thread = threading.Thread(target=lambda: result.append(stream.next_frame()))
            waiting = threading.Event()
            real_select = select.select
            def ready_select(*args):
                waiting.set()
                return real_select(*args)
            with patch('radmin_viewer.desktop.select.select', side_effect=ready_select):
                thread.start()
                self.assertTrue(waiting.wait(1))
                stream.close()
                thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [None])
        finally:
            left.close(); right.close()

    def test_control_mode_and_input_guard(self):
        class Channel:
            def __init__(self):
                self.sent = []
                self.replies = iter([b'\x1a', b'\x32' + bytes.fromhex('2000000400000003'), b'\x28'])
            def send(self, data): self.sent.append(data)
            def receive(self): return next(self.replies)
            def close(self): pass
        channel = Channel()
        stream = DesktopStream(channel, mode='control').start()
        self.assertEqual(channel.sent[0], bytes.fromhex('1a00000001'))
        stream.send_message(encode_tlv(0x70000000, b'\x13'))
        sent = len(channel.sent)
        stream.send_message(b'')
        self.assertEqual(len(channel.sent), sent)
        receiver = DesktopCompression()
        receiver.decode(channel.sent[3])
        self.assertEqual(receiver.decode(channel.sent[4]), encode_tlv(0x70000000, b'\x13'))
        viewer = DesktopStream(Channel()).start()
        with self.assertRaises(ProtocolError):
            viewer.send_message(encode_tlv(0x70000000, b'\x13'))
        with self.assertRaises(ValueError):
            DesktopStream(Channel(), mode='invalid')

    def test_close_interrupts_partial_record_read(self):
        left, right = socket.socketpair()
        reading = threading.Event()
        class Channel:
            _socket = left
            def receive(self):
                left.recv(1)
                reading.set()
                if not left.recv(16):
                    raise TransportError('closed during record')
            def close(self): left.close()
        stream = DesktopStream(Channel())
        stream._started = True
        result = []
        worker = threading.Thread(target=lambda: result.append(stream.next_frame()))
        try:
            right.sendall(b'x')
            worker.start()
            self.assertTrue(reading.wait(1))
            stream.close()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [None])
        finally:
            left.close(); right.close()


if __name__ == '__main__':
    unittest.main()
