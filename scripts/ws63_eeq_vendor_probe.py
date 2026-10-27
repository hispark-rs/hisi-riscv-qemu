#!/usr/bin/env python3
"""Execute EEQ submission on synthetic queues, without inventing consumption."""
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
    # The isolated probe does not execute startup's SRAM relocation. Restore
    # the exact linked executable segment containing memcpy, not a substitute.
    elf=Path(args.elf).read_bytes()
    assert elf[:6]==b'\x7fELF\x01\x01'
    phoff=struct.unpack_from('<I',elf,28)[0]
    phsize,phcount=struct.unpack_from('<HH',elf,42)
    segments=[struct.unpack_from('<8I',elf,phoff+i*phsize) for i in range(phcount)]
    code_segments=[s for s in segments if s[0]==1 and s[6]&1 and s[2]!=s[3] and s[4]]
    assert any(s[2]<=0xa0e4be<s[2]+s[4] for s in code_segments)
    assert any(s[2]<=0x14ce68<s[2]+s[4] for s in code_segments)
    replacement=getattr(args,'replacement_elf',None)
    pc=0x139104
    status_addr=None
    if replacement:
        symbols={parts[2]:int(parts[0],16)
                 for line in subprocess.check_output([args.nm,'--defined-only',replacement],text=True).splitlines()
                 if len(parts:=line.split())==3}
        pc=symbols['ws63_probe_eeq_push']
        status_addr=symbols['ws63_probe_status']
    cases=[('normal-empty',False,0,0,2,5),
           ('normal-append',False,1,1,1,9),
           ('normal-wrap',False,1,3,2,0x9d),
           ('urgent-empty',True,0,0,2,5),
           ('urgent-append',True,1,2,1,9),
           ('urgent-wrap',True,1,3,0xff,0xff)]
    results=[]
    for name,urgent,count,index,protocol,config in cases:
        with tempfile.TemporaryDirectory(prefix='eeq-probe-') as tmp:
            sock=Path(tmp)/'gdb'
            trace=out/(name+'.jsonl')
            cmd=[str(Path(args.qemu).resolve()),'-M','ws63','-S','-display','none',
                 '-monitor','none','-serial','null','-kernel',str(Path(args.elf).resolve()),
                 '-device',f'loader,file={Path(args.rom).resolve()},addr=0x100000,force-raw=on',
                 '-device',f'ws63-sle-research,trace-file={trace}',
                 '-gdb',f'unix:{sock},server=on,wait=off','-icount','shift=2']
            for segment in code_segments:
                code_path=Path(tmp)/f'linked-code-{segment[2]:x}.bin'
                code_path.write_bytes(elf[segment[1]:segment[1]+segment[4]])
                cmd+=['-device',f'loader,file={code_path},addr={segment[2]:#x},force-raw=on']
            if replacement:
                cmd+=['-device',f'loader,file={Path(replacement).resolve()}']
            with (out/(name+'.stderr')).open('w') as log:
                proc=subprocess.Popen(cmd,stdout=log,stderr=log,
                                      env=dict(os.environ,WS63_SLE_STRICT_ROM='1'))
                g=None
                try:
                    g=RSP(sock)
                    for address in (0x14ce68,0xa0e4be):
                        segment=next(s for s in code_segments if s[2]<=address<s[2]+s[4])
                        start_offset=segment[1]+address-segment[2]
                        assert g.read(address,4)==int.from_bytes(elf[start_offset:start_offset+4],'little')
                    task,queue,env,sed,previous=0xa50000,0xa50100,0xa50200,0xa50300,0xa50400
                    depth,offset,sed_base,sed_index=4,0xe00,0x1610,3
                    coarse,fine=0x123456ab,0x1e7
                    for address in (task,queue,env,sed,previous):
                        g.write(address,0,64)
                    g.write(task+4,sed)
                    g.write(task+8,env)
                    g.write(task+16,coarse)
                    g.write(task+28,fine,2)
                    g.write(sed+4,sed_index,2)
                    g.write(env+27,protocol,1)
                    g.write(env+32,config,1)
                    g.write(queue,previous if count else 0)
                    g.write(queue+4,previous if count else 0)
                    g.write(queue+8,count)
                    g.write(queue+12,depth,1)
                    g.write(queue+13,index,1)
                    g.write(queue+16,offset)
                    g.write(0x18027e,sed_base,2)
                    g.write(0x18024c,0)  # No optional logging path in this scenario.
                    g.write(0x49000444,0xa5a5a5a5)
                    before=bytes([0xcc])*(depth*6+8)
                    g.write(0x49010000+offset,int.from_bytes(before,'little'),len(before))
                    start=trace.stat().st_size
                    try:
                        g.call(pc,(task,queue,int(urgent)))
                    except TimeoutError:
                        g.sock.sendall(b'\x03')
                        stopped=g.receive()
                        raw=bytes.fromhex(g.request('g'))
                        registers=[int.from_bytes(raw[i:i+4],'little') for i in range(0,132,4)]
                        failure={'case':name,'stop':stopped,'registers':[hex(x) for x in registers],
                                 'stack':[hex(g.read(registers[2]+4*i)) for i in range(16)]}
                        (out/(name+'-timeout.json')).write_text(json.dumps(failure,indent=2)+'\n')
                        raise
                    stop=trace.stat().st_size
                    if status_addr is not None:
                        assert g.read(status_addr)==0
                    encoded=struct.pack('<HHH',((coarse&255)<<8)|(fine&127),
                                        (sed_base+196*sed_index)&65535,
                                        ((config&15)<<2)|(protocol&3))
                    expected=bytearray(before)
                    expected[index*6:index*6+6]=encoded
                    after=g.read(0x49010000+offset,len(before)).to_bytes(len(before),'little')
                    assert after==expected,(name,after.hex(),expected.hex())
                    state={'head':g.read(queue),'tail':g.read(queue+4),
                           'count':g.read(queue+8),'producer':g.read(queue+13,1),
                           'task_next':g.read(task),'previous_next':g.read(previous)}
                    assert state=={'head':previous if count else task,'tail':task,
                                   'count':count+1,'producer':(index+1)%depth,
                                   'task_next':0,'previous_next':task if count else 0},(name,state)
                    rows=[json.loads(line) for line in trace.read_bytes()[start:stop].splitlines()]
                    assert [r['op'] for r in rows]==['R','W','R','W'],rows
                    assert all(r['address']==0x49000444 and r['size']==4 for r in rows)
                    first=(0xa5a5a5a5 & ~(0x3f if urgent else 0x3f00)) | (1 if urgent else 0x100)
                    final=first | (0x40 if urgent else 0x4000)
                    assert [r['value'] for r in rows]==[0xa5a5a5a5,first,first,final]
                    results.append({'case':name,'pc':hex(pc),'reference_vendor_pc':'0x139104',
                        'implementation':'reconstructed C' if replacement else 'vendor ROM',
                        'input':{'urgent':urgent,'count':count,'index':index,'depth':depth,
                                 'offset':offset,'sed_base':sed_base,'sed_index':sed_index,
                                 'coarse':coarse,'fine':fine,'protocol_bits':protocol,'event_config':config},
                        'encoded_entry_hex':encoded.hex(),'em_after_hex':after.hex(),
                        'queue_after':state,'firmware_accesses':rows,
                        'trace_start_byte':start,'trace_end_byte':stop})
                finally:
                    if g:
                        g.sock.close()
                    proc.terminate()
                    proc.wait(timeout=5)
    report={'claim':'EEQ software submission on synthetic state; no hardware consumer or radio',
            'rom_sha256':sha(args.rom),'elf_sha256':sha(args.elf),'qemu_sha256':sha(args.qemu),
            'replacement_elf_sha256':sha(replacement) if replacement else None,'cases':results,
            'synthetic_executable_relocations':[{'elf_offset':hex(s[1]),'vma':hex(s[2]),
                'lma':hex(s[3]),'length':s[4],
                'sha256':hashlib.sha256(elf[s[1]:s[1]+s[4]]).hexdigest(),
                'purpose':'execute original linked callbacks/memcpy; startup was not run'} for s in code_segments]}
    (out/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'passed':len(results),'out':str(out)}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('qemu','rom','elf','out'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--replacement-elf')
    p.add_argument('--nm',default='nm')
    run(p.parse_args())
