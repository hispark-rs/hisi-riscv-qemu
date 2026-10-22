#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Run Rust GPIO traits/real ISR paths against externally driven QEMU inputs."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
from ws53_gpio_test import connect, BANKS


def gpio_ram_test(qemu, elf, all_features=False):
    transcript = []
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/ws53-pins.csv"
    pads = [list(map(int, line.split(",")[1:])) for line in fixture.read_text().splitlines()
            if line.startswith("PAD,")]
    pads = [row for row in pads if row[0] not in (44, 45, 46)]
    stages = [(bank, stage, step) for bank in range(3) for stage, step in (
        [("HIGH", 0), ("LOW", 0), ("HIGH", 1), ("RELEASED", 0), ("INPUT", 0)]
        + [(stage, step) for step in range(6) for stage in ("BASE", "ARM")]
        + [("PENDING", 0), ("CANCEL", 0), ("DROPPED", 0),
           ("PENDING_DROP", 0), ("DROPPED_INPUT", 0)])]
    with tempfile.TemporaryDirectory(prefix="ws53-gpio-ram-") as tmp:
        qt, serial, qm = [Path(tmp) / name for name in ("qtest", "uart", "qmp")]
        proc = subprocess.Popen([qemu, "-M", "ws53", "-S", "-display", "none",
            "-monitor", "none", "-serial", "null", "-serial", "null",
            "-serial", f"unix:{serial},server=on,wait=off", "-kernel", str(elf),
            "-qtest", f"unix:{qt},server=on,wait=off", "-qtest-log", "/dev/null",
            "-qmp", f"unix:{qm},server=on,wait=off",
            "-global", "ws53-uart.rx-timeout-ns=100000", "-d", "guest_errors"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with connect(qt) as control, control.makefile("rwb", buffering=0) as ctl, \
                 connect(serial) as uart, uart.makefile("rwb", buffering=0) as stream:
                def command(text):
                    ctl.write(text.encode() + b"\n")
                    result = ctl.readline().decode().split()
                    assert result and result[0] == "OK", (text, result)
                    return result[1:]
                # Distinct unrelated control bits must survive every pin RMW.
                for bank, (base, _, _) in enumerate(BANKS):
                    neighbor = 1 << (4, 5, 0)[bank]
                    for offset in (0x14, 0x18, 0x1c):
                        command(f"writel {base + offset:#x} {neighbor:#x}")
                with connect(qm) as qmp, qmp.makefile("rwb", buffering=0) as qio:
                    json.loads(qio.readline())
                    for name in ("qmp_capabilities", "cont"):
                        qio.write(json.dumps({"execute": name}).encode() + b"\n")
                        while True:
                            response = json.loads(qio.readline())
                            assert "error" not in response, response
                            if "return" in response:
                                break
                if all_features:
                    stream.write(b"\x00\xffWS53-RX\n")
                cursor = 0
                pad_cursor = 0
                while True:
                    line = stream.readline().decode().strip()
                    assert line, ("EOF", transcript)
                    transcript.append(line)
                    assert "FAIL" not in line, transcript
                    if line.startswith("WS53 PASS gpio_banks="):
                        assert cursor == len(stages), (cursor, stages[cursor:])
                        assert pad_cursor == 28, "pad configuration stages missing"
                        break
                    if line.startswith("WS53 PAD "):
                        _, _, pad_text, state = line.split()
                        pad, _, drive_bits, mux, control = pads[pad_cursor]
                        assert int(pad_text) == pad and state == "READY", line
                        expected = 0x2f | (0xc0 if drive_bits == 4 else 0)
                        assert int(command(f"readl {control:#x}")[0], 0) == expected, line
                        assert int(command(f"readl {mux:#x}")[0], 0) == 0, line
                        pad_cursor += 1
                        stream.write(b"g")
                        continue
                    if not line.startswith("WS53 GPIO "):
                        continue
                    _, _, bank_text, stage, step_text = line.split()
                    bank, step = int(bank_text), int(step_text)
                    assert (bank, stage, step) == stages[cursor], (line, stages[cursor])
                    cursor += 1
                    base = BANKS[bank][0]
                    bit = (2, 3, 2)[bank]
                    mask = 1 << bit
                    def read(offset):
                        return int(command(f"readl {base + offset:#x}")[0], 0)
                    def pin(high):
                        pad = (2, 35, 47)[bank]
                        command(f"set_irq_in /machine/pinctrl external-level {pad} {int(high)}")
                        command(f"set_irq_in /machine/pinctrl external-drive {pad} 1")
                    neighbor = 1 << (4, 5, 0)[bank]
                    valid = (1 << BANKS[bank][1]) - 1
                    assert read(4) & ~mask == valid & ~mask, "changed another pin's direction"
                    assert read(0x10) & ~mask == valid & ~mask, "changed another pin's mask"
                    for offset in (0x14, 0x18, 0x1c):
                        assert read(offset) & neighbor, "changed unrelated trigger configuration"
                    if stage in ("HIGH", "LOW"):
                        assert read(4) & mask == 0, line
                        assert bool(read(0) & mask) == (stage == "HIGH"), line
                    else:
                        assert read(4) & mask == mask, line
                    if stage in ("RELEASED", "DROPPED", "CANCEL", "DROPPED_INPUT"):
                        assert read(0xc) & mask == 0 and read(0x10) & mask == mask, line
                        assert read(0x24) & mask == 0, "cancel retained a pending edge"
                    if stage == "INPUT":
                        pin(True)
                    elif stage == "BASE":
                        pin(step % 2 == 1)
                    elif stage == "ARM":
                        assert read(0xc) & mask and read(0x10) & mask == 0, line
                        assert read(0x20) & mask == 0, "unmodeled debounce enabled"
                        pin(step % 2 == 0)
                    elif stage in ("PENDING", "PENDING_DROP"):
                        pin(False)
                        pin(True)
                        assert read(0x24) & mask and read(0x28) & mask, "edge did not become pending"
                    elif stage in ("CANCEL", "DROPPED_INPUT"):
                        pin(False)
                        pin(True)
                        pin(False)
                    stream.write(b"g")
        except Exception as error:
            raise AssertionError({"error": str(error), "uart": transcript}) from error
        finally:
            proc.terminate()
            _, errors = proc.communicate(timeout=5)
    assert b"unimplemented" not in errors.lower() and b"Invalid" not in errors, errors.decode()
    assert "WS53 PASS timer_irq=53 count=3" in transcript, transcript
    assert "WS53 PASS pinctrl_pads=28 readback=1 transfer=1" in transcript, transcript
    if all_features:
        for expected in ("WS53 PASS uart2_rx=10 empty=1",
                         "WS53 PASS uart_errors=3 partial=1 recovery=1",
                         "WS53 PASS uart_irq=39,41,42 notifications=6 masks=1 recovery=1"):
            assert expected in transcript, transcript
    return {"uart": transcript, "stages": len(stages), "pad_configuration_stages": 28, "banks": 3,
            "irqs": [34, 33, 29], "notifications_per_bank": 6,
            "all_features": all_features, "hardware_verified": False,
            "gpio_pad_configuration_verified_in_model": True,
            "alternate_peripheral_routes_and_clock_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    parser.add_argument("--elf", type=Path, required=True)
    parser.add_argument("--all-features", action="store_true")
    args = parser.parse_args()
    print(json.dumps(gpio_ram_test(args.qemu, args.elf, args.all_features), indent=2))
