#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS53 UART8N1 digital RX routes and cancellation; no silicon timing claim."""
import argparse
import json
import tempfile
import time
from pathlib import Path
from ws53_gpio_test import running, connect

OPTIONS=('-global','ws53-uart.assume-pad-rx=on',
         '-global','ws53-pinctrl.assume-uart-rx=on')
NOTICE='ws53-uart: RX pad8N1 center sampling assumes stable32MHz; physical timing unqualified'
ROUTES=((0,36,0x57036010,0x57036030,2),(1,18,0x52008048,0x57036088,2),
        (1,9,0x52008024,0x57036064,1),(2,34,0x57036008,0x57036028,1))
BASES=(0x52081000,0x52080000,0x57014000)


def uart_rx_pads_test(qemu):
    decoded=0
    with running(qemu,False,OPTIONS) as (cmd,irq,reset,diagnostics):
        cmd('irq_intercept_in /machine/intc')
        def write(addr,value): cmd(f'writel {addr:#x} {value:#x}')
        def read(addr): return int(cmd(f'readl {addr:#x}')[0],0)
        def signal(pad,kind,value): cmd(f'set_irq_in /machine/pinctrl external-{kind} {pad} {value}')
        def advance(ns):
            for _ in range(ns//1000): cmd('clock_step 1000')
        def frame(pad,byte,stop=1,bit_ns=10000):
            signal(pad,'level',1);advance(10000)
            for value in [0,*[(byte>>i)&1 for i in range(8)],stop]:
                signal(pad,'level',value)
                cmd(f'clock_step {bit_ns//2}');cmd(f'clock_step {bit_ns-bit_ns//2}')
            signal(pad,'level',1)
        for port,pad,mux,config,mode in ROUTES:
            base=BASES[port];cause=(39,41,42)[port]
            write(base+0x10,20);write(base+0x14,0) # exact100kbaud
            signal(pad,'level',1);signal(pad,'drive',1);write(mux,mode)
            write(base+0x18,4)
            for byte in range(256):
                frame(pad,byte)
                assert read(base+0x4c)==1 and irq.get(cause,False)
                assert read(base+0x34)&0xf==0
                assert read(base+4)==byte and not irq[cause]
                decoded+=1
            # The input buffer and mux must both participate, not just the pad ID.
            write(config,0);frame(pad,0x53);assert read(base+0x4c)==0
            write(config,4);write(mux,0);frame(pad,0x63);assert read(base+0x4c)==0
            write(mux,mode)
            # Reject a pulse that returns high before the start-bit center.
            signal(pad,'level',0);advance(2000);signal(pad,'level',1);advance(100000)
            assert read(base+0x4c)==0
            # Framing error enters the ordinary FIFO/IRQ path, then read-clears.
            write(base+0x18,1);frame(pad,0xa5,0)
            assert read(base)==0x16 and irq[cause]
            assert read(base+0x34)&4 and not irq[cause]
            assert read(base+0x34)&4==0 and read(base+4)==0xa5
            # Unplugging the external driver aborts a partial byte; reconnection
            # low does not synthesize a start. A fresh falling edge recovers.
            signal(pad,'level',0);advance(20000);signal(pad,'drive',0)
            advance(100000);signal(pad,'drive',1);advance(100000)
            assert read(base+0x4c)==0
            frame(pad,0x3c);assert read(base+4)==0x3c
            # Disable input mid-frame; no ghost byte after re-enabling it.
            signal(pad,'level',0);advance(20000);write(config,0);advance(100000)
            write(config,4);advance(100000);assert read(base+0x4c)==0
            frame(pad,0xc3);assert read(base+4)==0xc3
            signal(pad,'level',0);advance(20000);write(mux,0);advance(100000)
            write(mux,mode);advance(100000);assert read(base+0x4c)==0
            frame(pad,0x96);assert read(base+4)==0x96
            # Internal loopback owns the receiver while selected. Digital pin
            # traffic must not leak in, but software TX loopback still works.
            write(base+0x2c,2);frame(pad,0x5a);assert read(base+0x4c)==0
            write(base+4,0x22);assert read(base+4)==0x22
            write(base+0x2c,0)
            write(mux,0);write(base+0x18,0)
        # FIFO full/overrun via real digital frames, no synthetic FIFO injection.
        port,pad,mux,config,mode=ROUTES[0];base=BASES[0]
        write(mux,mode)
        for byte in range(65): frame(pad,byte)
        assert read(base+0x4c)==64 and read(base+0x34)&8
        assert [read(base+4) for _ in range(64)]==list(range(64))
        assert read(base+0x4c)==0
        # Local notification mask must not stop receiving.
        write(base+0x18,0);frame(pad,0x42);assert not irq[39]
        write(base+0x18,4);assert irq[39];assert read(base+4)==0x42 and not irq[39]
        # Clock loss aborts the partially sampled byte, with no synthetic resume.
        signal(pad,'level',0);advance(20000)
        cmd('set_irq_in /machine/uart0 clock 0 0');advance(100000)
        cmd('set_irq_in /machine/uart0 clock 0 1');advance(100000)
        assert read(base+0x4c)==0
        frame(pad,0x77);assert read(base+4)==0x77
        # Soft reset cancels its pending sampling timer.
        signal(pad,'level',0);advance(20000)
        cmd('set_irq_in /machine/uart0 soft-reset-n 0 0');advance(100000)
        cmd('set_irq_in /machine/uart0 soft-reset-n 0 1');advance(100000)
        assert read(base+0x4c)==0
        reset()
        assert read(mux)==0 and read(base+0x4c)==0
        assert cmd.qmp('qom-get',{'path':'/machine/uart0','property':'pad-rx-frames'})==0
        signal(pad,'level',1);write(mux,mode);write(base+0x10,20);write(base+0x14,0)
        frame(pad,0x91);assert read(base+4)==0x91
        # The configured115200 profile uses17+23/64, sampling16: modeled bit
        # duration ceil(8679.6875ns). This is not fractional jitter calibration.
        write(base+0x10,17);write(base+0x14,23)
        frame(pad,0xa5,bit_ns=8680);assert read(base+4)==0xa5
    assert diagnostics[0].splitlines()==[NOTICE]*3,diagnostics

    with running(qemu,False,OPTIONS) as (cmd,irq,reset,diagnostics):
        def advance(ns):
            for _ in range(ns//1000): cmd('clock_step 1000')
        for pad in (18,9):
            cmd(f'set_irq_in /machine/pinctrl external-level {pad} 1')
            cmd(f'set_irq_in /machine/pinctrl external-drive {pad} 1')
        cmd('writel 0x52008048 2');cmd('writel 0x52008024 1')
        for pad in (18,9): cmd(f'set_irq_in /machine/pinctrl external-level {pad} 0')
        advance(100000)
        assert int(cmd('readl 0x5208004c')[0],0)==0
        cmd('writel 0x52008024 0')
        cmd('set_irq_in /machine/pinctrl external-level 18 1')
        cmd('writel 0x52080008 8')
        cmd('set_irq_in /machine/pinctrl external-level 18 0');advance(100000)
        assert int(cmd('readl 0x5208004c')[0],0)==0
        cmd('writel 0x52080008 12')
        cmd('set_irq_in /machine/pinctrl external-level 18 1')
        cmd('set_irq_in /machine/pinctrl external-level 18 0');advance(20000)
        cmd('writel 0x52080010 99')
        assert int(cmd('readl 0x52080010')[0],0)==17
    assert diagnostics[0].count('multiple RX pads')==1
    assert 'requires8N1' in diagnostics[0] and 'live RX pad format' in diagnostics[0]

    with running(qemu,False,('-global','ws53-pinctrl.assume-uart-rx=on')) as (cmd,irq,reset,diagnostics):
        cmd('set_irq_in /machine/pinctrl external-level 36 1')
        cmd('set_irq_in /machine/pinctrl external-drive 36 1')
        cmd('writel 0x57036010 2')
        cmd('set_irq_in /machine/pinctrl external-level 36 0')
        for _ in range(100): cmd('clock_step 1000')
        assert int(cmd('readl 0x5208104c')[0],0)==0
    assert not diagnostics[0],diagnostics

    # A connected character backend cannot bypass physical routing when enabled.
    with tempfile.TemporaryDirectory(prefix='ws53-rx-peer-') as directory:
        path=Path(directory)/'rx'
        with running(qemu,False,OPTIONS+('-chardev',f'socket,id=rx,path={path},server=on,wait=off',
                                        '-serial','chardev:rx')) as (cmd,irq,reset,diagnostics):
            with connect(path) as peer:
                peer.sendall(b'\x53\x63');time.sleep(.02)
                assert int(cmd('readl 0x5208004c')[0],0)==0
    assert diagnostics[0].splitlines()==[NOTICE]*3
    return dict(passed=True,routes=4,all_byte_values_decoded=decoded,
                framing_overrun_irq_and_recovery=True,ambiguous_rx_rejected=True,
                direct_backend_bypass_disabled=True,fractional_divisor_sampling=True,
                default_disabled=True,physical_rx_qualified=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--qemu',required=True)
    print(json.dumps(uart_rx_pads_test(parser.parse_args().qemu),indent=2))
