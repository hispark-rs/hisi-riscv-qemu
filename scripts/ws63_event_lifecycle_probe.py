#!/usr/bin/env python3
"""Observe vendor event callback payloads and actual allocator reclamation.

The task callback is explicitly the original ROM no-op slot handler. No protocol
handler or RF outcome is synthesized. Heap init/alloc/free execute original code.
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


def observe(g,pc,args,task,queue):
    raw=bytearray.fromhex(g.request('g'))
    for index,value in {1:0xa7ff00,2:0xa7f000,3:0xa12938,32:pc,**{10+i:a for i,a in enumerate(args)}}.items():
        raw[index*4:index*4+4]=value.to_bytes(4,'little')
    assert g.request('G'+raw.hex())=='OK'
    stops=(0x1392e2,0x136706,0xa7ff00)
    for address in stops:assert g.request(f'Z0,{address:x},2')=='OK'
    events=[]
    for _ in range(12):
        reply=g.request('c');raw=bytes.fromhex(g.request('g'))
        values=[int.from_bytes(raw[i:i+4],'little') for i in range(0,132,4)]
        address=values[32];assert address in stops,(reply,hex(address))
        if address==0xa7ff00:return events
        event={'pc':hex(address),'a0':values[10],'a1':values[11],
               'queue':[g.read(queue+4*i) for i in range(3)],'task_next':g.read(task)}
        if address==0x1392e2:event['payload_hex']=g.read(values[12],12).to_bytes(12,'little').hex()
        events.append(event)
        assert g.request(f'z0,{address:x},2')=='OK'
        g.request('s')
        assert g.request(f'Z0,{address:x},2')=='OK'
    raise AssertionError('too many callback/free stops')


def run(args):
    assert sha(args.rom)==ROM_SHA and sha(args.elf)==ELF_SHA
    assert b'WS63 strict ROM: illegal instruction' in Path(args.qemu).read_bytes()
    out=Path(args.out).resolve();out.mkdir(parents=True,exist_ok=True)
    elf=Path(args.elf).read_bytes();phoff=struct.unpack_from('<I',elf,28)[0]
    phsize,phcount=struct.unpack_from('<HH',elf,42)
    segments=[struct.unpack_from('<8I',elf,phoff+i*phsize) for i in range(phcount)]
    code=[s for s in segments if s[0]==1 and s[2]!=s[3] and s[4]]
    symbols={}
    if args.replacement_elf:
        symbols={p[2]:int(p[0],16) for line in subprocess.check_output(
            [args.nm,'--defined-only',args.replacement_elf],text=True).splitlines()
            if len(p:=line.split())==3}
    results=[]
    for urgent in (False,True):
      for event_code in (6,7,8,9,10):
        finish=event_code!=10
        name=f'{"urgent" if urgent else "normal"}-event-{event_code}'
        vendor_pc=0x139302 if finish else 0x139338
        pc=symbols["ws63_probe_event_dispatch"] if symbols else vendor_pc
        with tempfile.TemporaryDirectory(prefix='event-life-') as tmp:
            sock=Path(tmp)/'gdb';trace=out/(name+'.jsonl')
            cmd=[str(Path(args.qemu).resolve()),'-M','ws63','-S','-display','none','-monitor','none',
                 '-serial','null','-kernel',str(Path(args.elf).resolve()),
                 '-device',f'loader,file={Path(args.rom).resolve()},addr=0x100000,force-raw=on',
                 '-device',f'ws63-sle-research,trace-file={trace}',
                 '-gdb',f'unix:{sock},server=on,wait=off','-icount','shift=2']
            for s in code:
                path=Path(tmp)/f'code-{s[2]:x}.bin';path.write_bytes(elf[s[1]:s[1]+s[4]])
                cmd+=['-device',f'loader,file={path},addr={s[2]:#x},force-raw=on']
            if symbols:cmd+=['-device',f'loader,file={Path(args.replacement_elf).resolve()}']
            with (out/(name+'.stderr')).open('w') as log:
                proc=subprocess.Popen(cmd,stdout=log,stderr=log,env=dict(os.environ,WS63_SLE_STRICT_ROM='1'))
                g=None
                try:
                    g=RSP(sock);pool=0xa40000;size=0x8000
                    got=next(s for s in code if s[2]<=0xa12108<s[2]+s[4])
                    got_value=struct.unpack_from('<I',elf,got[1]+0xa12108-got[2])[0]
                    assert g.read(0xa12108)==got_value  # __clzsi2 uses relocated initialized GOT data.
                    for off in range(0,size,1024):g.write(pool+off,0,1024)
                    g.write(0xa2d77c,0);g.write(0xa2d780,pool)
                    g.write(0x18024c,0);g.write(0x18369c,0,2);g.write(0x183698,0)
                    init=g.call(0x24c2cc,(pool,size));assert init==0,hex(init)
                    task=g.call(0xa0c880,(pool,32));assert pool<=task<pool+size-32,hex(task)
                    queue=0x1827ac if urgent else 0x182798
                    sed,env,irq=0xa50300,0xa50400,0xa50800
                    for a,n in ((task,32),(sed,32),(env,64),(irq,8),(0x182798,40)):g.write(a,0,n)
                    g.write(task+4,sed);g.write(task+8,env);g.write(task+12,0x89abcdef)
                    g.write(task+16,0x123456aa);g.write(task+20,0x76543210)
                    g.write(task+24,0xfffe,2);g.write(task+26,0xa55a,2)
                    g.write(sed+4,3,2);g.write(sed+8,0x1392e2);g.write(env+27,2 if urgent else 1,1)
                    g.write(queue,task);g.write(queue+4,task);g.write(queue+8,1)
                    word=0x80000000|(event_code<<25)|(int(urgent)<<24)|(3<<16)|(0xaa<<8)|0x55
                    g.write(irq,word);g.write(irq+4,0xfeedbeef);g.write(0x1828c5,0xf0,1)
                    start=trace.stat().st_size
                    events=observe(g,pc,(irq,0x11223344),task,queue)
                    stop=trace.stat().st_size
                    if symbols:assert g.read(symbols["ws63_probe_status"])==0
                    assert [e['pc'] for e in events]==(['0x1392e2','0x136706'] if finish else ['0x1392e2']),events
                    expected_queue=[0,0,0] if finish else [task,task,1]
                    for event in events:assert event['queue']==expected_queue and event['task_next']==0,event
                    cb=events[0];assert cb['a0']==event_code and cb['a1']==sed,cb
                    assert cb['payload_hex']==struct.pack('<IIHH',0x89abcdef,0x76543210,0xfffe,0xa55a).hex()
                    if finish:assert events[1]['a0']==task
                    after=[g.read(queue+4*i) for i in range(3)];assert after==expected_queue
                    assert g.read(0x1828c5,1)==(2 if urgent else 1)
                    assert g.read(irq)==word and g.read(irq+4)==0xfeedbeef
                    rows=[json.loads(line) for line in trace.read_bytes()[start:stop].splitlines()];assert not rows,rows
                    # Remove observational breakpoints before the allocator check.
                    for a in (0x1392e2,0x136706):assert g.request(f'z0,{a:x},2')=='OK'
                    next_task=g.call(0xa0c880,(pool,32))
                    assert pool<=next_task<pool+size-32
                    assert (next_task==task)==finish,(name,hex(task),hex(next_task))
                    results.append({'case':name,'pc':hex(pc),'reference_vendor_pc':hex(vendor_pc),
                        'implementation':'reconstructed lifecycle with original allocator/callback hooks' if symbols else 'vendor ROM lifecycle',
                        'input':{'urgent':urgent,'event_code':event_code,
                        'word':word,'task':task,'queue':queue,'sed':sed,'pool':pool,'pool_size':size},
                        'callbacks':events,'queue_after':after,'protocol_after':2 if urgent else 1,
                        'allocated_again':next_task,'same_address_reused':next_task==task,'firmware_accesses':rows})
                except TimeoutError:
                    if g:
                        g.sock.sendall(b'\x03');reply=g.receive();raw=bytes.fromhex(g.request('g'))
                        registers=[int.from_bytes(raw[i:i+4],'little') for i in range(0,132,4)]
                        snapshot={'stop':reply,'registers':[hex(x) for x in registers],
                                  'stack':[hex(g.read(registers[2]+4*i)) for i in range(20)]}
                        (out/(name+'-timeout.json')).write_text(json.dumps(snapshot,indent=2)+'\n')
                    raise
                finally:
                    if g:g.sock.close()
                    proc.terminate();proc.wait(timeout=5)
    report={'claim':'Matched event callback payload and task lifetime; original allocator executes, callback itself is an explicit no-op fixture; no real protocol handler or deferred link-task GC',
            'rom_sha256':sha(args.rom),'elf_sha256':sha(args.elf),'qemu_sha256':sha(args.qemu),'cases':results,
            'replacement_elf_sha256':sha(args.replacement_elf) if symbols else None,
            'synthetic_load_relocations':[{'elf_offset':hex(s[1]),'vma':hex(s[2]),'lma':hex(s[3]),'length':s[4],'elf_flags':s[6],
                'sha256':hashlib.sha256(elf[s[1]:s[1]+s[4]]).hexdigest()} for s in code]}
    (out/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'passed':len(results),'out':str(out)}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('qemu','rom','elf','out'):p.add_argument('--'+name,required=True)
    p.add_argument('--replacement-elf');p.add_argument('--nm',default='nm')
    run(p.parse_args())
