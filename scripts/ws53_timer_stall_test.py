#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Direct QEMU TIMER fault tests; no firmware, hardware or clock-tree claims.

Live QMP interface: qom-set /machine timer0-stall or timer1-stall to a bool.
Assertions freeze only count/expiry, retain pending IRQ/configuration, preserve
fractional phase and resume the remaining interval without catch-up events.
External fault settings survive system_reset; old timer work does not.
"""
import argparse
import hashlib
import json
from pathlib import Path
from ws53_gpio_test import running


def access(command, channel):
    base = 0x52002100 + channel * 0x100
    def read(offset):
        return int(command(f'readl {base + offset:#x}')[0], 0)
    def write(offset, value):
        command(f'writel {base + offset:#x} {value:#x}')
    def snapshot():
        write(0x10, (read(0x10) & 15) | 32)
        assert read(0x10) & 64
        return read(8)
    def start(load=320, mode=0, masked=False):
        write(0x10, 0)
        write(0x14, 1)
        write(0, load)
        write(0x10, 1 | (mode << 1) | (8 if masked else 0))
    def stall(value):
        command.qmp('qom-set', {'path': '/machine', 'property': f'timer{channel}-stall', 'value': value})
        assert command.qmp('qom-get', {'path': '/machine', 'property': f'timer{channel}-stall'}) == value
    return read, write, snapshot, start, stall


def timer_stall_test(qemu):
    cases = []
    with running(qemu, False) as (command, irq, reset, diagnostics):
        command('irq_intercept_in /machine/intc')
        step = lambda value: command(f'clock_step {value}')
        for ch in (0, 1):
            read, write, snapshot, start, stall = access(command, ch)
            other = access(command, 1 - ch)
            assert command.qmp('qom-get', {'path': '/machine', 'property': f'timer{ch}-stall'}) is False
            for mode in range(4):
                reset()
                # Default-off behavior still expires at the existing 32MHz
                # deadline; modes1/3 reload, modes0/2 disable after expiry.
                start(mode=mode)
                assert read(0) == 335 and snapshot() == 320
                step(9999)
                assert snapshot() == 1 and read(0x18) == 0
                step(1)
                assert read(0x18) == 1 and irq.get(53 + ch, False)
                assert bool(read(0x10) & 1) == (mode in (1, 3))
                cases.append(f'ch{ch}/mode{mode}/default-deadline')

                reset()
                start(mode=mode); other[3](160)
                step(1501)  # preserve the fractional32MHz phase across pause
                assert snapshot() == 272
                before = (read(0), read(0x10))
                stall(True); stall(True)  # idempotent assertion
                assert (read(0), read(0x10)) == before
                step(1_000_000_000)
                assert snapshot() == 272 and read(0x18) == 0
                assert not irq.get(53 + ch, False)
                assert other[0](0x18) == 1 and irq.get(54 - ch, False)
                other[1](0x14, 1)
                assert not irq.get(54 - ch, False)
                stall(False); stall(False)  # idempotent release
                assert snapshot() == 272 and read(0x18) == 0
                step(30); assert snapshot() == 272
                step(1); assert snapshot() == 271
                step(8467); assert snapshot() == 1 and read(0x18) == 0
                step(1)
                assert read(0x18) == 1 and irq.get(53 + ch, False)
                assert other[0](0x18) == 0 and not irq.get(54 - ch, False)
                assert bool(read(0x10) & 1) == (mode in (1, 3))
                write(0x14, 1)
                step(9999)
                assert read(0x18) == 0  # no catch-up expiry during the pause
                step(1)
                assert read(0x18) == int(mode in (1, 3))
                cases.append(f'ch{ch}/mode{mode}/pause-resume-isolation')

            reset()
            stall(True)
            start()
            step(1_000_000)
            assert snapshot() == 320 and read(0x18) == 0
            # Disable while stalled cancels old work. LOAD writes while
            # disabled and a fresh enable start a new complete interval.
            write(0x10, 0); stall(False); step(1_000_000)
            assert read(0x18) == 0 and snapshot() == 0
            stall(True); start(640); step(1_000_000)
            assert snapshot() == 640 and read(0) == 655
            stall(False); step(19999)
            assert read(0x18) == 0
            step(1); assert read(0x18) == 1
            cases.append(f'ch{ch}/disabled-start-and-cancel')

            reset()
            start(mode=1, masked=True); step(10000)
            assert read(0x18) == 1 and read(0x1c) == 0
            stall(True)
            write(0x10, read(0x10) & ~8)
            assert read(0x1c) == 1 and irq.get(53 + ch, False)
            step(1_000_000)
            assert read(0x18) == 1  # fault does not discard existing pending
            write(0x14, 1)
            assert read(0x18) == read(0x1c) == 0 and not irq.get(53 + ch, False)
            step(1_000_000)
            assert read(0x18) == 0
            # FSM reset cancels expiry even though the external stall remains.
            write(0x10, 0x10)
            assert not read(0x10) & 1 and read(8) == 0
            stall(False); step(1_000_000)
            assert read(0x18) == 0 and not irq.get(53 + ch, False)
            cases.append(f'ch{ch}/pending-mask-ack-and-fsm-reset')

            start(); step(1000); stall(True); reset()
            assert command.qmp('qom-get', {'path': '/machine', 'property': f'timer{ch}-stall'}) is True
            assert all(read(off) == 0 for off in (0, 8, 0x10, 0x18, 0x1c))
            assert not irq.get(53 + ch, False)
            stall(False); step(1_000_000)
            assert read(0x18) == 0  # reset cannot resurrect the old deadline
            stall(True); start(32); step(1_000_000)
            assert snapshot() == 32 and read(0x18) == 0
            stall(False); step(999); assert read(0x18) == 0
            step(1); assert read(0x18) == 1
            cases.append(f'ch{ch}/system-reset-retains-fault-only')
        # Both assertions can coexist and can be released independently.
        reset()
        a, b = access(command, 0), access(command, 1)
        a[4](True); b[4](True); a[3](); b[3]()
        step(1_000_000)
        assert a[2]() == b[2]() == 320
        a[4](False); step(10000)
        assert a[0](0x18) == 1 and b[0](0x18) == 0
        assert irq.get(53, False) and not irq.get(54, False)
        b[4](False); step(10000)
        assert b[0](0x18) == 1 and irq.get(54, False)
        cases.append('both/independent-release')
    assert diagnostics == [''], diagnostics

    # Startup properties exercise the pre-initialization setter path too.
    with running(qemu, False, ('-machine', 'timer0-stall=on,timer1-stall=on')) as (command, _, reset, diagnostics):
        for ch in (0, 1):
            read, _, snapshot, start, stall = access(command, ch)
            start(32); command('clock_step 1000000')
            assert snapshot() == 32 and read(0x18) == 0
            stall(False); command('clock_step 1000')
            assert read(0x18) == 1
            cases.append(f'ch{ch}/startup-property')
    assert diagnostics == [''], diagnostics
    return {'passed': True, 'qemu_sha256': hashlib.sha256(Path(qemu).read_bytes()).hexdigest(),
            'cases': cases, 'case_count': len(cases), 'irqs': [53, 54],
            'interface': {'path': '/machine', 'properties': ['timer0-stall', 'timer1-stall'], 'default': False},
            'semantics': 'count/expiry pause; resume remaining interval and fractional phase; pending IRQ/MMIO retained; fault survives reset but old work does not',
            'unexpected_diagnostics': 0, 'hardware_accessed': False, 'hardware_clock_behavior_verified': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    print(json.dumps(timer_stall_test(parser.parse_args().qemu), indent=2))
