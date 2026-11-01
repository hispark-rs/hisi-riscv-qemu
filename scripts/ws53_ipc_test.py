#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Explicit IPC digital peer/ACK/mask/reset model; not a shared-ring test."""
import argparse
import json
from ws53_gpio_test import running

BASE=0x52000020
PROFILE=('-global','ws53-ipc.assume-profile=on')
NOTICE='ws53-ipc: assumed digital two-channel doorbell/ACK/clock profile; no ring or peer ownership proof'


def run(qemu):
    rejected=0
    with running(qemu,False) as (cmd,irq,reset,diagnostics):
        cmd(f'readl {BASE:#x}')
        cmd(f'writew {BASE+20:#x} 1')
        assert cmd.qmp('qom-get',dict(path='/machine/ipc',property='rejections'))==2
    assert len(diagnostics[0].splitlines())==2 and all('ws53-ipc: unavailable/invalid' in n for n in diagnostics[0].splitlines())
    rounds=[]
    with running(qemu,False,PROFILE) as (cmd,irq,reset,diagnostics):
        cmd('irq_intercept_in /machine/intc')
        def mask():return cmd.qmp('qom-get',dict(path='/machine/ipc',property='receive-mask'))
        def read(off):return int(cmd(f'readl {BASE+off:#x}')[0],0)
        def write(off,value,size='l'):cmd(f'write{size} {BASE+off:#x} {value:#x}')
        def peer(n):
            cmd(f'set_irq_in /machine/ipc peer {n} 1');cmd(f'set_irq_in /machine/ipc peer {n} 0')
        def count(n):return cmd.qmp('qom-get',dict(path='/machine/ipc',property=n))
        for iteration in range(3):
            if iteration:reset()
            assert [mask(),read(8),read(16)]==[0,0,0]
            write(0,0x8000);peer(0);peer(1)
            assert read(8)==3 and not irq.get(26,False) and not irq.get(27,False)
            write(0,0x8003);assert irq[26] and irq[27]
            write(12,1);assert read(8)==2 and not irq[26] and irq[27]
            assert read(12)==0xa55a5aa5 and read(8)==2
            write(0,0x8001);assert not irq[27] and read(8)==2
            write(0,0x8003);assert irq[27]
            write(20,1,'w');write(20,2,'w');assert read(16)==3
            peer(2);assert read(16)==2
            peer(3);assert read(16)==0
            cmd('set_irq_in /machine/ipc fault 0 1');write(12,2);assert read(8)==2
            cmd('set_irq_in /machine/ipc fault 0 0');write(12,2);assert read(8)==0 and not irq[27]
            peer(0);cmd('set_irq_in /machine/ipc clock 0 0');assert not irq[26]
            read(8);rejected+=1
            cmd('set_irq_in /machine/ipc clock 0 1');assert irq[26] and read(8)==1
            before=(mask(),read(8),read(16),count('ack-writes'),count('send-writes'))
            for off,access,size,value in [(0,'r',4,0),(4,'r',4,0),(8,'w',4,3),(16,'w',4,3),(20,'w',4,1),
                                          (12,'w',2,1),(0,'w',2,1),(20,'r',2,0),(24,'w',4,0),
                                          (24,'w',4,3),(20,'w',2,4),(0,'w',4,0x10000)]:
                if access=='r':cmd(f'read{"l" if size==4 else "w"} {BASE+off:#x}')
                else:write(off,value,'l' if size==4 else 'w')
                rejected+=1
                assert (mask(),read(8),read(16),count('ack-writes'),count('send-writes'))==before
            write(24,1);assert (mask(),read(8),read(16))==before[:3]
            assert count('rejections')==13
            rounds.append(dict(iteration=iteration,rejections=13,ack_writes=count('ack-writes'),
                               sync_reads=count('sync-reads'),send_writes=count('send-writes')))
    lines=diagnostics[0].splitlines()
    assert lines[0]==NOTICE and len(lines)==1+rejected and all('unavailable/invalid' in n for n in lines[1:])
    return dict(passed=True,rounds=rounds,negative_accesses=rejected+2,
                hardware_accessed=False,ring_modeled=False,resource_handoff_verified=False)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--qemu',required=True)
    print(json.dumps(run(p.parse_args().qemu),indent=2))
