#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""UART TX pending/ack/rearm/FIFO tests; only UART2 inherited threshold has HIL."""
from ws53_gpio_test import running


def uart_tx_irq_test(qemu):
    checked=0
    args=('-global','ws53-uart.assume-dma-profile=on','-global','ws53-uart.char-ns=100000')
    with running(qemu,False,args) as (command,irq,reset,diagnostics):
        command('irq_intercept_in /machine/intc')
        for base,line in ((0x52081000,39),(0x52080000,41),(0x57014000,42)):
            def read(off):return int(command(f'readl {base+off:#x}')[0],0)
            def write(off,v):command(f'writel {base+off:#x} {v:#x}')
            for ptim in (False,True):
                for threshold,level in enumerate((0,2,16,32)):
                    reset()
                    write(0x24,0x70|threshold) # FIFO enable and reset both empty FIFOs.
                    enables=8|(16 if ptim else 0)
                    write(0x18,enables)
                    assert read(0x1c)==4 and irq.get(line,False)
                    assert read(0)&15==2
                    assert read(0)&15==1 and read(0x1c)==0 and not irq.get(line,False)
                    # Rewriting the same enable is not a new enable edge.
                    write(0x18,enables);assert read(0)&15==1
                    write(0x18,0);write(0x18,enables)
                    assert read(0)&15==2 and read(0)&15==1
                    assert read(0x34)==(0x80 if ptim else 0xc0)
                    write(0x18,16 if ptim else 0);write(0x50,1)
                    for _ in range(64):write(4,0x55)
                    assert read(0x48)==64
                    assert read(0x34)==(0x40 if ptim else 0)
                    write(0x18,enables);assert read(0)&15==1
                    # No drain while stalled, hence no fresh TX notification.
                    command('clock_step 1000000');assert read(0)&15==1
                    write(0x50,0)
                    remaining=level if ptim else 0
                    command(f'clock_step {(63-remaining)*100000}')
                    assert read(0x48)==remaining and irq.get(line,False)
                    assert read(0)&15==2 and read(0)&15==1
                    assert read(0x34)==(0 if ptim else 0x40) # shift register still busy
                    command(f'clock_step {(remaining+1)*100000}')
                    assert read(0x34)==(0x80 if ptim else 0xc0)
                    assert read(0)&15==1 # completion is not a second THRE event
                    checked+=1
            reset()
            assert read(0x18)==0 and read(0x1c)==0 and not irq.get(line,False)
    expected='\n'.join(['ws53-uart: assumed byte DMA requests, FIFO64 and timed serializer']*3)
    assert diagnostics==[expected+'\n'] or diagnostics==[expected],diagnostics
    return dict(cases=checked,uart2_rearm_read_clear_hardware_verified=True,
                fifo_thresholds='0/2/16/32 modeled; only inherited2 physically observed',
                uart0_uart1_hardware_verified=False,clock_fault_physical_verified=False)
