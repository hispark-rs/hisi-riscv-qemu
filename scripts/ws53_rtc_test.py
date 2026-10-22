#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS53 RTC two-channel digital tests under explicit timing/mask assumptions."""
import argparse
import json
from ws53_gpio_test import running

BASES = (0x57029100, 0x57029200)
NOTICE = 'ws53-rtc: assumed timing clock=32768 expiry=count+1 mask=high start=reload free=wrap'
UNKNOWN = 'ws53-rtc: unimplemented counter/mask timing without assume-timing profile'


def profile(sticky=False):
    return ('-global', 'ws53-rtc.assume-timing=on',
            '-global', f'ws53-rtc.sticky-request={"on" if sticky else "off"}')


def ns(ticks):
    return (ticks * 1_000_000_000 + 32767) // 32768


def access(command, channel):
    base = BASES[channel]
    def read(off):
        return int(command(f'readl {base+off:#x}')[0], 0)
    def write(off, value):
        command(f'writel {base+off:#x} {value:#x}')
    def sample():
        write(0x10, (read(0x10) & 15) | 32)
        assert read(0x10) & 64
        return read(8)
    def start(load, mode=0):
        write(0x10, 8 | (mode << 1))
        write(0x14, 1)
        write(0, load)
        write(0x10, 9 | (mode << 1))
    def step(delta):
        command(f'clock_step {delta}')
    def input_(signal, high):
        command(f'set_irq_in /machine/rtc{channel} {signal} 0 {int(high)}')
    return read, write, sample, start, step, input_


def rtc_test(qemu):
    with running(qemu, False) as (command, _, _, diagnostics):
        for ch in (0, 1):
            read, write, _, _, step, _ = access(command, ch)
            write(0, 3)
            write(0x10, 1)
            step(ns(4))
            assert read(0x10) == read(0x18) == 0
    assert diagnostics[0].splitlines() == [UNKNOWN] * 2, diagnostics
    for sticky in (False, True):
        with running(qemu, False, profile(sticky)) as (command, irq, reset, diagnostics):
            command('irq_intercept_in /machine/intc')
            for ch in (0, 1):
                read, write, sample, start, step, input_ = access(command, ch)
                other = access(command, 1-ch)[0]
                for mode in (0, 1, 2, 3):
                    reset()
                    start(3, mode)
                    assert sample() == 3
                    assert bool(read(0x10) & 32) == sticky
                    step(ns(3))
                    assert read(8) == 3  # old latch remains coherent
                    assert sample() == 0  # valid zero before final expiry tick
                    step(ns(4) - ns(3))
                    assert read(0x18) == 1 and read(0x1c) == 0
                    assert not irq.get(49+ch, False)
                    assert bool(read(0x10) & 1) == (mode in (1, 3))
                    if mode == 1:
                        assert sample() == 3
                    elif mode == 3:
                        assert sample() == 0xffffffff
                    write(0x10, read(0x10) & 7)
                    assert read(0x1c) == 1 and irq.get(49+ch, False)
                    write(0x14, 1)
                    assert read(0x18) == read(0x1c) == 0
                    assert not irq.get(49+ch, False) and other(0x18) == 0
                    write(0x10, (read(0x10) & 6) | 8)
                    step(ns(8))
                    assert read(0x18) == 0  # stop cancels the armed event
                    start(9)
                    assert sample() == 9  # explicit restart reloads
                reset()
                start(100)
                step(30_000)  # not a whole 32768-Hz cycle
                input_('clock', False)
                write(0x10, 41)
                assert not read(0x10) & 64
                step(1_000_000_000)
                input_('clock', True)
                assert read(0x10) & 64 and read(8) == 100
                step(518)
                assert sample() == 99  # fractional cycle survived the pause
                input_('stall-snapshot', True)
                write(0x10, 41)
                assert not read(0x10) & 64
                step(ns(3))
                input_('stall-snapshot', False)
                assert read(0x10) & 64 and read(8) == 96
                input_('clock', False)
                input_('stall-snapshot', True)
                reset()
                start(10)
                write(0x10, 41)
                input_('clock', True)
                assert not read(0x10) & 64  # reset retained external fault
                input_('stall-snapshot', False)
                assert read(8) == 10
                reset()
                start(0)
                assert sample() == 0
                step(ns(1))
                assert read(0x18) == 1 and not read(0x10) & 1
                start(0xffffffff)
                assert sample() == 0xffffffff
                step(ns(1 << 32) - 1)
                assert read(0x18) == 0 and sample() == 0
                step(1)
                assert read(0x18) == 1 and not read(0x10) & 1
                # Explicit model limitation/race: an RMW that read ENABLE=1
                # before expiry can write it back after hardware cleared it.
                # Do not mistake normal snapshot tests for proof of safety here.
                start(1)
                before_expiry = read(0x10)
                step(ns(2))
                assert not read(0x10) & 1
                write(0x10, before_expiry | 32)
                assert read(0x10) & 1 and read(8) == 1 and read(0x18) == 1
            # Simultaneous channels must not share count, latch, or IRQ state.
            reset()
            a, b = access(command, 0), access(command, 1)
            a[3](3); b[3](7)
            a[4](ns(4))
            assert a[0](0x18) == 1 and b[0](0x18) == 0 and b[2]() == 3
            a[1](0x14, 1)
            b[4](ns(8) - ns(4))
            assert a[0](0x18) == 0 and b[0](0x18) == 1
        assert diagnostics[0].splitlines() == [NOTICE] * 2, diagnostics

    rejected = 0
    with running(qemu, False, profile()) as (command, _, _, diagnostics):
        for ch, base in enumerate(BASES):
            read, write, sample, start, _, _ = access(command, ch)
            start(100)
            value = sample()
            operations = [f'{op} {base+off:#x}' for op, off in
                (('readb', 0), ('readw', 0), ('readq', 0), ('readl', 1),
                 ('readl', 4), ('readl', 12), ('readl', 20))]
            operations += [f'{op} {base+off:#x} {value:#x}' for op, off, value in
                (('writeb', 0, 1), ('writew', 0, 1), ('writeq', 0, 1), ('writel', 1, 1),
                 ('writel', 0, 1), ('writel', 4, 1), ('writel', 8, 1), ('writel', 12, 1),
                 ('writel', 16, 0x10), ('writel', 16, 0x80), ('writel', 16, 11),
                 ('writel', 20, 0), ('writel', 24, 1), ('writel', 28, 1))]
            for operation in operations:
                command(operation)
                assert read(0) == 100 and read(8) == value and read(0x18) == 0, operation
            write(0x10, 8)
            write(0x10, 9)
            write(0x10, 0x49)  # copying RO lock cannot make a valid snapshot
            assert not read(0x10) & 64
            read(8)
            rejected += len(operations) + 1
    lines = diagnostics[0].splitlines()
    assert lines.count(NOTICE) == 2
    invalid = [line for line in lines if line != NOTICE]
    assert len(invalid) == rejected and all(line.startswith('ws53-rtc: unimplemented') for line in invalid), lines
    return {'channels': 2, 'irqs': [49, 50], 'profile_count_bits': 32,
            'assume_timing_required': True, 'clock_hz': 32768, 'command_readback_profiles': 2,
            'modes_tested': [0, 1, 2, 3], 'invalid_accesses_diagnosed': rejected,
            'checks': ['zero/max count', 'latched not live words', 'masked raw pending',
                       'IRQ unmask/EOI', 'stop/restart', 'free-run wraps to u32 max',
                       'fractional clock pause', 'snapshot stall/recovery', 'channel isolation',
                       'reset retains external faults', 'read-only ready cannot be written'],
            'stale_enable_rmw_can_restart_one_shot_in_this_profile': True,
            'hardware_verified': False, 'common_bank_or_sleep_modeled': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    print(json.dumps(rtc_test(parser.parse_args().qemu), indent=2))
