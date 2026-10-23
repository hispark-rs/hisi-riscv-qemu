#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Explicit asynchronous START profile; 1us is a test assumption, not a measured latency."""
import argparse
import json
import subprocess
from ws53_gpio_test import running
from ws53_pwm_test import access, PROFILE, NOTICE

LATCH = 'ws53-pwm: assumed START acceptance delay=1000ns; live configuration sampled at acceptance'
OPTIONS = (*PROFILE, '-global', 'ws53-pwm.counter-hz=2000000',
           '-global', 'ws53-pwm.start-latch-ns=1000')


def pwm_start_test(qemu):
    for invalid in (1, 999, 1001):
        result = subprocess.run([str(qemu), '-M', 'ws53', '-display', 'none',
            '-serial', 'none', '-S', '-global', f'ws53-pwm.start-latch-ns={invalid}'],
            capture_output=True, timeout=3)
        assert result.returncode and b'start-latch-ns must be' in result.stderr
    bursts = []
    with running(qemu, False, OPTIONS) as (command, levels, reset, diagnostics):
        command('irq_intercept_out /machine/pinctrl level')
        read, write, clock, configure, commit, step = access(command)
        def setup(count):
            reset(); clock(); command('writel 0x52008054 1')
            configure(0, period=200, duty=100, offset=0, cycles=count)
        # Old immediate EN overwrite loses initial enable before acceptance.
        setup(8); commit(0, 1); write(0x100, 0); step(1000)
        assert not levels.get(47, False) and read(0x124) == 0
        commit(0, 1, False); step(2_000_000)
        assert not levels.get(47, False) and read(0x124) == 0
        for count in (8, 16):
            setup(count); commit(0, 1)
            step(999); assert not levels.get(47, False) and read(0x124) == 0
            step(1); assert levels[47] and read(0x124) == 1
            # This flag exists before the first falling edge, not at block end.
            step(9000); assert levels[47] and read(0x124) == 1
            write(0x518, 1); assert read(0x124) == 0
            write(0x100, 0); step(1000); commit(0, 1, False); step(1000)
            assert levels[47] and read(0x124) == 0
            step(39000); assert not levels[47]  # first high ends at51us
            for pulse in range(1, count):
                step(50000); assert levels[47]
                step(50000); assert not levels[47]
                assert read(0x124) == 0
            step(50000); assert not levels[47] and read(0x124) == 1
            write(0x518, 1); step(10_000_000)
            assert not levels[47] and read(0x124) == 0
            bursts.append(dict(pulses=count, period_ns=100000, high_ns=50000))
        # Reset must cancel a START that has not yet been accepted.
        setup(8); commit(0, 1); reset(); clock(); step(2000)
        assert read(0x124) == 0 and not levels[47]
        setup(8); commit(0, 1); commit(0, 1); step(1000)
        setup(8); commit(0, 1); clock(False); step(1000)
    assert diagnostics[0].splitlines() == [NOTICE.replace('32000000', '2000000'), LATCH,
        'ws53-pwm: unimplemented overlapping START group0',
        'ws53-pwm: unimplemented START acceptance while clock gated'], diagnostics
    return dict(bursts=bursts, early_enable_overwrite_rejected=True,
        initial_load_flag_is_not_completion=True, pending_reset_cancels=True,
        unsupported_acceptance_conditions_diagnosed=True, hardware_latency_proven=False,
        assumed_start_acceptance_ns=1000)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    print(json.dumps(pwm_start_test(parser.parse_args().qemu), indent=2))
