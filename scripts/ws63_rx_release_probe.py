#!/usr/bin/env python3
"""Execute real RX descriptor release with explicit synthetic ownership state."""
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
    out=Path(args.out).resolve(); out.mkdir(parents=True,exist_ok=True)
    elf=Path(args.elf).read_bytes()
    phoff=struct.unpack_from('<I',elf,28)[0]
    phsize,phcount=struct.unpack_from('<HH',elf,42)
    segments=[struct.unpack_from('<8I',elf,phoff+i*phsize) for i in range(phcount)]
    code=[s for s in segments if s[0]==1 and s[6]&1 and s[2]!=s[3] and s[4]]
    symbols={}
    if args.replacement_elf:
        symbols={p[2]:int(p[0],16) for line in subprocess.check_output(
            [args.nm,'--defined-only',args.replacement_elf],text=True).splitlines()
            if len(p:=line.split())==3}
    results=[]
    for protocol in ('ble','gle'):
      for buffer_id,ready in [(i,True) for i in (0,15,16,31,32,47,48,63,64,255)]+[(16,False)]:
        name=f'{protocol}-buffer-{buffer_id}-{"ready" if ready else "not-ready"}'
        vendor_pc=0x137fd6 if protocol=='ble' else 0x137b4a
        pc=symbols['ws63_probe_'+protocol+'_rx_release'] if symbols else vendor_pc
        offset=0x2000 if protocol=='ble' else 0x3000
        globals_=(0x180266,0x180264,0x182704) if protocol=='ble' else (0x180298,0x180296,0x182700)
        with tempfile.TemporaryDirectory(prefix='rx-release-') as tmp:
            sock=Path(tmp)/'gdb'; trace=out/(name+'.jsonl')
            cmd=[str(Path(args.qemu).resolve()),'-M','ws63','-S','-display','none',
                 '-monitor','none','-serial','null','-kernel',str(Path(args.elf).resolve()),
                 '-device',f'loader,file={Path(args.rom).resolve()},addr=0x100000,force-raw=on',
                 '-device',f'ws63-sle-research,trace-file={trace}',
                 '-gdb',f'unix:{sock},server=on,wait=off','-icount','shift=2']
            for s in code:
                path=Path(tmp)/f'code-{s[2]:x}.bin'; path.write_bytes(elf[s[1]:s[1]+s[4]])
                cmd+=['-device',f'loader,file={path},addr={s[2]:#x},force-raw=on']
            if symbols: cmd+=['-device',f'loader,file={Path(args.replacement_elf).resolve()}']
            with (out/(name+'.stderr')).open('w') as log:
                proc=subprocess.Popen(cmd,stdout=log,stderr=log,env=dict(os.environ,WS63_SLE_STRICT_ROM='1'))
                g=None
                try:
                    g=RSP(sock)
                    depth,index,current=4,2,3
                    before=bytearray([0xcc]*(depth*72+8))
                    struct.pack_into('<H',before,index*72+2,0x1234|(0x8000 if ready else 0))
                    before[index*72+34]=buffer_id
                    g.write(0x49010000+offset,int.from_bytes(before,'little'),len(before))
                    g.write(globals_[0],offset,2); g.write(globals_[1],depth,1); g.write(globals_[2],current,1)
                    g.write(0x18024c,0)  # Actual logging remains disabled in this fixture.
                    for i in range(4): g.write(0x490004ac+4*i,0xa5a5a5a5)
                    start=trace.stat().st_size
                    g.call(pc,(index,))
                    stop=trace.stat().st_size
                    expected_status=0 if ready and buffer_id<64 else -4
                    if symbols: assert g.read(symbols['ws63_probe_status'])==expected_status&0xffffffff
                    expected=bytearray(before)
                    if ready and (not symbols or buffer_id<64): struct.pack_into('<H',expected,index*72+2,0x1234)
                    after=g.read(0x49010000+offset,len(before)).to_bytes(len(before),'little')
                    assert after==expected,(name,after.hex(),expected.hex())
                    assert g.read(globals_[2],1)==current  # This helper does not advance the ring index.
                    rows=[json.loads(line) for line in trace.read_bytes()[start:stop].splitlines()]
                    if ready and buffer_id<64:
                        assert len(rows)==1
                        row=rows[0]
                        assert (row['op'],row['address'],row['size'],row['value'])==(
                            'W',0x490004ac+4*(buffer_id//16),4,1<<(16+buffer_id%16)),rows
                    else: assert rows==[],rows
                    results.append({'case':name,'pc':hex(pc),'reference_vendor_pc':hex(vendor_pc),
                        'implementation':'reconstructed C' if symbols else 'vendor ROM',
                        'input':{'protocol':protocol,'buffer_id':buffer_id,'ready':ready,'offset':offset,
                                 'depth':depth,'descriptor_index':index,'current_index':current},
                        'em_before_hex':before.hex(),'em_after_hex':after.hex(),'current_index_after':current,
                        'library_status':expected_status if symbols else None,'firmware_accesses':rows,
                        'intentional_difference':bool(symbols and ready and buffer_id>=64)})
                finally:
                    if g: g.sock.close()
                    proc.terminate(); proc.wait(timeout=5)
    report={'claim':'RX descriptor software release on synthetic state; no packet reception, pool ownership or MMIO command-effect model',
            'rom_sha256':sha(args.rom),'elf_sha256':sha(args.elf),'qemu_sha256':sha(args.qemu),
            'replacement_elf_sha256':sha(args.replacement_elf) if symbols else None,'cases':results,
            'synthetic_executable_relocations':[{'elf_offset':hex(s[1]),'vma':hex(s[2]),'lma':hex(s[3]),'length':s[4],
                'sha256':hashlib.sha256(elf[s[1]:s[1]+s[4]]).hexdigest()} for s in code]}
    (out/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'passed':len(results),'out':str(out)}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('qemu','rom','elf','out'): p.add_argument('--'+name,required=True)
    p.add_argument('--replacement-elf'); p.add_argument('--nm',default='nm')
    run(p.parse_args())
