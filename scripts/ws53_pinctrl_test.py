#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS53 canonical pad mapping and digital routing tests (no analog claims)."""
import argparse
import json
from pathlib import Path
from ws53_gpio_test import running, BANKS


def pinctrl_test(qemu):
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/ws53-pins.csv"
    pads = [list(map(int, line.split(",")[1:])) for line in fixture.read_text().splitlines()
            if line.startswith("PAD,")]
    assert len(pads) == 31
    with running(qemu, direct_gpio=False) as (command, irq, reset, diagnostics):
        command("irq_intercept_out /machine/pinctrl level")
        def write(addr, value):
            command(f"writel {addr:#x} {value:#x}")
        def read(addr):
            return int(command(f"readl {addr:#x}")[0], 0)
        def signal(name, pad, value):
            command(f"set_irq_in /machine/pinctrl {name} {pad} {int(value)}")
        for pad, modes, drive_bits, mux, control in pads:
            assert read(control) == 4, "initial RAM profile, not a silicon reset claim"
            if mux:
                assert read(mux) == 0
                write(mux, 0)
            max_drive = 3 | (0xc0 if drive_bits == 4 else 0)
            write(control, max_drive | 0x2c)  # pull up, input + Schmitt enabled
            assert read(control) == max_drive | 0x2c
            assert irq.get(pad, False)
            bank = 0 if pad < 23 else 1 if pad < 44 else 2
            bit = pad if bank == 0 else pad - 32 if bank == 1 else pad - 45
            def input_high():
                return bool(read(BANKS[bank][0]) & (1 << bit))
            if mux:
                assert input_high(), f"pad {pad}: pull-up did not reach correct GPIO bit"
            write(control, 8)  # pull-up with input buffer disabled
            assert irq.get(pad, False), "input buffer must not disable the external pull"
            if mux:
                assert not input_high(), "disabled input buffer still reached GPIO"
            write(control, 0x14)  # input enabled, pull down
            assert not irq.get(pad, False)
            signal("external-level", pad, True)
            signal("external-drive", pad, True)
            assert irq.get(pad, False), "active external driver must override weak pull"
            if mux:
                assert input_high()
            signal("external-drive", pad, False)
            assert not irq.get(pad, False)
        # Preserve real external driver state over model reset, including the
        # transition from an input-disabled pad to the RAM boot profile.
        signal("external-level", 38, True)
        signal("external-drive", 38, True)
        write(0x57036044, 0)
        assert read(0x57028000) & (1 << 6) == 0
        reset()
        assert read(0x57036044) == 4 and read(0x57028000) & (1 << 6)
    assert "unimplemented" not in diagnostics[0] and "Invalid" not in diagnostics[0], diagnostics

    for wire in ("level", "drive-enable", "floating", "contention"):
        with running(qemu, direct_gpio=False) as (command, irq, reset, diagnostics):
            command(f"irq_intercept_out /machine/pinctrl {wire}")
            def write(addr, value):
                command(f"writel {addr:#x} {value:#x}")
            def signal(name, value):
                command(f"set_irq_in /machine/pinctrl {name} 2 {int(value)}")
            write(0x57036048, 4)
            assert irq.get(2, False) == (wire == "floating")
            write(0x5700c030, 4)
            write(0x5700c004, 0x7fffff & ~4)
            assert irq.get(2, False) == (wire in ("level", "drive-enable"))
            signal("external-level", False)
            signal("external-drive", True)
            assert irq.get(2, False) == (wire != "floating")
            signal("external-level", True)
            assert irq.get(2, False) == (wire in ("level", "drive-enable"))
            write(0x5700c004, 0x7fffff)
            assert irq.get(2, False) == (wire == "level")
            signal("external-drive", False)
            assert irq.get(2, False) == (wire == "floating")
        assert "unimplemented" not in diagnostics[0] and "Invalid" not in diagnostics[0], diagnostics

    with running(qemu, direct_gpio=False) as (command, irq, reset, diagnostics):
        command("irq_intercept_out /machine/pinctrl drive-enable")
        command("writel 0x5700c030 4")
        command("writel 0x5700c004 0x7ffffb")
        assert irq[2]
        command("writel 0x52008008 1")  # valid SDK alternate mode, not a modeled peripheral route
        assert not irq[2], "non-GPIO mux must disconnect the GPIO output"
        command("writel 0x52008008 0")
        assert irq[2]
        for text in ("readl 0x52008004", "writel 0x52008004 0",  # noncanonical hole
                     "readl 0x57036000", "writel 0x57036000 0",  # no AGPIO0 here
                     "writel 0x52008008 3", "writel 0x52008008 8",  # unsupported mode/width
                     "writel 0x57036054 0xc0", "writel 0x57036048 0x1c",  # drive/pull
                     "readl 0x5703a000", "set_irq_in /machine/pinctrl external-drive 1 1"):
            command(text)
        assert int(command("readl 0x52008008")[0], 0) == 0
        assert int(command("readl 0x57036054")[0], 0) == 4
        assert int(command("readl 0x57036048")[0], 0) == 4
    errors = diagnostics[0]
    assert errors.count("unimplemented") == 11, errors
    assert "alternate peripheral route" in errors, errors
    return {"canonical_pads": 31, "gpio_routes": 30, "pad_domains": 3,
            "checks": ["SDK address/drive map", "pull up/down", "input buffer gate",
                       "external drive overrides pull", "output enable/data routing",
                       "floating indication", "contention indication", "mux disconnect",
                       "reset preserves external driver", "invalid config retained"],
            "unsupported_diagnostics": 11, "alternate_peripheral_signals_modeled": False,
            "electrical_or_hardware_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    print(json.dumps(pinctrl_test(parser.parse_args().qemu), indent=2))
