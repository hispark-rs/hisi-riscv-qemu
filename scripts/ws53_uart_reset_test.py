#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Shared H1/H0 soft reset, isolation, held-access diagnostics and boot recovery."""
import argparse
import json
from ws53_gpio_test import running

NOTICE='ws53-uart-reset: observed H1/H0 profile; divisor zero and in-flight cancellation assumed'
UART_NOTICE='ws53-uart: assumed byte DMA requests, FIFO64 and timed serializer'
BASES=(0x52081000,0x52080000,0x57014000)
RESET=0x52000168


def uart_reset_test(qemu):
    checked=0
    with running(qemu,False,('-global','ws53-uart-reset.assume-profile=on',
            '-global','ws53-uart.assume-dma-profile=on',
            '-global','ws53-uart.char-ns=100000')) as (cmd,irq,reset,diagnostics):
        def read(base,off): return int(cmd(f'readl {base+off:#x}')[0],0)
        def write(base,off,value): cmd(f'writel {base+off:#x} {value:#x}')
        def reset_word(value): cmd(f'writew {RESET:#x} {value}')
        cmd('irq_intercept_in /machine/intc')
        assert int(cmd(f'readw {RESET:#x}')[0],0)==3
        for base in BASES: assert read(base,0x60)==0xd04
        for port in range(2):
            base=BASES[port]; peer=BASES[1-port]
            write(peer,0x38,0xa5);write(BASES[2],0x38,0x3c)
            write(base,0x24,0x70);write(base,8,0xa8);write(base,0x10,1)
            write(base,0x14,38);write(base,0x58,0x37);write(base,0x2c,2)
            write(base,0x38,0x5a);write(base,0x28,1)
            write(base,0x40,0x15);write(base,0x18,4)
            assert read(base,0x4c)==1 and irq.get((39,41)[port],False)
            # Pending serializer work must not leak into a new post-reset FIFO.
            write(base,4,0x22)
            reset_word(3 & ~(2 if port==0 else 1))
            assert not irq.get((39,41)[port],False)
            # Unqualified accesses while held report diagnostics and cannot
            # mutate state; they are never silent successful register accesses.
            assert read(base,0x38)==0
            write(base,0x38,0x77)
            reset_word(3)
            for off,value in ((8,0),(0x14,0),(0x58,0x7f),(0x2c,0),
                              (0x18,0),(0x4c,0),(0x38,0),(0,1)):
                assert read(base,off)==value,(port,off)
                checked+=1
            assert read(peer,0x38)==0xa5 and read(BASES[2],0x38)==0x3c
            cmd('clock_step 200000')
            assert read(base,0x4c)==0
            # Redundant release is not a reset pulse.
            write(base,0x38,0x66);reset_word(3);assert read(base,0x38)==0x66
            # Reconfigure and complete actual data transfer after the reset.
            write(base,0x24,0x70);write(base,8,0xc);write(base,0x10,17)
            write(base,0x14,23);write(base,0x2c,2);write(base,4,0x0a)
            cmd('clock_step 100000')
            assert read(base,4)==0x0a and read(base,0x4c)==0
            checked+=4
        # Wrong width/unknown bits must not partly assert any port reset.
        write(BASES[0],0x38,0x55)
        for operation,value in (('writeb',0),('writel',0),('writeq',0),('writew',4)):
            cmd(f'{operation} {RESET:#x} {value}')
            assert int(cmd(f'readw {RESET:#x}')[0],0)==3 and read(BASES[0],0x38)==0x55
            checked+=1
        reset_word(0)
        reset() # Machine reset restores the configured-console boot handoff.
        assert int(cmd(f'readw {RESET:#x}')[0],0)==3
        for base in BASES:
            assert read(base,8)==0xc and read(base,0x10)==17 and read(base,0x14)==23 and read(base,0)==17
    lines=diagnostics[0].splitlines()
    assert lines.count(NOTICE)==1 and lines.count(UART_NOTICE)==3,lines
    remaining=[x for x in lines if x not in (NOTICE,UART_NOTICE)]
    assert len(remaining)==8,remaining
    assert sum('read while soft reset asserted' in x for x in remaining)==2
    assert sum('write while soft reset asserted' in x for x in remaining)==2
    assert sum('ws53-uart-reset: unimplemented write' in x for x in remaining)==4
    with running(qemu,False) as (cmd,_,_,diagnostics):
        cmd(f'writew {RESET:#x} 0')
        assert int(cmd(f'readl {BASES[0]+8:#x}')[0],0)==0xc
        for base in BASES: assert int(cmd(f'readl {base+0x60:#x}')[0],0)==0xd04
    assert diagnostics[0].count('ws53-uart-reset: unimplemented write')==1,diagnostics
    return dict(passed=True,cases=checked,observed_hardware_ports=[0,1],
                model_only=['neighbor isolation','held-reset access','in-flight TX cancellation','invalid widths'],
                divisor_zero_assumed=True,physical_defaults_qualified=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu',required=True)
    print(json.dumps(uart_reset_test(parser.parse_args().qemu),indent=2))
