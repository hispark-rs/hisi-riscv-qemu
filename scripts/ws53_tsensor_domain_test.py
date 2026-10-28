#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""QEMU-only TSENSOR/control-domain integration; no silicon fault-timing claim."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import time

from ws53_gpio_test import connect, running
from ws53_tsensor_test import Sensor, PROFILE, NOTICE, tsensor_test
from ws53_flash_handoff_test import handoff_test, NOTICE as WDT_NOTICE
from ws53_branch_immediate_test import li, load, store

RESET = 0x5703096c
CONTROL = 0x59006304
STATUS = CONTROL + 4
FLASH = ("-machine", "flashboot-handoff=on", "-global", "ws53-wdt.assume-timing=on")


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while data := stream.read(1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


def domain_test(qemu, out):
    denied_reads = denied_writes = rejected = 0
    with running(qemu, False, FLASH + PROFILE) as (cmd, _, reset, diagnostics):
        s = Sensor(cmd)
        def read_reset(): return int(cmd(f"readl {RESET:#x}")[0], 0)
        def release(value): cmd(f"writel {RESET:#x} {value:#x}")
        def blocked_read():
            nonlocal denied_reads
            before = s.count("blocked-reads")
            # qtest's memory protocol discards MemTxResult. Its returned value
            # is not success evidence; the CPU test below checks real traps.
            s.read(4)
            assert s.count("blocked-reads") == before + 1
            denied_reads += 1
        assert read_reset() == 0
        blocked_read()
        for off, value in ((0x30, 160), (4, 3), (0x10, 2), (0x34, 0), (0, 1)):
            before = s.count("blocked-writes")
            s.write(off, value)
            assert s.count("blocked-writes") == before + 1
            denied_writes += 1
        s.step(10_000_000)
        assert s.count("starts") == s.count("samples") == 0
        release(3)
        assert read_reset() == 3
        s.step(5000)  # SDK's documented delay, not an analog settling model.
        assert s.read(4) == s.read() == s.count("starts") == 0
        s.code(321)
        s.init()
        s.step(4_096_000)
        assert s.read() == (321 << 2 | 2)
        release(3)  # Idempotent release must not restart a running sensor.
        assert s.read() == (321 << 2 | 2) and s.count("starts") == 1
        for value in (1, 2, 4, 7, 0x103, 0xffffffff):
            release(value)
            rejected += 1
            assert read_reset() == 3 and s.read() == (321 << 2 | 2)
        for command in (f"writeb {RESET:#x} 0", f"writew {RESET:#x} 0"):
            cmd(command)
            rejected += 1
            assert read_reset() == 3
        release(0)
        assert read_reset() == 0
        blocked_read()
        s.step(10_000_000)
        assert s.count("starts") == s.count("samples") == s.count("reports") == 0
        release(3)
        assert s.read(4) == s.read() == 0
        # Half a conversion must not survive logic reset, nor complete late.
        s.code(100)
        s.init()
        s.step(2_048_000)
        assert s.count("samples") == 8
        release(0)
        release(3)
        s.step(10_000_000)
        assert s.read(4) == s.read() == s.count("samples") == 0
        s.code(900)
        s.init()
        s.step(4_096_000)
        assert s.read() == (900 << 2 | 2) and s.count("samples") == 16
        for _ in range(3):
            reset()
            assert read_reset() == 0
            blocked_read()
            assert s.count("starts") == s.count("samples") == 0
            release(3)
            assert s.read(4) == s.read() == 0
            s.init()
            s.step(4_096_000)
            assert s.read() == (900 << 2 | 2)
        # External clock faults survive a reset; domain release alone cannot
        # erase an injected conversion fault or restart an old transaction.
        s.fault("clock", False)
        release(0)
        release(3)
        s.init()
        s.step(10_000_000)
        assert s.read() == 0 and s.count("samples") == 0
        s.fault("clock", True)
        s.step(4_096_000)
        assert s.read() == (900 << 2 | 2)
    lines = diagnostics[0].splitlines()
    assert lines.count(NOTICE) == 1, lines
    assert lines.count(WDT_NOTICE) == 1, lines
    assert sum("inaccessible control domain read" in x for x in lines) == denied_reads, lines
    assert sum("inaccessible control domain write" in x for x in lines) == denied_writes, lines
    assert sum("ws53-memory: unimplemented write" in x for x in lines) == rejected, lines
    assert len(lines) == 2 + denied_reads + denied_writes + rejected, lines
    (out / "qtest.stderr").write_text(diagnostics[0])
    return {"denied_reads": denied_reads, "denied_writes": denied_writes,
            "rejected_unqualified_reset_writes": rejected, "system_reset_rounds": 3,
            "partial_conversion_cancelled": True, "idempotent_release_preserves_ready": True,
            "fault_input_preserved": True}


def csr_read(rd, number): return (number << 20) | (2 << 12) | (rd << 7) | 0x73
def csr_write(number, rs): return (number << 20) | (rs << 15) | (1 << 12) | 0x73
def addi(rd, rs, value): return ((value & 4095) << 20) | (rs << 15) | (rd << 7) | 0x13
def image(words): return struct.pack("<" + "I" * len(words), *words)


def cpu_fault_test(qemu, out):
    # An intentionally tiny synthetic CPU test in ITCM, not a Rust Flash app.
    # It installs its own trap handler and never relies on default RAM values.
    entry, trap, result, records = 0x28000, 0x28400, 0x29000, 0x29080
    code = li(8, result) + li(18, records) + li(9, 0)
    code += li(5, trap) + [csr_write(0x305, 5)]
    code += li(10, CONTROL) + li(11, RESET) + li(12, 3)
    faults = []
    faults.append((entry + len(code)*4, 5, CONTROL))
    code += [load(5, 10, 0)]
    faults.append((entry + len(code)*4, 7, CONTROL))
    code += [store(12, 10, 0)]
    code += [store(12, 11, 0), load(5, 10, 0), store(5, 8, 4)]
    code += [store(12, 10, 0), load(5, 10, 0), store(5, 8, 8)]
    code += [store(0, 11, 0)]
    faults.append((entry + len(code)*4, 5, STATUS))
    code += [load(5, 10, 4)]
    code += [store(12, 11, 0), load(5, 10, 0), store(5, 8, 12)]
    code += [load(5, 10, 4), store(5, 8, 16), store(9, 8, 20)]
    code += li(5, 0x53444f4e) + [store(5, 8, 0), 0x0000006f]
    handler = [csr_read(5, 0x342), store(5, 18, 0), csr_read(5, 0x341),
               store(5, 18, 4), csr_read(6, 0x343), store(6, 18, 8),
               addi(5, 5, 4), csr_write(0x341, 5), addi(18, 18, 12),
               addi(9, 9, 1), 0x30200073]
    (out / "cpu-fixture.bin").write_bytes(image(code))
    (out / "cpu-trap.bin").write_bytes(image(handler))
    transcript = []
    with tempfile.TemporaryDirectory(prefix="ws53-domain-cpu-") as tmp:
        tmp = Path(tmp)
        argv = [qemu, "-M", "ws53,flashboot-handoff=on", "-S", "-display", "none",
                "-monitor", "none", "-serial", "null", "-qtest-log", "/dev/null",
                "-qtest", f"unix:{tmp}/qt,server=on,wait=off",
                "-qmp", f"unix:{tmp}/qm,server=on,wait=off", "-d", "guest_errors",
                "-global", "ws53-wdt.assume-timing=on"]
        (out / "cpu-command.json").write_text(json.dumps(argv, indent=2)+"\n")
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with connect(tmp / "qt") as qs, qs.makefile("rwb", buffering=0) as qt, \
                 connect(tmp / "qm") as ms, ms.makefile("rwb", buffering=0) as qm:
                json.loads(qm.readline())
                def qmp(op):
                    qm.write(json.dumps({"execute": op}).encode()+b"\n")
                    while True:
                        reply = json.loads(qm.readline())
                        assert "error" not in reply, reply
                        if "return" in reply: return reply["return"]
                def cmd(text):
                    qt.write(text.encode()+b"\n")
                    reply = qt.readline().split()
                    assert reply and reply[0] == b"OK", (text, reply)
                    transcript.append([text, [v.decode() for v in reply]])
                    return reply[1:]
                qmp("qmp_capabilities")
                for addr, data in ((entry, image(code)), (trap, image(handler)),
                                   (result, bytes(24)), (records, bytes(36))):
                    cmd(f"write {addr:#x} {len(data)} 0x{data.hex()}")
                qmp("cont")
                deadline = time.monotonic()+5
                while int(cmd(f"readl {result:#x}")[0], 0) != 0x53444f4e:
                    assert time.monotonic() < deadline, "CPU did not finish access-fault fixture"
                    time.sleep(.01)
                qmp("stop")
                values = struct.unpack("<6I", bytes.fromhex(cmd(f"read {result:#x} 24")[0].decode()[2:]))
                assert values == (0x53444f4e, 0, 3, 0, 0, 3), values
                got = struct.unpack("<9I", bytes.fromhex(cmd(f"read {records:#x} 36")[0].decode()[2:]))
                actual = [tuple(got[i:i+3]) for i in range(0, 9, 3)]
                expected = [(cause, pc, addr) for pc, cause, addr in faults]
                assert actual == expected, (actual, expected)
                assert int(cmd(f"readl {RESET:#x}")[0], 0) == 3
                qmp("quit")
            stdout, stderr = proc.communicate(timeout=5)
            assert proc.returncode == 0 and not stdout, (proc.returncode, stdout, stderr)
        finally:
            if proc.poll() is None:
                proc.terminate()
            _, stderr = proc.communicate(timeout=5)
            (out / "cpu.stderr").write_bytes(stderr)
            (out / "cpu-transcript.json").write_text(json.dumps(transcript, indent=2)+"\n")
    lines = stderr.decode().splitlines()
    assert lines.count(WDT_NOTICE) == 1, lines
    assert len(lines) == 4 and sum("inaccessible control domain" in x for x in lines) == 3, lines
    return {"actual_tcg_load_store_execution": True, "traps": actual,
            "domain_release_allows_mmio": True, "reassert_clears_control_ready": True,
            "cpu_release_bit_remains_zero": True,
            "model_fault_policy": "MEMTX_ERROR; not a silicon fault/hang timing prediction"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    qemu = str(args.qemu.resolve())
    report = {"passed": False, "hardware_accessed": False, "qemu": qemu,
              "qemu_sha256": sha(qemu), "test_sha256": sha(__file__)}
    try:
        report["domain"] = domain_test(qemu, args.output)
        report["cpu"] = cpu_fault_test(qemu, args.output)
        report["ram_profile_regression"] = tsensor_test(qemu)
        report["flash_handoff_regression"] = handoff_test(qemu)
        assert report["qemu_sha256"] == sha(qemu)
        report["passed"] = True
    except BaseException as error:
        report["error"] = repr(error)
        raise
    finally:
        (args.output / "report.json").write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
