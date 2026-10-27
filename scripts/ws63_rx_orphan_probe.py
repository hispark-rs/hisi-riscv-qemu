#!/usr/bin/env python3
"""Run unmatched RX-event cleanup, preserving protocol-tag and error distinctions."""
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
    out=Path(args.out).resolve();out.mkdir(parents=True,exist_ok=True)
    elf=Path(args.elf).read_bytes();phoff=struct.unpack_from('<I',elf,28)[0]
    phsize,phcount=struct.unpack_from('<HH',elf,42)
    segments=[struct.unpack_from('<8I',elf,phoff+i*phsize) for i in range(phcount)]
    loads=[s for s in segments if s[0]==1 and s[2]!=s[3] and s[4]]
    symbols={}
    if args.replacement_elf:
        symbols={p[2]:int(p[0],16) for line in subprocess.check_output(
            [args.nm,'--defined-only',args.replacement_elf],text=True).splitlines() if len(p:=line.split())==3}
    variants=[(tag,index,ready,31) for tag in (1,2) for index in (0,3) for ready in (False,True)]
    variants += [(tag,3,True,31) for tag in (0,3)] + [(tag,3,True,64) for tag in (1,2)]
    results=[]
    for urgent in (False,True):
      for tag,index,ready,buffer_id in variants:
        name=f'{"urgent" if urgent else "normal"}-tag{tag}-index{index}-ready{int(ready)}-buffer{buffer_id}'
        with tempfile.TemporaryDirectory(prefix='rx-orphan-') as tmp:
            sock=Path(tmp)/'gdb';trace=out/(name+'.jsonl')
            cmd=[str(Path(args.qemu).resolve()),'-M','ws63','-S','-display','none','-monitor','none',
                 '-serial','null','-kernel',str(Path(args.elf).resolve()),
                 '-device',f'loader,file={Path(args.rom).resolve()},addr=0x100000,force-raw=on',
                 '-device',f'ws63-sle-research,trace-file={trace}',
                 '-gdb',f'unix:{sock},server=on,wait=off','-icount','shift=2']
            for s in loads:
                path=Path(tmp)/f'load-{s[2]:x}.bin';path.write_bytes(elf[s[1]:s[1]+s[4]])
                cmd+=['-device',f'loader,file={path},addr={s[2]:#x},force-raw=on']
            if symbols:cmd+=['-device',f'loader,file={Path(args.replacement_elf).resolve()}']
            with (out/(name+'.stderr')).open('w') as log:
                proc=subprocess.Popen(cmd,stdout=log,stderr=log,env=dict(os.environ,WS63_SLE_STRICT_ROM='1'))
                g=None
                try:
                    g=RSP(sock)
                    g.call(0x24a410,(0,)) # Original log_set_local_log_level(LOG_LEVEL_NONE).
                    assert g.read(0x1828f8,1)==0
                    before=bytearray([0xcc]*(4*72+8))
                    struct.pack_into('<H',before,index*72+2,0x1234|(0x8000 if ready else 0))
                    before[index*72+34]=buffer_id
                    for offset,base_global,depth_global,index_global in (
                        (0x2000,0x180266,0x180264,0x182704),(0x3000,0x180298,0x180296,0x182700)):
                        g.write(0x49010000+offset,int.from_bytes(before,'little'),len(before))
                        g.write(base_global,offset,2);g.write(depth_global,4,1);g.write(index_global,index,1)
                    g.write(0x182798,0,40);g.write(0x1828c5,tag,1);g.write(0x18024c,0)
                    irq=0xa50800;word=0x9403aa55|(int(urgent)<<24)
                    g.write(irq,word);g.write(irq+4,0xfeedbeef)
                    for i in range(4):g.write(0x490004ac+4*i,0xa5a5a5a5)
                    pc=symbols['ws63_probe_event_dispatch'] if symbols else 0x139338
                    start=trace.stat().st_size;g.call(pc,(irq,0x11223344));stop=trace.stat().st_size
                    expected_status=1 if tag not in (1,2) else -4 if ready and buffer_id>=64 else 0
                    if symbols:assert g.read(symbols['ws63_probe_status'])==expected_status&0xffffffff
                    reclaimed=tag in (1,2) and ready and (not symbols or buffer_id<64)
                    after={};indices={}
                    for pool_tag,offset,index_global in ((1,0x2000,0x182704),(2,0x3000,0x182700)):
                        data=g.read(0x49010000+offset,len(before)).to_bytes(len(before),'little')
                        expected=bytearray(before)
                        if reclaimed and pool_tag==tag:struct.pack_into('<H',expected,index*72+2,0x1234)
                        assert data==expected,(name,pool_tag,data.hex(),expected.hex())
                        after[str(pool_tag)]=data.hex();indices[str(pool_tag)]=g.read(index_global,1)
                        assert indices[str(pool_tag)]==((index+1)%4 if reclaimed and pool_tag==tag else index)
                    assert g.read(0x1828c5,1)==tag and g.read(0x182798,40)==0
                    assert g.read(irq)==word and g.read(irq+4)==0xfeedbeef
                    rows=[json.loads(line) for line in trace.read_bytes()[start:stop].splitlines()]
                    if tag in (1,2) and ready and buffer_id<64:
                        assert len(rows)==1
                        assert (rows[0]['op'],rows[0]['address'],rows[0]['size'],rows[0]['value'])==('W',0x490004b0,4,0x80000000)
                    else:assert not rows,rows
                    results.append({'case':name,'pc':hex(pc),'reference_vendor_pc':'0x139338',
                        'implementation':'reconstructed event lifecycle' if symbols else 'vendor ROM and linked patch',
                        'input':{'urgent':urgent,'last_protocol':tag,'index':index,'ready':ready,'buffer_id':buffer_id,'word':word},
                        'pool_before_hex':before.hex(),'pools_after':after,'indices_after':indices,
                        'protocol_after':tag,'firmware_accesses':rows,'library_status':expected_status if symbols else None,
                        'intentional_difference':bool(symbols and tag in (1,2) and ready and buffer_id>=64)})
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
    report={'claim':'Unmatched event10 cleanup on synthetic pools; protocol tag selection and cursor advancement, no actual packet reception',
            'rom_sha256':sha(args.rom),'elf_sha256':sha(args.elf),'qemu_sha256':sha(args.qemu),
            'replacement_elf_sha256':sha(args.replacement_elf) if symbols else None,'cases':results,
            'logging_precondition':'original log_set_local_log_level(0), LOG_LEVEL_NONE; log filtering executes normally',
            'synthetic_load_relocations':[{'elf_offset':hex(s[1]),'vma':hex(s[2]),'lma':hex(s[3]),'length':s[4],'elf_flags':s[6],
                'sha256':hashlib.sha256(elf[s[1]:s[1]+s[4]]).hexdigest()} for s in loads]}
    (out/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'passed':len(results),'out':str(out)}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('qemu','rom','elf','out'):p.add_argument('--'+name,required=True)
    p.add_argument('--replacement-elf');p.add_argument('--nm',default='nm')
    run(p.parse_args())
