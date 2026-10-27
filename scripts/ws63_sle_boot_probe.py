#!/usr/bin/env python3
"""Bounded real-ROM boot attempt; record PC and diagnostics without success stubs."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from ws63_sle_vendor_probe import RSP, ROM_SHA, sha


def run(args):
    assert sha(args.rom) == ROM_SHA
    assert b'WS63 strict ROM: illegal instruction' in Path(args.qemu).read_bytes()
    out=Path(args.out).resolve()
    out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='sle-boot-') as tmp:
        sock=Path(tmp)/'gdb'
        device=f'ws63-sle-research,trace-file={out / "mmio.jsonl"}'
        if args.boot_systick:
            device+=',assume-boot-systick=on'
        cmd=[str(Path(args.qemu).resolve()), '-M','ws63','-S',
             '-display','none','-monitor','none','-serial','stdio',
             '-kernel',str(Path(args.elf).resolve()),
             '-device',f'loader,file={Path(args.rom).resolve()},addr=0x100000,force-raw=on',
             '-device',device, '-icount','shift=2',
             '-gdb',f'unix:{sock},server=on,wait=off',
             '-d','unimp,guest_errors','-D',str(out/'unimplemented.log')]
        if args.flash_dir:
            for file,addr in [('partition_params.bin',0x200000),('nv.bin',0x5fc000),
                              ('nv_factory.bin',0x20c000)]:
                cmd+=['-device',f'loader,file={Path(args.flash_dir).resolve()/file},addr={addr:#x}']
        with (out/'uart.log').open('w') as log:
            proc=subprocess.Popen(cmd,stdout=log,stderr=log,
                                  env=dict(os.environ,WS63_SLE_STRICT_ROM='1'))
            g=None
            try:
                g=RSP(sock)
                g.sock.sendall(b'$c#63')
                time.sleep(args.seconds)
                g.sock.sendall(b'\x03')
                stop=g.receive()
                regs=bytes.fromhex(g.request('g'))
                snapshots={f'x{i}':hex(int.from_bytes(regs[4*i:4*i+4],'little'))
                           for i in range(32)}
                snapshots['pc']=hex(int.from_bytes(regs[128:132],'little'))
                result={'classification':'bounded boot snapshot, NOT a boot pass',
                        'wall_seconds':args.seconds, 'boot_systick_assumption':args.boot_systick,
                        'rom_sha256':sha(args.rom),'elf_sha256':sha(args.elf),
                        'qemu_sha256':sha(args.qemu),'stop':stop,
                        'registers':snapshots,'command':cmd}
                (out/'snapshot.json').write_text(json.dumps(result,indent=2)+'\n')
                print(json.dumps({'pc':snapshots['pc'],'out':str(out)}))
            finally:
                if g:
                    g.sock.close()
                proc.terminate()
                proc.wait(timeout=5)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('qemu','rom','elf','out'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--flash-dir')
    p.add_argument('--seconds',type=float,default=2)
    p.add_argument('--boot-systick',action='store_true')
    a=p.parse_args()
    assert 0<a.seconds<=30
    run(a)
