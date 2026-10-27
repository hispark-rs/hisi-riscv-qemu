#!/usr/bin/env python3
"""Exercise linked vendor EEQ event matching; no hardware completion is invented."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
from ws63_sle_vendor_probe import RSP, ROM_SHA, ELF_SHA, sha


def run(args):
    assert sha(args.rom)==ROM_SHA and sha(args.elf)==ELF_SHA
    assert b'WS63 strict ROM: illegal instruction' in Path(args.qemu).read_bytes()
    out=Path(args.out).resolve()
    out.mkdir(parents=True,exist_ok=True)
    elf=Path(args.elf).read_bytes()
    phoff=struct.unpack_from('<I',elf,28)[0]
    phsize,phcount=struct.unpack_from('<HH',elf,42)
    segments=[struct.unpack_from('<8I',elf,phoff+i*phsize) for i in range(phcount)]
    code=[s for s in segments if s[0]==1 and s[6]&1 and s[2]!=s[3] and s[4]]
    pc,status=0x14ebc2,None
    if args.replacement_elf:
        symbols={p[2]:int(p[0],16) for line in subprocess.check_output(
            [args.nm,'--defined-only',args.replacement_elf],text=True).splitlines()
            if len(p:=line.split())==3}
        pc,status=symbols['ws63_probe_eeq_match'],symbols['ws63_probe_status']
    variants=[
        ('exact-priority',[(0xaa,7),(0xbb,3),(0xaa,3)],2),
        ('label-fallback',[(0xcc,9),(0xbb,3),(0xdd,5)],1),
        ('clock-fallback-full-label',[(0xaa,7),(0xbb,0x103),(0xdd,5)],0),
        ('truncated-exact',[(0xaa,0x103),(0xbb,3),(0xdd,5)],0),
        ('singleton',[(0xaa,3)],0),
        ('miss',[(0xcc,7),(0xbb,0x103),(0xdd,5)],None),
        ('empty',[],None)]
    results=[]
    for urgent in (False,True):
      for remove in (False,True):
       for kind,metadata,selected in variants:
        name=f'{"urgent" if urgent else "normal"}-{"fetch" if remove else "locate"}-{kind}'
        with tempfile.TemporaryDirectory(prefix='eeq-match-') as tmp:
            sock=Path(tmp)/'gdb'
            trace=out/(name+'.jsonl')
            cmd=[str(Path(args.qemu).resolve()),'-M','ws63','-S','-display','none',
                 '-monitor','none','-serial','null','-kernel',str(Path(args.elf).resolve()),
                 '-device',f'loader,file={Path(args.rom).resolve()},addr=0x100000,force-raw=on',
                 '-device',f'ws63-sle-research,trace-file={trace}',
                 '-gdb',f'unix:{sock},server=on,wait=off','-icount','shift=2']
            for s in code:
                path=Path(tmp)/f'code-{s[2]:x}.bin'
                path.write_bytes(elf[s[1]:s[1]+s[4]])
                cmd+=['-device',f'loader,file={path},addr={s[2]:#x},force-raw=on']
            if args.replacement_elf:
                cmd+=['-device',f'loader,file={Path(args.replacement_elf).resolve()}']
            with (out/(name+'.stderr')).open('w') as log:
                proc=subprocess.Popen(cmd,stdout=log,stderr=log,
                                      env=dict(os.environ,WS63_SLE_STRICT_ROM='1'))
                g=None
                try:
                    g=RSP(sock)
                    queue=0x1827ac if urgent else 0x182798
                    other=0x182798 if urgent else 0x1827ac
                    tasks=[0xa50000+i*0x100 for i in range(len(metadata))]
                    irq=0xa50800
                    for q in (queue,other): g.write(q,0,20)
                    for i,(coarse,label) in enumerate(metadata):
                        task,sed,env=tasks[i],0xa51000+i*0x100,0xa52000+i*0x100
                        for a in (task,sed,env): g.write(a,0,64)
                        g.write(task,tasks[i+1] if i+1<len(tasks) else 0)
                        g.write(task+4,sed)
                        g.write(task+8,env)
                        g.write(task+16,0x12345600|coarse)
                        g.write(sed+4,label,2)
                        g.write(env+27,(1,2,0xfe)[i],1)
                    g.write(queue,tasks[0] if tasks else 0)
                    g.write(queue+4,tasks[-1] if tasks else 0)
                    g.write(queue+8,len(tasks))
                    g.write(0x1828c5,0x7f,1)
                    g.write(0x18024c,0)
                    irq_word=0x80000000|(9<<25)|(int(urgent)<<24)|(3<<16)|(0xaa<<8)|0x55
                    g.write(irq,irq_word)
                    g.write(irq+4,0xdeadbeef)
                    start=trace.stat().st_size
                    try:
                        result=g.call(pc,(irq,9,int(remove)))
                    except TimeoutError:
                        g.sock.sendall(b'\x03')
                        stopped=g.receive()
                        raw=bytes.fromhex(g.request('g'))
                        regs=[int.from_bytes(raw[i:i+4],'little') for i in range(0,132,4)]
                        (out/(name+'-timeout.json')).write_text(json.dumps(
                            {'stop':stopped,'registers':[hex(x) for x in regs]},indent=2)+'\n')
                        raise
                    stop=trace.stat().st_size
                    if status is not None: assert g.read(status)==0
                    assert result==(tasks[selected] if selected is not None else 0),(name,hex(result))
                    remaining=[i for i in range(len(tasks)) if not(remove and i==selected)]
                    links=[tasks[i+1] if i+1<len(tasks) else 0 for i in range(len(tasks))]
                    if remove and selected is not None:
                        links[selected]=0
                        if selected: links[selected-1]=tasks[selected+1] if selected+1<len(tasks) else 0
                    state={'head':g.read(queue),'tail':g.read(queue+4),'count':g.read(queue+8),
                           'next':[g.read(t) for t in tasks], 'protocol':g.read(0x1828c5,1),
                           'other_queue_hex':g.read(other,20).to_bytes(20,'little').hex(),
                           'irq_hex':g.read(irq,8).to_bytes(8,'little').hex()}
                    assert state=={'head':tasks[remaining[0]] if remaining else 0,
                        'tail':tasks[remaining[-1]] if remaining else 0,'count':len(remaining),
                        'next':links,'protocol':(1,2,0xfe)[selected] if selected is not None else 0x7f,
                        'other_queue_hex':'00'*20,'irq_hex':struct.pack('<II',irq_word,0xdeadbeef).hex()},(name,state)
                    rows=[json.loads(line) for line in trace.read_bytes()[start:stop].splitlines()]
                    assert rows==[],(name,rows)
                    results.append({'case':name,'pc':hex(pc),'reference_vendor_pc':'0x14ebc2',
                        'implementation':'reconstructed C' if args.replacement_elf else 'vendor linked code',
                        'input':{'urgent':urgent,'remove':remove,'event_code':9,'irq_word':irq_word,
                                 'metadata':metadata,'previous_protocol':0x7f},
                        'return':result,'queue_after':state,'firmware_accesses':rows})
                finally:
                    if g: g.sock.close()
                    proc.terminate()
                    proc.wait(timeout=5)
    report={'claim':'Software EEQ lookup/unlink only, event code 9; RX orphan cleanup and callbacks not executed',
            'rom_sha256':sha(args.rom),'elf_sha256':sha(args.elf),'qemu_sha256':sha(args.qemu),
            'replacement_elf_sha256':sha(args.replacement_elf) if args.replacement_elf else None,
            'cases':results,'synthetic_executable_relocations':[
                {'elf_offset':hex(s[1]),'vma':hex(s[2]),'lma':hex(s[3]),'length':s[4],
                 'sha256':hashlib.sha256(elf[s[1]:s[1]+s[4]]).hexdigest()} for s in code]}
    (out/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'passed':len(results),'out':str(out)}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('qemu','rom','elf','out'): p.add_argument('--'+name,required=True)
    p.add_argument('--replacement-elf')
    p.add_argument('--nm',default='nm')
    run(p.parse_args())
