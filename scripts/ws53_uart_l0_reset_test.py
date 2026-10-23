#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Independent L0 halfword reset, neighboring state and boot recovery."""
from ws53_gpio_test import running

NOTICE='ws53-uart-reset: observed L0 profile; divisor zero and in-flight cancellation assumed'


def uart_l0_reset_test(qemu):
    base=0x57014000;reset_word=0x570000b0
    with running(qemu,False,('-global','ws53-uart-reset.assume-l0-profile=on')) as (cmd,irq,reset,diagnostics):
        def read(addr): return int(cmd(f'readl {addr:#x}')[0],0)
        def write(addr,value): cmd(f'writel {addr:#x} {value:#x}')
        cmd('irq_intercept_in /machine/intc')
        assert int(cmd(f'readw {reset_word:#x}')[0],0)==128
        peers=(0x52081000,0x52080000)
        for peer in peers: write(peer+0x38,0xa5)
        write(base+0x38,0x5a);write(base+0x28,1);write(base+0x40,0x15);write(base+0x18,4)
        assert read(base+0x4c)==1 and irq.get(42,False)
        cmd(f'writew {reset_word:#x} 0')
        assert not irq.get(42,False)
        cmd(f'writew {reset_word:#x} 128')
        for off,value in ((8,0),(0x38,0),(0x4c,0),(0x58,127),(0x44,2)):
            assert read(base+off)==value
        assert all(read(peer+0x38)==0xa5 for peer in peers)
        write(base+0x24,0x70);write(base+8,0xc);write(base+0x2c,2);write(base+4,0x2a)
        assert read(base+4)==0x2a
        # Atomic rejection, no partial assertion by wide/neighbor writes.
        write(base+0x38,0x66)
        cmd(f'writel {reset_word:#x} 0');cmd(f'writew {reset_word:#x} 129')
        assert read(base+0x38)==0x66
        reset()
        assert int(cmd(f'readw {reset_word:#x}')[0],0)==128 and read(base+8)==12
    lines=diagnostics[0].splitlines()
    assert lines.count(NOTICE)==1 and len(lines)==3,lines
    assert sum('unimplemented write' in line for line in lines)==2
    with running(qemu,False) as (cmd,_,_,diagnostics):
        cmd(f'writew {reset_word:#x} 0')
        assert int(cmd(f'readl {base+8:#x}')[0],0)==12
    assert diagnostics[0].count('unimplemented write')==1
    return dict(passed=True,checks=12,hardware_queued_rx_and_neighbor_isolation=True,
                held_reset_access_and_inflight_irq_dma_hardware_verified=False)
