#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS53 PWM digital-profile tests; channel outputs are not physical pad proof."""
import argparse
import json
from ws53_gpio_test import running

BASE = 0x52082000
NOTICE = 'ws53-pwm: assumed timing clock=32000000 width=16 clear=immediate deferred=block-end'
UNKNOWN = 'ws53-pwm: unimplemented timing without assume-timing profile'
PROFILE = ('-global', 'ws53-pwm.assume-timing=on')


def access(command):
    def read(off):
        return int(command(f'readl {BASE+off:#x}')[0], 0)
    def write(off, value):
        command(f'writel {BASE+off:#x} {value:#x}')
    def clock(on=True):
        command(f'writew 0x52000040 {0x80 if on else 0}')
        if on:
            command('writew 0x5200007c 0x41')
            command('writew 0x5200007c 0x43')
    def configure(ch, period=32, duty=8, offset=4, cycles=0, polarity=0, oe=0):
        for off, value in ((0, 1), (4, polarity), (8, oe), (12, offset),
                           (20, period), (28, duty), (40, cycles)):
            write(0x100 + 0x40*ch + off, value)
    def commit(group, mask, clear=True):
        write(group*16, mask)
        write(group*16+4, int(clear))
        write(group*16+8, 1)
    def step(ns):
        command(f'clock_step {ns}')
    return read, write, clock, configure, commit, step


def pwm_test(qemu):
    with running(qemu, False) as (command, _, _, diagnostics):
        read, _, clock, configure, commit, step = access(command)
        clock(); configure(0, cycles=1); commit(0, 1); step(1000)
        assert read(0x124) == read(0x12c) == 0
    assert diagnostics[0].splitlines() == [UNKNOWN], diagnostics

    # Each group and channel independently: phase edges, shadow writes, polarity.
    with running(qemu, False, PROFILE) as (command, out, reset, diagnostics):
        command('irq_intercept_out /machine/pwm out')
        read, write, clock, configure, commit, step = access(command)
        for ch in range(8):
            reset(); clock(); configure(ch); commit(ch % 4, 1 << ch)
            assert not out.get(ch, False)
            step(124); assert not out.get(ch, False)
            step(1); assert out.get(ch, False)
            step(249); assert out.get(ch, False)
            step(1); assert not out.get(ch, False)
            step(625); assert read(0x12c + ch*64) == 1
            # Shadow duty change does not affect live output until a commit.
            write(0x11c + ch*64, 0)
            step(125); assert out.get(ch, False)
            commit(ch % 4, 1 << ch, False)
            step(875); step(125); assert not out.get(ch, False)
            assert read(0x124 + ch*64) == 0  # continuous has no block-end flag
            configure(ch, polarity=1); commit(ch % 4, 1 << ch)
            assert out.get(ch, False)
            step(125); assert not out.get(ch, False)
            step(250); assert out.get(ch, False)
        # Duty zero/full and fractional 31.25ns ticks (no invented minimum duty).
        reset(); clock(); configure(0, period=4, duty=4, offset=0); commit(0, 1)
        assert out.get(0, False)
        step(125); assert out.get(0, False) and read(0x12c) == 1
        configure(0, period=4, duty=0, offset=0); commit(0, 1)
        assert not out.get(0, False)
        configure(0, period=4, duty=1, offset=1); commit(0, 1)
        step(31); assert not out.get(0, False)
        step(1); assert out.get(0, False)
        step(30); assert out.get(0, False)
        step(1); assert not out.get(0, False)
        # Both divider enable and gate stop time, preserving fractional phase.
        reset(); clock(); configure(0, period=4, duty=1, offset=1); commit(0, 1)
        step(30); clock(False); step(10000); clock()
        step(1); assert not out.get(0, False)
        step(1); assert out.get(0, False)
        command('writew 0x5200007c 0x42'); step(10000)
        assert out.get(0, False)
        command('writew 0x5200007c 0x43'); step(31)
        assert not out.get(0, False)
    assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    with running(qemu, False, PROFILE) as (command, irq, reset, diagnostics):
        command('irq_intercept_in /machine/intc')
        read, write, clock, configure, commit, step = access(command)
        # All four groups concurrently, with finite stop staging on only ch0.
        clock()
        for ch in range(8):
            configure(ch, cycles=3)
        for group in range(4):
            commit(group, 3 << (group*2))
        assert all(read(0x124+ch*64) for ch in range(8))
        write(0x518, 255)  # Initial configuration load is not block completion.
        write(0x100, 0); commit(0, 3, False)
        step(2999); assert not any(read(0x124+ch*64) for ch in range(8))
        step(1); assert all(read(0x124+ch*64) for ch in range(8))
        assert not irq.get(72, False)  # latched, masked event
        write(0x510, 2); assert irq.get(72, False)
        write(0x518, 1); assert irq.get(72, False) and read(0x124) == 0
        write(0x518, 254); assert not irq.get(72, False)
        step(3000)
        assert read(0x124) == 0 and all(read(0x124+ch*64) for ch in range(1, 8))
        write(0x518, 255)
        # Delayed second commit cannot retroactively stop the first block:
        # expose the SDK's short-block/interrupt-latency hazard, don't hide it.
        reset(); clock(); configure(0, period=16, duty=8, offset=0, cycles=1)
        commit(0, 1); write(0x100, 0); step(1000)
        assert read(0x124) == 1  # two blocks have elapsed before stop staging
        commit(0, 1, False); write(0x518, 1); step(499)
        assert read(0x124) == 0
        step(1); assert read(0x124) == 1
        write(0x518, 1); step(1000); assert read(0x124) == 0
        # Invalid count and group overlap are separate, maskable, W1C sources.
        reset(); clock(); configure(0, period=0, duty=0, offset=0)
        commit(0, 1); assert read(0x504) == 1 and not irq.get(71, False)
        write(0x510, 1); assert irq.get(71, False)
        write(0x50c, 2); assert irq.get(71, False)
        write(0x50c, 1); assert irq.get(71, False) and read(0x504) == 1
        assert read(0x124) == 1  # invalid count still produces load notification
        write(0x510, 0); assert not irq.get(71, False)
        configure(0); commit(0, 1); write(0x50c, 1); write(0x518, 1)
        assert read(0x504) == read(0x124) == 0
        write(0x510, 1); assert not irq.get(71, False)
        commit(1, 1)
        assert read(0x500) == 1 and irq.get(71, False)
        write(0x508, 1); assert irq.get(71, False) and read(0x500) == 1
        write(0x510, 0); write(0x10, 0); commit(0, 1); write(0x508, 1)
        assert read(0x500) == 0
        write(0x510, 1); assert not irq.get(71, False)
        # A rejected group is atomic: ch1 must not start if ch0 is invalid.
        reset(); clock(); configure(0, duty=33); configure(1, cycles=1)
        commit(0, 3); step(10000)
        assert read(0x504) == 1 and read(0x164) == 0
        # Clock fault survives reset; recovery starts only from explicit config.
        command('set_irq_in /machine/pwm clock 0 0'); reset(); clock()
        configure(7, cycles=1); commit(3, 128); step(10000)
        assert read(0x2e4) == 0
        command('set_irq_in /machine/pwm clock 0 1'); step(1000)
        assert read(0x2e4) == 1
        reset(); assert read(0x500) == read(0x504) == read(0x510) == 0
        assert not irq.get(71, False) and not irq.get(72, False)
    assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    with running(qemu, False, PROFILE) as (command, enabled, reset, diagnostics):
        command('irq_intercept_out /machine/pwm output-enable')
        read, write, clock, configure, commit, step = access(command)
        clock(); configure(0, oe=1); commit(0, 1)
        assert not enabled.get(0, False)
        step(125); assert enabled.get(0, False)
        step(250); assert not enabled.get(0, False)
        # The modeled 16-bit counter wraps; one large time jump also tests
        # bounded catch-up arithmetic rather than millions of edge callbacks.
        reset(); clock(); configure(0, period=32, duty=0, offset=0); commit(0, 1)
        step(65_535_000); assert read(0x12c) == 65535
        step(1000); assert read(0x12c) == 0
        write(0x100, 0); commit(0, 1)
        configure(1, period=65535, duty=0, offset=0, cycles=65535)
        commit(1, 2)
        assert read(0x164) == 1
        write(0x518, 2)
        end_ns = (65535*65535*1_000_000_000 + 31_999_999)//32_000_000
        step(end_ns-1); assert read(0x164) == 0
        step(1); assert read(0x164) == 1
    assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    with running(qemu, False, PROFILE) as (command, _, _, diagnostics):
        read, write, clock, configure, commit, step = access(command)
        clock(); configure(0); commit(0, 1)
        operations = [f'{op} {BASE+off:#x}' for op, off in
                      (('readb', 0), ('readw', 0), ('readq', 0), ('readl', 1),
                       ('readl', 8), ('readl', 0x508), ('readl', 0x518), ('readl', 0x300))]
        operations += [f'{op} {BASE+off:#x} {value:#x}' for op, off, value in
                       (('writeb', 0, 0), ('writew', 0, 0), ('writeq', 0, 0),
                        ('writel', 1, 0), ('writel', 8, 0), ('writel', 8, 2),
                        ('writel', 0, 256), ('writel', 4, 2),
                        ('writel', 0x124, 1), ('writel', 0x12c, 1),
                        ('writel', 0x114, 65536), ('writel', 0x110, 1),
                        ('writel', 0x118, 1), ('writel', 0x120, 1),
                        ('writel', 0x510, 4), ('writel', 0x514, 1),
                        ('writel', 0x518, 256), ('writel', 0x508, 65536))]
        operations += ['writew 0x52000040 0x81', 'writew 0x5200007c 0xc3',
                       'writew 0x5200007c 0x40', 'writew 0x5200007c 0x3f']
        # Disable divider before the rejected rising-load value; restore below.
        rejected = len(operations)-1
        for operation in operations:
            command(operation)
            assert read(0) == 1 and read(4) == 1 and read(0x114) == 32
            assert read(0x110) == read(0x118) == read(0x120) == 0
            assert read(0x510) == read(0x514) == 0
        command('writew 0x5200007c 0x41')
        command('writew 0x5200007c 0x43')
        step(1000); assert read(0x12c) == 1
    invalid = [line for line in diagnostics[0].splitlines() if line != NOTICE]
    assert len(invalid) == rejected and all(line.startswith(('ws53-pwm: unimplemented', 'ws53-mclken0: unimplemented')) for line in invalid), diagnostics

    return {'channels': 8, 'groups': 4, 'irqs': [71, 72],
            'assume_timing_required': True, 'clock_hz': 32000000,
            'invalid_accesses_diagnosed': rejected,
            'checks': ['phase/duty/polarity', 'shadow and deferred commit',
                       'per-channel finite stop', 'concurrent groups',
                       'short-block late stop hazard', 'error/period IRQ and W1C',
                       'atomic invalid group rejection', 'fractional clock pause',
                       'reset and clock fault recovery', 'output-enable',
                       '16-bit wrap and maximum block', 'invalid MMIO rejected'],
            'hardware_verified': False, 'physical_pinmux_modeled': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    print(json.dumps(pwm_test(parser.parse_args().qemu), indent=2))
