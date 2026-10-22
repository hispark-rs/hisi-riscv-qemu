#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Boot-free WS53 timer qtest and optional RAM UART/IRQ integration smoke."""
import argparse
import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path


def connect(path):
    deadline = time.monotonic() + 5
    while True:
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(5)
        try:
            sock.connect(str(path))
            return sock
        except (FileNotFoundError, ConnectionRefusedError):
            sock.close()
            if time.monotonic() > deadline:
                raise
            time.sleep(.02)


def model_test(qemu):
    with tempfile.TemporaryDirectory(prefix="ws53-qtest-") as tmp:
        qt, qm = Path(tmp) / "qtest", Path(tmp) / "qmp"
        proc = subprocess.Popen([qemu, "-M", "ws53", "-accel", "qtest",
            "-display", "none", "-monitor", "none", "-serial", "null",
            "-qtest", f"unix:{qt},server=on,wait=off",
            "-qmp", f"unix:{qm},server=on,wait=off", "-d", "guest_errors"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with connect(qt) as sock, sock.makefile("rwb", buffering=0) as io:
                def command(text):
                    io.write(text.encode() + b"\n")
                    line = io.readline().decode().strip()
                    assert line.startswith("OK"), (text, line)
                    return line.split()[1:]
                def read(off):
                    return int(command(f"readl {0x52002000 + off:#x}")[0], 0)
                def write(off, val):
                    command(f"writel {0x52002000 + off:#x} {val:#x}")
                def step(ns):
                    command(f"clock_step {ns}")
                assert read(0x110) == read(0x210) == 0
                write(0x100, 320_000)
                write(0x110, 1)  # one-shot, 10 ms at the SDK's 32 MHz
                step(9_999_999)
                assert read(0x118) == 0, "timer fired early"
                step(1)
                assert read(0x118) == 1 and read(0x110) & 1 == 0
                read(0x114)
                assert read(0x118) == 1, "EOI reads must not clear"
                write(0x114, 0)
                assert read(0x118) == 1, "EOI zero must not clear"
                write(0x114, 1)
                step(20_000_000)
                assert read(0x118) == 0, "one-shot rearmed itself"

                write(0x200, 320_000)
                write(0x210, 11)  # periodic, masked
                step(10_000_000)
                assert read(0x7c) == 2 and read(0x80) == 0
                write(0x210, 3)  # unmask already pending interrupt
                assert read(0x80) == 2
                write(0x78, 1)  # clearing channel 0 must preserve channel 1
                assert read(0x7c) == 2
                write(0x78, 2)
                assert read(0x7c) == 0
                step(10_000_000)
                assert read(0x218) == 1, "periodic timer did not reload"
                write(0x214, 1)
                write(0x210, 0x23)  # coherent count latch
                assert read(0x210) & 0x40
                latched = read(0x208)
                step(1_000_000)
                assert read(0x208) == latched, "count register must be a snapshot"
                write(0x210, 0)
                step(20_000_000)
                assert read(0x218) == 0, "disabled timer fired"

                with connect(qm) as qmp, qmp.makefile("rwb", buffering=0) as stream:
                    json.loads(stream.readline())
                    def execute(name):
                        stream.write(json.dumps({"execute": name}).encode() + b"\n")
                        while True:
                            response = json.loads(stream.readline())
                            if "return" in response:
                                return
                            assert "error" not in response, response
                    execute("qmp_capabilities")
                    execute("system_reset")
                assert read(0x100) == read(0x200) == read(0x7c) == 0
        finally:
            proc.terminate()
            _, errors = proc.communicate(timeout=5)
        assert b"unimplemented" not in errors.lower(), errors.decode(errors="replace")
        return ["32MHz", "one-shot", "periodic", "mask/unmask", "W1C",
                "channel isolation", "snapshot", "stop", "system reset"]


def ram_test(qemu, elf):
    proc = subprocess.Popen([qemu, "-M", "ws53", "-display", "none",
        "-monitor", "none", "-serial", "null", "-serial", "null", "-serial", "stdio",
        "-kernel", str(elf), "-d", "guest_errors"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        output, errors = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        output, errors = proc.communicate(timeout=5)
    assert b"WS53 RAM data/bss OK" in output, output.decode(errors="replace")
    assert b"WS53 PASS timer_irq=53 count=3" in output, output.decode(errors="replace")
    assert b"unimplemented" not in errors.lower(), errors.decode(errors="replace")
    assert b"Invalid" not in errors, errors.decode(errors="replace")
    return output.decode().splitlines()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    parser.add_argument("--elf", type=Path)
    args = parser.parse_args()
    result = {"model": model_test(args.qemu), "hardware_verified": False}
    if args.elf:
        result["ram"] = ram_test(args.qemu, args.elf)
    print(json.dumps(result, indent=2))
