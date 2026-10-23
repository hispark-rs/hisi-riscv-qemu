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
from ws53_gpio_test import gpio_test
from ws53_gpio_ram_test import gpio_ram_test
from ws53_pinctrl_test import pinctrl_test
from ws53_nmi_test import nmi_test
from ws53_wdt_test import wdt_test
from ws53_tcxo_test import tcxo_test
from ws53_rtc_test import rtc_test
from ws53_pwm_test import pwm_test
from ws53_i2c_test import i2c_test
from ws53_spi_test import spi_test
from ws53_tsensor_test import tsensor_test
from ws53_i2s_test import i2s_test
from ws53_adc_test import adc_test


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
                write(0x114, 0)
                assert read(0x118) == 1, "EOI zero must not clear"
                write(0x114, 1)
                step(20_000_000)
                assert read(0x118) == 0, "one-shot rearmed itself"

                write(0x200, 320_000)
                write(0x210, 11)  # periodic, masked
                step(10_000_000)
                assert read(0x218) == 1 and read(0x21c) == 0
                write(0x210, 3)  # unmask already pending interrupt
                assert read(0x21c) == 1
                write(0x114, 1)  # clearing channel 0 must preserve channel 1
                assert read(0x218) == read(0x21c) == 1
                write(0x214, 1)
                assert read(0x218) == read(0x21c) == 0
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
                assert read(0x100) == read(0x200) == read(0x118) == read(0x218) == 0
        finally:
            proc.terminate()
            _, errors = proc.communicate(timeout=5)
        assert b"unimplemented" not in errors.lower(), errors.decode(errors="replace")
        return ["32MHz", "one-shot", "periodic", "mask/unmask", "W1C",
                "channel isolation", "snapshot", "stop", "system reset"]


def unsupported_test(qemu):
    """Unknown addresses must be diagnosed, not accepted as zero-valued MMIO."""
    reads = (0x60, 0x64, 0x68, 0x78, 0x7c, 0x80, 0x88, 0x8c, 0x90, 0x114, 0x214)
    writes = (0x64, 0x78, 0x88)
    with tempfile.TemporaryDirectory(prefix="ws53-unknown-") as tmp:
        qt = Path(tmp) / "qtest"
        proc = subprocess.Popen([qemu, "-M", "ws53", "-accel", "qtest",
            "-display", "none", "-monitor", "none", "-serial", "null",
            "-qtest", f"unix:{qt},server=on,wait=off", "-d", "guest_errors"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with connect(qt) as sock, sock.makefile("rwb", buffering=0) as io:
                for operation, offsets in (("read", reads), ("write", writes)):
                    for off in offsets:
                        value = " 1" if operation == "write" else ""
                        io.write(f"{operation}l {0x52002000 + off:#x}{value}\n".encode())
                        assert io.readline().startswith(b"OK")
                # Unsupported UART command reads / PRO register / TX IRQ do not
                # silently become shadow storage or a misleading zero value.
                for base in (0x52081000, 0x52080000, 0x57014000):
                    for operation, off, value in (("read", 0x24, ""), ("read", 0x40, ""),
                                                   ("write", 0x64, " 1"), ("write", 0x18, " 8"),
                                                   ("write", 0x24, " 0")):
                        io.write(f"{operation}l {base + off:#x}{value}\n".encode())
                        assert io.readline().startswith(b"OK")
        finally:
            proc.terminate()
            _, errors = proc.communicate(timeout=5)
        for operation, offsets in (("read", reads), ("write", writes)):
            for off in offsets:
                diagnostic = f"ws53: unimplemented TIMER {operation} +{off:#x}"
                assert diagnostic.encode() in errors, errors.decode(errors="replace")
        for operation, off in (("read", 0x24), ("read", 0x40), ("write", 0x64), ("write", 0x18), ("write", 0x24)):
            diagnostic = f"ws53-uart: unimplemented {operation} +{off:#x}"
            assert errors.count(diagnostic.encode()) == 3, errors.decode(errors="replace")
        return {"diagnosed_reads": len(reads), "diagnosed_writes": len(writes),
                "uart_diagnostics": 15}


def uart_test(qemu):
    """Exercise all three UARTs without a guest CPU or physical pins."""
    with tempfile.TemporaryDirectory(prefix="ws53-uart-") as tmp:
        qt, qm = Path(tmp) / "qtest", Path(tmp) / "qmp"
        proc = subprocess.Popen([qemu, "-M", "ws53", "-accel", "qtest",
            "-display", "none", "-monitor", "none", "-serial", "null",
            "-global", "ws53-uart.rx-timeout-ns=100000",
            "-qtest-log", "/dev/null",
            "-qtest", f"unix:{qt},server=on,wait=off",
            "-qmp", f"unix:{qm},server=on,wait=off", "-d", "guest_errors"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with connect(qt) as sock, sock.makefile("rwb", buffering=0) as io:
                irq = {39: False, 41: False, 42: False}
                def command(text):
                    io.write(text.encode() + b"\n")
                    while True:
                        parts = io.readline().decode().strip().split()
                        if parts and parts[0] == "IRQ":
                            irq[int(parts[2])] = parts[1] == "raise"
                            continue
                        assert parts and parts[0] == "OK", (text, parts)
                        return parts[1:]
                bases = (0x52081000, 0x52080000, 0x57014000)
                command("irq_intercept_in /machine/intc")
                for index, base in enumerate(bases):
                    irq_number = (39, 41, 42)[index]
                    def read(off):
                        return int(command(f"readl {base + off:#x}")[0], 0)
                    def write(off, value):
                        command(f"writel {base + off:#x} {value:#x}")
                    assert read(0x44) == 2 and read(0x34) == 0xc0
                    assert read(0x4c) == 0 and (read(0) & 15) == 1
                    write(0x28, 1)  # SDK-defined FIFO test injection
                    write(0x24, 0x50)
                    for value in range(64):
                        write(0x40, value)
                    assert read(0x4c) == 64 and read(0x44) == 14
                    assert read(0) & 15 == 1 and not irq[irq_number], "RX ignored mask"
                    write(0x18, 4)
                    assert read(0) & 15 == 4 and irq[irq_number], "pending RX not unmasked"
                    write(0x18, 0)
                    assert read(0) & 15 == 1 and not irq[irq_number]
                    write(0x40, 0xaa)  # full FIFO drops new byte and records overrun
                    assert read(0x34) & 8
                    assert read(0x34) & 8 == 0, "overrun did not clear on status read"
                    for value in range(64):
                        assert read(4) == value, "FIFO pop order/overflow retention"
                    assert read(0x44) == 2 and read(0x4c) == 0
                    assert read(0x34) & 0x20 == 0, "empty FIFO falsely claims RX data"
                    # Framing and parity are captured once, then clear on LSR read.
                    write(0x18, 1)
                    write(0x40, 0x300 | 0x5a)
                    assert read(0) & 15 == 6 and irq[irq_number]
                    assert read(0x34) & 6 == 6
                    assert read(0x34) & 6 == 0 and not irq[irq_number]
                    assert read(4) == 0x5a
                    # Timeout interval is explicitly a test input, not a silicon fact.
                    write(0x24, 0x54)  # quarter-full RX threshold (16)
                    write(0x18, 4)
                    write(0x40, 0xa5)
                    assert read(0) & 15 == 1 and not irq[irq_number]
                    command("clock_step 99999")
                    assert read(0) & 15 == 1
                    command("clock_step 1")
                    assert read(0) & 15 == 12 and irq[irq_number]
                    assert read(4) == 0xa5 and read(0) & 15 == 1 and not irq[irq_number]
                    write(0x24, 0x50)
                    write(0x2c, 2)  # loopback uses DATA TX -> RX, not the test window
                    write(4, 0x42)
                    assert read(4) == 0x42
                    write(4, 0x99)
                    write(0x24, 0x50)
                    assert read(0x4c) == 0 and not irq[irq_number], "FIFO reset did not cancel IRQ"
                    command("clock_step 200000")
                    assert read(0) & 15 == 1, "reset leaked a timeout"
                    write(0x18, 0)
                    write(0x24, 0x40)  # disable FIFO with explicit RBR reset
                    write(0x40, 0x12)
                    write(0x40, 0x34)
                    assert read(0x4c) == 1 and read(0x34) & 8
                    assert read(4) == 0x34, "non-FIFO overrun must overwrite RBR"
                    write(0x24, 0x50)
                    write(0x38, index + 1)
                    # Other instances must retain their independent scratch/RX state.
                    for other, address in enumerate(bases):
                        expected = other + 1 if other <= index else 0
                        assert int(command(f"readl {address + 0x38:#x}")[0], 0) == expected
                    write(0x18, 5)
                    write(0x40, 0x300 | 0x67)  # leave pending data/error for system reset
                    write(0x18, 0)
                with connect(qm) as qmp, qmp.makefile("rwb", buffering=0) as stream:
                    json.loads(stream.readline())
                    for name in ("qmp_capabilities", "system_reset"):
                        stream.write(json.dumps({"execute": name}).encode() + b"\n")
                        while True:
                            result = json.loads(stream.readline())
                            assert "error" not in result, result
                            if "return" in result:
                                break
                command("clock_step 200000")
                for base in bases:
                    assert int(command(f"readl {base + 0x4c:#x}")[0], 0) == 0
                    assert int(command(f"readl {base + 0x34:#x}")[0], 0) == 0xc0
                    assert int(command(f"readl {base + 0x38:#x}")[0], 0) == 0
                    assert int(command(f"readl {base:#x}")[0], 0) & 15 == 1
        finally:
            proc.terminate()
            _, errors = proc.communicate(timeout=5)
        assert b"unimplemented" not in errors.lower(), errors.decode(errors="replace")
        assert b"Invalid" not in errors, errors.decode(errors="replace")
    return {"instances": 3, "rx_fifo_depth": 64, "injected_timeout_ns": 100000,
            "checks": ["empty status", "FIFO ordering", "mask/unmask", "IRQ line",
                       "overrun retention", "parity/framing clear", "injected timeout",
                       "loopback", "FIFO reset", "single-byte mode", "instance isolation", "system reset"],
            "hardware_timeout_verified": False}


def ram_test(qemu, elf, receive=False, receive_errors=False, receive_irq=False):
    # An explicit model-only timeout, never advertised as silicon UART timing.
    timeout = ["-global", "ws53-uart.rx-timeout-ns=100000"] if receive_irq else []
    proc = subprocess.Popen([qemu, "-M", "ws53", *timeout, "-display", "none",
        "-monitor", "none", "-serial", "null", "-serial", "null", "-serial", "stdio",
        "-kernel", str(elf), "-d", "guest_errors"], stdin=subprocess.PIPE if receive else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        output, errors = proc.communicate(input=b"\x00\xffWS53-RX\n" if receive else None, timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        output, errors = proc.communicate(timeout=5)
    assert b"WS53 RAM data/bss OK" in output, output.decode(errors="replace")
    assert b"WS53 PASS timer_irq=53 count=3" in output, output.decode(errors="replace")
    if receive:
        assert b"WS53 PASS uart2_rx=10 empty=1" in output, output.decode(errors="replace")
    if receive_errors:
        assert b"WS53 PASS uart_errors=3 partial=1 recovery=1" in output, output.decode(errors="replace")
    if receive_irq:
        assert b"WS53 PASS uart_irq=39,41,42 notifications=6 masks=1 recovery=1" in output, output.decode(errors="replace")
    assert b"WS53 FAIL" not in output, output.decode(errors="replace")
    assert b"unimplemented" not in errors.lower(), errors.decode(errors="replace")
    assert b"Invalid" not in errors, errors.decode(errors="replace")
    return output.decode().splitlines()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    parser.add_argument("--elf", type=Path)
    parser.add_argument("--elf-uart-rx", type=Path, help="RAM example built with uart-rx-smoke")
    parser.add_argument("--elf-uart-errors", type=Path, help="RAM example built with uart-error-smoke")
    parser.add_argument("--elf-uart-combined", type=Path, help="RAM example built with both UART test features")
    parser.add_argument("--elf-uart-irq", type=Path, help="RAM example built with uart-irq-smoke (QEMU only)")
    parser.add_argument("--elf-uart-all", type=Path, help="RAM example built with all three UART test features")
    parser.add_argument("--elf-gpio", type=Path, help="RAM GPIO traits/IRQ test (gpio-smoke, QEMU only)")
    parser.add_argument("--elf-all", type=Path, help="RAM test built with all UART and GPIO features")
    args = parser.parse_args()
    result = {"model": model_test(args.qemu), "unsupported": unsupported_test(args.qemu),
              "uart": uart_test(args.qemu), "gpio": gpio_test(args.qemu),
              "pinctrl": pinctrl_test(args.qemu), "nmi": nmi_test(args.qemu),
              "wdt": wdt_test(args.qemu),
              "tcxo": tcxo_test(args.qemu),
              "rtc": rtc_test(args.qemu),
              "pwm": pwm_test(args.qemu),
              "i2c": i2c_test(args.qemu),
              "spi": spi_test(args.qemu),
              "tsensor": tsensor_test(args.qemu),
              "i2s": i2s_test(args.qemu),
              "adc_calibration": adc_test(args.qemu),
              "hardware_verified": False}
    if args.elf:
        result["ram"] = ram_test(args.qemu, args.elf)
    if args.elf_uart_rx:
        result["ram_uart_rx"] = ram_test(args.qemu, args.elf_uart_rx, receive=True)
    if args.elf_uart_errors:
        result["ram_uart_errors"] = ram_test(args.qemu, args.elf_uart_errors, receive_errors=True)
    if args.elf_uart_combined:
        result["ram_uart_combined"] = ram_test(args.qemu, args.elf_uart_combined, receive=True, receive_errors=True)
    if args.elf_uart_irq:
        result["ram_uart_irq"] = ram_test(args.qemu, args.elf_uart_irq, receive_irq=True)
    if args.elf_uart_all:
        result["ram_uart_all"] = ram_test(args.qemu, args.elf_uart_all, receive=True, receive_errors=True, receive_irq=True)
    if args.elf_gpio:
        result["ram_gpio"] = gpio_ram_test(args.qemu, args.elf_gpio)
    if args.elf_all:
        result["ram_all"] = gpio_ram_test(args.qemu, args.elf_all, all_features=True)
    print(json.dumps(result, indent=2))
