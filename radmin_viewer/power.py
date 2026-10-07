"""One-shot remote power requests. Evidence scope: docs/protocol-notes.md.

Use a fresh, started EncryptedChannel. execute selects the mode and closes the
channel on every outcome. Only the executing thread performs I/O/teardown;
other threads cancel by setting an Event. Never automatically retry UNKNOWN.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
import math
import struct
import threading
import time
from types import MappingProxyType

from .auxiliary import ShutdownAction, encode_shutdown
from .protocol import ProtocolError, RadminError, RadminTimeout


class PowerAction(str, Enum):
    RESTART = "restart"
    SHUTDOWN = "shutdown"
    POWER_OFF = "power_off"
    SLEEP = "sleep"
    HIBERNATE = "hibernate"


class PowerMethod(str, Enum):
    NATIVE = "native_mode11"
    SHELL = "telnet_powershell"


@dataclass(frozen=True)
class PowerDescriptor:
    action: PowerAction
    label: str
    method: PowerMethod
    permission: str
    confirmation: str
    may_force_close: bool


_FORCE = "May forcibly close programs and lose unsaved work. Disconnects remote sessions."
_SUSPEND = "Requires Windows power-state support and shutdown privilege. Disconnects remote sessions; waking remotely is not guaranteed."
POWER_DESCRIPTORS = MappingProxyType({
    action: PowerDescriptor(action, label, method, permission, warning, force)
    for action, label, method, permission, warning, force in (
        (PowerAction.RESTART, "Restart", PowerMethod.NATIVE, "Radmin Shutdown permission", _FORCE, True),
        (PowerAction.SHUTDOWN, "Shut down", PowerMethod.NATIVE, "Radmin Shutdown permission", _FORCE, True),
        (PowerAction.POWER_OFF, "Power off", PowerMethod.NATIVE, "Radmin Shutdown permission", _FORCE, True),
        (PowerAction.SLEEP, "Sleep", PowerMethod.SHELL, "Radmin Telnet permission; Windows SeShutdownPrivilege; PowerShell/Add-Type", _SUSPEND, False),
        (PowerAction.HIBERNATE, "Hibernate", PowerMethod.SHELL, "Radmin Telnet permission; Windows SeShutdownPrivilege; PowerShell/Add-Type; hibernation already enabled", _SUSPEND, False),
    )
})


class PowerOutcome(str, Enum):
    ACCEPTED = "accepted"  # Receipt/API acceptance, never a measured power state.
    UNKNOWN = "unknown"    # Disruptive send attempted; result not established.
    CANCELLED = "cancelled"  # No disruptive send attempted.
    REJECTED = "rejected"  # Explicit remote error.
    UNAVAILABLE = "unavailable"
    NOT_SENT = "not_sent"  # Setup failed before disruptive send.


class PowerFailure(str, Enum):
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    PROTOCOL = "protocol"
    REMOTE_ERROR = "remote_error"
    UNAVAILABLE = "unavailable"
    PERMISSION = "permission"


@dataclass(frozen=True)
class PowerResult:
    action: PowerAction
    method: PowerMethod
    outcome: PowerOutcome
    dispatched: bool
    detail: str
    failure: PowerFailure | None = None
    remote_code: int | None = None


class _Cancelled(RadminError):
    pass


class _RemoteError(RadminError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Remote error 0x{code:08x}")


def _check_remote_error(reply: bytes) -> None:
    """2f + packed TLVs; tag 10000000 contains a BE32 error code."""
    if not reply.startswith(b"\x2f"):
        return
    pos, code = 1, None
    while pos < len(reply):
        if len(reply) - pos < 4:
            raise ProtocolError("Truncated remote error record")
        header = struct.unpack_from(">I", reply, pos)[0]
        tag, size = header & 0xf8000000, header & 0x07ffffff
        pos += 4
        if not tag or not size or size > len(reply) - pos:
            raise ProtocolError("Malformed remote error record")
        if tag == 0x10000000:
            if size != 4 or code is not None:
                raise ProtocolError("Malformed or duplicate remote error code")
            code = struct.unpack_from(">I", reply, pos)[0]
        pos += size
    if code is None:
        raise ProtocolError("Missing remote error code")
    raise _RemoteError(code)


# Fixed source only: no host/user text or arbitrary script input. BOOLEAN is one
# byte, BOOL four bytes. Enable only the calling process's existing privilege;
# AdjustTokenPrivileges success alone is insufficient (ERROR_NOT_ALL_ASSIGNED).
_CS = r'''using System;
using System.Runtime.InteropServices;
public class P {
[DllImport("powrprof.dll")] public static extern byte IsPwrSuspendAllowed();
[DllImport("powrprof.dll")] public static extern byte IsPwrHibernateAllowed();
[DllImport("powrprof.dll",SetLastError=true)] public static extern byte SetSuspendState(byte h,byte f,byte w);
[StructLayout(LayoutKind.Sequential,Pack=4)] struct TP { public uint count; public long luid; public uint attr; }
[DllImport("advapi32.dll",SetLastError=true)] static extern bool OpenProcessToken(IntPtr p,uint a,out IntPtr t);
[DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool LookupPrivilegeValue(string s,string n,out long l);
[DllImport("advapi32.dll",SetLastError=true)] static extern bool AdjustTokenPrivileges(IntPtr t,bool d,ref TP p,uint n,IntPtr o,IntPtr r);
[DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
public static int Enable() {
IntPtr t; if(!OpenProcessToken(new IntPtr(-1),40,out t)) return Marshal.GetLastWin32Error();
try { TP p=new TP(); p.count=1; p.attr=2;
if(!LookupPrivilegeValue(null,"SeShutdownPrivilege",out p.luid)) return Marshal.GetLastWin32Error();
bool ok=AdjustTokenPrivileges(t,false,ref p,0,IntPtr.Zero,IntPtr.Zero);
int e=Marshal.GetLastWin32Error(); return ok ? e : (e==0 ? 1 : e);
} finally { CloseHandle(t); }
}
}'''
# TOKEN_PRIVILEGES has a DWORD followed by a DWORD-aligned LUID, not an
# eight-byte-aligned CLR long. Pack=4 is necessary on both x86 and x64.


def _program(hibernate: bool, *, dispatch: bool) -> str:
    # The only branches select fixed literals; no caller text is interpolated.
    capability = "IsPwrHibernateAllowed" if hibernate else "IsPwrSuspendAllowed"
    h = "1" if hibernate else "0"
    prefix = "$ErrorActionPreference='Stop';try { Add-Type -TypeDefinition @'\n" + _CS + "\n'@;\n"
    checks = ("if([P]::" + capability + "() -eq 0){[Console]::WriteLine('REA_POWER_V1:UNAVAILABLE');exit};"
              "$e=[P]::Enable();if($e -ne 0){[Console]::WriteLine('REA_POWER_V1:PERMISSION:'+$e);exit};")
    if dispatch:
        tail = ("}catch{[Console]::WriteLine('REA_POWER_V1:UNAVAILABLE');exit};try{"
                "$r=[P]::SetSuspendState(" + h + ",0,0);"
                "$e=[Runtime.InteropServices.Marshal]::GetLastWin32Error();"
                "if($r -eq 0){[Console]::WriteLine('REA_POWER_V1:ERROR:'+$e)}"
                "else{[Console]::WriteLine('REA_POWER_V1:ACCEPTED')};")
    else:
        tail = "[Console]::WriteLine('REA_POWER_V1:READY');"
    catch = "UNKNOWN" if dispatch else "UNAVAILABLE"
    return prefix + checks + tail + "}catch{[Console]::WriteLine('REA_POWER_V1:" + catch + "')}"


def _command(hibernate: bool, *, dispatch: bool) -> bytes:
    encoded = base64.b64encode(_program(hibernate, dispatch=dispatch).encode("utf-16-le"))
    command = (b'"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
               b'-NoLogo -NoProfile -NonInteractive -EncodedCommand ' + encoded + b"\r\n")
    # cmd.exe limit is below TelnetClient's 64 KiB limit. No chunked commands:
    # a partial write must never be treated as successful command completion.
    if len(command) > 8000:
        raise ValueError("Fixed power program exceeds command-shell limit")
    return command


class PowerClient:
    """Single-use synchronous owner. execute(action, cancel=None) -> PowerResult.

    action must be PowerAction (use PowerAction(ui_key) at the UI boundary).
    Invalid arguments, reuse and overlapping calls raise ValueError/RadminError;
    expected operation failures return typed results. No retry or reconnection.
    Cancellation after attempting an action send always returns UNKNOWN.
    """
    MAX_OUTPUT = 1024 * 1024

    def __init__(self, channel, *, operation_timeout=20.0, io_timeout=2.0):
        if any(not math.isfinite(t) or t <= 0 for t in (operation_timeout, io_timeout)):
            raise ValueError("Timeouts must be positive and finite")
        self.channel = channel
        self.operation_timeout = float(operation_timeout)
        self.io_timeout = float(io_timeout)
        self._lock = threading.Lock()
        self._used = False
        self._dispatched = False
        self._cancel = None
        self._deadline = 0.0

    def _check(self):
        if self._cancel is not None and self._cancel.is_set():
            raise _Cancelled("Power operation cancelled")
        if time.monotonic() >= self._deadline:
            raise RadminTimeout("Power operation deadline exceeded")

    def _io(self, method, *args, dispatch=False):
        self._check()
        session = self.channel._session
        previous = session.timeout
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise RadminTimeout("Power operation deadline exceeded")
        session.timeout = min(previous, self.io_timeout, remaining)
        try:
            if dispatch:
                # Linearization boundary. A failed send may have sent partial or
                # all bytes, so it is never safe to report NOT_SENT afterwards.
                self._dispatched = True
            return method(*args)
        finally:
            session.timeout = previous

    def _exchange(self, payload, *, dispatch=False):
        self._io(self.channel.send, payload, dispatch=dispatch)
        reply = self._io(self.channel.receive)
        self._check()
        if not reply or len(reply) > self.MAX_OUTPUT + 1:
            raise ProtocolError("Empty or oversized power reply")
        _check_remote_error(reply)
        return reply

    def _expect(self, payload, expected):
        if self._exchange(payload) != expected:
            raise ProtocolError("Power mode/start was not immediately accepted")

    def _shell_program(self, hibernate, *, dispatch):
        data = self._exchange(b"\x14" + _command(hibernate, dispatch=dispatch), dispatch=dispatch)
        output = bytearray()
        total = 0
        while True:
            if data[:1] != b"\x14":
                raise ProtocolError("Invalid command-shell reply")
            output.extend(data[1:])
            total += len(data) - 1
            if total > self.MAX_OUTPUT:
                raise ProtocolError("Command-shell accumulated output exceeds limit")
            # Complete lines only; a marker in encoded command echo cannot match.
            # Preserve partial lines across packets and consume immediate output.
            while b"\n" in output:
                line, _, rest = output.partition(b"\n")
                output = bytearray(rest)
                line = line.rstrip(b"\r")
                if line.startswith(b"REA_POWER_V1:"):
                    token = line[len(b"REA_POWER_V1:"):].decode("ascii", errors="strict")
                    if token in ("READY", "ACCEPTED", "UNAVAILABLE", "UNKNOWN"):
                        if token == ("READY" if dispatch else "ACCEPTED") or (token == "UNKNOWN" and not dispatch):
                            raise ProtocolError("Out-of-phase power marker")
                        return token, None
                    for name in ("PERMISSION", "ERROR"):
                        if token.startswith(name + ":"):
                            if name == "ERROR" and not dispatch:
                                raise ProtocolError("Out-of-phase power error")
                            number = token[len(name) + 1:]
                            if number.isascii() and number.isdecimal() and len(number) <= 10 and int(number) <= 0xffffffff:
                                return name, int(number)
                    raise ProtocolError("Malformed power marker")
            delay = min(.1, max(0, self._deadline - time.monotonic()))
            if self._cancel is None:
                time.sleep(delay)
            else:
                self._cancel.wait(delay)
            self._check()
            data = self._exchange(b"\x14")

    def execute(self, action: PowerAction, cancel=None) -> PowerResult:
        if not isinstance(action, PowerAction):
            raise ValueError("action must be PowerAction")
        if not self._lock.acquire(blocking=False):
            raise RadminError("Power client already has an active operation")
        try:
            if self._used:
                raise RadminError("Power client is single-use; never retry an uncertain action")
            self._used = True
            self._cancel = cancel
            self._deadline = time.monotonic() + self.operation_timeout
            method = POWER_DESCRIPTORS[action].method

            def result(outcome, detail, failure=None, code=None):
                return PowerResult(action, method, outcome, self._dispatched, detail, failure, code)

            try:
                self._check()
                if method == PowerMethod.NATIVE:
                    self._expect(bytes.fromhex("1a0000000b"), b"\x1a")
                    native = {PowerAction.RESTART: ShutdownAction.RESTART,
                              PowerAction.SHUTDOWN: ShutdownAction.SHUT_DOWN,
                              PowerAction.POWER_OFF: ShutdownAction.POWER_OFF}[action]
                    reply = self._exchange(encode_shutdown(native), dispatch=True)
                    if reply != b"\x1e":
                        raise ProtocolError("Unexpected native power acknowledgement")
                    return result(PowerOutcome.ACCEPTED, "Server acknowledged request; Windows result and machine power state are unverified.")
                self._expect(bytes.fromhex("1a00000002"), b"\x1a")
                self._expect(b"\x15", b"\x11")
                self._expect(b"\x12", b"\x12")
                hibernate = action == PowerAction.HIBERNATE
                status, code = self._shell_program(hibernate, dispatch=False)
                if status == "READY":
                    status, code = self._shell_program(hibernate, dispatch=True)
                if status == "ACCEPTED":
                    return result(PowerOutcome.ACCEPTED, "SetSuspendState reported success; current machine power state is unverified.")
                if status == "UNAVAILABLE":
                    return result(PowerOutcome.UNAVAILABLE, "Power state, PowerShell or required API unavailable; no state/configuration change requested.", PowerFailure.UNAVAILABLE)
                if status == "UNKNOWN":
                    return result(PowerOutcome.UNKNOWN if self._dispatched else PowerOutcome.NOT_SENT,
                                  "Power API result unavailable; do not retry.", PowerFailure.REMOTE_ERROR)
                return result(PowerOutcome.REJECTED, "Windows rejected power capability/privilege check or API call.",
                              PowerFailure.PERMISSION if status == "PERMISSION" else PowerFailure.REMOTE_ERROR, code)
            except (RadminError, OSError, UnicodeError) as error:
                cancelled = isinstance(error, _Cancelled) or (cancel is not None and cancel.is_set())
                if cancelled:
                    return result(PowerOutcome.UNKNOWN if self._dispatched else PowerOutcome.CANCELLED,
                                  "Cancelled after possible dispatch; do not retry." if self._dispatched else "Cancelled before power dispatch.", PowerFailure.CANCELLED)
                if isinstance(error, _RemoteError):
                    return result(PowerOutcome.REJECTED, str(error), PowerFailure.REMOTE_ERROR, error.code)
                failure = (PowerFailure.TIMEOUT if isinstance(error, (RadminTimeout, TimeoutError)) else
                           PowerFailure.PROTOCOL if isinstance(error, (ProtocolError, UnicodeError)) else PowerFailure.TRANSPORT)
                return result(PowerOutcome.UNKNOWN if self._dispatched else PowerOutcome.NOT_SENT,
                              "No conclusive acknowledgement; do not retry." if self._dispatched else "Setup failed before power dispatch.", failure)
            finally:
                self._cancel = None
                self.channel.close()
        finally:
            self._lock.release()
