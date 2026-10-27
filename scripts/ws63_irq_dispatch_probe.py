#!/usr/bin/env python3
"""Execute the linked IRQ dispatch loop with synthetic callback tables.

The callback is the original ROM slot handler (a real no-op), deliberately
installed for test event codes. This proves dispatch/release, not event-specific
processing. The OS HCI queue is absent; notification is observed, not delivered.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
from ws63_sle_vendor_probe import RSP, ROM_SHA, ELF_SHA, sha


def regs(g):
    data=bytes.fromhex(g.request('g'))
    return [int.from_bytes(data[i:i+4],'little') for i in range(0,132,4)]


def execute(g,pc,trace,queue_state,trace_start):
    data=bytearray.fromhex(g.request('g'))
    for i,value in {1:0xa7ff00,2:0xa7f000,3:0xa12938,32:pc}.items():
        data[i*4:i*4+4]=value.to_bytes(4,'little')
    assert g.request('G'+data.hex())=='OK'
    stops=(0x1392e2,0x13675a,0xa0dc62,0xa7ff00)
    for address in stops: assert g.request(f'Z0,{address:x},2')=='OK'
    events=[]
    for _ in range(20):
        stop=g.request('c'); values=regs(g); current=values[32]
        assert current in stops,(stop,hex(current))
        if current==0xa7ff00: return events
        event={'pc':hex(current),'a0':values[10],'a1':values[11],'trace_byte':trace.stat().st_size}
        if current==0x1392e2:
            event['word_before_release']=g.read(values[10])
            event['second_word']=g.read(values[10]+4)
            event['consumer']=g.read(queue_state+4,1)
        event['mmio_count']=len(trace.read_bytes()[trace_start:].splitlines())
        events.append(event)
        assert g.request(f'z0,{current:x},2')=='OK'
        g.request('s')
        assert g.request(f'Z0,{current:x},2')=='OK'
    raise AssertionError('unexpected number of callback stops')


def run(args):
    assert sha(args.rom)==ROM_SHA and sha(args.elf)==ELF_SHA
    assert b'WS63 strict ROM: illegal instruction' in Path(args.qemu).read_bytes()
    out=Path(args.out).resolve(); out.mkdir(parents=True,exist_ok=True)
    elf=Path(args.elf).read_bytes(); phoff=struct.unpack_from('<I',elf,28)[0]
    phsize,phcount=struct.unpack_from('<HH',elf,42)
    segments=[struct.unpack_from('<8I',elf,phoff+i*phsize) for i in range(phcount)]
    code=[s for s in segments if s[0]==1 and s[6]&1 and s[2]!=s[3] and s[4]]
    variants=[('single',1,2,[(1,4)]),('batch',0,3,[(0,4),(1,11),(2,24)]),
              ('wrap',3,1,[(3,11),(0,4)]),('skip',0,3,[(2,24)]),
              ('empty',0,3,[]),('full',0,0,[(0,4),(1,11),(2,24),(3,4)])]
    symbols={}
    if args.replacement_elf:
        symbols={p[2]:int(p[0],16) for line in subprocess.check_output(
            [args.nm,'--defined-only',args.replacement_elf],text=True).splitlines()
            if len(p:=line.split())==3}
    results=[]
    for protocol in ('ble','gle'):
      for kind,consumer,producer,valid in variants:
        name=protocol+'-'+kind; ble=protocol=='ble'
        vendor_pc=0x2f230a if ble else 0x3061e8
        pc=symbols["ws63_probe_"+protocol+"_irq_service"] if symbols else vendor_pc
        state=0x182484 if ble else 0x182800
        table=0x180194 if ble else 0x180360
        position=0x490012e4 if ble else 0x49001600
        status=0x4900100c if ble else 0x49001414
        with tempfile.TemporaryDirectory(prefix='irq-dispatch-') as tmp:
            sock=Path(tmp)/'gdb'; trace=out/(name+'.jsonl')
            cmd=[str(Path(args.qemu).resolve()),'-M','ws63','-S','-display','none','-monitor','none',
                 '-serial','null','-kernel',str(Path(args.elf).resolve()),
                 '-device',f'loader,file={Path(args.rom).resolve()},addr=0x100000,force-raw=on',
                 '-device',f'ws63-sle-research,trace-file={trace}',
                 '-gdb',f'unix:{sock},server=on,wait=off','-icount','shift=2']
            for s in code:
                path=Path(tmp)/f'code-{s[2]:x}.bin';path.write_bytes(elf[s[1]:s[1]+s[4]])
                cmd+=['-device',f'loader,file={path},addr={s[2]:#x},force-raw=on']
            if symbols: cmd+=['-device',f'loader,file={Path(args.replacement_elf).resolve()}']
            with (out/(name+'.stderr')).open('w') as log:
                proc=subprocess.Popen(cmd,stdout=log,stderr=log,env=dict(os.environ,WS63_SLE_STRICT_ROM='1'))
                g=None
                try:
                    g=RSP(sock); base=0x49011020; depth=4
                    words=[0x10000+i for i in range(depth)]
                    for i,event in valid: words[i]=0x80000000|(event<<25)|(i<<16)|0x1255
                    for i,word in enumerate(words):
                        g.write(base+8*i,word);g.write(base+8*i+4,0xfeed0000+i)
                    g.write(state,base);g.write(state+4,consumer,1);g.write(state+5,depth,1)
                    g.write(position,producer<<19);g.write(status,0)
                    g.write(table,0,100)
                    for event in (4,11,24): g.write(table+4*event,0x1392e2)
                    g.write(0xa2d6c8,0xa50900);g.write(0xa50900,0,32);g.write(0xa50904,0xa0dc62)
                    g.write(0x18369c,0,2);g.write(0x183698,0)
                    g.write(0xa2a85c,0)  # No HCI thread queue: real notifier takes its absent-queue branch.
                    g.write(0x18024c,0)
                    start=trace.stat().st_size
                    events=execute(g,pc,trace,state,start)
                    if symbols: assert g.read(symbols["ws63_probe_status"])==0
                    stop=trace.stat().st_size
                    callbacks=[e for e in events if e['pc']=='0x1392e2']
                    assert [e['a0'] for e in callbacks]==[base+8*i for i,_ in valid],(name,events)
                    assert all(e['word_before_release']==words[(e['a0']-base)//8] for e in callbacks)
                    assert all(e['consumer']==(e['a0']-base)//8 for e in callbacks)
                    assert events[0]['pc']=='0xa0dc62' and events[0]['a0']==(46 if ble else 47)
                    assert events[-1]['pc']=='0x13675a'
                    after=[g.read(base+8*i) for i in range(depth)]
                    assert after==[w&0x7fffffff for w in words]
                    assert [g.read(base+8*i+4) for i in range(depth)]==[0xfeed0000+i for i in range(depth)]
                    expected_index=(valid[-1][0]+1)%depth if valid else producer
                    assert g.read(state+4,1)==expected_index
                    assert g.read(0x18369c,2)==0
                    rows=[json.loads(line) for line in trace.read_bytes()[start:stop].splitlines()]
                    assert [(r['op'],r['address']) for r in rows[:2]]==[('R',status),('W',status+8)]
                    assert rows[0]['value']==rows[1]['value']==0
                    assert sum(r['address']==position for r in rows)==1
                    assert any(r['address']==0x49001500 for r in rows) # Both ISR paths use GLE clock.
                    results.append({'case':name,'pc':hex(pc),'reference_vendor_pc':hex(vendor_pc),
                        'implementation':'reconstructed core with original platform hooks' if symbols else 'vendor linked ISR',
                        'input':{'consumer':consumer,'producer':producer,'words':words},
                        'callback_events':events,'words_after':after,'consumer_after':expected_index,
                        'firmware_accesses':rows,'thread_notification_delivered':False})
                finally:
                    if g:g.sock.close()
                    proc.terminate();proc.wait(timeout=5)
    report={'claim':'Linked ISR control-flow with synthetic ROM slot callbacks and absent HCI queue; no event-specific processing, asynchronous IRQ or notification delivery',
            'rom_sha256':sha(args.rom),'elf_sha256':sha(args.elf),'qemu_sha256':sha(args.qemu),'cases':results,
            'replacement_elf_sha256':sha(args.replacement_elf) if symbols else None,
            'synthetic_executable_relocations':[{'elf_offset':hex(s[1]),'vma':hex(s[2]),'lma':hex(s[3]),'length':s[4],
                'sha256':hashlib.sha256(elf[s[1]:s[1]+s[4]]).hexdigest()} for s in code]}
    (out/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'passed':len(results),'out':str(out)}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('qemu','rom','elf','out'):p.add_argument('--'+name,required=True)
    p.add_argument('--replacement-elf');p.add_argument('--nm',default='nm')
    run(p.parse_args())
