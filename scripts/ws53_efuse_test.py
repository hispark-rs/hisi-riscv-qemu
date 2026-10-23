#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS53 eFuse read-only synthetic model; never accesses/programs physical OTP."""
import argparse
import json
import subprocess
from ws53_gpio_test import running

BASE = 0x57024000
PROFILE = ('-global', 'ws53-efuse.assume-profile=on')
NOTICE = 'ws53-efuse: assumed immutable-test-image boot-input timing1f one-word-per-read-command; NO programming'


class Efuse:
    def __init__(self, cmd): self.cmd = cmd
    def read(self, off): return int(self.cmd(f'readl {BASE+off:#x}')[0], 0)
    def write(self, off, value): self.cmd(f'writel {BASE+off:#x} {value:#x}')
    def boot(self, value=True): self.cmd(f'set_irq_in /machine/efuse boot-done 0 {int(value)}')
    def clock(self, value): self.cmd(f'set_irq_in /machine/efuse clock 0 {int(value)}')
    def fault(self, index, value=True): self.cmd(f'set_irq_in /machine/efuse fault {index} {int(value)}')
    def prop(self, name): return self.cmd.qmp('qom-get', {'path': '/machine/efuse', 'property': name})
    def fail_after(self, reads): self.cmd.qmp('qom-set', {'path': '/machine/efuse', 'property': 'fail-after-read', 'value': reads})
    def configure(self):
        self.boot(); self.write(0x34, 0x1234561f); self.write(0x30, 0xcafe5a5a)
    def word(self, slot):
        self.write(0x30, 0xcafe5a5a); return self.read(0x800+slot*4)


def expected(slot, key=19):
    return 0xdead0000 | ((slot*2*73+key)&255) | ((((slot*2+1)*73+key)&255)<<8)


def efuse_test(qemu):
    invalid = subprocess.run([qemu, '-M', 'ws53', '-accel', 'qtest', '-display', 'none', '-nodefaults',
                              '-global', 'ws53-efuse.test-byte-key=256'], capture_output=True, text=True, timeout=5)
    assert invalid.returncode != 0 and 'test-byte-key must fit u8' in invalid.stderr
    with running(qemu, False) as (cmd, _, _, diagnostics):
        e = Efuse(cmd); e.configure(); assert e.read(0x2c) == 0 and e.read(0x800) == 0
        assert e.prop('data-reads') == 0
    assert diagnostics[0].splitlines() == ['ws53-efuse: unimplemented read service without assume-profile',
        'ws53-efuse: unimplemented/unavailable read +0x800 size=4'], diagnostics

    for key in (0, 19, 255):
        with running(qemu, False, PROFILE+('-global', f'ws53-efuse.test-byte-key={key}')) as (cmd, _, reset, diagnostics):
            e = Efuse(cmd); assert e.read(0x2c) == e.read(0x30) == e.read(0x34) == 0
            e.configure()
            for slot in range(128):
                assert e.word(slot) == expected(slot, key)
                assert e.read(0x30) == 0xcafe5a5a and e.read(0x34) == 0x1234561f
                assert e.read(0x2c) == 4
            assert e.prop('data-reads') == 128 and e.prop('read-commands') == 129
            reset(); assert e.prop('data-reads') == e.prop('read-commands') == 0
            # Externally asserted boot input persists; reset is not a boot model.
            assert e.read(0x2c) == 4 and e.read(0x30) == e.read(0x34) == 0
            e.configure(); assert e.word(127) == expected(127, key)
        assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    with running(qemu, False, PROFILE) as (cmd, _, reset, diagnostics):
        e = Efuse(cmd); e.configure()
        e.clock(False); assert e.read(0x2c) == 0
        e.clock(True); assert e.read(0x2c) == 4
        assert e.word(0) == expected(0)
        e.boot(False); assert e.read(0x2c) == 0; e.boot()
        e.fault(0); assert e.read(0x2c) == 0; e.fault(0, False)
        e.fault(1); assert e.read(0x30) == 0xcafe1234; e.fault(1, False)
        e.fault(2); assert e.read(0x34) == 0x12345607; e.fault(2, False)
        e.fault(3); e.write(0x30, 0xcafe5a5a); assert e.read(0x30) == 0xcafe1234
        e.fault(3, False); assert e.word(1) == expected(1)
        e.fail_after(e.prop('data-reads')+1)
        assert e.word(2) == expected(2) and e.read(0x2c) == 0
        e.fail_after(0); e.boot(); assert e.word(2) == expected(2)
        e.fault(0); reset(); e.configure(); assert e.read(0x2c) == 0
        e.fault(0, False); assert e.word(127) == expected(127)
    assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    rejected = 0
    with running(qemu, False, PROFILE) as (cmd, _, _, diagnostics):
        e = Efuse(cmd); e.configure()
        commands = [f'writel {BASE+0x30:#x} 0xcafea5a5', f'writel {BASE+0x30:#x} 0',
            f'writel {BASE+0x3c:#x} 1', 'writel 0x57030258 0xa5a5', 'readl 0x57030258',
            f'writel {BASE+0x800:#x} 0xffffffff', f'writew {BASE+0x800:#x} 1',
            f'writeb {BASE+0x800:#x} 1', f'writeq {BASE+0x800:#x} 1', f'writel {BASE+0x9fc:#x} 1',
            f'writel {BASE+0x2c:#x} 4', f'readl {BASE+0x3c:#x}', f'readl {BASE+0xa00:#x}',
            f'readb {BASE+0x800:#x}', f'readw {BASE+0x800:#x}', f'readq {BASE+0x800:#x}',
            f'readl {BASE+0x801:#x}', f'writew {BASE+0x30:#x} 0x5a5a', f'writew {BASE+0x34:#x} 0x1f']
        for command in commands:
            cmd(command); rejected += 1
            assert e.prop('data-reads') == 0 and e.prop('read-commands') == 1
            assert e.read(0x30) == 0xcafe5a5a and e.read(0x34) == 0x1234561f
        # No command means no second data transaction; no silent zero success.
        assert e.read(0x800) == expected(0)
        assert e.read(0x800) == 0; rejected += 1
        for slot in range(128): assert e.word(slot) == expected(slot)
        # Changes to clock, boot or timing invalidate any previous command.
        for kind in ('clock', 'boot', 'timing'):
            e.write(0x30, 0xcafe5a5a)
            if kind == 'clock': e.clock(False)
            elif kind == 'boot': e.boot(False)
            else: e.write(0x34, 0x12345607)
            before = e.prop('data-reads'); assert e.read(0x800) == 0; rejected += 1
            assert e.prop('data-reads') == before
            e.clock(True); e.configure()
    lines = diagnostics[0].splitlines()
    assert lines[0] == NOTICE and len(lines) == rejected+1, diagnostics
    assert all('ws53-efuse:' in line and ('rejected' in line or 'unimplemented' in line or 'unsupported' in line) for line in lines[1:]), diagnostics
    return {'synthetic_contents_checked': 3*256, 'upper_bits_and_stride_verified': True,
            'post_read_state_loss_and_recovery': True, 'clock_boot_mode_timing_faults': True,
            'immutable_after_program_attempts_and_reset': True, 'rejected_accesses': rejected,
            'hardware_accessed': False, 'physical_otp_programmed': False}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--qemu', required=True)
    print(json.dumps(efuse_test(p.parse_args().qemu), indent=2))
