#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""UART0/1 request-paced MDMA, FIFO/serializer and cancellation model contracts."""
import argparse
import json
import subprocess
from ws53_gpio_test import running
from ws53_mdma_test import Bus, SRC, DST, PROFILE, NOTICE

ARGS = (*PROFILE, '-global', 'ws53-mdma.assume-uart=on')
NOTICES = [NOTICE, 'ws53-mdma: assumed UART0/1 byte handshakes with flow1/2, master0/prot0',
           *['ws53-uart: assumed byte DMA requests, FIFO64 and timed serializer'] * 2]
BASES = (0x52081000, 0x52080000)


class Uart:
    def __init__(self, b, port):
        self.b, self.port, self.base = b, port, BASES[port]
        self.tx_request = 10 if port == 0 else 8
    def write(self, off, value): self.b.cmd(f'writel {self.base+off:#x} {value:#x}')
    def read(self, off): return int(self.b.cmd(f'readl {self.base+off:#x}')[0], 0)
    def prop(self, name): return self.b.cmd.qmp('qom-get', {'path':f'/machine/uart{self.port}', 'property':name})
    def fault(self, name, on): self.b.cmd(f'set_irq_in /machine/uart{self.port} {name} 0 {int(on)}')
    def inject(self, data):
        self.write(0x28, 1)
        for byte in data: self.write(0x40, byte)
    def drain(self, length): return bytes(self.read(4) for _ in range(length))
    def configure(self, ch, count, receive, addr=None):
        address = (DST if receive else SRC) if addr is None else addr
        self.b.configure(ch, count, 1, self.base+4 if receive else address,
                         address if receive else self.base+4, not receive, receive)
        cfg = 0x3001 | ((2 if receive else 1) << 9)
        cfg |= (self.tx_request+1) << 1 if receive else self.tx_request << 5
        self.b.set(ch, 8, cfg)
        return cfg


def clean(diagnostics):
    assert diagnostics[0].splitlines() == NOTICES, diagnostics


def uart_dma_test(qemu):
    combinations = 0
    with running(qemu, False, ARGS) as (cmd, irq, reset, diagnostics):
        b = Bus(cmd); cmd('irq_intercept_in /machine/intc')
        for port in range(2):
            u = Uart(b, port)
            for ch in range(8):
                for receive in (False, True):
                    reset(); b.write(0x1c, 1); u.write(0x2c, 2)
                    assert u.read(0x60) == 0xa04
                    data = bytes((i*13+ch*23+port*17)&255 for i in range(17))
                    if receive:
                        u.inject(data); b.fill(DST-1, b'\xa5'*19)
                    else: b.fill(SRC, data)
                    u.configure(ch, 17, receive)
                    b.step(499); assert b.prop('reads') == 0
                    b.step(1); assert b.prop('reads') == 1 and b.prop('pending-writes') == 1
                    b.step(16500)
                    assert b.prop('transfers') == 1 and b.prop('writes') == 17, (port,ch,receive,b.prop('reads'),b.prop('writes'),b.prop('pending-writes'),hex(b.reg(ch,8)),hex(b.read(0xc)))
                    assert b.read(0x10) == 0 and b.read(0xc) == 1<<ch and irq[58]
                    if receive:
                        assert b.mem(DST-1, 19) == b'\xa5'+data+b'\xa5'
                        assert u.read(0x4c) == 0 and b.reg(ch, 0x10) == u.base+4
                    else:
                        # DMA completion means FIFO accepted, not bytes serialized.
                        assert u.prop('transmitted') == 1 and not u.read(0x34)&0x80
                        assert b.reg(ch, 4) == u.base+4
                        b.step(170000)
                        assert u.prop('transmitted') == 17 and u.read(0x34)&0x80
                        assert u.drain(17) == data
                    b.write(8, 1<<ch); assert not irq[58]
                    combinations += 1
        # Both ports, simultaneous TX and RX, beyond FIFO capacity; no fake echo
        # before serializer completion, exact byte data and guard preservation.
        reset(); b.write(0x1c, 1); count = 257
        for port in range(2):
            u = Uart(b, port); u.write(0x2c, 2)
            data = bytes((i*31+port*19)&255 for i in range(count))
            b.fill(SRC+port*512, data); b.fill(DST+port*512-1, b'\xa5'*(count+2))
            u.configure(port*2+1, count, True, DST+port*512)
            u.configure(port*2, count, False, SRC+port*512)
        b.step(3000000)
        assert b.prop('transfers') == 4 and b.prop('writes') == count*4 and b.read(0xc) == 15
        for port in range(2):
            u = Uart(b, port)
            assert u.prop('transmitted') == count and u.read(0x4c) == 0
            assert not u.read(0x34)&0x1f
            assert b.mem(DST+port*512-1, count+2) == b'\xa5'+bytes((i*31+port*19)&255 for i in range(count))+b'\xa5'
        # No RX request: no fabricated zeros or memory progress. New data resumes.
        reset(); b.write(0x1c, 1); u = Uart(b, 0); b.fill(DST, b'Z'*3)
        u.configure(0, 3, True); b.step(100000)
        assert b.prop('reads') == b.prop('writes') == 0 and b.mem(DST, 3) == b'ZZZ'
        u.inject(b'abc'); b.step(3000); assert b.mem(DST, 3) == b'abc'
        # FIFO full backpressures DMA rather than dropping bytes/reporting TC.
        reset(); b.write(0x1c, 1); u.fault('tx-stall', True); u.write(0x2c, 2)
        b.fill(SRC, bytes(range(96))); u.configure(0, 96, False); b.step(100000)
        assert b.prop('writes') == 64 and b.prop('pending-writes') == 0
        assert u.read(0x48) == 64 and not u.read(0x44)&2 and b.read(0xc) == 0
        u.configure(1, 96, True); u.fault('tx-stall', False); b.step(1000000)
        assert b.mem(DST, 96) == bytes(range(96)) and b.prop('transfers') == 2
        # Software halt stops starting another frame, not the active shift byte.
        reset(); u.write(4, 65)
        assert u.read(0x44)&3 == 2 and not u.read(0x34)&0x80
        u.write(4, 66)
        assert u.read(0x44)&3 == 0 and u.read(0x48) == 1
        b.step(20000)
        assert u.read(0x44)&3 == 2 and u.read(0x34)&0x80
        reset(); u.write(0x2c, 2); u.write(0x50, 1); u.write(4, 65); u.write(4, 66)
        b.step(100000); assert u.read(0x48) == 2 and u.prop('transmitted') == 0
        u.write(0x50, 0); b.step(5000); u.write(0x50, 1); b.step(5000)
        assert u.prop('transmitted') == 1 and u.read(0x48) == 1
        b.step(100000); assert u.prop('transmitted') == 1
        u.write(0x50, 0); b.step(10000); assert u.drain(2) == b'AB'
        # Gate prevents requests and resumes pending serializer phase exactly.
        reset(); b.write(0x1c, 1); u.fault('clock', False); b.fill(SRC, b'x'); u.configure(0, 1, False)
        b.step(100000); assert b.prop('reads') == 0
        u.fault('clock', True); b.step(1123); assert b.prop('writes') == 1
        u.fault('clock', False); b.step(100000); assert u.prop('transmitted') == 0
        u.fault('clock', True); b.step(9876); assert u.prop('transmitted') == 0
        b.step(1); assert u.prop('transmitted') == 1
        # A consumed RX byte remains a posted RAM write after cancel or UART gate.
        for action in ('halt', 'disable', 'controller-disable', 'uart-clock'):
            reset(); b.write(0x1c, 1); u.inject(b'abc'); b.fill(DST, b'ZZZ')
            cfg = u.configure(0, 3, True); b.step(600)
            assert u.read(0x4c) == 2 and b.prop('pending-writes') == 1
            if action == 'halt': b.set(0, 8, cfg|0x10000)
            elif action == 'disable': b.set(0, 8, cfg&~1)
            elif action == 'controller-disable': b.write(0x1c, 0)
            else: u.fault('clock', False)
            b.step(100000)
            assert b.mem(DST, 3) == b'aZZ' and b.prop('writes') == 1 and b.prop('pending-writes') == 0
            u.fault('clock', True)
        # Errors never report TC; read failure preserves RX, write failure loses
        # consumed byte but retains diagnostic state and untouched destination.
        for receive in (False, True):
            for error in ('read-error', 'write-error'):
                reset(); b.write(0x1c, 1); u.inject(b'abc'); b.fill(SRC, b'def'); b.fill(DST, b'ZZZ')
                b.fault(error); u.configure(0, 3, receive); b.step(100000)
                assert b.prop('errors') == 1 and b.read(0xc) == 0x100 and b.read(0x10) == 0
                assert u.prop('transmitted') == 0 and b.mem(DST, 3) == b'ZZZ'
                assert u.read(0x4c) == (2 if receive and error == 'write-error' else 3)
                b.fault(error, on=False)
        # UART gate disappearing after a TX memory read yields model bus error.
        reset(); b.write(0x1c, 1); b.fill(SRC, b'q'); u.configure(0, 1, False); b.step(600)
        u.fault('clock', False); b.step(1000)
        assert b.prop('errors') == 1 and b.prop('pending-writes') == 0 and u.prop('transmitted') == 0
        u.fault('clock', True)
        # FIFO reset does not retract an already shifting byte; full reset does.
        reset(); u.write(0x2c, 2); u.write(4, 65); u.write(4, 66); u.write(0x24, 0x70)
        assert u.read(0x48) == 0 and not u.read(0x34)&0x80
        b.step(10000); assert u.drain(1) == b'A' and u.prop('transmitted') == 1
        u.write(4, 67); reset(); b.step(10000); assert u.prop('transmitted') == 0
    clean(diagnostics)
    rejected = 0
    with running(qemu, False, ARGS) as (cmd, irq, reset, diagnostics):
        b = Bus(cmd); u = Uart(b, 0)
        for flow in (1, 2):
            for request in range(16):
                if request in ((8, 10) if flow == 1 else (9, 11)): continue
                reset(); b.write(0x1c, 1)
                b.configure(0, 3, 1, SRC if flow == 1 else u.base+4, u.base+4 if flow == 1 else DST, flow == 1, flow == 2)
                b.set(0, 8, 0x3001|(flow<<9)|(request<<(5 if flow == 1 else 1)))
                assert b.read(0x10) == 0; rejected += 1
        # Valid request with wrong endpoint, UART2 address, width, increment,
        # unused request bits or foreign HPROT must not silently succeed.
        for src, dst, width, si, di, cfg in (
            (SRC, 0x52080004, 1, True, False, 0x3341),
            (SRC, 0x57014004, 1, True, False, 0x3341),
            (SRC, u.base+4, 2, True, False, 0x3341),
            (SRC, u.base+4, 4, True, False, 0x3341),
            (SRC, u.base+4, 1, False, False, 0x3341),
            (SRC, u.base+4, 1, True, True, 0x3341),
            (SRC, u.base+4, 1, True, False, 0x3343),
        ):
            reset(); b.write(0x1c, 1); b.configure(0, 3, width, src, dst, si, di)
            b.set(0, 8, cfg); assert b.read(0x10) == 0; rejected += 1
        reset(); b.write(0x1c, 1); cfg = u.configure(0, 3, True)
        b.set(0, 8, cfg^(2<<1)); assert b.reg(0, 8) == cfg; rejected += 1
        b.set(0, 0x14, 1<<28); rejected += 1
        reset(); b.configure(0, 3, 1)
        old_control = b.reg(0, 0x14)
        b.set(0, 0x14, old_control|(1<<28)); rejected += 1
        assert b.reg(0, 0x14) == old_control
        for flow in range(3, 8):
            b.set(0, 8, flow<<9); rejected += 1
            assert b.reg(0, 8) == 0
        for off in (0x60, 0x54):
            u.write(off, 0x800); rejected += 1
        assert u.read(0x60) == 0xa04
        reset(); u.write(0x50, 1); u.write(4, 65); u.write(4, 66)
        u.write(0x24, 0x40); rejected += 1
        assert u.read(0)&0x10 and u.read(0x48) == 2
    lines = diagnostics[0].splitlines()
    assert lines[:4] == NOTICES and len(lines[4:]) == rejected, diagnostics
    assert all('unimplemented write' in line for line in lines[4:]), diagnostics
    result = subprocess.run([qemu,'-M','ws53','-display','none','-global','ws53-mdma.assume-uart=on'],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0 and 'assume-uart requires assume-profile' in result.stderr
    for ns in (0, 1000000001):
        result = subprocess.run([qemu,'-M','ws53','-display','none','-global',f'ws53-uart.char-ns={ns}'],
                                capture_output=True, text=True, timeout=5)
        assert result.returncode != 0 and 'char-ns must be' in result.stderr
    return {'channel_port_direction_cases':combinations,'concurrent_ports_bytes':257,
            'invalid_accesses_rejected':rejected,'fifo_backpressure':True,'dma_complete_is_not_serialized':True,
            'cancelled_rx_posted_write_commits':True,'hardware_verified':False}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--qemu', required=True)
    print(json.dumps(uart_dma_test(p.parse_args().qemu), indent=2))
