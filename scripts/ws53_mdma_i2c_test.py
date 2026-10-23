#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""I2C DMA byte data, halfword commands, bursts, short tails and cancellation."""
import argparse
import json
import subprocess
from ws53_gpio_test import running
from ws53_mdma_test import Bus, SRC, DST, PROFILE, NOTICE
from ws53_i2c_test import Bus as I2c, PROFILE as I2C_PROFILE, NOTICE as I2C_NOTICE

ARGS = (*PROFILE, *I2C_PROFILE, '-global', 'ws53-mdma.assume-i2c=on')
DMA_NOTICE = 'ws53-mdma: assumed I2C0/1 byte-data halfword-command requests, burst1/4, TXprot1/RXprot2'
NOTICES = [NOTICE, DMA_NOTICE, I2C_NOTICE]


def requests(i, tx=4, rx=3, enable=3):
    i.write(0xa4,tx);i.write(0xa8,rx);i.write(0xa0,enable)


def configure(b,i,ch,count,rx=False,width=2,burst=4,fixed=False,address=None):
    width=1 if rx else width
    data=i.base+0x1c;address=address if address is not None else DST if rx else SRC
    b.configure(ch,count,width,data if rx else address,address if rx else data,not rx and not fixed,rx)
    b.set(ch,0x14,b.reg(ch,0x14)|((2 if rx else 1)<<28)|(0x9000 if burst==4 else 0))
    cfg=0x3001|((2 if rx else 1)<<9)|((i.index*2+int(rx))<<(1 if rx else 5))
    b.set(ch,8,cfg);return cfg


def commands(count): return b'\x00\x01'*(count-1)+b'\x00\x03'


def i2c_dma_test(qemu):
    combinations=static_reads=byte_writes=0
    with running(qemu,False,ARGS) as (cmd,irq,reset,diagnostics):
        b=Bus(cmd);cmd('irq_intercept_in /machine/intc')
        def setup(port=0,burst=4):
            reset();b.write(0x1c,1);i=I2c(cmd,port);i.init();requests(i,rx=3 if burst==4 else 0);return i
        for port in range(2):
            for burst in (1,4):
                for ch in range(8):
                    i=setup(port,burst);rxch=(ch+1)%8;count=33;data=commands(count)
                    b.fill(SRC,data);b.fill(DST-4,b'\x5a'*(count+8));i.write(0xb0,0x200)
                    configure(b,i,rxch,count,True,burst=burst);configure(b,i,ch,count,burst=burst)
                    b.step(499);assert b.prop('reads')==0
                    b.step(1);assert b.prop('reads')==1 and b.prop('pending-writes')==1
                    b.step(5_000_000)
                    if burst==4:
                        # No fabricated last-request pulse for the final byte.
                        assert b.prop('transfers')==1 and i.read(0x68)==1
                        assert b.reg(rxch,0x14)&0xfff==1 and b.mem(DST+32,1)==b'\x5a'
                        i.write(0xa8,0);b.step(1000)
                    assert b.prop('transfers')==2 and b.prop('writes')==count*2
                    assert b.mem(SRC,len(data))==data
                    assert b.mem(DST-4,count+8)==b'\x5a'*4+bytes(x^0xa5 for x in range(count))+b'\x5a'*4
                    assert b.reg(ch,0x10)==SRC+len(data) and b.reg(rxch,4)==DST+count
                    assert b.reg(ch,4)==b.reg(rxch,0x10)==i.base+0x1c
                    assert i.read(0x64)==i.read(0x68)==0 and not i.read(0x60)&1
                    assert i.counter('read-acks')==count-1 and i.counter('read-nacks')==1
                    assert i.counter('stops')==1 and irq[62+port] and irq[58]
                    b.write(8,1<<ch);assert irq[58];b.write(8,1<<rxch);assert not irq[58]
                    assert irq[62+port];i.write(0xc4,1);assert not irq[62+port]
                    combinations+=1
        for port in range(2):
            for count in (1,3,4,5,8,9,17,33,257):
                i=setup(port);b.fill(SRC,b'\x00\x01');b.fill(DST-1,b'\x5a'*(count+2))
                configure(b,i,1,count,True)
                if count>1:
                    configure(b,i,0,count-1,fixed=True);b.step(40_000_000)
                    assert b.reg(0,0x14)&0xfff==0 and b.reg(0,0x10)==SRC
                    assert i.counter('stops')==0
                # CPU final command after fixed-source command DMA, like SDK.
                i.write(0x1c,0x300);b.step(1_000_000)
                if count%4:
                    assert b.reg(1,0x14)&0xfff==count%4 and i.read(0x68)==count%4
                    i.write(0xa8,0);b.step(4000)
                assert b.reg(1,0x14)&0xfff==0
                assert b.mem(DST-1,count+2)==b'\x5a'+bytes((x&255)^0xa5 for x in range(count))+b'\x5a'
                assert i.counter('read-acks')==count-1 and i.counter('read-nacks')==1
                static_reads+=1
            for burst in (1,4):
                i=setup(port,burst);data=bytes([0x20])+bytes(range(17));b.fill(SRC,data)
                configure(b,i,0,len(data),width=1,burst=burst);b.step(3_000_000)
                assert b.prop('transfers')==1 and i.counter('stops')==0
                i.send(0x2ef);assert i.counter('stops')==1;i.send(0x20);actual=[]
                for x in range(18):
                    i.send((0x500 if x==0 else 0x100)|(0x200 if x==17 else 0));actual.append(i.read(0x1c))
                assert bytes(actual)==bytes(range(17))+b'\xef';byte_writes+=1
            # Accepted RX burst continues below watermark.
            i=setup(port)
            for _ in range(4):i.send(0x100)
            b.fill(DST,b'\x5a'*4);configure(b,i,1,4,True);b.step(500)
            assert i.read(0x68)==3 and b.prop('reads')==1
            b.step(3500);assert b.prop('writes')==4 and i.read(0x68)==0
            # Below-watermark burst phase still pauses precisely on clock loss.
            i=setup(port)
            for _ in range(4):i.send(0x100)
            configure(b,i,1,4,True);b.step(1200)
            assert b.prop('reads')==b.prop('writes')==1 and i.read(0x68)==3
            i.clock(False);b.step(10000);assert b.prop('reads')==1
            i.clock();b.step(299);assert b.prop('reads')==1
            b.step(1);assert b.prop('reads')==2;b.step(2500);assert b.prop('writes')==4
            # Low watermark with four-beat mode cannot consume missing data;
            # availability changes within the burst must wake it again.
            i=setup(port);requests(i,rx=0);configure(b,i,1,4,True)
            for n in range(4):
                i.send(0x300 if n==3 else 0x100);b.step(1000)
                assert b.prop('writes')==n+1 and b.mem(DST+n,1)==bytes([n^0xa5])
            # Burst versus single-beat TX pacing under FIFO pressure.
            for burst,expected in ((1,5),(4,8)):
                i=setup(port,burst);i.fault('stretch');b.fill(SRC,commands(17))
                configure(b,i,0,17,burst=burst);b.step(20000)
                assert b.prop('writes')==expected and i.read(0x64)==expected;i.fault('stretch',False)
            # Posted TX survives cancel/requests-off; future beats do not.
            i=setup(port);b.fill(SRC,commands(3));b.fault('bus-stall')
            cfg=configure(b,i,0,3);b.step(500);assert b.prop('pending-writes')==1
            b.set(0,8,(cfg&~1)|0x10000);requests(i,enable=0);b.fault('bus-stall',on=False);b.step(500)
            assert b.prop('writes')==1 and b.reg(0,0x14)&0xfff==2 and i.read(0x64)==1
            b.step(1_000_000);assert b.prop('writes')==1
            # Requests-off pauses an accepted burst, then resumes its remaining beats.
            i=setup(port);b.fill(SRC,commands(8));configure(b,i,0,8);b.step(1000)
            requests(i,enable=0);b.step(10000);assert b.prop('writes')==1
            requests(i);b.step(10000);assert b.prop('writes')==8
            # Consumed RX byte survives cancellation and loss of peripheral clock.
            i=setup(port,1);i.send(0x300);b.fill(DST,b'\x5a');b.fault('bus-stall',1)
            cfg=configure(b,i,1,1,True,burst=1);b.step(500)
            assert i.read(0x68)==0 and b.mem(DST,1)==b'\x5a'
            b.set(1,8,(cfg&~1)|0x10000);requests(i,enable=0);i.clock(False)
            b.fault('bus-stall',1,False);b.step(500);assert b.mem(DST,1)==b'\xa5'
            # Clock withdrawal preserves unread phase time; posted TX errors.
            i=setup(port);b.fill(SRC,commands(3));configure(b,i,0,3);b.step(200)
            i.clock(False);b.step(10000);assert b.prop('reads')==0
            i.clock();b.step(299);assert b.prop('reads')==0
            b.step(1);assert b.prop('reads')==1
            i.clock(False);b.step(500);assert b.prop('errors')==1 and b.prop('writes')==0
            # Peripheral abort withdraws requests without inventing DMA completion.
            for fault in ('nack-address','arbitration'):
                i=setup(port);i.fault(fault);b.fill(SRC,commands(33))
                configure(b,i,1,33,True);configure(b,i,0,33);b.step(1_000_000)
                assert i.read(0xb8)&64 and b.prop('transfers')==0
                writes=b.prop('writes');b.step(1_000_000);assert b.prop('writes')==writes;i.fault(fault,False)
            # Invalid halfword command and FIFO overflow at a posted write
            # become DMA errors rather than silently accepted command loss.
            i=setup(port);b.fill(SRC,b'\x00\x08');configure(b,i,0,1);b.step(1000)
            assert b.prop('errors')==1 and i.read(0x64)==0
            i=setup(port);i.fault('stretch');b.fill(SRC,commands(1));b.fault('bus-stall')
            configure(b,i,0,1);b.step(500)
            for _ in range(8):i.write(0x1c,0x100)
            b.fault('bus-stall',on=False);b.step(500)
            assert b.prop('errors')==1 and i.read(0x64)==8;i.fault('stretch',False)
            for rx in (False,True):
                for fault in ('read-error','write-error'):
                    i=setup(port,1);b.fill(SRC,commands(1));b.fill(DST,b'\x5a')
                    if rx:i.send(0x300)
                    b.fault(fault);configure(b,i,0,1,rx,burst=1);b.step(1000)
                    assert b.prop('errors')==1 and b.prop('writes')==0 and b.read(0xc)==0x100
                    assert b.mem(DST,1)==b'\x5a';b.fault(fault,on=False)
            # Reset discards accepted burst credit and pending transactions.
            i=setup(port);b.fault('bus-stall');configure(b,i,0,3);b.step(500)
            reset();b.fault('bus-stall',on=False);b.step(1_000_000)
            assert b.prop('pending-writes')==b.prop('writes')==i.read(0x64)==0
        reset();b.write(0x1c,1)
        for port in range(2):
            i=I2c(cmd,port);i.init();requests(i);b.fill(SRC+port*1024,commands(32));b.fill(DST+port*1024,b'\x5a'*32)
            configure(b,i,port*2+1,32,True,address=DST+port*1024);configure(b,i,port*2,32,address=SRC+port*1024)
        b.step(5_000_000);assert b.prop('transfers')==4
        for port in range(2):assert b.mem(DST+port*1024,32)==bytes(x^0xa5 for x in range(32))
    assert diagnostics[0].splitlines()==NOTICES,diagnostics
    rejects=0
    with running(qemu,False,ARGS) as (cmd,irq,reset,diagnostics):
        b=Bus(cmd)
        for port in range(2):
            for kind in ('wrong-data','wrong-route','wrong-direction','word','rx-halfword','protection','increment','zero','burst-memory'):
                reset();b.write(0x1c,1);i=I2c(cmd,port);i.init();requests(i);rx=kind=='rx-halfword';data=i.base+0x1c
                b.configure(0,0 if kind=='zero' else 1,4 if kind=='word' else 2,data if rx else SRC,
                            DST if rx else data+4 if kind=='wrong-data' else data,not rx,rx or kind=='increment')
                if kind=='burst-memory':b.configure(0,1,2,SRC,DST)
                b.set(0,0x14,b.reg(0,0x14)|((0 if kind in ('protection','burst-memory') else 2 if rx else 1)<<28)|0x9000)
                request=port*2+int(rx)+(1 if kind=='wrong-direction' else 0)
                if kind=='wrong-route':request=(1-port)*2
                cfg=0x3001 if kind=='burst-memory' else 0x3001|((2 if rx else 1)<<9)|(request<<(1 if rx else 5))
                b.set(0,8,cfg);assert b.read(0x10)==0;rejects+=1
            for off,value in ((0xa0,4),(0xa4,8),(0xa8,8)):i.write(off,value);rejects+=1
            for ctrl in ((1<<12)|1,(2<<12)|(2<<15)|1):b.set(0,0x14,ctrl);rejects+=1
    lines=diagnostics[0].splitlines();assert lines[:3]==NOTICES and len(lines)==3+rejects,lines
    assert all('unimplemented write' in line for line in lines[3:])
    bad=subprocess.run([qemu,'-M','ws53','-display','none','-global','ws53-mdma.assume-i2c=on'],capture_output=True,text=True,timeout=5)
    assert bad.returncode!=0 and 'assume-i2c requires assume-profile' in bad.stderr
    return {'port_channel_burst_read_combinations':combinations,'fixed_command_short_long_reads':static_reads,
            'byte_write_roundtrips':byte_writes,'concurrent_ports':2,'rejected_configs':rejects,
            'checks':['byte/halfword lanes','burst1/4 threshold pacing','short tail waits for software watermark change',
                      'STOP ACK/NACK accounting','IRQ58/62/63 isolation','request/clock withdrawal','posted TX/RX after cancel',
                      'NACK/arbitration and DMA bus errors','FIFO pressure','reset clears burst credits'],
            'hardware_verified':False,'physical_burst_tail_or_drain_verified':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--qemu',required=True)
    print(json.dumps(i2c_dma_test(p.parse_args().qemu),indent=2))
