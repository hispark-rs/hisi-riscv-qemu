#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""ADC/MCU_DIAG assumed capture profile: real RAM writes, hazards and recovery.

The explicit profile is a test environment, not silicon bus-ordering evidence.
In particular neither DONE nor a stopped producer proves buffer quiescence.
"""
import argparse
import json
import subprocess
from ws53_gpio_test import running
from ws53_adc_test import Bus, PROFILE as CAL_PROFILE, NOTICE as CAL_NOTICE

PROFILE = (*CAL_PROFILE, '-global', 'ws53-adc.assume-capture=on')
NOTICE = 'ws53-adc-capture: assumed DTCM exact-inclusive-end round-robin posted-write profile'
DIAG = 0x52004100
RAM = 0x20050040
SENTINEL = 0xdeadbeef
SCAN = (3 << 8) | (6 << 10) | (26 << 15) | (32 << 23)


class Capture(Bus):
    def dwrite(self, off, value): self.cmd(f'writel {DIAG+off:#x} {value:#x}')
    def dread(self, off): return self.mem(DIAG+off)
    def mem(self, address): return int(self.cmd(f'readl {address:#x}')[0], 0)
    def split(self, off, value):
        self.dwrite(off, value & 0xffff); self.dwrite(off+4, value >> 16)
    def address(self): return self.dread(0x120) | self.dread(0x124) << 16
    def calibrate(self):
        self.initialize()
        for stage in range(3): self.complete(stage)
    def configure(self, length=2, start=RAM, end=None, channels=255):
        self.write(0, SCAN | channels); self.write(0x28, 0)
        for off, value in ((0, 1), (4, 1), (12, 0x1ff), (0x100, 6)):
            self.dwrite(off, value)
        self.split(0x108, length); self.split(0x110, start)
        self.split(0x118, start + 4*length-1 if end is None else end)
        self.dwrite(0x104, 0x200)
    def arm(self, repeat=False): self.dwrite(0x104, 0x301 if repeat else 0x300)
    def scan(self): self.write(0x1c, 1)
    def stop(self): self.write(0x1c, 2); self.dwrite(0x104, 0)
    def fill(self, length, start=RAM):
        for i in range(length): self.cmd(f'writel {start+4*i:#x} {SENTINEL:#x}')
    def input(self, channel, value):
        for bit in range(12): self.fault('input', (value >> bit) & 1, channel*12+bit)
    def inflight(self): return self.prop('capture-in-flight')


def clean(diagnostics):
    assert diagnostics[0].splitlines() == [CAL_NOTICE, NOTICE], diagnostics


def capture_test(qemu):
    # Every nonempty channel set, sparse cycling, packing and both DTCM edges.
    words = 0
    with running(qemu, False, PROFILE) as (cmd, irq, reset, diagnostics):
        b = Capture(cmd); cmd('irq_intercept_in /machine/intc')
        for mask in range(1, 256):
            reset(); b.calibrate()
            inputs = [(mask*31 + ch*521) & 4095 for ch in range(8)]
            for ch, value in enumerate(inputs): b.input(ch, value)
            channels = [ch for ch in range(8) if mask & (1 << ch)]
            length = len(channels)*2+1
            b.fill(length+2, RAM-4); b.configure(length, channels=mask); b.arm(); b.scan()
            for i in range(length):
                b.step(9999); assert b.mem(RAM+4*i) == SENTINEL and b.inflight() == 0
                b.step(1); assert b.inflight() == 1 and b.mem(RAM+4*i) == SENTINEL
                b.step(4999); assert b.mem(RAM+4*i) == SENTINEL
                b.step(1); ch = channels[i % len(channels)]
                assert b.mem(RAM+4*i) == (inputs[ch] << 2) | (ch << 14)
                assert b.address() == RAM+4*i and b.inflight() == 0
                assert b.dread(0x128) == int(i == length-1)
            b.step(1000000)
            assert b.prop('capture-words') == length and b.prop('capture-cycles') == 1
            assert b.mem(RAM-4) == b.mem(RAM+4*length) == SENTINEL
            assert not irq.get(80, False)
            b.stop(); words += length
        for start in (0x20010000, 0x2005fffc):
            reset(); b.calibrate(); b.fill(1, start)
            b.configure(1, start, channels=128); b.arm(); b.scan(); b.step(15000)
            assert b.address() == start and b.mem(start) == (inputs[7] << 2) | (7 << 14)
            # Verify actual byte layout rather than only word-level reads.
            word = b.mem(start)
            for byte in range(4):
                assert int(cmd(f'readb {start+byte:#x}')[0], 0) == (word >> (8*byte)) & 255
            b.stop()
    clean(diagnostics)

    # High half of length/address and largest profile aperture: no 16-bit wrap.
    with running(qemu, False, PROFILE) as (cmd, _, _, diagnostics):
        b = Capture(cmd); b.calibrate()
        max_words = length = 0x50000 // 4
        b.configure(length, 0x20010000, channels=1); b.arm(); b.scan()
        b.step(length*15000)
        assert b.dread(0x128) == 1 and b.address() == 0x2005fffc
        assert b.prop('capture-words') == length and b.prop('capture-cycles') == 1
        raw = bytes.fromhex(cmd('read 0x20010000 0x50000')[0].removeprefix('0x'))
        assert raw == (400).to_bytes(4, 'little') * length
        b.stop()
    clean(diagnostics)

    # Producer stop, diagnostic disable and ADC reset cannot retract a posted beat.
    with running(qemu, False, PROFILE) as (cmd, _, reset, diagnostics):
        b = Capture(cmd)
        reset(); b.calibrate(); b.fill(1); b.configure(1); b.arm(); b.scan(); b.step(9999)
        b.stop(); b.step(1000000)
        assert b.inflight() == 0 and b.mem(RAM) == SENTINEL
        for action in ('stop', 'soft-reset', 'adc-clock', 'apb-gate', 'global-gate'):
            reset(); b.calibrate(); b.fill(2); b.configure(); b.arm(); b.scan(); b.step(11000)
            assert b.inflight() == 1 and b.mem(RAM) == SENTINEL
            if action == 'stop':
                b.stop(); assert b.dread(0x104) == 0
            elif action == 'soft-reset': cmd('writel 0x52000054 0')
            elif action == 'adc-clock': b.fault('clock', False)
            elif action == 'apb-gate': b.gate(0x52000044, 4, False)
            else: b.gate(0x52000548, 0x1000, False)
            b.step(3999); assert b.mem(RAM) == SENTINEL
            b.step(1); assert b.mem(RAM) == 400 and b.inflight() == 0
            b.step(1000000); assert b.mem(RAM+4) == SENTINEL
            b.fault('clock')
        # System reset is separately specified to cancel the model's bus master.
        reset(); b.calibrate(); b.fill(2); b.configure(); b.arm(); b.scan(); b.step(10000)
        reset(); b.step(1000000)
        assert b.mem(RAM) == SENTINEL and b.inflight() == b.prop('capture-words') == 0
    clean(diagnostics)

    # Profile timing knobs control real events, and external faults survive reset.
    with running(qemu, False, (*PROFILE, '-global','ws53-adc.sample-period-ns=7',
                              '-global','ws53-adc.write-latency-ns=11')) as (cmd,_,reset,diagnostics):
        b = Capture(cmd); b.fault('bus-stall'); reset(); b.calibrate(); b.fill(1)
        b.configure(1); b.arm(); b.scan(); b.step(6); assert b.inflight() == 0
        b.step(1); assert b.inflight() == 1
        b.step(1000); assert b.mem(RAM) == SENTINEL
        b.fault('bus-stall', False); b.step(10); assert b.mem(RAM) == SENTINEL
        b.step(1); assert b.mem(RAM) == 400
    clean(diagnostics)

    # Both sample clock and bus clock preserve exact outstanding time.
    with running(qemu, False, PROFILE) as (cmd, _, reset, diagnostics):
        b = Capture(cmd)
        for name, enable in (('sample-stall', True), ('clock', False), ('diag-clock', False)):
            reset(); b.calibrate(); b.fill(1); b.configure(1); b.arm(); b.scan(); b.step(1234)
            b.fault(name, enable); b.step(1000000); assert b.inflight() == 0
            b.fault(name, not enable); b.step(8765); assert b.inflight() == 0
            b.step(1); assert b.inflight() == 1
            b.step(5000); assert b.mem(RAM) == 400
        for name, enable in (('bus-stall', True), ('diag-clock', False)):
            reset(); b.calibrate(); b.fill(1); b.configure(1); b.arm(); b.scan(); b.step(11234)
            b.fault(name, enable); b.stop(); b.step(1000000)
            assert b.inflight() == 1 and b.mem(RAM) == SENTINEL and b.dread(0x104) == 0
            b.fault(name, not enable); b.step(3765); assert b.mem(RAM) == SENTINEL
            b.step(1); assert b.mem(RAM) == 400 and b.dread(0x128) == 1
    clean(diagnostics)

    # DONE/address can be visible while the final beat is still owned by hardware.
    with running(qemu, False, PROFILE) as (cmd, _, reset, diagnostics):
        b = Capture(cmd); b.calibrate(); b.fill(1); b.configure(1)
        b.fault('early-done'); b.arm(); b.scan(); b.step(10000); b.stop()
        assert b.dread(0x128) == 1 and b.address() == RAM and b.dread(0x104) == 0
        assert b.inflight() == 1 and b.mem(RAM) == SENTINEL
        b.step(5000); assert b.mem(RAM) == 400
        b.fault('early-done', False)
        # Continuous capture keeps DONE set while later data changes in-place.
        reset(); b.calibrate(); b.fill(1); b.configure(1); b.arm(True); b.scan()
        b.step(15000); assert b.mem(RAM) == 400 and b.dread(0x128) == 1
        b.input(1, 4095); b.step(10000)
        assert b.dread(0x128) == 1 and b.inflight() == 1
        b.step(5000); assert b.mem(RAM) == (4095 << 2) | (1 << 14)
        assert b.prop('capture-cycles') == 2
        b.stop(); b.step(100000); assert b.prop('capture-words') == 2
    clean(diagnostics)

    # A failed bus transaction withholds completion; explicit rearm recovers.
    with running(qemu, False, PROFILE) as (cmd, _, _, diagnostics):
        b = Capture(cmd); b.calibrate(); b.fill(1); b.configure(1); b.arm(); b.scan()
        b.fault('bus-error'); b.step(15000)
        assert b.mem(RAM) == SENTINEL and b.dread(0x128) == 0
        assert b.prop('capture-errors') == 1 and b.inflight() == 0
        b.step(100000); assert b.prop('capture-words') == 0
        b.stop(); b.fault('bus-error', False); b.configure(1); b.arm(); b.scan(); b.step(15000)
        assert b.mem(RAM) == 400 and b.dread(0x128) == 1
    assert diagnostics[0].splitlines() == [CAL_NOTICE, NOTICE,
        'ws53-adc-capture: RAM write failed, completion withheld'], diagnostics

    # Unsupported requests fail closed, including active reconfiguration.
    invalid = 0
    with running(qemu, False, PROFILE) as (cmd, _, reset, diagnostics):
        b = Capture(cmd)
        for length, start, end in ((0,RAM,RAM-1),(1,RAM+1,RAM+4),(1,0x2000fffc,0x2000ffff),
                (1,0x20060000,0x20060003),(0x40000000,RAM,RAM-1),
                (2,RAM,RAM+3),(2,RAM,RAM+8),(1,0x5208a000,0x5208a003)):
            reset(); b.calibrate(); b.configure(length,start,end); b.arm(); invalid += 1
            assert b.dread(0x104) == 0x200 and b.inflight() == 0
        reset(); b.calibrate(); b.fill(2); b.configure(); b.arm(); b.scan(); b.step(1000)
        b.write(0x24, 0x260); b.write(0, SCAN | 1); invalid += 2
        assert b.read(0x24) == 0x1ffff and b.read(0) == SCAN | 255
        b.dwrite(0x104, 0x200); invalid += 1  # Reject without cancelling sample timer.
        b.step(9000); assert b.inflight() == 1
        b.stop(); b.dwrite(0x110, 0x1000); b.dwrite(0x104, 0x300); invalid += 2
        assert b.dread(0x110) == RAM & 0xffff and b.dread(0x104) == 0
        b.step(5000); assert b.mem(RAM) == 400
        for command in ('readb 0x52004100','readw 0x52004100','readq 0x52004100',
                        'readl 0x52004101','readl 0x52004110','readl 0x5208a01c',
                        'writew 0x52004100 1','writel 0x52004228 1','writel 0x52004220 1',
                        'writel 0x52004224 1','writel 0x52004208 0x10000',
                        'writel 0x52004200 5','writel 0x52004100 2',
                        'writel 0x52004104 2','writel 0x52004108 3',
                        'writel 0x5200410c 0x200','writel 0x52004204 0x302',
                        'writel 0x5208a01c 3','writel 0x5208a028 1','writel 0x5208a000 0'):
            cmd(command); invalid += 1
        # Calibration prerequisite is enforced even with valid diagnostic setup.
        reset(); b.initialize(); b.configure(1); b.arm(); b.scan(); invalid += 1
        b.step(100000); assert b.inflight() == b.prop('capture-words') == 0
    lines = diagnostics[0].splitlines()
    assert lines[:2] == [CAL_NOTICE,NOTICE] and len(lines) == invalid+2, diagnostics
    assert all('unimplemented' in line for line in lines[2:]), diagnostics

    with running(qemu, False, (*PROFILE,'-global','ws53-adc.initial-trace=3')) as (cmd,_,_,diagnostics):
        b = Capture(cmd); b.calibrate(); b.fill(1); b.configure(1); b.arm()
        assert b.dread(8) == 3 and b.dread(0x104) == 0x200
        b.dwrite(8,0); b.arm(); b.scan(); b.step(15000); assert b.mem(RAM) == 400
    assert len(diagnostics[0].splitlines()) == 3 and 'unimplemented write' in diagnostics[0], diagnostics
    with running(qemu, False, CAL_PROFILE) as (cmd,_,_,diagnostics):
        b = Capture(cmd); b.calibrate(); b.configure(1); b.arm()
        assert b.dread(0x104) == 0x200 and b.inflight() == 0
    assert diagnostics[0].splitlines() == [CAL_NOTICE,
        'ws53-adc-capture: unimplemented arm without assume-capture'], diagnostics
    invalid_properties = ('sample-period-ns=0','sample-period-ns=1000000001',
                          'write-latency-ns=0','write-latency-ns=1000000001',
                          'initial-trace=4','assume-capture=on')
    for prop in invalid_properties:
        result = subprocess.run([qemu, '-M','ws53','-accel','qtest','-display','none',
            '-monitor','none','-serial','null','-global',f'ws53-adc.{prop}'],
            capture_output=True, text=True, timeout=5)
        assert result.returncode != 0 and 'invalid capture timing/trace/profile' in result.stderr, result
    return {'channel_masks':255, 'ram_words_checked':words+2+max_words, 'dtcm_edges':True,
            'maximum_aperture_bytes':0x50000, 'invalid_properties':len(invalid_properties),
            'posted_write_survives_producer_stop':True, 'early_done_is_not_quiescence':True,
            'repeat_done_is_not_quiescence':True, 'exact_clock_pauses':True,
            'bus_error_recovery':True, 'invalid_diagnostics':invalid,
            'inherited_trace_conflict':True, 'explicit_profile_required':True,
            'capture_hardware_verified':False, 'safe_buffer_release_proven':False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    print(json.dumps(capture_test(parser.parse_args().qemu), indent=2))
