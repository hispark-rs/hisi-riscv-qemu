#!/usr/bin/env python3
"""Test the declared research-model contracts, not unknown silicon behavior."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
from ws53_gpio_test import connect


def test(qemu):
    with tempfile.TemporaryDirectory(prefix='sle-qtest-') as tmp:
        qt, qm = Path(tmp)/'qt', Path(tmp)/'qm'
        log = (Path(tmp)/'stderr').open('w+')
        proc = subprocess.Popen([qemu, '-M', 'ws63', '-accel', 'qtest',
            '-display', 'none', '-monitor', 'none', '-serial', 'null',
            '-device', 'ws63-sle-research', '-qtest-log', '/dev/null',
            '-qtest', f'unix:{qt},server=on,wait=off',
            '-qmp', f'unix:{qm},server=on,wait=off'], stderr=log)
        try:
            with connect(qt) as sock, sock.makefile('rwb', buffering=0) as io:
                irq = {}
                def cmd(text):
                    io.write(text.encode()+b'\n')
                    while True:
                        parts = io.readline().decode().split()
                        if parts and parts[0] == 'IRQ':
                            irq[int(parts[2])] = parts[1] == 'raise'
                            continue
                        assert parts and parts[0] == 'OK', (text, parts)
                        return parts[1:]
                def qmp(op):
                    with connect(qm) as qs, qs.makefile('rwb', buffering=0) as stream:
                        json.loads(stream.readline())
                        for name in ('qmp_capabilities', op):
                            stream.write(json.dumps({'execute':name}).encode()+b'\n')
                            while True:
                                reply=json.loads(stream.readline())
                                assert 'error' not in reply, reply
                                if 'return' in reply:
                                    break
                def read(off):
                    return int(cmd(f'readl {0x49000000+off:#x}')[0],0)
                def write(off, value):
                    cmd(f'writel {0x49000000+off:#x} {value:#x}')
                cmd('irq_intercept_in /machine/intc')
                # Pending while masked; unmask delivers; unrelated ACK retains.
                write(0x14fc, 2)
                cmd('clock_step 249999')
                assert read(0x1414) == 0
                cmd('clock_step 1')
                assert read(0x1414) == 32 and not irq.get(47,False)
                write(0x1410,32)
                assert irq.get(47,False)
                write(0x141c,16)
                assert read(0x1414) == 32 and irq[47]
                write(0x141c,32)
                assert read(0x1414) == 0 and not irq[47]
                # Capture retains bit30; busy clears; value changes with time.
                write(0x1500,0xc0000000)
                assert read(0x1500) == 0x40000002
                assert read(0x1504) == 124
                cmd('clock_step 37000')
                write(0x1500,0x80000000)
                assert read(0x1500) == 2 and read(0x1504) == 87
                # Real byte-addressed EM RAM, including first/last bytes.
                cmd('writel 0x49010000 0x12345678')
                cmd('writeb 0x49010001 0xab')
                assert int(cmd('readl 0x49010000')[0],0) == 0x1234ab78
                cmd('writeb 0x4901ffff 0xcd')
                assert int(cmd('readb 0x4901ffff')[0],0) == 0xcd
                # Reset must cancel scheduled work and clear only model state.
                write(0x1410,32)
                write(0x14fc,4)
                qmp('system_reset')
                cmd('clock_step 1000000')
                assert read(0x1414) == read(0x1410) == 0
                assert not irq.get(47,False)
                assert int(cmd('readl 0x49010000')[0],0) == 0
                qmp('quit')
            proc.wait(timeout=5)
            assert proc.returncode == 0
            print(json.dumps({'passed':['masked-target','unmask-irq47',
                'selective-ack','capture','fine-phase-model','em-widths',
                'em-boundaries','reset-cancels-target'], 'silicon_verified':False}))
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            log.seek(0)
            diagnostics=log.read()
            log.close()
            if proc.returncode:
                print(diagnostics)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu',required=True)
    test(parser.parse_args().qemu)
