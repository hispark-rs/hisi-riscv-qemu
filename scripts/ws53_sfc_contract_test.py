#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Independent byte/state contract for the explicit synthetic WS53 SFC peer.

This is NOT a board Flash, IRQ, XIP, DMA, persistence-file, timing or drain test.
Only CMD_CONFIG/INS/ADDR and sixteen DATA words are admitted. The peer's 4 MiB
capacity, 256-byte pages, 4 KiB sectors, JEDEC ID 7f5301, operation latencies,
strict page/alignment rejection, protection and reset rules are synthetic test
assumptions. They must not be presented as GD25LE32E hardware specifications.

The oracle maintains its own whole-array bytes. QOM supplies observability, not
expected data. Rejected STARTs must preserve every exposed state except the
rejection counter. qtest's OK (or an MMIO read returning zero) is never evidence
that an unsupported access was accepted or rejected. Normal and rejected
traffic, stderr, exact executable/source hashes and per-case results are saved.

Use --self-test for host-only checks of this test harness. That mode never
executes QEMU and cannot establish model behavior. The parent task builds QEMU.
"""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time


BASE = 0x53000000
DEVICE = "/machine/sfc-command"
SIZE = 4 * 1024 * 1024
PAGE = 256
SECTOR = 4096
CMD_NS = 10000
PROGRAM_NS = 200000
ERASE_NS = 1000000
STATUS_NS = 100000
CONFIG = 0x300
INS = 0x308
ADDR = 0x30c
DATA = 0x400
PROPERTIES = (
    "commands-started", "commands-completed", "rejections", "peer-operations",
    "peer-commits", "peer-busy", "peer-wel", "peer-volatile-enable",
    "peer-sr1", "peer-sr2", "peer-nv-sr1", "peer-nv-sr2", "peer-sha256", "peer-power-cycles",
)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def config(*, read=False, length=0, address=False):
    require(type(length) is int and 0 <= length <= 64, "invalid data length")
    # SDK's no-data size zero underflows to the masked six-bit field. The
    # model must ignore this field when DATA_EN is zero.
    return (3 | (8 if address else 0) | (0x80 if length else 0)
            | (0x100 if read else 0) | (((length - 1) & 63) << 9))


def programmed(old, address, data):
    require(0 <= address < SIZE and 1 <= len(data) <= 64, "program shape")
    require(address + len(data) <= SIZE, "program boundary")
    require(address // PAGE == (address + len(data) - 1) // PAGE, "page crossing")
    result = bytearray(old)
    for i, value in enumerate(data):
        result[address + i] &= value
    return result


def erased(old, address):
    require(0 <= address <= SIZE - SECTOR and address % SECTOR == 0, "erase shape")
    result = bytearray(old)
    result[address:address + SECTOR] = b"\xff" * SECTOR
    return result


def parse_qtest(line):
    parts = line.decode("ascii").strip().split()
    require(parts and parts[0] == "OK", f"qtest rejected transport: {line!r}")
    return parts[1:]


def connect(path, process):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        require(process.poll() is None, "QEMU exited before socket connection")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect(str(path))
            return sock
        except (FileNotFoundError, ConnectionRefusedError):
            sock.close()
            time.sleep(0.01)
    raise TimeoutError(f"QEMU socket unavailable: {path}")


class Peer:
    def __init__(self, bus, monitor, trace):
        self.bus, self.monitor, self.trace = bus, monitor, trace
        self.image = bytearray(b"\xff" * SIZE)
        self.cases = []

    def record(self, name, **details):
        self.cases.append({"name": name, "passed": True, **details})

    def qt(self, command):
        self.trace.write(json.dumps({"qtest": command}) + "\n")
        self.trace.flush()
        self.bus.write(command.encode("ascii") + b"\n")
        while True:
            raw = self.bus.readline()
            require(bool(raw), "qtest disconnected")
            if raw.startswith((b"IRQ raise ", b"IRQ lower ")):
                continue
            return parse_qtest(raw)

    def qmp(self, command, arguments=None, *, expect_error=False):
        request = {"execute": command}
        if arguments is not None:
            request["arguments"] = arguments
        self.trace.write(json.dumps({"qmp": request}) + "\n")
        self.monitor.write(json.dumps(request).encode() + b"\n")
        while True:
            raw = self.monitor.readline()
            require(bool(raw), "QMP disconnected")
            reply = json.loads(raw)
            if "event" in reply:
                continue
            if expect_error:
                require("error" in reply and "return" not in reply, f"QMP unexpectedly accepted {request}")
                require(isinstance(reply["error"].get("desc"), str), "QMP error lacks diagnostic")
                return reply["error"]
            require("return" in reply and "error" not in reply, f"QMP failure {reply}")
            return reply["return"]

    def get(self, name):
        return self.qmp("qom-get", {"path": DEVICE, "property": name})

    def rd(self, offset):
        values = self.qt(f"readl {BASE + offset:#x}")
        require(len(values) == 1, "invalid readl reply")
        return int(values[0], 0)

    def wr(self, offset, value):
        self.qt(f"writel {BASE + offset:#x} {value:#x}")

    def step(self, ns):
        require(type(ns) is int and ns >= 0, "invalid clock step")
        self.qt(f"clock_step {ns}")

    def fault(self, name, high):
        require(name in ("controller-stall", "peer-stall", "peer-power-cycle"), "unknown fault")
        self.qt(f"set_irq_in {DEVICE} {name} 0 {int(high)}")

    def power_cycle(self):
        self.fault("peer-power-cycle", True)
        self.fault("peer-power-cycle", False)

    def words(self):
        return [self.rd(DATA + 4 * i) for i in range(16)]

    def snapshot(self, *, mmio=True):
        result = {key: self.get(key) for key in PROPERTIES}
        if mmio:
            result["controller"] = [self.rd(CONFIG), self.rd(INS), self.rd(ADDR)]
            result["data"] = self.words()
        return result

    def reject(self, name, operation, *, mmio=True):
        before = self.snapshot(mmio=mmio)
        operation()
        after = self.snapshot(mmio=mmio)
        require(after.pop("rejections") == before.pop("rejections") + 1,
                f"{name}: no explicit one-access rejection")
        require(after == before, f"{name}: rejected access changed state: {before} -> {after}")
        self.record(name, rejection_atomic=True)

    def stage(self, opcode, *, read=False, address=None, data=b"", length=None):
        if data:
            require(len(data) <= 64, "data buffer overflow")
            padded = bytes(data).ljust(64, b"\xa7")
            for i, word in enumerate(struct.unpack("<16I", padded)):
                self.wr(DATA + 4 * i, word)
        self.wr(INS, opcode)
        if address is not None:
            self.wr(ADDR, address)
        return config(read=read, length=len(data) if length is None else length,
                      address=address is not None)

    def start(self, opcode, **kwargs):
        value = self.stage(opcode, **kwargs)
        started = self.get("commands-started")
        self.wr(CONFIG, value)
        require(self.get("commands-started") == started + 1, f"opcode {opcode:02x} not started")
        require(self.rd(CONFIG) & 1, "START did not latch")

    def done(self):
        self.step(CMD_NS - 1)
        require(self.rd(CONFIG) & 1, "command completed before synthetic deadline")
        self.step(1)
        require(not self.rd(CONFIG) & 1, "command not complete at deadline")

    def command(self, opcode, **kwargs):
        self.start(opcode, **kwargs)
        self.done()

    def read(self, address, length=64):
        self.command(0x03, read=True, address=address, length=length)
        return struct.pack("<16I", *self.words())[:length]

    def status(self, second=False):
        self.command(0x35 if second else 0x05, read=True, length=1)
        return self.rd(DATA) & 255

    def wren(self):
        self.command(0x06)
        require(bool(self.get("peer-wel")), "WREN did not set WEL")

    def program(self, address, data):
        self.wren()
        self.start(0x02, address=address, data=data)
        commits = self.get("peer-commits")
        digest = hashlib.sha256(self.image).hexdigest()
        self.done()
        require(self.get("peer-busy"), "CMD complete must precede NOR program complete")
        require(not self.get("peer-wel"), "accepted program did not consume WEL")
        require(self.get("peer-commits") == commits, "program committed with CMD completion")
        require(self.get("peer-sha256") == digest, "program bytes changed with CMD completion")
        self.step(PROGRAM_NS - 1)
        require(self.get("peer-busy") and self.get("peer-commits") == commits, "early NOR completion")
        require(self.get("peer-sha256") == digest, "program bytes changed before NOR deadline")
        self.step(1)
        require(not self.get("peer-busy") and self.get("peer-commits") == commits + 1, "NOR commit missing")
        self.image = programmed(self.image, address, data)
        require(self.read(address, len(data)) == self.image[address:address + len(data)], "program bytes differ")

    def status_write(self, sr1, sr2, *, volatile=False):
        self.command(0x50 if volatile else 0x06)
        self.start(0x01, data=bytes((sr1, sr2)))
        before = [self.get(key) for key in ("peer-nv-sr1", "peer-nv-sr2", "peer-sr2")]
        self.done()
        require(self.get("peer-busy"), "status write has no NOR busy phase")
        require([self.get(key) for key in ("peer-nv-sr1", "peer-nv-sr2", "peer-sr2")] == before,
                "status committed at CMD completion")
        self.step(STATUS_NS - 1)
        require(self.get("peer-busy"), "status committed early")
        require([self.get(key) for key in ("peer-nv-sr1", "peer-nv-sr2", "peer-sr2")] == before,
                "status changed before NOR deadline")
        self.step(1)
        require(not self.get("peer-busy"), "status write stayed busy")
        require(self.status() & 0xfc == sr1 and self.status(True) == sr2, "status bytes differ")

    def verify_array(self, name):
        # Digest the entire committed peer array and independently compare
        # actual command bytes at altered areas and deterministic boundaries.
        # A 4 MiB qtest command readback would add 65536 timer transactions.
        digest = self.get("peer-sha256")
        expected = hashlib.sha256(self.image).hexdigest()
        require(digest == expected, f"{name}: whole array digest mismatch")
        offsets = (0, 64, 192, 224, 256, SECTOR - 32, SECTOR,
                   SECTOR + 64, SIZE // 2 - 32, SIZE - 64)
        for address in offsets:
            require(self.read(address) == self.image[address:address + 64],
                    f"{name}: READ bytes differ at {address:#x}")
        self.record(name, array_digest_bytes=SIZE, command_bytes_compared=64 * len(offsets),
                    expected_sha256=expected, observed_sha256=digest, read_offsets=list(offsets))


@contextmanager
def session(qemu, directory, *, assume=True):
    directory.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="ws53-sfc-") as temporary:
        qt, qm = Path(temporary) / "qt", Path(temporary) / "qm"
        command = [str(qemu), "-M", "ws53", "-accel", "qtest", "-display", "none",
                   "-monitor", "none", "-serial", "null", "-nic", "none",
                   "-qtest", f"unix:{qt},server=on,wait=off", "-qtest-log", "/dev/null",
                   "-qmp", f"unix:{qm},server=on,wait=off", "-d", "guest_errors"]
        if assume:
            command += ["-global", "ws53-sfc-command.assume-profile=on"]
        (directory / "command.json").write_text(json.dumps(command, indent=2) + "\n")
        with (directory / "stderr.log").open("wb") as errors, \
                (directory / "traffic.jsonl").open("w") as trace:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors)
            try:
                with connect(qt, process) as bus, connect(qm, process) as monitor, \
                        bus.makefile("rwb", buffering=0) as bus_io, \
                        monitor.makefile("rwb", buffering=0) as monitor_io:
                    greeting = json.loads(monitor_io.readline())
                    require("QMP" in greeting, "no QMP greeting")
                    peer = Peer(bus_io, monitor_io, trace)
                    peer.qmp("qmp_capabilities")
                    yield peer
                    require(process.poll() is None, "QEMU exited unexpectedly")
                    peer.qmp("quit")
                    process.wait(timeout=5)
                    require(process.returncode == 0, f"QEMU exit {process.returncode}")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


def normal(p):
    require(not p.get("peer-busy") and not p.get("peer-wel"), "initial busy/WEL")
    for i in range(16):
        p.wr(DATA + 4 * i, 0xa7a7a7a7)
    p.start(0x9f, read=True, length=3)
    require(p.words() == [0xa7a7a7a7] * 16, "DATA changed before command completion")
    p.step(CMD_NS - 1)
    require(p.words() == [0xa7a7a7a7] * 16 and p.rd(CONFIG) & 1,
            "read data exposed before command deadline")
    p.step(1)
    require(struct.pack("<I", p.rd(DATA))[:3] == bytes.fromhex("7f5301"), "synthetic ID mismatch")
    require(struct.pack("<16I", *p.words())[3:] == b"\xa7" * 61, "short read corrupted DATA tail")
    p.record("read-data-changes-only-on-command-completion")
    require(p.status() == 0 and p.status(True) == 0, "initial status")
    # A stale address must not break a command with ADDR_EN clear.
    p.wr(ADDR, 0x3fffffff)
    p.command(0x06)
    require(p.status() == 2, "RDSR must expose WEL")
    p.command(0x04)
    require(p.status() == 0, "WRDI must clear WEL")
    p.record("identity-status-write-enable-and-stale-address")
    for address, length in ((0, 1), (63, 3), (192, 64), (256, 64), (SIZE - 1, 1)):
        data = bytes((i * 37 + address + 11) & 255 for i in range(length))
        p.program(address, data)
        p.record(f"program-{address:04x}-{length}")
    for length in range(1, 65):
        require(p.read(256, length) == p.image[256:256 + length], "READ data length or byte order")
    p.record("all-64-read-lengths-and-byte-order")
    p.program(192, b"\x0f\xf0\x00\xff" * 16)
    p.program(192, b"\xff" * 64)
    p.record("program-bitwise-and-no-zero-to-one")
    p.program(SECTOR, b"\x39" * 64)
    p.wren()
    p.start(0x20, address=0)
    p.done()
    require(p.get("peer-busy"), "erase busy missing")
    require(p.get("peer-sha256") == hashlib.sha256(p.image).hexdigest(), "erase committed at CMD completion")
    p.step(ERASE_NS - 1)
    require(p.get("peer-busy"), "erase completed early")
    require(p.get("peer-sha256") == hashlib.sha256(p.image).hexdigest(), "erase changed bytes before NOR completion")
    p.step(1)
    p.image = erased(p.image, 0)
    require(p.read(0) == b"\xff" * 64 and p.read(SECTOR) == b"\x39" * 64,
            "erase affected adjacent sector")
    p.record("sector-erase-isolated-and-deadline")
    p.verify_array("normal-whole-array-oracle")


def status_rules(p):
    p.program(0, b"\x12\x34\x56")
    p.status_write(0, 2)
    require(p.get("peer-nv-sr1") == 0 and p.get("peer-nv-sr2") == 2, "NV write absent")
    p.status_write(0x1c, 0, volatile=True)
    require(p.get("peer-nv-sr1") == 0 and p.get("peer-nv-sr2") == 2, "volatile write changed NV")
    require(not p.get("peer-volatile-enable") and not p.get("peer-wel"), "status latch not consumed")
    p.record("volatile-status-is-distinct-from-nv")
    p.wren()
    value = p.stage(0x02, address=0, data=b"\x00")
    p.reject("protected-program-rejected", lambda: p.wr(CONFIG, value))
    value = p.stage(0x20, address=0)
    p.reject("protected-erase-rejected", lambda: p.wr(CONFIG, value))
    p.qmp("system_reset")
    require(p.status() & 0xfc == 0x1c and p.status(True) == 0, "controller reset reset NOR status")
    p.record("controller-reset-retains-volatile-status")
    p.power_cycle()
    require(p.status() == 0 and p.status(True) == 2, "power cycle did not restore NV status")
    require(not p.get("peer-wel") and not p.get("peer-volatile-enable"), "power cycle retained latches")
    p.record("peer-power-cycle-restores-nv-not-controller-reset")
    p.status_write(0x1c, 2)
    p.status_write(0, 0, volatile=True)
    p.power_cycle()
    require(p.status() == 0x1c and p.status(True) == 2, "NV state did not survive peer power cycle")
    p.status_write(0, 0)
    p.verify_array("status-and-resets-do-not-erase-array")


def rejects(p):
    p.program(0, bytes(range(64)))
    p.wren()
    before = p.snapshot()
    error = p.qmp("qom-set", {"path": DEVICE, "property": "assume-profile", "value": False}, expect_error=True)
    require(p.snapshot() == before, "runtime profile mutation changed state")
    p.record("assumption-profile-cannot-change-after-realize", diagnostic=error)
    for address in (0x400000, 0x7ffffc):
        p.reject(f"xip-read-{address:x}", lambda address=address: p.qt(f"readl {address:#x}"))
        p.reject(f"xip-write-{address:x}", lambda address=address: p.qt(f"writel {address:#x} 0"))
    for verb in ("readb", "readw", "readq", "writeb", "writew", "writeq"):
        tail = " 0" if verb.startswith("write") else ""
        p.reject(f"unsupported-width-{verb}", lambda verb=verb, tail=tail: p.qt(f"{verb} {BASE + DATA:#x}{tail}"))
    for offset in (DATA + 1, DATA + 2):
        p.reject(f"unaligned-read-{offset:x}", lambda off=offset: p.rd(off))
        p.reject(f"unaligned-write-{offset:x}", lambda off=offset: p.wr(off, 0))
    p.reject("instruction-reserved-bits", lambda: p.wr(INS, 0x100))
    p.reject("address-reserved-bits", lambda: p.wr(ADDR, 0x40000000))
    # No IRQ routing, DMA, bus-map or timing capability is inferred from an
    # offset shared with another HiSilicon IP.
    for offset in (0x110, 0x120, 0x124, 0x128, 0x12c, 0x130,
                   0x200, 0x210, 0x218, 0x240, 0x244, 0x248, 0x24c, 0x250,
                   0x304, 0x310, 0x440, 0x1ffc):
        p.reject(f"unsupported-read-{offset:03x}", lambda off=offset: p.rd(off))
        p.reject(f"unsupported-write-{offset:03x}", lambda off=offset: p.wr(off, 0xdeadbeef))
    requests = [
        ("unknown-opcode", 0xab, dict(), None),
        ("chip-erase-not-modeled", 0xc7, dict(), None),
        ("quad-read-not-modeled", 0x6b, dict(read=True, address=0, length=1), None),
        ("read-outside", 0x03, dict(read=True, address=SIZE, length=1), None),
        ("read-straddle", 0x03, dict(read=True, address=SIZE - 1, length=2), None),
        ("program-straddle-page", 0x02, dict(address=255, data=b"\x00\x01"), None),
        ("program-outside", 0x02, dict(address=SIZE, data=b"\x00"), None),
        ("erase-unaligned", 0x20, dict(address=1), None),
        ("erase-outside", 0x20, dict(address=SIZE), None),
        ("rdid-wrong-length", 0x9f, dict(read=True, length=2), None),
        ("rdsr-wrong-length", 0x05, dict(read=True, length=2), None),
        ("status-reserved-sr1", 0x01, dict(data=b"\x80\x00"), None),
        ("status-reserved-sr2", 0x01, dict(data=b"\x00\x04"), None),
        ("status-wrong-length", 0x01, dict(data=b"\x00"), None),
        ("read-direction-reversed", 0x03, dict(address=0, data=b"\x00"), None),
        ("program-direction-reversed", 0x02, dict(read=True, address=0, length=1), None),
        ("wrong-chip-select", 0x05, dict(read=True, length=1), lambda x: x & ~2),
        ("dummy-not-supported", 0x05, dict(read=True, length=1), lambda x: x | 0x10),
        ("interface-not-supported", 0x05, dict(read=True, length=1), lambda x: x | (1 << 17)),
        ("reserved-config-bit", 0x05, dict(read=True, length=1), lambda x: x | (1 << 31)),
    ]
    for name, opcode, kwargs, transform in requests:
        value = p.stage(opcode, **kwargs)
        p.reject(name, lambda value=transform(value) if transform else value: p.wr(CONFIG, value))
    p.command(0x04)
    for name, opcode, kwargs in (("program-without-wel", 2, dict(address=0, data=b"\x00")),
                                 ("erase-without-wel", 0x20, dict(address=0)),
                                 ("status-without-latch", 1, dict(data=b"\x00\x00"))):
        value = p.stage(opcode, **kwargs)
        p.reject(name, lambda value=value: p.wr(CONFIG, value))
    p.verify_array("rejected-requests-never-change-array")


def busy_and_stall(p):
    p.wren()
    p.start(2, address=0, data=b"\x5a" * 8)
    p.fault("controller-stall", True)
    before = p.snapshot()
    p.step(10 * ERASE_NS)
    require(p.snapshot() == before, "controller stall advanced state")
    for offset, value in ((CONFIG, 0), (INS, 0x04), (ADDR, 0x100), (DATA, 0)):
        p.reject(f"command-busy-write-{offset:x}", lambda off=offset, val=value: p.wr(off, val))
    p.fault("controller-stall", False)
    p.done()
    require(p.get("peer-busy") and not p.rd(CONFIG) & 1, "CMD/NOR busy not separate")
    p.fault("peer-stall", True)
    commits = p.get("peer-commits")
    p.step(10 * ERASE_NS)
    require(p.get("peer-busy") and p.get("peer-commits") == commits, "peer stall committed")
    require(p.status() & 1 and p.status(True) == 0, "RDSR forbidden during NOR busy")
    for name, opcode, kwargs in (("read-while-peer-busy", 3, dict(read=True, address=0, length=1)),
                                 ("wren-while-peer-busy", 6, dict()),
                                 ("program-while-peer-busy", 2, dict(address=0, data=b"\x00"))):
        value = p.stage(opcode, **kwargs)
        p.reject(name, lambda value=value: p.wr(CONFIG, value))
    p.fault("peer-stall", False)
    p.step(PROGRAM_NS - 1)
    require(p.get("peer-busy"), "peer stall failed to retain remaining time")
    p.step(1)
    p.image = programmed(p.image, 0, b"\x5a" * 8)
    require(p.read(0, 8) == b"\x5a" * 8, "resumed program bytes")
    p.record("independent-controller-and-peer-stall-resume")
    # Assert midway through a command and resume only its remaining duration.
    p.start(0x9f, read=True, length=3)
    p.step(4000)
    p.fault("controller-stall", True)
    p.step(1000000)
    p.fault("controller-stall", False)
    p.step(5999)
    require(p.rd(CONFIG) & 1, "controller resumed with wrong remainder")
    p.step(1)
    require(not p.rd(CONFIG) & 1, "controller remaining deadline missing")
    p.record("controller-mid-command-remaining-time")
    p.verify_array("busy-stall-whole-array-oracle")


def reset_rules(p):
    p.program(0, b"\x77" * 8)
    p.wren()
    p.start(2, address=16, data=b"\x33" * 8)
    p.step(CMD_NS // 2)
    peer_before = {key: p.get(key) for key in PROPERTIES if key.startswith("peer-")}
    p.qmp("system_reset")
    require(all(p.rd(off) == 0 for off in (CONFIG, INS, ADDR)), "controller reset did not clear registers")
    require({key: p.get(key) for key in peer_before} == peer_before, "controller reset changed NOR")
    p.step(ERASE_NS * 2)
    require(not p.get("peer-busy"), "cancelled controller command became NOR work")
    require(p.read(16, 8) == b"\xff" * 8, "cancelled command later committed")
    p.record("system-reset-cancels-command-before-peer-submission")
    p.wren()
    p.start(2, address=32, data=b"\x66" * 8)
    p.done()
    p.step(10000)
    require(p.get("peer-busy"), "program unexpectedly idle")
    p.qmp("system_reset")
    require(p.get("peer-busy"), "controller reset incorrectly aborted NOR program")
    p.step(PROGRAM_NS - 10001)
    require(p.get("peer-busy"), "NOR deadline changed by controller reset")
    p.step(1)
    p.image = programmed(p.image, 32, b"\x66" * 8)
    require(p.read(32, 8) == b"\x66" * 8, "NOR operation not retained over controller reset")
    p.record("system-reset-preserves-already-submitted-peer-operation")
    p.wren()
    p.start(2, address=48, data=b"\x11" * 8)
    p.done()
    p.fault("peer-stall", True)
    p.power_cycle()
    require(not p.get("peer-busy") and not p.get("peer-wel"), "peer power cycle retained work/latch")
    p.fault("peer-stall", False)
    p.step(ERASE_NS * 2)
    require(p.read(48, 8) == b"\xff" * 8, "power-cancelled program committed later")
    p.record("peer-power-cycle-cancels-uncommitted-program")
    p.wren()
    p.start(0x20, address=0)
    p.done()
    p.step(ERASE_NS // 2)
    p.power_cycle()
    p.step(ERASE_NS * 2)
    require(p.read(0, 8) == b"\x77" * 8, "power-cancelled erase destroyed committed data")
    p.record("peer-power-cycle-cancels-uncommitted-erase")
    p.status_write(0, 2)
    p.wren()
    p.start(0x01, data=b"\x1c\x00")
    p.done()
    p.step(STATUS_NS // 2)
    p.power_cycle()
    p.step(STATUS_NS * 2)
    require(p.status() == 0 and p.status(True) == 2 and p.get("peer-nv-sr2") == 2,
            "power-cancelled status write altered NV state")
    p.record("peer-power-cycle-cancels-uncommitted-nv-status")
    p.wren()
    p.start(2, address=64, data=b"\x00" * 8)
    p.step(CMD_NS // 2)
    p.power_cycle()
    p.step(ERASE_NS * 2)
    require(not p.rd(CONFIG) & 1 and p.read(64, 8) == b"\xff" * 8,
            "power-cycle during controller transfer submitted a stale write")
    p.record("peer-power-cycle-cancels-incomplete-controller-exchange")
    p.start(0x9f, read=True, length=3)
    p.fault("controller-stall", True)
    p.qmp("system_reset")
    p.fault("controller-stall", False)
    p.step(ERASE_NS)
    require(p.rd(CONFIG) == 0, "reset revived old stalled command")
    p.command(0x9f, read=True, length=3)
    require(p.rd(DATA) & 0xffffff == 0x01537f, "command recovery after reset failed")
    p.record("reset-stalled-command-then-recover")
    p.verify_array("reset-whole-array-oracle")


def disabled(p):
    for offset in (CONFIG, INS, ADDR, DATA, 0x120, 0x240):
        p.reject(f"default-profile-denies-read-{offset:x}", lambda off=offset: p.rd(off), mmio=False)
        p.reject(f"default-profile-denies-write-{offset:x}", lambda off=offset: p.wr(off, 1), mmio=False)


def host_checks():
    cases = []
    for n in range(1, 65):
        value = config(read=True, length=n, address=True)
        require(value & 0x18b == 0x18b and ((value >> 9) & 63) + 1 == n, "encoding mismatch")
    cases.append("all-64-supported-lengths-encode-exactly")
    require(config() == 3 | (63 << 9), "no-data SDK underflow encoding")
    original = bytearray(b"\xff" * SIZE)
    once = programmed(original, 255, b"\x0f")
    twice = programmed(once, 255, b"\xf0")
    require(original[255] == 255 and twice[255] == 0, "oracle mutation or bitwise-AND error")
    require(erased(twice, 0) == original, "erase oracle error")
    cases += ["immutable-oracle-input-and-bitwise-and", "sector-erase-oracle"]
    invalid = [lambda: config(length=-1), lambda: config(length=65), lambda: config(length=True),
               lambda: programmed(original, 255, b"12"), lambda: programmed(original, SIZE, b"1"),
               lambda: programmed(original, 0, b""), lambda: erased(original, 1),
               lambda: erased(original, SIZE), lambda: parse_qtest(b"FAIL access\n"),
               lambda: parse_qtest(b"\n")]
    for i, call in enumerate(invalid):
        try:
            call()
        except AssertionError:
            cases.append(f"harness-rejection-{i}")
        else:
            raise AssertionError(f"host invalid input {i} accepted")
    require(parse_qtest(b"OK 0x00000000\n") == ["0x00000000"], "transport parsing")
    return {"passed": True, "scope": "host harness only; no QEMU executed",
            "model_executed": False, "hardware_accessed": False,
            "test_source_sha256": sha(__file__), "cases": cases, "case_count": len(cases)}


def run(qemu, output):
    output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    source_paths = sorted(set([Path(__file__).resolve(), *root.glob("src/**/*sfc*"), root / "src/hw/riscv/ws53.c"]))
    report = {"passed": False, "scope": "independent synthetic SFC command/NOR contract",
              "hardware_accessed": False, "hardware_qualification_eligible": False,
              "irq_routing_verified": False, "dma_or_drain_verified": False,
              "board_nor_semantics_verified": False, "model_executed": True,
              "qemu_sha256": sha(qemu),
              "sources_sha256": {str(p.relative_to(root)): sha(p) for p in source_paths if p.is_file()},
              "synthetic_assumptions": {"size": SIZE, "page": PAGE, "sector": SECTOR,
                                        "id": "7f5301", "command_ns": CMD_NS,
                                        "program_ns": PROGRAM_NS, "erase_ns": ERASE_NS,
                                        "status_ns": STATUS_NS,
                                        "partial_program_on_power_loss_modeled": False},
              "groups": []}
    report_path = output / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    try:
        for name, function in (("normal", normal), ("status", status_rules),
                               ("reject", rejects), ("stall", busy_and_stall),
                               ("reset", reset_rules), ("disabled", disabled)):
            with session(qemu, output / name, assume=name != "disabled") as peer:
                function(peer)
                expected_rejections = sum(case.get("rejection_atomic", False) for case in peer.cases)
                require(peer.get("rejections") == expected_rejections,
                        f"{name}: unexpected rejection during apparently valid traffic")
            lines = (output / name / "stderr.log").read_text().splitlines()
            rejections = [line for line in lines if line.startswith("ws53-sfc-command: rejected ")]
            notices = [line for line in lines if line.startswith("ws53-sfc-command: assumed synthetic NOR4MiB ")]
            require(len(rejections) == expected_rejections and len(notices) == int(name != "disabled"),
                    f"{name}: missing or unexpected diagnostics")
            require(len(lines) == len(rejections) + len(notices), f"{name}: unclassified guest diagnostic")
            report["groups"].append({"name": name, "passed": True, "cases": peer.cases})
        report["case_count"] = sum(len(group["cases"]) for group in report["groups"])
        report["passed"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report["artifacts_sha256"] = {str(p.relative_to(output)): sha(p)
                                      for p in sorted(output.rglob("*")) if p.is_file() and p != report_path}
        temporary = output / "report.pending.json"
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        os.replace(temporary, report_path)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        require(args.qemu is None and args.output is None, "self-test cannot claim QEMU output")
        print(json.dumps(host_checks(), indent=2))
    else:
        require(args.qemu is not None and args.output is not None, "--qemu and --output required")
        result = run(args.qemu.resolve(strict=True), args.output)
        print(json.dumps({"passed": result["passed"], "case_count": result["case_count"],
                          "report": str(args.output.resolve() / "report.json")}, indent=2))


if __name__ == "__main__":
    main()
