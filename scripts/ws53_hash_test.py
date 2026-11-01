#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Qtest HASH: real compression, retained DMA reads, IRQ59 and fault recovery.

This is an explicit model profile; it proves neither actual chip timing nor
cross-core ownership, cache coherence, physical drain or Flash startup.
"""
import argparse
import hashlib
import json
import struct
from ws53_gpio_test import running
from ws53_hash_compression_test import PROFILES, pad

BASE = 0x52010000
PROFILE = ('-global', 'ws53-hash.assume-profile=on')
NOTICE = 'ws53-hash: assumed two-channel REE/ACK/ring/DMA profile; no ownership or physical drain proof'


class Hash:
    def __init__(self, command): self.cmd = command
    def read(self, offset): return int(self.cmd(f'readl {BASE+offset:#x}')[0], 0)
    def write(self, offset, value): self.cmd(f'writel {BASE+offset:#x} {value:#x}')
    def step(self, ns): self.cmd(f'clock_step {ns}')
    def count(self, name): return self.cmd.qmp('qom-get', {'path':'/machine/hash', 'property':name})
    def fault(self, channel, number, level=True):
        self.cmd(f'set_irq_in /machine/hash fault {(channel-1)*8+number} {int(level)}')
    def clock(self, enabled): self.cmd(f'set_irq_in /machine/hash clock 0 {int(enabled)}')
    def inject(self, name, value): self.cmd.qmp('qom-set', {'path':'/machine/hash', 'property':'inject-'+name, 'value':value})
    def memory(self, address, data): self.cmd(f'write {address:#x} {len(data)} 0x{data.hex()}')
    def bytes(self, address, length): return bytes.fromhex(self.cmd(f'read {address:#x} {length}')[0].removeprefix('0x'))
    def ch(self, channel, offset): return 0x5000+channel*0x100+offset
    def state(self, channel): return 0xa000+channel*0x80
    def addresses(self, channel): return (0x20050000+channel*0x2000, 0x2005f000+(channel-1)*32)

    def configure(self, channel, name, *, clear=True):
        bit = 1 << channel
        if clear:
            self.write(0x40, self.read(0x40) | (1 << (channel*4)))
            self.write(0x8604, self.read(0x8604) & ~bit)
            self.write(0x14, bit)
            self.write(0x68, bit)
            self.step(5000)
            assert self.read(0x14) & bit
            self.write(0x14, bit)
            assert not self.read(0x14) & bit
            self.write(0x8600, bit)
        algorithm, mode, _, _, iv = PROFILES[name]
        self.write(self.ch(channel, 0), 0x80000a00)
        self.write(self.ch(channel, 0x10), algorithm << 16 | mode << 20)
        for i, (word,) in enumerate(struct.iter_unpack('<I', bytes.fromhex(iv))):
            self.write(self.state(channel)+4, i)
            self.write(self.state(channel), word)
        _, table = self.addresses(channel)
        self.write(self.ch(channel, 0x20), 0)
        self.write(self.ch(channel, 0x24), table)
        self.write(self.ch(channel, 0x2c), 2)

    def input(self, channel, encoded, flags=3, slot=0):
        address, table = self.addresses(channel)
        self.memory(address, encoded)
        descriptor = struct.pack('<IIII', flags, len(encoded), address, 0)
        self.memory(table+slot*16, descriptor)
        return address, table

    def output(self, channel, size):
        words = []
        for i in range(size//4):
            self.write(self.state(channel)+4, i)
            words.append(self.read(self.state(channel)))
        return b''.join(w.to_bytes(4, 'little') for w in words)

    def run(self, channel, name, data):
        self.configure(channel, name)
        _, _, block, result, _ = PROFILES[name]
        encoded = pad(data, block)
        address, table = self.input(channel, encoded)
        # Guard and unused descriptor slot must not be written by HASH.
        self.memory(address-32, b'G'*32)
        self.memory(address+len(encoded), b'H'*32)
        self.memory(table+16, b'T'*16)
        before = self.count('bytes-read')
        self.write(self.ch(channel, 0x30), 1)
        self.step((len(encoded)//block)*2000)
        assert self.read(0x8600) & (1 << channel)
        assert self.read(self.ch(channel, 0x34)) == 1
        assert self.read(self.ch(channel, 0xc0)) == 0
        assert self.count('bytes-read')-before == len(encoded)
        assert self.output(channel, result) == hashlib.new(name, data).digest()
        assert self.bytes(address-32,32) == b'G'*32
        assert self.bytes(address+len(encoded),32) == b'H'*32
        assert self.bytes(table+16,16) == b'T'*16


def clean(diagnostics):
    assert diagnostics[0].splitlines() == [NOTICE], diagnostics


def accepted_read_clock_test(qemu):
    for channel in (1,2):
        with running(qemu,False,PROFILE) as (cmd,_,_,diagnostics):
            h=Hash(cmd); h.configure(channel,'sha256'); h.input(channel,pad(b'abc',64))
            h.write(h.ch(channel,0x30),1); h.step(1000)
            assert h.count('pending-reads') == 1
            h.clock(False); h.step(1000)
            assert h.count('bytes-read') == 64 and h.count('pending-reads') == 0
            assert h.count('transfers') == 0, 'compression completed while peripheral clock was stopped'
            h.clock(True); h.step(1000)
            assert h.count('transfers') == 1 and h.read(0x8600) == 1<<channel
            assert h.output(channel,32) == hashlib.sha256(b'abc').digest()
        clean(diagnostics)
    return {'accepted_reads_without_clock': 2, 'compression_waited_for_clock': True}


def hash_test(qemu):
    result = dict(digests=0, split_transfers=0, invalid_mmio=0, rejected_descriptors=0,
                  rejected_configurations=0, fault_recoveries=0)
    with running(qemu, False) as (cmd, _, _, diagnostics):
        h = Hash(cmd)
        assert h.read(0x40) == 0 and h.count('rejections') == 1
    assert diagnostics[0].splitlines() == ['ws53-hash: unimplemented/unavailable read +0x40']

    with running(qemu, False, PROFILE) as (cmd, irq, reset, diagnostics):
        h = Hash(cmd)
        for channel in (1,2):
            for name in PROFILES:
                for length in (0,1,3,55,56,63,64,65,111,112,127,128,129,239,240,247,248,255,256,257,511,512,513,4096):
                    data = bytes((i*37+length+11)&255 for i in range(length))
                    h.run(channel, name, data)
                    result['digests'] += 1
                # Feed complete blocks across both ring slots; first/last do
                # not install an implicit IV or add a second padding block.
                h.configure(channel, name)
                block = PROFILES[name][2]
                data = bytes((i*13+7)&255 for i in range(block*3+5))
                encoded = pad(data, block)
                pointer = 0
                for i in range(0, len(encoded), block):
                    final = i+block == len(encoded)
                    h.input(channel, encoded[i:i+block], int(i==0) | (int(final)<<1), slot=pointer)
                    pointer = (pointer+1) % 2
                    h.write(h.ch(channel,0x30),pointer)
                    h.step(2000)
                    assert h.read(h.ch(channel,0x34)) == pointer
                    assert bool(h.read(0x8600)&(1<<channel)) == final
                assert h.output(channel, PROFILES[name][3]) == hashlib.new(name,data).digest()
                result['split_transfers'] += 1
        assert h.count('rejections') == 0
    clean(diagnostics)

    # Two channels and layered IRQ59 gates. Masking cannot consume raw status.
    with running(qemu, False, PROFILE) as (cmd, irq, reset, diagnostics):
        h=Hash(cmd); cmd('irq_intercept_in /machine/intc')
        for channel in (1,2):
            h.configure(channel,'sha256'); h.input(channel,pad(b'abc',64))
        h.write(4,1); h.write(0x8604,6)
        h.write(h.ch(1,0x30),1); h.write(h.ch(2,0x30),1)
        h.step(999); assert h.count('pending-reads') == 0 and not irq.get(59,False)
        h.step(1); assert h.count('pending-reads') == 2
        h.step(999); assert h.read(0x8600) == 0
        h.step(1); assert h.read(0x8600) == 6 and irq.get(59,False)
        h.write(0x8600,2); assert h.read(0x8600) == 4 and irq.get(59,False)
        h.write(0x8604,0); assert not irq.get(59,False) and h.read(0x8600) == 4
        h.write(0x8604,4); assert irq.get(59,False)
        h.write(4,0); assert not irq.get(59,False)
        h.write(4,1); assert irq.get(59,False)
        h.clock(False); assert not irq.get(59,False)
        h.clock(True); h.read(4); assert irq.get(59,False)
        reset(); h.read(4); assert not irq.get(59,False)
        assert h.read(0x40) == h.read(0x8600) == 0
        result['simultaneous_channels_irq_masks_clock_reset'] = True
    clean(diagnostics)

    for channel in (1,2):
        with running(qemu,False,PROFILE) as (cmd,_,reset,diagnostics):
            h=Hash(cmd)
            h.fault(channel,4)
            h.write(0x40,1<<(4*channel)); assert h.read(0x40) == 0
            h.fault(channel,4,False)
            h.write(0x40,1<<(4*channel)); assert h.read(0x40) == 1<<(4*channel)
            h.fault(channel,0); h.write(0x68,1<<channel); h.step(100000)
            assert h.read(0x14) == 0
            h.clock(False); h.fault(channel,0,False); h.step(100000)
            h.clock(True); h.step(4999); assert h.read(0x14) == 0
            h.step(1); assert h.read(0x14) == 1<<channel
            h.fault(channel,6); h.write(0x14,1<<channel); assert h.read(0x14) == 1<<channel
            h.fault(channel,6,False); h.write(0x14,1<<channel); assert h.read(0x14) == 0
            h.configure(channel,'sha256',clear=False); h.input(channel,pad(b'abc',64))
            h.fault(channel,1); h.write(h.ch(channel,0x30),1); h.step(100000)
            assert h.count('descriptors') == 0
            h.fault(channel,1,False); h.step(2000)
            assert h.output(channel,32) == hashlib.sha256(b'abc').digest()
            result['fault_recoveries'] += 1
        clean(diagnostics)

        # Accepted DMA reads survive clock loss, channel clear and system reset.
        with running(qemu,False,PROFILE) as (cmd,_,reset,diagnostics):
            h=Hash(cmd); h.configure(channel,'sha256'); h.input(channel,pad(b'abc',64))
            h.fault(channel,7); h.write(h.ch(channel,0x30),1); h.step(1000)
            assert h.count('pending-reads') == 1
            h.write(0x8604,0); h.write(0x68,1<<channel); h.step(5000)
            assert h.read(0x14) == 1<<channel and h.count('pending-reads') == 1
            reset(); assert h.count('pending-reads') == 1
            h.clock(False); h.fault(channel,7,False); h.step(1000)
            assert h.count('pending-reads') == 0 and h.count('reads-after-reset') == 1
            assert h.count('bytes-read') == 64
            h.clock(True); assert h.read(0x8600) == 0
            h.write(0x40,1<<(channel*4))
            assert h.output(channel,32) == bytes(32)
            h.run(channel,'sha256',b'recovered')
            result['fault_recoveries'] += 1
        clean(diagnostics)

        # An owner change cannot retract an accepted read. No writes are made
        # under the stolen owner; the HAL must retain and cease MMIO mutation.
        with running(qemu,False,PROFILE) as (cmd,_,reset,diagnostics):
            h=Hash(cmd); h.configure(channel,'sha256'); h.input(channel,pad(b'abc',64))
            h.write(h.ch(channel,0x30),1); h.step(1000); h.fault(channel,5); h.step(1000)
            assert (h.read(0x40)>>(4*channel))&15 == 2
            assert h.count('bytes-read') == 64
            reset(); h.fault(channel,5,False); h.run(channel,'sha256',b'recovered')
            result['fault_recoveries'] += 1
        clean(diagnostics)

        with running(qemu,False,PROFILE) as (cmd,_,reset,diagnostics):
            h=Hash(cmd); h.configure(channel,'sha256'); h.input(channel,pad(b'abc',64))
            h.fault(channel,3); h.write(h.ch(channel,0x30),1); h.step(10000)
            assert h.read(0x8600) == 0 and h.count('transfers') == 1
            assert h.output(channel,32) == hashlib.sha256(b'abc').digest()
            reset(); h.fault(channel,3,False); h.run(channel,'sha256',b'recovered')
            result['fault_recoveries'] += 1
        clean(diagnostics)

        for accepted in (False,True):
            with running(qemu,False,PROFILE) as (cmd,_,reset,diagnostics):
                h=Hash(cmd); h.configure(channel,'sha256'); h.input(channel,pad(b'abc',64))
                if not accepted: h.fault(channel,2)
                h.write(h.ch(channel,0x30),1)
                if accepted: h.step(1000); h.fault(channel,2)
                h.step(10000)
                assert h.count('rejections') == 1 and h.count('bytes-read') == h.read(0x8600) == 0
                reset(); h.fault(channel,2,False); h.run(channel,'sha256',b'recovered')
                result['fault_recoveries'] += 1
            assert diagnostics[0].splitlines()[0] == NOTICE and len(diagnostics[0].splitlines()) == 2

    for field,value in ((0,4),(1,0),(1,63),(2,0),(2,0x52010000),(2,0x2005ffe0),(3,1)):
        with running(qemu,False,PROFILE) as (cmd,_,_,diagnostics):
            h=Hash(cmd); h.configure(1,'sha256')
            address, table=h.input(1,pad(b'abc',64))
            h.memory(table+field*4,value.to_bytes(4,'little'))
            h.write(h.ch(1,0x30),1); h.step(10000)
            assert h.count('rejections') == 1 and h.count('bytes-read') == h.read(0x8600) == 0
            result['rejected_descriptors'] += 1
        assert len(diagnostics[0].splitlines()) == 2

    for offset,value in ((0,0),(0,0x80000500),(0x10,0),(0x10,0xb0000|0x200),
                         (0x10,0xb0001),(0x20,1),(0x24,0x2005f004),(0x2c,1),(0x2c,0)):
        with running(qemu,False,PROFILE) as (cmd,_,_,diagnostics):
            h=Hash(cmd); h.configure(1,'sha256'); h.input(1,pad(b'abc',64))
            h.write(h.ch(1,offset),value); h.write(h.ch(1,0x30),1); h.step(10000)
            assert h.count('rejections') == 1 and h.count('descriptors') == h.read(0x8600) == 0
            result['rejected_configurations'] += 1
        assert len(diagnostics[0].splitlines()) == 2

    with running(qemu,False,PROFILE) as (cmd,_,reset,diagnostics):
        h=Hash(cmd); h.configure(1,'sha256')
        for bank in ('tee','ree'):
            h.inject(bank+'-cause',0x80002000); h.inject(bank+'-status',0x20000)
            offset=0x8068 if bank=='tee' else 0x8078
            assert h.read(offset)==0x80002000 and h.read(offset+4)==0x20000
        commands=[f'read{width} {BASE+0x40:#x}' for width in ('b','w','q')]
        commands += [f'readl {BASE+offset:#x}' for offset in (0x41,0x68,0xa084,0x8030,0x9000)]
        commands += [f'writel {BASE+offset:#x} 1' for offset in (0x5134,0x51c0,0x8068,0x806c,0x8078,0x807c,0x8030)]
        commands += [f'writel {BASE+offset:#x} 0x100' for offset in (0x14,0x68,0x8600)]
        commands += [f'writel {BASE+0xa084:#x} 16',f'writeb {BASE+0x40:#x} 0']
        for command in commands:
            cmd(command); result['invalid_mmio']+=1
        assert h.count('rejections') == len(commands)
        reset(); assert h.read(0x8068)==h.read(0x8078)==0
    assert len(diagnostics[0].splitlines()) == result['invalid_mmio']+1
    assert all(line.startswith('ws53-hash: unimplemented/unavailable') for line in diagnostics[0].splitlines()[1:])
    result.update(accepted_read_clock_test(qemu))
    return dict(passed=True, **result, actual_compression=True, hardware_verified=False,
                limitation='Opt-in register/timing/DMA assumptions; no silicon ownership, drain or boot qualification')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu',required=True)
    parser.add_argument('--clock-only',action='store_true')
    args=parser.parse_args()
    print(json.dumps(accepted_read_clock_test(args.qemu) if args.clock_only else hash_test(args.qemu),indent=2))
