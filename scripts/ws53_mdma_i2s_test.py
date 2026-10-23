#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Assumed I2S interleaved sample-word DMA, FIFO requests and posted writes."""
import argparse
import json
from ws53_gpio_test import running
from ws53_mdma_test import Bus, SRC, DST, PROFILE, NOTICE
from ws53_i2s_test import Bus as I2s, PROFILE as I2S_PROFILE, NOTICE as I2S_NOTICE, TX, RX

ARGS=(*PROFILE,*I2S_PROFILE,'-global','ws53-mdma.assume-i2s=on')
DMA_NOTICE='ws53-mdma: assumed I2S merge alternating-LR words, burst1/4/8/16, prot1'
NOTICES=[NOTICE,DMA_NOTICE,I2S_NOTICE]


def words(values):return b''.join(v.to_bytes(4,'little') for v in values)


def configure(b,ch,count,rx=False,burst=1,width=4,protection=1,request=None,address=None):
    data=0x520300a0 if rx else 0x520300c0
    if address is not None:data=address
    b.configure(ch,count,width,data if rx else SRC,DST if rx else data,not rx,rx)
    n={1:0,4:1,8:2,16:3}[burst]
    b.set(ch,0x14,b.reg(ch,0x14)|(protection<<28)|(n<<12)|(n<<15))
    request=request if request is not None else 13 if rx else 12
    cfg=0x3001|((2 if rx else 1)<<9)|(request<<(1 if rx else 5))
    b.set(ch,8,cfg);return cfg


def i2s_dma_test(qemu):
    combinations=short_tails=faults=0
    with running(qemu,False,ARGS) as (cmd,irq,reset,diagnostics):
        b=Bus(cmd);i=I2s(cmd);cmd('irq_intercept_in /machine/intc')
        def setup(width=16,burst=1):
            reset();b.write(0x1c,1);period=i.init(width)
            i.write(0x88,1);i.write(0x64,(max(burst//2-1,0)<<8)|4)
            i.write(0x8c,63);i.write(0x90,3)
            return period
        for width in (16,18,20,24,32):
            for burst in (1,4,8,16):
                for ch in range(8):
                    period=setup(width,burst);rxch=(ch+1)%8;count=48
                    data=[((n+1)*0x9137137+ch)&((1<<width)-1) for n in range(count)]
                    b.fill(SRC,words(data));b.fill(DST-4,b'\x5a'*(count*4+8))
                    configure(b,rxch,count,True,burst);configure(b,ch,count,burst=burst)
                    b.step(16000);assert i.prop('frames')==0 and b.prop('writes')>0
                    i.start();b.step(period*24+16000)
                    if b.reg(rxch,0x14)&0xfff:
                        # No inferred last request for a partial watermark group.
                        i.write(0x64,4);b.step(16000)
                    assert b.prop('transfers')==2 and not b.read(0x10)
                    expected=[v^(0xa5a5 if n%2==0 else 0x5a5a) for n,v in enumerate(data)]
                    assert b.mem(DST-4,count*4+8)==b'\x5a'*4+words(expected)+b'\x5a'*4
                    assert b.mem(SRC,count*4)==words(data)
                    assert b.reg(ch,0x10)==SRC+count*4 and b.reg(rxch,4)==DST+count*4
                    assert b.reg(ch,4)==0x520300c0 and b.reg(rxch,0x10)==0x520300a0
                    assert irq[58];b.write(8,1<<ch);assert irq[58]
                    b.write(8,1<<rxch);assert not irq[58]
                    combinations+=1
        # RX may drain after serializer disable, and tail needs lower threshold.
        for burst in (4,8,16):
            for count in (2,4,6,10,14):
                period=setup(burst=burst);i.start(RX);b.step(period*(count//2));i.stop()
                b.fill(DST-4,b'\x5a'*(count*4+8));configure(b,0,count,True,burst)
                b.step(20000);remaining=b.reg(0,0x14)&0xfff
                assert remaining==count%burst
                if remaining:i.write(0x64,4);b.step(16000)
                expected=[v for n in range(1,count//2+1) for v in (n,(~n)&0xffff)]
                assert b.mem(DST-4,count*4+8)==b'\x5a'*4+words(expected)+b'\x5a'*4
                assert b.prop('transfers')==1 and i.read(0x68)==0
                short_tails+=1
        # Odd DMA lengths retain channel phase across transfers; completion of
        # one sample is not completion of a stereo frame.
        for burst in (1,4,8,16):
            period=setup(burst=burst);i.write(0x64,4)
            b.fill(SRC,words([0x1234]));configure(b,0,1,burst=burst);b.step(1000)
            assert i.read(0x6c)==1 and i.prop('transmitted')==0
            b.fill(SRC,words([0x5678]));configure(b,0,1,burst=burst);b.step(1000)
            assert i.read(0x6c)==0x101
            i.start();b.step(period);i.stop()
            configure(b,1,1,True,burst);b.step(1000)
            assert b.mem(DST,4)==words([0x1234^0xa5a5]) and i.read(0x68)==0x100
            configure(b,1,1,True,burst);b.step(1000)
            assert b.mem(DST,4)==words([0x5678^0x5a5a]) and i.read(0x68)==0
            assert b.prop('transfers')==4
        # Clock gate pauses a partially served burst without consuming more data.
        period=setup(burst=16);b.fill(SRC,words(range(16)));configure(b,0,16,burst=16)
        b.step(2000);assert b.prop('writes')==2;i.gate(False);b.step(100000)
        assert b.prop('writes')==2;i.gate();b.step(14000);assert b.prop('transfers')==1
        assert i.read(0x6c)==0x808
        # Posted TX survives HALT/disable if the peripheral can still accept it.
        for rx in (False,True):
            for stop in ('halt','disable'):
                period=setup();b.fill(SRC,words([0x1234,0x5678]));b.fill(DST,b'\x5a'*8)
                if rx:i.start(RX);b.step(period);i.stop()
                cfg=configure(b,0,2,rx);b.fault('bus-stall');b.step(500)
                assert b.prop('pending-writes')==1
                b.set(0,8,cfg|0x10000 if stop=='halt' else cfg&~1)
                b.step(20000);assert b.prop('writes')==0
                b.fault('bus-stall',on=False);b.step(500)
                assert b.prop('writes')==1 and b.prop('pending-writes')==0
                assert b.reg(0,0x14)&0xfff==1
                if rx:assert b.mem(DST,8)==words([1])+b'\x5a'*4
                else:assert i.read(0x6c)==1
                faults+=1
        for rx in (False,True):
            for fault in ('read-error','write-error'):
                period=setup();b.fill(SRC,words([1,2]));b.fill(DST,b'\x5a'*8)
                if rx:i.start(RX);b.step(period);i.stop()
                b.fault(fault);configure(b,0,2,rx);b.step(1000)
                assert b.read(0xc)==1<<8 and not b.read(0x10) and irq[58]
                assert b.prop('writes')==0 and b.mem(DST,8)==b'\x5a'*8
                if rx:assert i.read(0x68)==(0x101 if fault=='read-error' else 0x100)
                b.fault(fault,on=False)
                faults+=1
        # A posted TX now reaches an unavailable endpoint: report bus error,
        # never invent a successful drain when merge/clock/reset is removed.
        for action in ('merge','clock','reset'):
            setup();b.fill(SRC,words([1,2]));b.fault('bus-stall');configure(b,0,2)
            b.step(500);assert b.prop('pending-writes')==1
            if action=='merge':i.write(0x88,0)
            elif action=='clock':i.gate(False)
            else:i.write(0x60,0x8000)
            b.fault('bus-stall',on=False);b.step(500)
            assert b.read(0xc)==1<<8 and b.prop('pending-writes')==0 and b.prop('writes')==0
            assert i.read(0x6c)==0
            faults+=1
        # RX has already consumed a sample; peripheral reset cannot retract
        # the pending memory write, even after the DMA channel is disabled.
        period=setup();i.start(RX);b.step(period);i.stop();b.fill(DST,b'\x5a'*8)
        b.fault('bus-stall');cfg=configure(b,0,2,True);b.step(500)
        i.write(0x60,0x8000);b.set(0,8,cfg&~1);b.fault('bus-stall',on=False);b.step(500)
        assert b.mem(DST,8)==words([1])+b'\x5a'*4 and b.prop('writes')==1
        assert i.read(0x68)==0 and b.prop('pending-writes')==0
        faults+=1
        # Serializer FIFO under/overflow stays visible and independently maskable.
        period=setup();i.write(0x8c,0);i.start();b.step(period*10)
        assert i.read(0x44)&60==60 and irq[67]
        i.stop();i.write(0x48,60);assert not irq[67]
        # Partial stereo phase survives direction stop; peripheral reset clears it.
        period=setup();i.write(0xc0,0x1234);assert i.read(0x6c)==1
        i.write(0xc0,0x5678);i.start();b.step(period);i.stop()
        assert i.read(0xa0)==0x1234^0xa5a5
        assert i.read(0xa0)==0x5678^0x5a5a
        # IRQ watermark remains paired (MAX TX / MIN RX), unlike the merge
        # request's next-sample phase. An asymmetric split FIFO must not alter it.
        reset();period=i.init();i.write(0x64,0);i.push(1,2);i.start();b.step(period)
        i.read(0x54);assert not i.read(0x44)&1
        # Pause serializer clock to hold unequal TX queues during IRQ inspection.
        i.write(0x90,1);i.write(0x50,3);assert not i.read(0x44)&2
        reset();assert i.read(0x88)==0 and not b.read(0x10)
    assert diagnostics[0].splitlines()==NOTICES,diagnostics
    rejected=0
    with running(qemu,False,ARGS) as (cmd,_,reset,diagnostics):
        b=Bus(cmd);i=I2s(cmd)
        for rx in (False,True):
            for fault in ('width8','width16','protection','request','address'):
                reset();b.write(0x1c,1);i.init();i.write(0x88,1);i.write(0x90,3)
                configure(b,0,4,rx,width=1 if fault=='width8' else 2 if fault=='width16' else 4,
                          protection=2 if fault=='protection' else 1,
                          request=14 if fault=='request' else None,
                          address=0x52030054 if fault=='address' else None)
                b.step(10000);assert not b.read(0x10) and b.prop('reads')==0;rejected+=1
        # Mixed split/merge accesses, active merge switch, unsupported byte lane.
        reset();i.init();i.write(0x88,1);i.write(0x90,3)
        i.write(0x4c,1);rejected+=1
        i.write(0xc0,1);i.write(0x88,0);rejected+=1
        cmd('writew 0x520300c0 1');rejected+=1
        i.start();i.write(0x88,0);rejected+=1
    lines=diagnostics[0].splitlines();assert lines[:3]==NOTICES
    assert len(lines)==3+rejected and all('unimplemented' in x for x in lines[3:]),diagnostics
    return {'width_burst_channel_combinations':combinations,'short_tails':short_tails,'odd_sample_sequences':4,
            'posted_write_and_error_cases':faults,'invalid_diagnostics':rejected,
            'assumed_merge':'alternating left/right 32-bit sample accesses',
            'assumed_bursts':[1,4,8,16],'hardware_verified':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--qemu',required=True)
    print(json.dumps(i2s_dma_test(p.parse_args().qemu),indent=2))
