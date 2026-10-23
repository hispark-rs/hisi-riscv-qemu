#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Execute xlinx branch-immediate boundary cases on the WS53 TCG CPU.

Vendor assembler encodes bgeui a1,129,+16 as 0x8105f43b, while
bgei a1,-127,+16 is 0x8105d43b. Unsigned comparators zero-extend imm8;
signed/equality comparators sign-extend it. No SDK or hardware needed.
"""
import argparse
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time


def signed(x): return x if x < 0x80000000 else x-0x100000000

def li(rd, value):
    upper = (value+0x800) & 0xfffff000
    return [upper | (rd<<7) | 0x37, ((value & 0xfff)<<20) | (rd<<15) | (rd<<7) | 0x13]

def load(rd, rs, off): return (off<<20) | (rs<<15) | (2<<12) | (rd<<7) | 3

def store(rs, base, off): return ((off>>5)<<25) | (rs<<20) | (base<<15) | (2<<12) | ((off&31)<<7) | 0x23

def branch(rs1, rs2, f3, offset):
    return (((offset>>12)&1)<<31) | (((offset>>5)&63)<<25) | (rs2<<20) | (rs1<<15) | (f3<<12) | (((offset>>1)&15)<<8) | (((offset>>11)&1)<<7) | 0x63

def connect(path, proc):
    deadline = time.monotonic()+5
    while True:
        s = socket.socket(socket.AF_UNIX); s.settimeout(5)
        try: s.connect(str(path)); return s
        except (FileNotFoundError, ConnectionRefusedError):
            s.close()
            if proc.poll() is not None or time.monotonic()>deadline: raise
            time.sleep(.01)


def run(qemu):
    code_base, functions_base, table_base, done = 0x28000, 0x20000, 0x20010000, 0x2005fff0
    functions, cases, table = [], [], bytearray()
    for f3 in (0,1,4,5,6,7):
        for imm in range(256):
            constant = imm if f3 in (6,7) or imm < 128 else imm-256
            values = {0,127,128,255,256,0x7fffffff,0x80000000,0xffffffff}
            values.update((constant+d)&0xffffffff for d in (-1,0,1))
            ptr = functions_base+4*len(functions)
            # Branch forward12 to li a0,1; otherwise li a0,0 and return.
            functions += [(imm<<24) | (11<<15) | (f3<<12) | (3<<8) | 0x3b,
                          0x00000513, 0x00008067, 0x00100513, 0x00008067]
            for value in sorted(values):
                lhs = value if f3 in (6,7) else signed(value)
                expected = {0:lhs==constant, 1:lhs!=constant, 4:lhs<constant,
                            5:lhs>=constant, 6:lhs<constant, 7:lhs>=constant}[f3]
                cases.append((f3,imm,value,int(expected)))
                table.extend(struct.pack('<III',value,ptr,0xdeadbeef))
    assert table_base+len(table)<done and functions_base+4*len(functions)<0x30000
    code = li(8,table_base)+li(9,table_base+len(table))
    loop = len(code)*4
    code += [load(11,8,0),load(5,8,4),0x000280e7,store(10,8,8),0x00c40413]
    code += [branch(8,9,6,loop-len(code)*4)]
    code += li(5,done)+[0x00100313,store(6,5,0),0x0000006f]
    image=bytearray(struct.pack('<'+'I'*len(code),*code))
    function_image=struct.pack('<'+'I'*len(functions),*functions)
    result=execute_ram(qemu, ((code_base,image),(functions_base,function_image),(table_base,table)),
                       table_base,len(table),done)
    failures=[]
    for i,(f3,imm,value,expected) in enumerate(cases):
        original_value,ptr,actual=struct.unpack_from('<III',result,i*12)
        assert original_value==value and ptr==struct.unpack_from('<I',table,i*12+4)[0], 'input corruption'
        if actual!=expected: failures.append({'f3':f3,'imm8':imm,'value':hex(value),'expected':expected,'actual':actual})
    assert not failures, json.dumps({'failed':len(failures),'examples':failures[:8]})
    return {'cases':len(cases),'immediates_per_comparator':256,'comparators':6,'tcg_execution':True,'hardware_accessed':False}


def execute_ram(qemu, regions, result_base, result_size, done):
    """Run RAM-only TCG code at the WS53 reset PC and read a guarded result."""
    with tempfile.TemporaryDirectory(prefix='ws53-branch-') as tmp:
        tmp=Path(tmp)
        proc=subprocess.Popen([qemu,'-M','ws53','-S','-display','none','-monitor','none','-serial','null',
            '-qtest',f'unix:{tmp}/qt,server=on,wait=off','-qtest-log','/dev/null',
            '-qmp',f'unix:{tmp}/qm,server=on,wait=off','-d','guest_errors'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            with connect(tmp/'qt',proc) as qs, qs.makefile('rwb',buffering=0) as qt, connect(tmp/'qm',proc) as ms, ms.makefile('rwb',buffering=0) as qm:
                json.loads(qm.readline())
                def execute(name):
                    qm.write(json.dumps({'execute':name}).encode()+b'\n')
                    while True:
                        r=json.loads(qm.readline()); assert 'error' not in r,r
                        if 'return' in r: return r['return']
                def cmd(s):
                    qt.write(s.encode()+b'\n'); r=qt.readline().split(); assert r[0]==b'OK',r; return r[1:]
                execute('qmp_capabilities')
                try:
                    for base, payload in regions:
                        for off in range(0,len(payload),4096):
                            chunk=payload[off:off+4096]
                            cmd(f'write {base+off:#x} {len(chunk)} 0x{chunk.hex()}')
                    execute('cont'); deadline=time.monotonic()+10
                    while int(cmd(f'readl {done:#x}')[0],0)!=1:
                        assert time.monotonic()<deadline,'guest did not complete'
                        time.sleep(.01)
                    execute('stop')
                    result=bytearray()
                    for off in range(0,result_size,4096):
                        n=min(4096,result_size-off)
                        result.extend(bytes.fromhex(cmd(f'read {result_base+off:#x} {n}')[0].decode().removeprefix('0x')))
                finally: execute('quit')
            _,errors=proc.communicate(timeout=5)
            assert proc.returncode==0 and not errors,errors.decode()
        except BaseException:
            if proc.poll() is None: proc.terminate()
            _, errors = proc.communicate(timeout=5)
            if errors: print(errors.decode(), file=sys.stderr)
            raise
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--qemu',required=True)
    print(json.dumps(run(p.parse_args().qemu),indent=2))
