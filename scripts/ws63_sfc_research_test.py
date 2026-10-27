#!/usr/bin/env python3
"""Check explicit volatile SFC model contracts; not a hardware qualification."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
from ws53_gpio_test import connect


def test(qemu, failure=None):
    with tempfile.TemporaryDirectory(prefix='sfc-qtest-') as tmp:
        qt, qm = Path(tmp)/'qt', Path(tmp)/'qm'
        with (Path(tmp)/'stderr').open('w+') as log:
            proc = subprocess.Popen([qemu, '-M', 'ws63', '-accel', 'qtest',
                '-display', 'none', '-monitor', 'none', '-serial', 'null',
                '-global', 'ws63-sfc.research-data=on', '-qtest-log', '/dev/null',
                '-qtest', f'unix:{qt},server=on,wait=off',
                '-qmp', f'unix:{qm},server=on,wait=off'], stderr=log)
            try:
                with connect(qt) as sock, sock.makefile('rwb', buffering=0) as io:
                    def cmd(text, expect_reply=True):
                        io.write(text.encode()+b'\n')
                        reply=io.readline().decode().split()
                        if not expect_reply:
                            return
                        assert reply and reply[0]=='OK', (text,reply)
                        return reply[1:]
                    def wr(addr, value):
                        cmd(f'writel {addr:#x} {value:#x}')
                    def rd(addr):
                        return int(cmd(f'readl {addr:#x}')[0],0)
                    def op(code, addr=0, length=0, read=False, address=False,
                           expect_reply=True):
                        wr(0x48000308,code)
                        wr(0x4800030c,addr)
                        cfg=3 | (8 if address else 0)
                        if length:
                            cfg|=128 | ((length-1)<<9)
                        if read:
                            cfg|=256
                        cmd(f'writel 0x48000300 {cfg:#x}',expect_reply)
                        if expect_reply:
                            assert rd(0x48000300)==cfg & ~1
                    if failure:
                        if failure=='opcode':
                            op(0xab,expect_reply=False)
                        elif failure=='no-wren':
                            op(2,0x100,1,address=True,expect_reply=False)
                        elif failure=='boundary':
                            op(3,0x7fffff,2,read=True,address=True,expect_reply=False)
                        elif failure=='page-cross':
                            op(6)
                            op(2,0xff,2,address=True,expect_reply=False)
                        proc.wait(timeout=5)
                        log.seek(0)
                        assert proc.returncode != 0 and 'WS63 research SFC:' in log.read()
                        return
                    assert rd(0x200100)==0xffffffff
                    # 64-byte command read must agree with XIP, including last word.
                    for i in range(16):
                        wr(0x200100+4*i,0x12340000+i)
                    op(3,0x100,64,read=True,address=True)
                    for i in range(16):
                        assert rd(0x48000400+4*i)==0x12340000+i
                    # Single byte transfer preserves the rest of buffer word.
                    wr(0x48000400,0xaabbccdd)
                    op(3,0x103,1,read=True,address=True)
                    assert rd(0x48000400)==0xaabbcc12
                    # WEL follows WREN/WRDI/program; NOR cannot program zero to one.
                    op(6)
                    op(5,length=1,read=True)
                    assert rd(0x48000400)&255==2
                    wr(0x48000400,0xf0ccaa55)
                    op(2,0x200,4,address=True)
                    assert rd(0x200200)==0xf0ccaa55
                    op(5,length=1,read=True)
                    assert rd(0x48000400)&255==0
                    op(6)
                    wr(0x48000400,0x0f55ffff)
                    op(2,0x200,4,address=True)
                    assert rd(0x200200)==0x0044aa55
                    op(6)
                    op(4)
                    op(5,length=1,read=True)
                    assert rd(0x48000400)&255==0
                    # Erase only modeled sector; adjacent data stays intact.
                    wr(0x201000,0x87654321)
                    op(6)
                    op(0x20,0x200,address=True)
                    assert rd(0x200200)==0xffffffff
                    assert rd(0x201000)==0x87654321
                    # Reset clears controller state, retaining simulated flash.
                    op(6)
                    with connect(qm) as qs, qs.makefile('rwb',buffering=0) as stream:
                        json.loads(stream.readline())
                        for command in ('qmp_capabilities','system_reset'):
                            stream.write(json.dumps({'execute':command}).encode()+b'\n')
                            while True:
                                result=json.loads(stream.readline())
                                assert 'error' not in result,result
                                if 'return' in result:
                                    break
                    op(5,length=1,read=True)
                    assert rd(0x48000400)&255==0
                    assert rd(0x201000)==0x87654321
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu',required=True)
    args=parser.parse_args()
    for case in (None,'opcode','no-wren','boundary','page-cross'):
        test(args.qemu,case)
    print(json.dumps({'model_contracts_passed':True,'silicon_verified':False,
        'checks':['read64','partial-read','xip-coherence','nor-program','wel',
                  'sector-erase','reset-retains-flash','unsupported-opcode',
                  'no-wren','out-of-range','page-crossing']}))
