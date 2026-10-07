import unittest

from radmin_viewer.input import InputEncoder
from radmin_viewer.desktop import parse_tlvs, DesktopCompression


class InputTests(unittest.TestCase):
    def test_native_ctrl_escape_fixtures(self):
        e = InputEncoder(1024, 768)
        # Literal native shortcut values at Viewer 0x147a578..0x147a5c8.
        self.assertEqual(e.key(0x11, True, scancode=0x1d).hex(), '70000006001101001d00')
        self.assertEqual(e.key(0x1b, True, scancode=1).hex(), '70000006001b01000100')
        self.assertEqual(e.key(0x1b, False).hex(), '70000006021b010001c0')
        self.assertEqual(e.key(0x11, False).hex(), '70000006021101001dc0')
        self.assertEqual(e.release_all(), b'')

    def test_pointer_pixel_fixture_and_buttons(self):
        e = InputEncoder(1024, 768)
        self.assertEqual(e.move(24,748).hex(), '700000050b1800ec02')
        self.assertEqual(e.button('left',True,24,748).hex(), '7000000a0b1800ec02081800ec02')
        self.assertEqual(e.button('left',False,24,748).hex(), '7000000a0b1800ec02071800ec02')
        for name, down, up in [('right',6,5), ('middle',10,9)]:
            self.assertEqual(e.button(name, True, 0, 0)[9], down)
            self.assertEqual(e.button(name, False, 0, 0)[9], up)

    def test_wheel_and_extra_buttons(self):
        e = InputEncoder(1024,768)
        self.assertEqual(e.wheel(-120,24,748).hex(), '7000000c0b1800ec020f88ff1800ec02')
        self.assertEqual(e.button('x2',True,24,748).hex(), '7000000c0b1800ec021102001800ec02')
        self.assertEqual(e.release_all().hex(), '700000071002001800ec02')

    def test_release_lifecycle_and_repeat(self):
        e = InputEncoder(100,100)
        e.key(0x11,True,extended=True)
        e.key(0x11,True)
        self.assertEqual(e.key(0x11,True), b'')
        self.assertEqual(e.key(0x11,True,repeat=True).hex(), '70000006001101000040')
        e.button('left',True,25,30)
        self.assertEqual(e.release_all().hex(), '700000110211000000000211000000010719001e00')
        self.assertEqual(e.release_all(), b'')
        self.assertEqual(e.key(0x11,False), b'')
        self.assertEqual(e.button('left',False,25,30), b'')

    def test_system_keys_preserve_release_identity(self):
        e = InputEncoder(10,10)
        self.assertEqual(e.key(0x12,True,system=True,alt=True,scancode=0x38).hex(),
                         '70000006011201003820')
        self.assertEqual(e.key(0x12,False).hex(), '700000060312010038c0')

    def test_bounds_and_validation_no_stuck_state(self):
        e = InputEncoder(1024,768)
        self.assertEqual(e.move(-10,9999).hex(), '700000050b0000ff02')
        for call in (lambda:e.key(256,True), lambda:e.key(65,1),
                     lambda:e.key(65,True,repeat=True), lambda:e.wheel(32768,0,0),
                     lambda:e.button('bogus',True,0,0), lambda:e.move(1.0,0),
                     lambda:e.resize(0,768)):
            with self.assertRaises(ValueError): call()
        self.assertEqual(e.release_all(), b'')
        self.assertEqual(e.width,1024)

    def test_shared_stream_compression(self):
        e = InputEncoder(1024,768)
        sender, receiver = DesktopCompression(), DesktopCompression()
        for payload in (e.move(5,6),e.key(65,True),e.release_all()):
            wire = sender.encode(payload)
            self.assertEqual(receiver.decode(wire),payload)
            self.assertEqual(set(parse_tlvs(payload)), {0x70000000})


if __name__ == '__main__':
    unittest.main()
