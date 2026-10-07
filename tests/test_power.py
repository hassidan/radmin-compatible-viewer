"""Offline only: disruptive bytes are sent exclusively to in-memory fake peers."""
import base64
import socket
import shutil
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from radmin_viewer.channel import EncryptedChannel
from radmin_viewer.protocol import RadminError, RadminTimeout, TransportError
from radmin_viewer.power import (
    POWER_DESCRIPTORS, PowerAction, PowerClient, PowerFailure, PowerMethod,
    PowerOutcome, _command, _CS,
)


class Peer:
    def __init__(self, replies=(), send_hook=None):
        self.replies = iter(replies)
        self.sent = []
        self.closed = 0
        self._session = SimpleNamespace(timeout=8.0)
        self.timeouts = []
        self.send_hook = send_hook

    def send(self, data):
        self.sent.append(data)
        self.timeouts.append(self._session.timeout)
        if self.send_hook:
            self.send_hook(data)

    def receive(self):
        self.timeouts.append(self._session.timeout)
        reply = next(self.replies)
        if callable(reply):
            return reply()
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def close(self):
        self.closed += 1


def marker(value):
    return b"\x14\r\nREA_POWER_V1:" + value + b"\r\n"


class PowerTests(unittest.TestCase):
    def shell(self, *replies, **kwargs):
        peer = Peer([b"\x1a", b"\x11", b"\x12", *replies])
        return PowerClient(peer, **kwargs), peer

    def test_ui_contract(self):
        self.assertEqual({a.value for a in PowerAction},
                         {"restart", "shutdown", "power_off", "sleep", "hibernate"})
        self.assertEqual(set(POWER_DESCRIPTORS), set(PowerAction))
        for a, d in POWER_DESCRIPTORS.items():
            self.assertEqual(d.action, a)
            self.assertTrue(d.label and d.permission and d.confirmation)
            self.assertEqual(d.may_force_close, d.method == PowerMethod.NATIVE)

    def test_native_golden_packets_and_receipt_only(self):
        for action, packet in [(PowerAction.RESTART, "1e00000006"),
                               (PowerAction.SHUTDOWN, "1e00000005"),
                               (PowerAction.POWER_OFF, "1e0000000c")]:
            with self.subTest(action=action):
                peer = Peer([b"\x1a", b"\x1e"])
                client = PowerClient(peer)
                result = client.execute(action)
                self.assertEqual(peer.sent, [bytes.fromhex("1a0000000b"), bytes.fromhex(packet)])
                self.assertEqual(result.outcome, PowerOutcome.ACCEPTED)
                self.assertTrue(result.dispatched)
                self.assertIn("unverified", result.detail)
                self.assertEqual(peer.closed, 1)
                self.assertEqual(peer._session.timeout, 8)
                self.assertTrue(all(0 < t <= 2 for t in peer.timeouts))
                with self.assertRaises(RadminError):
                    client.execute(action)
                self.assertEqual(len(peer.sent), 2)
                self.assertEqual(peer.closed, 1)

    def test_remote_error_is_tagged_not_bare_integer(self):
        for after in (False, True):
            peer = Peer(([b"\x1a"] if after else []) + [bytes.fromhex("2f1000000400000005")])
            r = PowerClient(peer).execute(PowerAction.RESTART)
            self.assertEqual(r.outcome, PowerOutcome.REJECTED)
            self.assertEqual(r.failure, PowerFailure.REMOTE_ERROR)
            self.assertEqual(r.remote_code, 5)
            self.assertEqual(r.dispatched, after)
            self.assertEqual(len(peer.sent), 2 if after else 1)

    def test_error_extra_tag_and_unknown_code_preserved(self):
        peer = Peer([bytes.fromhex("2f200000017810000004abcd1234")])
        r = PowerClient(peer).execute(PowerAction.RESTART)
        self.assertEqual(r.remote_code, 0xabcd1234)

    def test_malformed_errors_and_unexpected_ack_never_accept(self):
        bad = [b"", b"\x30", b"\x1ex", b"\x2f", bytes.fromhex("2f00000005"),
               bytes.fromhex("2f10000003000005"), bytes.fromhex("2f10000004000000"),
               bytes.fromhex("2f10000004000000051000000400000005"),
               bytes.fromhex("2f100000040000000578"), bytes.fromhex("2f2000000400000005")]
        for reply in bad:
            for after in (False, True):
                with self.subTest(reply=reply, after=after):
                    peer = Peer(([b"\x1a"] if after else []) + [reply])
                    r = PowerClient(peer).execute(PowerAction.RESTART)
                    self.assertEqual(r.outcome, PowerOutcome.UNKNOWN if after else PowerOutcome.NOT_SENT)
                    self.assertEqual(r.failure, PowerFailure.PROTOCOL)
                    self.assertEqual(len(peer.sent), 2 if after else 1)

    def test_lost_ack_is_unknown_not_success_or_retry(self):
        for error in (RadminTimeout("timeout"), TransportError("EOF"), OSError("network")):
            peer = Peer([b"\x1a", error])
            client = PowerClient(peer)
            r = client.execute(PowerAction.POWER_OFF)
            self.assertEqual(r.outcome, PowerOutcome.UNKNOWN)
            self.assertTrue(r.dispatched)
            with self.assertRaises(RadminError):
                client.execute(PowerAction.POWER_OFF)
            self.assertEqual(len(peer.sent), 2)

    def test_partial_send_failure_is_unknown(self):
        def fail(data):
            if data[0] == 0x1e:
                raise TransportError("sendall partially wrote record")
        peer = Peer([b"\x1a"], fail)
        r = PowerClient(peer).execute(PowerAction.SHUTDOWN)
        self.assertEqual(r.outcome, PowerOutcome.UNKNOWN)
        self.assertTrue(r.dispatched)

    def test_cancel_before_any_io(self):
        event = threading.Event()
        event.set()
        peer = Peer()
        r = PowerClient(peer).execute(PowerAction.SLEEP, event)
        self.assertEqual(r.outcome, PowerOutcome.CANCELLED)
        self.assertEqual(peer.sent, [])
        self.assertEqual(peer.closed, 1)

    def test_cancel_at_mode_ack_blocks_action(self):
        event = threading.Event()
        def ack():
            event.set()
            return b"\x1a"
        peer = Peer([ack])
        r = PowerClient(peer).execute(PowerAction.RESTART, event)
        self.assertEqual(r.outcome, PowerOutcome.CANCELLED)
        self.assertFalse(r.dispatched)
        self.assertEqual(len(peer.sent), 1)

    def test_cancel_during_action_send_or_ack_is_unknown(self):
        for at_send in (False, True):
            event = threading.Event()
            def sent(data):
                if at_send and data[0] == 0x1e:
                    event.set()
            def ack():
                event.set()
                return b"\x1e"
            peer = Peer([b"\x1a", ack], sent)
            r = PowerClient(peer).execute(PowerAction.RESTART, event)
            self.assertEqual(r.outcome, PowerOutcome.UNKNOWN)
            self.assertEqual(r.failure, PowerFailure.CANCELLED)
            self.assertEqual(peer._session.timeout, 8)

    def test_single_owner_rejects_overlap_without_teardown(self):
        entered, release = threading.Event(), threading.Event()
        def ack():
            entered.set()
            self.assertTrue(release.wait(2))
            return b"\x1a"
        peer = Peer([ack, b"\x1e"])
        client = PowerClient(peer)
        results = []
        worker = threading.Thread(target=lambda: results.append(client.execute(PowerAction.RESTART)))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(RadminError):
                client.execute(PowerAction.SLEEP)
            self.assertEqual(peer.closed, 0)
        finally:
            release.set()
            worker.join(2)
        self.assertEqual(results[0].outcome, PowerOutcome.ACCEPTED)

    def test_invalid_input_and_timeout_config(self):
        peer = Peer()
        client = PowerClient(peer)
        for action in ("restart", 6, None, True):
            with self.assertRaises(ValueError):
                client.execute(action)
        for t in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                PowerClient(peer, operation_timeout=t)
        self.assertEqual(peer.sent, [])

    def test_fixed_shell_programs_correct_api_and_no_echo_marker(self):
        for h in (False, True):
            for dispatch in (False, True):
                command = _command(h, dispatch=dispatch)
                self.assertLess(len(command), 8000)
                self.assertNotIn(b"REA_POWER_V1:", command)
                script = base64.b64decode(command.split(b"-EncodedCommand ")[1]).decode("utf-16-le")
                self.assertIn("Pack=4", script)
                self.assertIn("byte SetSuspendState(byte h,byte f,byte w)", script)
                self.assertIn("IsPwrHibernateAllowed()" if h else "IsPwrSuspendAllowed()", script)
                self.assertIn("$e=[P]::Enable()", script)
                self.assertNotIn("rundll32", script.lower())
                self.assertNotIn("powercfg", script.lower())
                self.assertEqual("$r=[P]::SetSuspendState(" in script, dispatch)
                if dispatch:
                    self.assertIn("$r=[P]::SetSuspendState(" + ("1" if h else "0") + ",0,0)", script)

    @unittest.skipUnless(shutil.which("mcs"), "Optional offline C# compiler unavailable")
    def test_pinvoke_source_compiles_without_loading_native_apis(self):
        # Compile only; never execute this assembly or any generated PowerShell.
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Power.cs"
            source.write_text(_CS, encoding="utf-8")
            completed = subprocess.run([shutil.which("mcs"), "-target:library", str(source)],
                                       capture_output=True, text=True, timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_shell_full_sequence_for_both_actions(self):
        for a in (PowerAction.SLEEP, PowerAction.HIBERNATE):
            client, peer = self.shell(marker(b"READY"), marker(b"ACCEPTED"))
            r = client.execute(a)
            self.assertEqual(r.outcome, PowerOutcome.ACCEPTED)
            self.assertEqual(r.method, PowerMethod.SHELL)
            self.assertEqual(peer.sent[:3], [bytes.fromhex("1a00000002"), b"\x15", b"\x12"])
            self.assertEqual(peer.sent[3], b"\x14" + _command(a == PowerAction.HIBERNATE, dispatch=False))
            self.assertEqual(peer.sent[4], b"\x14" + _command(a == PowerAction.HIBERNATE, dispatch=True))
            self.assertEqual(peer.closed, 1)

    def test_unavailable_and_permissions_block_dispatch(self):
        for reply, outcome, failure, code in [(b"UNAVAILABLE", PowerOutcome.UNAVAILABLE, PowerFailure.UNAVAILABLE, None),
                                             (b"PERMISSION:1300", PowerOutcome.REJECTED, PowerFailure.PERMISSION, 1300)]:
            client, peer = self.shell(marker(reply))
            r = client.execute(PowerAction.HIBERNATE)
            self.assertEqual((r.outcome, r.failure, r.remote_code), (outcome, failure, code))
            self.assertFalse(r.dispatched)
            self.assertEqual(len(peer.sent), 4)

    def test_shell_action_errors(self):
        for token, outcome in [(b"ERROR:5", PowerOutcome.REJECTED), (b"PERMISSION:1314", PowerOutcome.REJECTED),
                               (b"UNAVAILABLE", PowerOutcome.UNAVAILABLE), (b"UNKNOWN", PowerOutcome.UNKNOWN)]:
            client, peer = self.shell(marker(b"READY"), marker(token))
            r = client.execute(PowerAction.SLEEP)
            self.assertEqual(r.outcome, outcome)
            self.assertTrue(r.dispatched)
            self.assertEqual(len(peer.sent), 5)

    def test_console_echo_and_partial_line_do_not_complete(self):
        echo = b"\x14C:\\>" + _command(False, dispatch=False)
        client, peer = self.shell(echo, b"\x14REA_POWER_V1:REA", b"\x14DY\r", b"\x14\n",
                                  b"\x14REA_POWER_V1:ACCEPTED", TransportError("EOF"))
        r = client.execute(PowerAction.SLEEP)
        self.assertEqual(r.outcome, PowerOutcome.UNKNOWN)
        self.assertEqual(sum(p.startswith(b"\x14\"") for p in peer.sent), 2)

    def test_shell_partial_command_send_is_unknown(self):
        client, peer = self.shell(marker(b"READY"))
        def fail(data):
            if len(peer.sent) == 5:
                raise RadminTimeout("partial command")
        peer.send_hook = fail
        r = client.execute(PowerAction.SLEEP)
        self.assertEqual(r.outcome, PowerOutcome.UNKNOWN)
        self.assertEqual(len(peer.sent), 5)

    def test_shell_ack_loss_and_cancel_after_dispatch(self):
        for cancelled in (False, True):
            event = threading.Event()
            def fail():
                if cancelled:
                    event.set()
                raise TransportError("lost shell output")
            client, peer = self.shell(marker(b"READY"), fail)
            r = client.execute(PowerAction.HIBERNATE, event)
            self.assertEqual(r.outcome, PowerOutcome.UNKNOWN)
            self.assertEqual(r.failure, PowerFailure.CANCELLED if cancelled else PowerFailure.TRANSPORT)
            self.assertTrue(r.dispatched)
            self.assertEqual(len(peer.sent), 5)

    def test_total_deadline_applies_to_all_shell_phases(self):
        client, peer = self.shell(*([b"\x14"] * 100), operation_timeout=.02)
        started = time.monotonic()
        r = client.execute(PowerAction.SLEEP)
        self.assertEqual((r.outcome, r.failure), (PowerOutcome.NOT_SENT, PowerFailure.TIMEOUT))
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(peer._session.timeout, 8)

    def test_cancel_after_preflight_prevents_disruptive_command(self):
        event = threading.Event()
        def ready():
            event.set()
            return marker(b"READY")
        client, peer = self.shell(ready)
        r = client.execute(PowerAction.SLEEP, event)
        self.assertEqual(r.outcome, PowerOutcome.CANCELLED)
        self.assertEqual(len(peer.sent), 4)

    def test_output_limit_counts_discarded_complete_lines(self):
        client, peer = self.shell(b"\x14" + b"x\n" * 10, b"\x14" + b"x\n" * 10)
        client.MAX_OUTPUT = 30
        r = client.execute(PowerAction.SLEEP)
        self.assertEqual(r.failure, PowerFailure.PROTOCOL)
        self.assertFalse(r.dispatched)

    def test_bad_shell_markers_and_startup(self):
        for value in (b"ACCEPTED", b"PERMISSION:-1", b"ERROR:4294967296", b"ERROR:abc", b"nonsense"):
            client, peer = self.shell(marker(value))
            r = client.execute(PowerAction.SLEEP)
            self.assertEqual(r.failure, PowerFailure.PROTOCOL)
            self.assertFalse(r.dispatched)
        for replies in ([b"\x30"], [b"\x1a", b"\x13"], [b"\x1a", b"\x11", b"\x13"]):
            peer = Peer(replies)
            r = PowerClient(peer).execute(PowerAction.SLEEP)
            self.assertFalse(r.dispatched)
            self.assertEqual(peer.closed, 1)

    def test_real_local_socket_wait_bounded_with_owner_cancellation(self):
        left, right = socket.socketpair()
        session = SimpleNamespace(authenticated=True, _socket=left, _session_key=b"x" * 40, timeout=8.0)
        def close():
            left.close()
            session._socket = None
        session.close = close
        channel = EncryptedChannel(session)
        channel._started = True
        event = threading.Event()
        entered = threading.Event()
        delivered = threading.Event()
        def cancel_during_receive():
            if entered.wait(1):
                event.set()
                delivered.set()
        canceller = threading.Thread(target=cancel_during_receive)
        canceller.start()
        real_receive = channel.receive
        def receive():
            # Synchronize after PowerClient's pre-I/O cancellation check, rather
            # than racing a 20ms Timer against an 80ms socket deadline under load.
            entered.set()
            self.assertTrue(delivered.wait(1))
            return real_receive()
        started = time.monotonic()
        try:
            with patch.object(channel, "send"), patch.object(channel, "receive", side_effect=receive):
                r = PowerClient(channel, io_timeout=.08).execute(PowerAction.RESTART, event)
            self.assertEqual(r.outcome, PowerOutcome.CANCELLED)
            self.assertLess(time.monotonic() - started, .8)
            self.assertIsNone(session._socket)
            self.assertEqual(session.timeout, 8)
        finally:
            entered.set()
            canceller.join()
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
