#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS53 temperature profile: controlled samples, average, stale READY, faults and reset."""
import argparse
import json
import subprocess
from ws53_gpio_test import running

BASE = 0x59006300
PROFILE = ('-global', 'ws53-tsensor.assume-profile=on')
NOTICE = 'ws53-tsensor: assumed profile samples16 period256us report5ms sticky-ready'
UNKNOWN = 'ws53-tsensor: unimplemented START without assume-profile'


class Sensor:
    def __init__(self, cmd): self.cmd = cmd
    def read(self, off=8): return int(self.cmd(f'readl {BASE+off:#x}')[0], 0)
    def write(self, off, value): self.cmd(f'writel {BASE+off:#x} {value:#x}')
    def step(self, ns): self.cmd(f'clock_step {ns}')
    def fault(self, name, high): self.cmd(f'set_irq_in /machine/tsensor {name} 0 {int(high)}')
    def code(self, value):
        for bit in range(10): self.cmd(f'set_irq_in /machine/tsensor code {bit} {(value >> bit) & 1}')
    def count(self, name): return self.cmd.qmp('qom-get', {'path': '/machine/tsensor', 'property': name})
    def init(self):
        for off, value in ((0x30, 160), (4, 3), (0x10, 2), (0x34, 0), (0, 1)):
            self.write(off, value)


def tsensor_test(qemu):
    invalid = subprocess.run([qemu, '-M', 'ws53', '-accel', 'qtest', '-display', 'none', '-nodefaults',
        '-global', 'ws53-tsensor.initial-code=1024'], capture_output=True, text=True, timeout=5)
    assert invalid.returncode != 0 and 'initial-code must fit ten bits' in invalid.stderr
    with running(qemu, False) as (cmd, _, _, diagnostics):
        s = Sensor(cmd); s.init(); s.step(10000000)
        assert s.read(4) == 3 and s.read() == s.count('samples') == 0
    assert diagnostics[0].splitlines() == [UNKNOWN], diagnostics

    with running(qemu, False, PROFILE) as (cmd, _, reset, diagnostics):
        s = Sensor(cmd); assert s.read(4) == s.read() == 0
        s.code(100); s.init(); s.step(2048000)
        assert s.count('samples') == 8 and s.read() == 0
        s.code(900); s.step(2047999); assert s.read() == 0
        s.step(1); assert s.read() == (500 << 2 | 2)
        assert (s.count('starts'), s.count('samples'), s.count('reports')) == (1, 16, 1)
        # Reading does not clear READY; no claim that this is a fresh conversion.
        assert s.read() == s.read() == 2002
        s.code(933); s.step(4999999); assert s.read() == 2002
        s.step(1); assert s.read() == (933 << 2 | 2)
        # Raw code is never clamped to nominal conversion endpoints.
        for raw in range(1024):
            s.code(raw); s.step(5000000)
            assert s.read() == (raw << 2 | 2)
        # Floor arithmetic for a non-integral average; START deliberately clears
        # the latest latch and restarts only in this explicit test profile.
        s.code(0); s.write(0, 1); s.step(15*256000)
        s.code(15); s.step(256000); assert s.read() == 2
        # Stop source or stall conversion part-way; preserve partial sum and time.
        for name in ('clock', 'stall', 'corrupt-control'):
            s.code(118); s.write(0, 1); s.step(100000)
            s.fault(name, name != 'clock'); s.step(10000000)
            assert s.read() == 0
            if name == 'corrupt-control': assert s.read(4) == 0xdead
            s.fault(name, name == 'clock'); s.step(3995999); assert s.read() == 0
            s.step(1); assert s.read() == (118 << 2 | 2)
        # Already-ready values survive lost clock; stale data is observable.
        s.fault('clock', False); before = s.count('reports')
        s.code(1023); s.step(10000000)
        assert s.read() == (118 << 2 | 2) and s.count('reports') == before
        reset(); s.step(10000000)
        assert s.read(4) == s.read() == s.count('samples') == s.count('starts') == 0
        s.init(); s.step(10000000); assert s.read() == 0  # external clock fault persists
        s.fault('clock', True); s.step(4096000)
        assert s.read() == (1023 << 2 | 2)  # external input code persists, too
        # Reset after a partial average cancels its timer and old contribution.
        s.code(0); s.write(0, 1); s.step(2048000); reset(); s.code(933)
        s.init(); s.step(4096000); assert s.read() == (933 << 2 | 2)
    assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    count = 0
    with running(qemu, False, PROFILE) as (cmd, _, _, diagnostics):
        s = Sensor(cmd)
        s.write(0, 1); count += 1  # incomplete configuration cannot start
        assert s.count('starts') == 0
        s.init(); s.step(4096000); value = s.read()
        commands = [f'readb {BASE+8:#x}', f'readw {BASE+8:#x}', f'readq {BASE+4:#x}',
                    f'readl {BASE+5:#x}', f'writew {BASE+4:#x} 3',
                    f'writel {BASE+8:#x} 1', f'writel {BASE:#x} 0',
                    f'writel {BASE+4:#x} 0', f'writel {BASE+4:#x} 4',
                    f'writel {BASE+0x10:#x} 1', f'writel {BASE+0x30:#x} 159',
                    f'writel {BASE+0x34:#x} 1', f'readl {BASE+0xc:#x}',
                    f'writel {BASE+0xc:#x} 0']
        commands += [f'readl {BASE+off:#x}' for off in (0, 0x10, 0x30, 0x34)]
        for command in commands: cmd(command); count += 1
        assert s.read(4) == 3 and s.read() == value and s.count('starts') == 1
        s.fault('corrupt-control', True); s.write(4, 3); count += 1
        assert s.read(4) == 0xdead
    lines = diagnostics[0].splitlines()
    assert lines[0] == NOTICE and len(lines) == count+1, diagnostics
    assert all('unimplemented' in line for line in lines[1:]), diagnostics
    return {'raw_code_cases': 1024, 'average': '16-point floor, varying input',
            'first_report_ns_assumption': 4096000, 'report_period_ns_assumption': 5000000,
            'sticky_ready': True, 'clock_stall_control_faults': True,
            'reset_cancels_pending_average': True, 'invalid_mmio_diagnostics': count,
            'hardware_verified': False}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--qemu', required=True)
    print(json.dumps(tsensor_test(p.parse_args().qemu), indent=2))
