#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""8N1 UART TX serializer, independent pad routes, fanout and interruption."""
from ws53_gpio_test import running

OPTIONS=('-global','ws53-uart.assume-dma-profile=on',
         '-global','ws53-uart.assume-pad-tx=on',
         '-global','ws53-pinctrl.assume-uart-tx=on')
NOTICE='ws53-uart: TX pad8N1 profile assumes stable32MHz; RX pads unmodeled'


def fnv(data):
    value=14695981039346656037
    for byte in data:
        value=((value^byte)*1099511628211)&((1<<64)-1)
    return value


def uart_pads_test(qemu):
    cases=0
    with running(qemu,False,OPTIONS) as (cmd,irq,reset,diagnostics):
        cmd('irq_intercept_out /machine/pinctrl level')
        def advance(ns):
            for _ in range(ns//1000): cmd('clock_step 1000')
        def write(addr,value): cmd(f'writel {addr:#x} {value:#x}')
        def count(pad):
            return cmd.qmp('qom-get',{'path':'/machine/pinctrl','property':f'uart-tx-bytes-{pad}'})
        def hash_(pad):
            return cmd.qmp('qom-get',{'path':'/machine/pinctrl','property':f'uart-tx-hash-{pad}'})
        # Exact32MHz/16/20=100kbaud,10us/bit. No fractional rounding here.
        for base in (0x52081000,0x52080000,0x57014000):
            write(base+0x10,20);write(base+0x14,0)
        for pad,mux,mode in ((12,0x52008030,2),(17,0x52008044,2),
                             (8,0x52008020,1),(33,0x57036004,1)):
            write(mux,mode)
            assert irq[pad], 'selected UART TX must idle high'
        for port,base,pads in ((0,0x52081000,(12,)),(1,0x52080000,(17,8)),(2,0x57014000,(33,))):
            packet=[0x55,0xaa,0,255]
            for byte in packet:
                before=[count(p) for p in pads]
                write(base+4,byte)
                for bit in [0,*[(byte>>b)&1 for b in range(8)],1]:
                    assert all(irq[p]==bool(bit) for p in pads),(port,byte,bit,irq)
                    cmd('clock_step 9999')
                    assert all(irq[p]==bool(bit) for p in pads)
                    assert [count(p) for p in pads]==before, 'completed before final stop bit'
                    cmd('clock_step 1')
                assert [count(p) for p in pads]==[n+1 for n in before]
                cases+=1
            assert all(hash_(p)==fnv(packet) for p in pads)
        # Detaching a pad halfway through a frame must not count that frame;
        # reattaching it before the end is not a successful complete transfer.
        write(0x52080004,0x53);advance(40000)
        write(0x52008020,0);write(0x52008020,1)
        advance(60000)
        assert count(17)==5 and count(8)==4
        # Leave M17 disconnected; next complete packet reaches only M8.
        write(0x52008044,0);write(0x52080004,0x63);advance(100000)
        assert count(17)==5 and count(8)==5
        assert hash_(17)==fnv([0x55,0xaa,0,255,0x53])
        assert hash_(8)==fnv([0x55,0xaa,0,255,0x63])
        # Loopback never puts data bits onto the physical TX wire.
        write(0x5208002c,2);write(0x52080004,0);advance(100000)
        assert irq[8] and count(8)==5
        assert int(cmd('readl 0x52080004')[0],0)==0
        write(0x5208002c,0)
        # A model clock pause preserves remaining bit time and does not finish.
        write(0x52080004,0xa5);advance(5000)
        cmd('set_irq_in /machine/uart1 clock 0 0');cmd('clock_step 1000000')
        assert not irq[8] and count(8)==5
        cmd('set_irq_in /machine/uart1 clock 0 1');advance(95000)
        assert count(8)==6
        # Repeated released reset input must not abort a frame.
        write(0x52080004,0x12);advance(20000)
        cmd('set_irq_in /machine/uart1 soft-reset-n 0 1');advance(80000)
        assert count(8)==7
        # Conflicting external drive at start or mid-frame cannot count as a
        # successful virtual peer byte, even though internal drive wins level.
        cmd('set_irq_in /machine/pinctrl external-level 8 1')
        cmd('set_irq_in /machine/pinctrl external-drive 8 1')
        write(0x52080004,0);advance(100000)
        assert count(8)==7
        cmd('set_irq_in /machine/pinctrl external-drive 8 0')
        write(0x52080004,0);advance(20000)
        cmd('set_irq_in /machine/pinctrl external-drive 8 1');advance(80000)
        assert count(8)==7
        cmd('set_irq_in /machine/pinctrl external-drive 8 0')
        # Actual soft reset cancels a partial frame and deasserts pad drive.
        write(0x52080004,0);advance(20000)
        cmd('set_irq_in /machine/uart1 soft-reset-n 0 0');advance(100000)
        assert count(8)==7
        cmd('set_irq_in /machine/uart1 soft-reset-n 0 1')
        reset()
        assert all(count(p)==0 and hash_(p)==fnv([]) for p in (12,17,8,33))
        assert int(cmd('readl 0x52008020')[0],0)==0
        cases+=9
    assert diagnostics[0].count(NOTICE)==3
    assert 'unimplemented' not in diagnostics[0] and 'unqualified' not in diagnostics[0], diagnostics
    with running(qemu,False,OPTIONS) as (cmd,irq,reset,diagnostics):
        def advance(ns):
            for _ in range(ns//1000): cmd('clock_step 1000')
        cmd('writel 0x52008030 2')
        cmd('writel 0x52081008 8') # seven-bit external format has no qualification
        cmd('writel 0x52081004 0x55')
        assert cmd.qmp('qom-get',{'path':'/machine/pinctrl','property':'uart-tx-bytes-12'})==0
        cmd('writel 0x52081008 12');cmd('writel 0x52081004 0x55')
        cmd('writel 0x52081010 99') # active frame must not silently change timing
        assert int(cmd('readl 0x52081010')[0],0)==17
        advance(100000)
    assert 'requires8N1' in diagnostics[0] and 'live TX pad format' in diagnostics[0]
    with running(qemu,False) as (cmd,irq,reset,diagnostics):
        cmd('writel 0x52008030 2');cmd('writel 0x52081004 0x55')
        assert cmd.qmp('qom-get',{'path':'/machine/pinctrl','property':'uart-tx-bytes-12'})==0
    assert 'unimplemented alternate peripheral route pad 12' in diagnostics[0]
    return dict(passed=True,frame_cases=cases,invalid_format_and_live_change_rejected=True,
                default_disabled=True,rx_qualified=False,analog_timing_qualified=False)


if __name__=='__main__':
    import argparse,json
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu',required=True)
    print(json.dumps(uart_pads_test(parser.parse_args().qemu),indent=2))
