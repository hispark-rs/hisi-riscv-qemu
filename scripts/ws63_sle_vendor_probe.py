#!/usr/bin/env python3
"""Execute captured vendor code under QEMU, with explicit synthetic inputs.

No firmware is distributed here. This is function execution, not a boot or RF
qualification. Only the exact ROM revision below is supported. All GDB writes
are harness interventions; the trace slice starts after them.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

ROM_SHA = '22d92fbba1a7a7333a6fc4a8e1f38b791c06ec3b495e5dc8734bfb44575f39db'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RSP:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(5)
        end = time.monotonic() + 5
        while True:
            try:
                self.sock.connect(str(path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= end:
                    raise
                time.sleep(.02)

    def request(self, text):
        payload = text.encode()
        self.sock.sendall(b'$' + payload + b'#' + f'{sum(payload) & 255:02x}'.encode())
        while True:
            ch = self.sock.recv(1)
            if ch == b'$':
                break
            if not ch:
                raise EOFError('QEMU closed GDB connection')
        data = b''
        while True:
            c = self.sock.recv(1)
            if c == b'#':
                break
            if not c:
                raise EOFError('QEMU closed GDB connection')
            data += c
        check = b''
        while len(check) < 2:
            check += self.sock.recv(2 - len(check))
        assert int(check, 16) == sum(data) & 255
        self.sock.sendall(b'+')
        return data.decode()

    def write(self, addr, value, size=4):
        data = value.to_bytes(size, 'little').hex()
        assert self.request(f'M{addr:x},{size:x}:{data}') == 'OK'

    def read(self, addr, size=4):
        value = self.request(f'm{addr:x},{size:x}')
        assert not value.startswith('E'), value
        return int.from_bytes(bytes.fromhex(value), 'little')

    def call(self, pc, args=()):
        regs = bytearray.fromhex(self.request('g'))
        for idx, val in {1: 0xa7ff00, 2: 0xa7f000, 3: 0xa12938,
                         32: pc, **{10+i: v for i, v in enumerate(args)}}.items():
            regs[idx*4:idx*4+4] = val.to_bytes(4, 'little')
        assert self.request('G' + regs.hex()) == 'OK'
        assert self.request('Z0,a7ff00,2') == 'OK'
        stop = self.request('c')
        regs = bytes.fromhex(self.request('g'))
        actual_pc = int.from_bytes(regs[128:132], 'little')
        assert actual_pc == 0xa7ff00, (stop, hex(actual_pc))
        return int.from_bytes(regs[40:44], 'little')


def run(args):
    assert sha(args.rom) == ROM_SHA, 'Unsupported ROM revision'
    assert b'WS63 strict ROM: illegal instruction' in Path(args.qemu).read_bytes(), (
        'Apply ws63_sle_strict_rom.py and rebuild before recording evidence')
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    results = []
    cases = [('clock', 0x136576), ('fine', 0x1365a8),
             ('queue-init', 0x13fba0), ('em-offsets', 0x137c84)]
    for name, pc in cases:
        with tempfile.TemporaryDirectory(prefix='sle-rsp-') as tmp:
            trace = out / (name + '.jsonl')
            sock = Path(tmp) / 'gdb'
            command = [str(Path(args.qemu).resolve()), '-M', 'ws63', '-S',
                       '-display', 'none', '-monitor', 'none', '-serial', 'null',
                       '-kernel', str(Path(args.elf).resolve()),
                       '-device', f'loader,file={Path(args.rom).resolve()},addr=0x100000,force-raw=on',
                       '-device', f'ws63-sle-research,trace-file={trace}',
                       '-gdb', f'unix:{sock},server=on,wait=off',
                       '-icount', 'shift=2']
            with (out / (name + '.stderr')).open('w') as err:
                env = dict(os.environ, WS63_SLE_STRICT_ROM='1')
                proc = subprocess.Popen(command, stdout=err, stderr=err, env=env)
                g = None
                try:
                    g = RSP(sock)
                    # Synthetic layout: each offset has a distinctive value.
                    seeds = {0x182702: 0xe00, 0x18028c: 0xe60,
                             0x18028e: 0x1110, 0x18025e: 0x1020,
                             0x180294: 0x1090, 0x180254: 0x1234,
                             0x180258: 0x2340, 0x180256: 0x2380,
                             0x180262: 0x2400, 0x180266: 0x3000,
                             0x180298: 0x38f4, 0x180278: 0x3b3c}
                    for addr, val in seeds.items():
                        g.write(addr, val, 2)
                    g.write(0x180290, 16, 1)
                    for addr in (0x490015fc, 0x49001600, 0x490010c0,
                                 0x49000274, 0x49001114, 0x49001118,
                                 0x490010c4, 0x49001130, 0x49000288,
                                 0x49000430):
                        g.write(addr, 0xa5a5a5a5)
                    start = trace.stat().st_size
                    returned = g.call(pc)
                    stop = trace.stat().st_size
                    rows = [json.loads(x) for x in trace.read_bytes()[start:stop].splitlines()]
                    result = {'case': name, 'pc': hex(pc), 'return_a0': returned,
                              'synthetic_input_halfwords': {hex(k): hex(v) for k,v in seeds.items()},
                              'synthetic_queue_count': 16,
                              'synthetic_mmio_seed': '0xa5a5a5a5',
                              'trace_start_byte': start, 'trace_end_byte': stop,
                              'firmware_accesses': rows}
                    if name == 'queue-init':
                        assert g.read(0x182800) == 0x49011090
                        assert g.read(0x182805, 1) == 16
                        assert g.read(0x49001600) == 0xa5a50080
                        assert g.read(0x490015fc) == ((0xa5a5a5a5 | 0x01100000))
                    if name == 'em-offsets':
                        assert g.read(0x490014bc) == 0x1090
                        assert g.read(0x490015c0) == 0x38f4
                        assert g.read(0x49001114) == 0xa5a02340
                        assert g.read(0x49001118) == 0xa5a02380
                    results.append(result)
                finally:
                    if g:
                        g.sock.close()
                    proc.terminate()
                    proc.wait(timeout=5)
    report = {'claim': 'vendor function execution with synthetic state; no radio/boot proof',
              'rom_sha256': sha(args.rom), 'elf_sha256': sha(args.elf),
              'qemu_sha256': sha(args.qemu), 'cases': results}
    (out / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': len(results), 'report': str(out / 'results.json')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('qemu', 'rom', 'elf', 'out'):
        parser.add_argument('--' + name, required=True)
    run(parser.parse_args())
