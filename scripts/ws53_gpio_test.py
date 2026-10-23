#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS53 bank-level GPIO behavior tests; no board pin or electrical claims."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time

BANKS = ((0x5700c000, 23, 34), (0x57028000, 12, 33), (0x57010000, 3, 29))


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
            if time.monotonic() >= deadline:
                raise
            time.sleep(.02)


@contextmanager
def running(qemu, direct_gpio=True, extra_args=()):
    with tempfile.TemporaryDirectory(prefix="ws53-gpio-") as tmp:
        qt, qm = Path(tmp) / "qtest", Path(tmp) / "qmp"
        machine = "ws53,gpio-direct=on" if direct_gpio else "ws53"
        proc = subprocess.Popen([qemu, "-M", machine, "-accel", "qtest", "-display", "none",
            "-monitor", "none", "-serial", "null", "-qtest-log", "/dev/null",
            "-qtest", f"unix:{qt},server=on,wait=off", "-qmp", f"unix:{qm},server=on,wait=off",
            "-d", "guest_errors", *extra_args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        diagnostics = []
        try:
            with connect(qt) as sock, sock.makefile("rwb", buffering=0) as io:
                irq = {}
                def command(text):
                    io.write(text.encode() + b"\n")
                    while True:
                        parts = io.readline().decode().split()
                        if parts and parts[0] == "IRQ":
                            irq[int(parts[2])] = parts[1] == "raise"
                            continue
                        assert parts and parts[0] == "OK", (text, parts)
                        return parts[1:]
                def qmp_execute(operation, arguments=None):
                    with connect(qm) as qmp, qmp.makefile("rwb", buffering=0) as stream:
                        json.loads(stream.readline())
                        for name in ("qmp_capabilities", operation):
                            request = {"execute": name}
                            if name == operation and arguments is not None:
                                request["arguments"] = arguments
                            stream.write(json.dumps(request).encode() + b"\n")
                            while True:
                                response = json.loads(stream.readline())
                                assert "error" not in response, response
                                if "return" in response:
                                    break
                        return response["return"]
                command.qmp = qmp_execute
                def reset():
                    qmp_execute("system_reset")
                try:
                    yield command, irq, reset, diagnostics
                finally:
                    # Quit while the qtest channel is still open. Closing it
                    # concurrently with SIGTERM races qtest's log timer teardown.
                    if proc.poll() is None:
                        qmp_execute("quit")
        finally:
            try:
                _, errors = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                _, errors = proc.communicate(timeout=5)
            diagnostics.append(errors.decode(errors="replace"))


def gpio_test(qemu):
    with running(qemu) as (command, irq, reset, diagnostics):
        command("irq_intercept_in /machine/intc")
        for bank, (base, width, irq_number) in enumerate(BANKS):
            valid = (1 << width) - 1
            def read(off):
                return int(command(f"readl {base + off:#x}")[0], 0)
            def write(off, value):
                command(f"writel {base + off:#x} {value:#x}")
            def pin(n, level):
                command(f"set_irq_in /machine/gpio{bank} pin {n} {int(level)}")
            def pending(raw, masked):
                assert read(0x24) == raw and read(0x28) == masked
                assert irq.get(irq_number, False) == bool(masked)
            assert read(4) == read(0x10) == valid and read(0xc) == 0
            write(4, 0xffffffff)
            assert read(4) == valid
            pin(width - 1, True)
            assert read(0) == 1 << (width - 1)
            write(0, 3)
            assert read(0) == 1 << (width - 1), "input reads must ignore output latch"
            write(4, valid & ~3)
            assert read(0) == 3 | (1 << (width - 1))
            write(0x34, 1)
            assert read(0) & 3 == 2, "CLR changed another pin"
            write(0x30, 1)
            write(0x34, 0)
            assert read(0) & 3 == 3, "SET/zero command changed unrelated latch"
            pin(width - 1, False)
            write(4, valid)
            pin(0, False)
            write(0x20, 0)
            write(0xc, 1)
            write(0x14, 1)
            write(0x18, 1)
            pin(0, True)
            pending(1, 0)  # masked edge remains latched
            write(0x2c, 0)
            pending(1, 0)
            write(0x10, valid & ~1)
            pending(1, 1)
            write(0x2c, 2)
            pending(1, 1)
            write(0x2c, 1)
            pending(0, 0)
            pin(0, True)
            pending(0, 0)  # no transition
            write(0x18, 0)
            pin(0, False)
            pending(1, 1)
            write(0x2c, 1)
            write(0x1c, 1)
            pin(0, True)
            pending(1, 1)
            write(0x2c, 1)
            pin(0, False)
            pending(1, 1)
            write(0xc, 0)
            pending(0, 0)
            write(0xc, 1)
            pending(1, 1)  # disable hides, but does not acknowledge a captured edge
            write(0x2c, 1)
            write(0x14, 0)
            write(0x18, 1)
            pin(0, True)
            pending(1, 1)
            write(0x2c, 1)
            pending(1, 1)  # EOI cannot deassert an active level
            pin(0, False)
            pending(0, 0)
            write(0x18, 0)
            pending(1, 1)
            pin(0, True)
            pending(0, 0)
            write(4, valid & ~1)
            pin(0, False)
            pending(0, 0)  # output pin is not an IRQ input
            write(0x14, 1)
            write(0x18, 1)
            write(0x1c, 0)
            write(4, valid)
            write(0xc, 0)
            pin(0, True)
            write(0xc, 1)
            pending(0, 0)  # disabled transitions are not queued by this model
            top = 1 << (width - 1)
            write(0xc, top)
            write(0x14, top)
            write(0x18, top)
            write(0x10, 0)
            pin(width - 1, True)
            pending(top, top)
            write(0x2c, top)
            pending(0, 0)
            write(0xc, 0)
            for other, _, _ in BANKS:
                if other != base:
                    assert int(command(f"readl {other + 0x28:#x}")[0], 0) == 0
        before = [int(command(f"readl {base:#x}")[0], 0) for base, _, _ in BANKS]
        reset()
        for (base, width, irq_number), inputs in zip(BANKS, before):
            valid = (1 << width) - 1
            assert int(command(f"readl {base:#x}")[0], 0) == inputs
            for off, value in ((4, valid), (0xc, 0), (0x10, valid), (0x24, 0), (0x28, 0)):
                assert int(command(f"readl {base + off:#x}")[0], 0) == value
            assert not irq.get(irq_number, False)
            command(f"writel {base + 4:#x} 0")
            assert int(command(f"readl {base:#x}")[0], 0) == 0
    assert "unimplemented" not in diagnostics[0] and "Invalid" not in diagnostics[0], diagnostics

    # Observe both digital output data and drive-enable wires, not just MMIO storage.
    for bank, (base, width, _) in enumerate(BANKS):
        for wire in ("out", "output-enable"):
            with running(qemu) as (command, irq, _, diagnostics):
                command(f"irq_intercept_out /machine/gpio{bank} {wire}")
                command(f"writel {base + 0x30:#x} 1")
                assert not irq.get(0, False)
                command(f"writel {base + 4:#x} {((1 << width) - 1) & ~1:#x}")
                assert irq[0]
                command(f"writel {base + 0x34:#x} 1")
                assert irq[0] == (wire == "output-enable")
                command(f"writel {base + 4:#x} {(1 << width) - 1:#x}")
                assert not irq[0]
            assert "unimplemented" not in diagnostics[0] and "Invalid" not in diagnostics[0], diagnostics

    expected = []
    with running(qemu) as (command, _, _, diagnostics):
        base = BANKS[0][0]
        for off in (8, 0x2c, 0x30, 0x34, 0x38, 0x200, 0x204):
            command(f"readl {base + off:#x}")
            expected.append(f"unimplemented read +{off:#x}")
        for off in (8, 0x20, 0x24, 0x28, 0x38, 0x200, 0x204):
            command(f"writel {base + off:#x} 1")
            expected.append(f"unimplemented write +{off:#x}")
        command("set_irq_in /machine/gpio0 pin 23 1")
        expected.append("unimplemented input 23")
    for message in expected:
        assert message in diagnostics[0], (message, diagnostics)
    return {"banks": 3, "bit_spans": [23, 12, 3], "irqs": [34, 33, 29],
            "checks": ["direction/input versus latch", "SET/CLR and zero commands", "output wires",
                       "drive-enable wires", "rising", "falling", "dual edge", "high/low level",
                       "mask retains edge", "EOI selective clear", "enable gating",
                       "output excludes input IRQ", "bank isolation", "system reset preserves external inputs"],
            "unsupported_diagnostics": len(expected), "hardware_verified": False,
            "pad_mux_clock_and_debounce_modeled": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    print(json.dumps(gpio_test(parser.parse_args().qemu), indent=2))
