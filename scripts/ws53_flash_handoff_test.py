#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Observed FlashBoot handoff contract, not full boot-chain qualification."""
import argparse
import json
from ws53_gpio_test import running

PROFILE = ("-machine", "flashboot-handoff=on", "-global", "ws53-wdt.assume-timing=on")
NOTICE = "ws53-wdt: assumed timing low-byte=255 expiry=count+1 stage=reload clock=32000"
STARTUP_ONLY = 'ws53: flashboot-handoff is a startup-only property'


def startup_only_test(qemu):
    rejected = 0
    for initial in (False, True):
        with running(qemu, False, PROFILE if initial else ()) as (cmd, _, reset, diagnostics):
            def get():
                return cmd.qmp('qom-get', {'path': '/machine', 'property': 'flashboot-handoff'})
            def mappings():
                return cmd.qmp('human-monitor-command', {'command-line': 'info mtree'})
            assert get() is initial
            initial_maps = mappings()
            for reset_round in range(2):
                cmd('writel 0x20050000 0x13579bdf')
                cmd('writel 0x2f000 0x2468ace0')
                for value in (False, True):
                    try:
                        cmd.qmp('qom-set', {'path': '/machine', 'property': 'flashboot-handoff', 'value': value})
                    except AssertionError as error:
                        # The existing qtest helper raises on a QMP error. Only
                        # this exact expected response counts as a rejection.
                        reply = error.args[0]
                        assert reply == {'error': {'class': 'GenericError', 'desc': STARTUP_ONLY}}, reply
                    else:
                        raise AssertionError(('post-init handoff property accepted', initial, value, reset_round))
                    rejected += 1
                    assert get() is initial and mappings() == initial_maps
                    assert int(cmd('readl 0x20050000')[0], 0) == 0x13579bdf
                    assert int(cmd('readl 0x2f000')[0], 0) == 0x2468ace0
                    if initial:
                        assert int(cmd('readl 0x57030234')[0], 0) == 0x3f0
                        assert int(cmd('readl 0x57030238')[0], 0) == 0x20
                reset()
                assert get() is initial and mappings() == initial_maps
                expected = 0xa5a5a5a5 if initial else 0x13579bdf
                assert int(cmd('readl 0x20050000')[0], 0) == expected
                expected = 0xa5a5a5a5 if initial else 0x2468ace0
                assert int(cmd('readl 0x2f000')[0], 0) == expected
        assert diagnostics[0].splitlines() == ([NOTICE] if initial else []), diagnostics
    return {'passed': True, 'post_init_rejections': rejected, 'initial_modes': [False, True],
            'same_value_and_changed_value_rejected': True, 'maps_and_ram_preserved_on_rejection': True,
            'reset_keeps_selected_startup_profile': True, 'hardware_accessed': False}


def handoff_test(qemu):
    startup_only = startup_only_test(qemu)
    with running(qemu, False, PROFILE) as (cmd, _, reset, diagnostics):
        def read(addr):
            return int(cmd(f"readl {addr:#x}")[0], 0)
        def write(addr, value):
            cmd(f"writel {addr:#x} {value:#x}")
        assert read(0x57030234) == 0x3f0
        assert read(0x57030238) == 0x20
        assert read(0x57030604) == read(0x5703096c) == 0
        assert read(0x5702b010) == 0x3d
        assert int(cmd("readw 0x52000044")[0], 0) == 0xffff
        assert read(0x2f000) == read(0x20050000) == 0xa5a5a5a5
        assert read(0x20010000) == 0  # bank2 initially assigned to ITCM
        write(0x57030238, 0x3c)
        assert read(0x20010000) == 0xa5a5a5a5
        write(0x20050000, 0x12345678)
        write(0x57030234, 0x3f8)  # reject ownership change while clocks ON
        assert read(0x57030234) == 0x3f0
        write(0x57030234, 0x300)
        assert read(0x20050000) == 0  # gated; bootstrap stack remains accessible
        assert read(0x2f000) == 0xa5a5a5a5
        write(0x20050000, 0xbadbad)  # blocked store cannot alter underlying RAM
        write(0x57030234, 0x308)
        write(0x57030234, 0x3f8)
        assert read(0x20050000) == 0x12345678
        # A pair actually handed to the control core becomes inaccessible.
        write(0x57030234, 0x308)
        write(0x57030234, 0x30c)
        write(0x57030234, 0x3fc)
        assert read(0x20050000) == 0 and read(0x20040000) == 0xa5a5a5a5
        for address, value in ((0x5703096c, 7), (0x57030604, 64),
                               (0x57030238, 0x3e), (0x57030234, 0x4fc)):
            write(address, value)
        cmd("writew 0x57030238 0x20")
        assert read(0x57030238) == 0x3c
        assert read(0x57030604) == read(0x5703096c) == 0
        assert read(0x57030234) == 0x3fc
        reset()
        assert read(0x57030234) == 0x3f0 and read(0x57030238) == 0x20
        assert read(0x20050000) == 0xa5a5a5a5
        assert read(0x5702b010) == 0x3d
        # The inherited WDT is running, not a register-only enabled bit.
        cmd("clock_step 7000000000")
        assert read(0x5702b018) == 1
    lines = diagnostics[0].splitlines()
    assert lines.count(NOTICE) == 1, lines
    assert sum("ws53-memory: unimplemented write" in x for x in lines) == 6, lines
    assert sum("inaccessible DTCM read" in x for x in lines) == 3, lines
    assert sum("inaccessible DTCM write" in x for x in lines) == 1, lines
    assert sum("unimplemented SoC reset topology" in x for x in lines) == 1, lines
    assert len(lines) == 12, lines
    return {"passed": True, "ram_poison": "a5", "gate_ownership_reset": True,
            "startup_only": startup_only,
            "inherited_watchdog_expiry": True, "rom_or_vendor_firmware_emulated": False,
            "hardware_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    print(json.dumps(handoff_test(parser.parse_args().qemu), indent=2))
