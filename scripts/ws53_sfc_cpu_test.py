#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""True TCG load/store fault checks for unsupported SFC access; no hardware."""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import time
from hisi_priority_test import connect, csr
from ws53_branch_immediate_test import li, load, store

POISON=0xa5a5a5a5
BASE=0x28000
OUTPUT=0x20050000
VECTOR=0x2f000


def program(assumed):
    # Each operation executes through the CPU memory subsystem, unlike qtest's
    # OK transport response, which does not communicate MemTxResult.
    rows=([(0x53000120,False,4,False),(0x5300012c,True,4,False),
           (0x00400000,False,4,False),(0x007ffffc,True,4,False),
           (0x53000300,False,1,False),(0x53000400,True,4,True),
           (0x53000400,False,4,True)] if assumed else
          [(0x53000300,False,4,False),(0x53000300,True,4,False),
           (0x53000240,False,4,False),(0x53000240,True,4,False)])
    code=li(5,VECTOR)+[csr(0x305,5)]+li(9,0)
    expected=[]
    for i,(addr,write,width,accepted) in enumerate(rows):
        code+=li(8,OUTPUT+16+24*i)+li(5,addr)+li(6,POISON)
        word=store(6,5,0) if write else (load(6,5,0) if width==4 else (5<<15)|(6<<7)|3)
        pc=BASE+len(code)*4
        code += [word,store(6,8,0),store(9,8,16)]
        expected.append(dict(address=addr,write=write,width=width,accepted=accepted,pc=pc))
    done=OUTPUT+16+24*len(rows)+16
    code+=li(8,done)+li(6,1)+[store(6,8,0),0x6f]
    handler=[csr(0x342,0,2,10),store(10,8,4),csr(0x341,0,2,10),store(10,8,8),
             csr(0x343,0,2,11),store(11,8,12),0x00450513,csr(0x341,10),0x00148493,0x30200073]
    pack=lambda a:struct.pack('<'+'I'*len(a),*a)
    return expected,done,[(BASE,pack(code)),(VECTOR,pack(handler)),
                         (OUTPUT,pack([POISON]*((done-OUTPUT)//4))+bytes(4))]


@contextmanager
def session(qemu,assumed):
    with tempfile.TemporaryDirectory(prefix='ws53-sfc-cpu-') as tmp:
        d=Path(tmp);qt,qm=d/'qt',d/'qm'
        args=[str(qemu),'-M','ws53','-accel','tcg','-S','-display','none','-monitor','none',
              '-serial','null','-nic','none','-device',f'loader,addr={BASE:#x},cpu-num=0',
              '-qtest',f'unix:{qt},server=on,wait=off','-qtest-log','/dev/null',
              '-qmp',f'unix:{qm},server=on,wait=off','-d','guest_errors']
        if assumed:args+=['-global','ws53-sfc-command.assume-profile=on']
        diagnostics=[]
        with (d/'stderr').open('w+b') as err:
            p=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=err)
            try:
                with connect(qt,p) as bus,bus.makefile('rwb',buffering=0) as io,\
                     connect(qm,p) as monitor,monitor.makefile('rwb',buffering=0) as qmp:
                    assert 'QMP' in json.loads(qmp.readline())
                    def execute(name,arguments=None):
                        request={'execute':name}
                        if arguments is not None:request['arguments']=arguments
                        qmp.write(json.dumps(request).encode()+b'\n')
                        while True:
                            r=json.loads(qmp.readline());assert 'error' not in r,r
                            if 'return' in r:return r['return']
                    def command(s):
                        io.write(s.encode()+b'\n');r=io.readline().split();assert r[0]==b'OK',r;return r[1:]
                    execute('qmp_capabilities')
                    try:yield execute,command,diagnostics
                    finally:
                        if p.poll() is None:execute('quit')
                assert p.wait(timeout=5)==0
            finally:
                if p.poll() is None:
                    p.terminate()
                    try:p.wait(timeout=5)
                    except subprocess.TimeoutExpired:p.kill();p.wait()
                err.seek(0);diagnostics.extend(err.read().decode().splitlines())


def cpu_test(qemu):
    qemu=Path(qemu).resolve();results=[]
    for assumed in (False,True):
        rows,done,regions=program(assumed);rounds=[]
        with session(qemu,assumed) as (execute,bus,diagnostics):
            for reset_round in range(2):
                execute('system_reset')
                for address,raw in regions:bus(f'write {address:#x} {len(raw)} 0x{raw.hex()}')
                execute('cont');deadline=time.monotonic()+5
                while int(bus(f'readl {done:#x}')[0],0)!=1:
                    assert time.monotonic()<deadline,'TCG deadline'
                    time.sleep(.005)
                execute('stop')
                raw=bytes.fromhex(bus(f'read {OUTPUT:#x} {done-OUTPUT}')[0].decode().removeprefix('0x'))
                words=struct.unpack('<'+'I'*(len(raw)//4),raw);assert words[:4]==words[-4:]==(POISON,)*4
                traps=0
                for i,row in enumerate(rows):
                    if not row['accepted']:traps+=1
                    trap=(POISON,)*3 if row['accepted'] else (7 if row['write'] else 5,row['pc'],row['address'])
                    assert words[4+6*i:10+6*i]==(POISON,*trap,traps,POISON),(row,words[4+6*i:10+6*i])
                rejected=execute('qom-get',{'path':'/machine/sfc-command','property':'rejections'})
                assert rejected==traps*(reset_round+1)
                rounds.append(dict(reset_round=reset_round,operations=rows,expected_traps=traps,guards_intact=True))
        notices=[x for x in diagnostics if x.startswith('ws53-sfc-command: assumed ')]
        rejects=[x for x in diagnostics if x.startswith('ws53-sfc-command: rejected ')]
        assert len(notices)==int(assumed)
        assert len(rejects)==2*sum(not r['accepted'] for r in rows)
        assert len(diagnostics)==len(notices)+len(rejects),diagnostics
        results.append(dict(assume_profile=assumed,rounds=rounds,diagnostics=diagnostics))
    sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
    here=Path(__file__).resolve().parent
    return dict(passed=True,qemu_sha256=sha(qemu),source_sha256={n:sha(here/n) for n in
        ('ws53_sfc_cpu_test.py','hisi_priority_test.py','ws53_branch_immediate_test.py')},
        profiles=results,tcg_operations=22,expected_access_faults=18,
        hardware_accessed=False,physical_drain_verified=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--qemu',required=True)
    print(json.dumps(cpu_test(parser.parse_args().qemu),indent=2))
