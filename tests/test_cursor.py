import base64
from dataclasses import FrozenInstanceError
import hashlib
import struct
import unittest
import zlib

from radmin_viewer.cursor import (
    CURSOR_POSITION_FLAG, CURSOR_SHAPE_FLAG, DESKTOP_CURSOR_FLAGS,
    CursorCache, CursorShape, composite_cursor, decode_cursor,
)
from radmin_viewer.desktop import encode_tlv
from radmin_viewer.protocol import ProtocolError, UnsupportedProtocolError

H, D = 0x10000000, 0x20000000

# Actual tag-80 values, captured 2026-10-06. These contain only cursor metadata
# and pixels, never screen contents, credentials or session material.
ARROW = zlib.decompress(base64.b64decode(
    "eJzt0r0OAUEUBeB7Z23LkPiJagsvoPIUXtFLUNLoJBqRoBUFUW1iE0TBnmXZiL/CnWnmJLM72eY7"
    "c2cD0g1NOtBENSLy6JHgtuKHpjzTuuKrFlkKfK14YKsD/HMcWx1S31aHrG+jw7NvusMr32SHd76"
    "pDp/80/Eg3uGTj+zCULTDNx/ZbjZiHX7xkdVymXSQ9vdRdN/De16SPuw8UdTvdVO/X86p9r/NV35"
    "q444LzNN05thL+1kb34qKe6PhMJkB9pIzwJ1mbeQ6A5rDX8xmmMFEyn8XnBt28h8wj037Nd+rYw"
    "YlxZ2qr5qmfRcXFxcXF8loolz84gtzCmCr"))
IBEAM = zlib.decompress(base64.b64decode(
    "eJxTYGBUEWBgVBBgYJBgYGBgBGIOIOYEYgUoBhKMDP8HGDBAAeMdJJeRiMF6yQTA8GEBUkwAaHSEIg=="))


def selection(ident):
    return encode_tlv(H, struct.pack(">I", ident))


def frame(kind, width, height, pixels, hotspot=(0, 0), delay=0):
    return encode_tlv(H, encode_tlv(H, struct.pack(">6I", kind, *hotspot, width, height, delay))
                      + encode_tlv(D, pixels))


def definition(*frames, ident=1):
    return encode_tlv(D, b"".join(frames)) + selection(ident)


class CursorTests(unittest.TestCase):
    def test_live_arrow(self):
        self.assertEqual(hashlib.sha256(ARROW).hexdigest(),
                         "ee202bfc3b53329012fc51ea64273e6e5209f88277c1b368bb33dad071a93963")
        s = decode_cursor(ARROW)
        self.assertEqual((s.cache_id, s.kind, s.size, s.hotspot), (1, 3, (32, 32), (0, 0)))
        self.assertEqual(s.rgba[:8], bytes((0, 1, 14, 231, 2, 5, 22, 55)))
        self.assertEqual(s.rgba[8:12], b"\0" * 4)
        result = composite_cursor(bytes((120, 150, 200)) * 1024, 32, 32, s, (0, 0))
        self.assertEqual(result[:3], bytes((11, 15, 31)))
        self.assertEqual(result[6:9], bytes((120, 150, 200)))

    def test_live_ibeam_is_inverting(self):
        self.assertEqual(hashlib.sha256(IBEAM).hexdigest(),
                         "6bf3fba6f0d9956d18278a4d95a86d686d3d445a482113b95ee2af690b5febb6")
        s = decode_cursor(IBEAM)
        self.assertEqual((s.kind, s.size, s.hotspot), (1, (32, 32), (8, 9)))
        self.assertEqual(set(s.and_mask), {255})
        self.assertEqual(s.xor_mask.count(255), 26)
        background = bytes((17, 128, 240)) * 1024
        result = composite_cursor(background, 32, 32, s, s.hotspot)
        for p, value in enumerate(s.xor_mask):
            self.assertEqual(result[p*3:p*3+3], bytes((238, 127, 15)) if value else bytes((17,128,240)))
        self.assertEqual(composite_cursor(result, 32, 32, s, s.hotspot), background)

    def test_all_monochrome_truth_table_cases_and_msb_order(self):
        # Black, white, transparent, invert (AND/XOR = 00,01,10,11).
        s = decode_cursor(definition(frame(1, 4, 1, b"\x30\0\0\0\x50\0\0\0")))
        rgb = bytes((10, 50, 240)) * 4
        self.assertEqual(s.and_mask, b"\0\0\xff\xff")
        self.assertEqual(s.xor_mask, b"\0\xff\0\xff")
        self.assertEqual(composite_cursor(rgb, 4, 1, s, (0, 0)),
                         bytes((0,0,0, 255,255,255, 10,50,240, 245,205,15)))
        self.assertEqual(rgb, bytes((10,50,240)) * 4)

    def test_mask_padding_and_top_down_rows(self):
        s = decode_cursor(definition(frame(1, 9, 2,
            b"\x00\x80\xff\xff\x80\x00\xff\xff" + b"\0" * 8)))
        self.assertEqual(s.and_mask, bytes([0]*8+[255,255]+[0]*8))
        self.assertEqual(s.xor_mask, b"\0" * 18)

    def test_color_xor_and_bgr_padding(self):
        # 3 pixels/row => 12-byte color stride. Mask rows are 4 bytes.
        pixels = (b"\x60\0\0\0\xa0\0\0\0"
                  + bytes((3,2,1, 0,0,0, 255,0,170, 99,99,99))
                  + bytes((1,2,3, 6,5,4, 9,8,7, 99,99,99)))
        s = decode_cursor(definition(frame(2, 3, 2, pixels)))
        self.assertEqual(s.rgb, bytes((1,2,3, 0,0,0, 170,0,255, 3,2,1, 4,5,6, 7,8,9)))
        b = bytes((16,32,64)) * 6
        self.assertEqual(composite_cursor(b,3,2,s,(0,0)),
                         bytes((1,2,3, 16,32,64, 186,32,191, 19,34,65, 4,5,6, 23,40,73)))

    def test_straight_alpha_exact_floor(self):
        s = decode_cursor(definition(frame(3, 3, 1, bytes((30,60,90,0, 30,60,90,128, 30,60,90,255)))))
        self.assertEqual(composite_cursor(bytes((11,22,33))*3,3,1,s,(0,0)),
                         bytes((11,22,33, 50,41,31, 90,60,30)))
        # Alpha zero stays transparent, including entirely zero-alpha bitmaps.
        s = decode_cursor(definition(frame(3,1,1,bytes((255,255,255,0)))))
        self.assertEqual(composite_cursor(b"abc",1,1,s,(0,0)),b"abc")

    def test_hotspot_clipping_all_edges(self):
        rgba = bytes(c for i in range(1,10) for c in (i,i,i,255))
        s = decode_cursor(definition(frame(3,3,3,rgba,(1,1))))
        background = b"\0" * 12
        expected = {(0,0):(5,6,8,9), (1,0):(4,5,7,8),
                    (0,1):(2,3,5,6), (1,1):(1,2,4,5)}
        for position, pixels in expected.items():
            with self.subTest(position=position):
                self.assertEqual(composite_cursor(background,2,2,s,position),
                                 bytes(c for p in pixels for c in (p,p,p)))
        for position in ((-100,0),(0,-100),(100,0),(0,100)):
            self.assertEqual(composite_cursor(background,2,2,s,position),background)

    def test_cache_selection_replacement_and_reset(self):
        cache = CursorCache()
        arrow = cache.decode(ARROW)
        beam = cache.decode(IBEAM)
        self.assertIs(cache.decode(selection(1)),arrow)
        self.assertIs(cache.decode(selection(2)),beam)
        self.assertTrue(decode_cursor(selection(1)).is_reference)
        self.assertFalse(cache.decode(selection(0)).visible)
        self.assertFalse(cache.decode(selection(999)).visible)
        new = cache.decode(definition(frame(1,1,1,b"\0"*8),ident=1))
        self.assertIs(cache.decode(selection(1)),new)
        with self.assertRaises(ProtocolError):
            cache.decode(ARROW[:-1])
        self.assertIs(cache.decode(selection(1)),new)
        cache.clear()
        self.assertFalse(cache.decode(selection(1)).visible)

    def test_reference_not_painted_as_pixels(self):
        with self.assertRaises(ProtocolError):
            composite_cursor(b"abc",1,1,decode_cursor(selection(1)),(0,0))
        for s in (None, CursorShape(0)):
            self.assertEqual(composite_cursor(b"abc",1,1,s,(0,0)),b"abc")
        self.assertEqual(composite_cursor(b"abc",1,1,decode_cursor(ARROW),None),b"abc")
        with self.assertRaises(ValueError):
            composite_cursor(b"abc",2,1,None,None)

    def test_animation_repeated_tags_and_delays(self):
        s = decode_cursor(definition(frame(3,1,1,b"\0\0\xff\xff",delay=20),
                                     frame(3,1,1,b"\0\xff\0\xff",delay=30)))
        self.assertEqual(len(s.frames),1)
        for t,color in ((0,b"\xff\0\0"),(19,b"\xff\0\0"),(20,b"\0\xff\0"),
                        (49,b"\0\xff\0"),(50,b"\xff\0\0")):
            self.assertEqual(composite_cursor(b"abc",1,1,s,(0,0),elapsed_ms=t),color)
        with self.assertRaises(ValueError):
            s.frame_at(-1)

    def test_immutable(self):
        s = decode_cursor(bytearray(IBEAM))
        with self.assertRaises(FrozenInstanceError):
            s.width = 10
        with self.assertRaises(TypeError):
            s.and_mask[0] = 0
        with self.assertRaises(ValueError):
            CursorShape(1,1,1,(0,0),1,b"\x01",b"\0")

    def test_malformed_and_unsupported(self):
        cases = [b"", b"\x10", b"\x10\0\0\0", b"\0\0\0\x01a",
                 selection(1)+selection(2), encode_tlv(H,b"\0"),
                 definition(frame(1,1,1,b"\0"*7)),
                 definition(frame(1,1,1,b"\0"*9)),
                 definition(frame(1,0,1,b"\0"*8)),
                 definition(frame(1,1,1,b"\0"*8,hotspot=(1,0))),
                 definition(frame(1,0xffffffff,1,b"\0"*8)),
                 encode_tlv(D,encode_tlv(0x30000000,b"a"))+selection(1)]
        for data in cases:
            with self.subTest(data=data[:32]), self.assertRaises(ProtocolError):
                decode_cursor(data)
        for data in (definition(frame(4,1,1,b"\0"*8)), encode_tlv(0x30000000,b"a")):
            with self.assertRaises(UnsupportedProtocolError):
                decode_cursor(data)
        for end in range(1,len(IBEAM)):
            # The complete data TLV without the optional ID is itself valid (ID 0).
            if end == len(IBEAM)-8:
                continue
            with self.subTest(truncated_at=end), self.assertRaises(ProtocolError):
                decode_cursor(IBEAM[:end])

    def test_resource_limits(self):
        with self.assertRaises(ProtocolError):
            decode_cursor(definition(*([frame(1,1,1,b"\0"*8)]*257)))
        cache = CursorCache()
        for i in range(256):
            cache.decode(definition(frame(1,1,1,b"\0"*8),ident=i))
        with self.assertRaises(ProtocolError):
            cache.decode(definition(frame(1,1,1,b"\0"*8),ident=256))
        self.assertTrue(cache.decode(selection(0)).visible)

    def test_recovered_request_flags(self):
        self.assertEqual((CURSOR_SHAPE_FLAG,CURSOR_POSITION_FLAG,DESKTOP_CURSOR_FLAGS),(4,8,13))


if __name__ == "__main__":
    unittest.main()
