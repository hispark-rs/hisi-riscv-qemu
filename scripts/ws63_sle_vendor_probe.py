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
ELF_SHA = '95920853592b1ee92ce975e311fdd478719d2b1bfbeec7096b884cf70943a4d0'


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
        return self.receive()

    def receive(self):
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
    assert sha(args.elf) == ELF_SHA, 'Unsupported ELF layout/GP/callback revision'
    assert b'WS63 strict ROM: illegal instruction' in Path(args.qemu).read_bytes(), (
        'Apply ws63_sle_strict_rom.py and rebuild before recording evidence')
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    results = []
    replacement = getattr(args, 'replacement_elf', None)
    replacement_symbols = {}
    if replacement:
        for line in subprocess.check_output([args.nm,'--defined-only',replacement],text=True).splitlines():
            fields=line.split()
            if len(fields)==3:
                replacement_symbols[fields[2]]=int(fields[0],16)
        assert 'ws63_probe_status' in replacement_symbols
    replacements={
        0x136576:'ws63_probe_gle_clock',0x1365a8:'ws63_probe_gle_fine',
        0x13fba0:'ws63_probe_gle_queue_init',0x13fc26:'ws63_probe_gle_queue_head',
        0x136516:'ws63_probe_ble_clock',0x136548:'ws63_probe_ble_fine',
        0x13418c:'ws63_probe_ble_queue_init',0x134286:'ws63_probe_ble_queue_head',
        0x139200:'ws63_probe_eeq_allow',0x137c84:'ws63_probe_em_offsets'}
    cases = [('clock', 0x136576), ('fine', 0x1365a8),
             ('queue-init', 0x13fba0), ('em-offsets', 0x137c84),
             ('queue-valid', 0x13fc26), ('queue-skip', 0x13fc26),
             ('queue-wrap', 0x13fc26), ('queue-empty', 0x13fc26),
             ('ble-clock', 0x136516), ('ble-fine', 0x136548),
             ('ble-queue-init', 0x13418c), ('ble-queue-valid', 0x134286),
             ('ble-queue-skip', 0x134286), ('ble-queue-wrap', 0x134286),
             ('ble-queue-empty', 0x134286),
             ('eeq-normal-room', 0x139200), ('eeq-urgent-room', 0x139200),
             ('eeq-normal-underflow', 0x139200), ('eeq-urgent-underflow', 0x139200)]
    for name, pc in cases:
        vendor_pc=pc
        if replacement:
            if pc not in replacements:
                continue
            pc=replacement_symbols[replacements[pc]]
        with tempfile.TemporaryDirectory(prefix='sle-rsp-') as tmp:
            trace = out / (name + '.jsonl')
            sock = Path(tmp) / 'gdb'
            command = [str(Path(args.qemu).resolve()), '-M', 'ws63', '-S',
                       '-display', 'none', '-monitor', 'none', '-serial', 'null',
                       '-kernel', str(Path(args.elf).resolve()),
                       '-device', f'loader,file={Path(args.rom).resolve()},addr=0x100000,force-raw=on',
                       '-device', f'ws63-sle-research,trace-file={trace}' +
                       (',assume-ble-clock=on' if name.startswith('ble-') else ''),
                       '-gdb', f'unix:{sock},server=on,wait=off',
                       '-icount', 'shift=2']
            if replacement:
                command+=['-device',f'loader,file={Path(replacement).resolve()}']
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
                    g.write(0x18025a, 16, 1)
                    g.write(0x18028a, 16, 1)
                    g.write(0x18028b, 8, 1)
                    for addr in (0x490015fc, 0x49001600, 0x490010c0,
                                 0x49000274, 0x49001114, 0x49001118,
                                 0x490010c4, 0x49001130, 0x49000288,
                                 0x49000430, 0x490012e0, 0x490012e4,
                                 0x49000448):
                        g.write(addr, 0xa5a5a5a5)
                    queue_input = None
                    ble = name.startswith('ble-')
                    base_name = name[4:] if ble else name
                    queue_base = 0x49011020 if ble else 0x49011090
                    queue_state = 0x182484 if ble else 0x182800
                    queue_position = 0x490012e4 if ble else 0x49001600
                    call_args = ()
                    eeq_input = None
                    if name.startswith('eeq-'):
                        urgent = 'urgent' in name
                        occupancy = 13 if 'underflow' in name else 10
                        shift = 16 if urgent else 24
                        g.write(0x49000448, 12 << shift)
                        g.write(0x4900044c, occupancy << shift)
                        call_args = (int(urgent),)
                        eeq_input = {'capacity':12, 'occupancy':occupancy,
                                     'urgent':urgent,
                                     'within_known_invariant':occupancy <= 12}
                    if base_name.startswith('queue-') and base_name != 'queue-init':
                        idx = 15 if base_name == 'queue-wrap' else 0
                        hardware_idx = 1 if base_name == 'queue-skip' else 0
                        valid_idx = {'queue-valid':0,'queue-skip':1,
                                     'queue-wrap':0,'queue-empty':None}[base_name]
                        g.write(queue_state, queue_base)
                        g.write(queue_state+4, idx, 1)
                        g.write(queue_state+5, 16, 1)
                        g.write(queue_position, (hardware_idx << 19) | 128)
                        if valid_idx is not None:
                            g.write(queue_base + valid_idx*8, 0x94000000)
                        queue_input = {'consumer':idx,'producer':hardware_idx,
                                       'valid_entry':valid_idx,'event_word':'0x94000000'}
                    start = trace.stat().st_size
                    returned = g.call(pc, call_args)
                    stop = trace.stat().st_size
                    rows = [json.loads(x) for x in trace.read_bytes()[start:stop].splitlines()]
                    result = {'case': name, 'pc': hex(pc), 'return_a0': returned,
                              'reference_vendor_pc':hex(vendor_pc),
                              'implementation':'reconstructed C' if replacement else 'vendor ROM',
                              'synthetic_input_halfwords': {hex(k): hex(v) for k,v in seeds.items()},
                              'synthetic_queue_count': 16, 'synthetic_queue_input': queue_input,
                              'synthetic_mmio_seed': '0xa5a5a5a5',
                              'synthetic_eeq_input': eeq_input,
                              'ble_clock_profile_assumption': ble,
                              'trace_start_byte': start, 'trace_end_byte': stop,
                              'firmware_accesses': rows}
                    if replacement:
                        assert g.read(replacement_symbols['ws63_probe_status'])==0, name
                    if name == 'queue-init':
                        assert g.read(0x182800) == 0x49011090
                        assert g.read(0x182805, 1) == 16
                        assert g.read(0x49001600) == 0xa5a50080
                        assert g.read(0x490015fc) == ((0xa5a5a5a5 | 0x01100000))
                    if name == 'ble-queue-init':
                        assert g.read(queue_state) == queue_base
                        assert g.read(queue_state+4, 1) == 0
                        assert g.read(queue_state+5, 1) == 16
                        assert g.read(0x490012e4) == 0xa5a50080
                        assert g.read(0x490012e0) == (0xa5a5a5a5 | 0x01100000)
                        expected = (0xa5a5a5a5 & ~0x3f3f7f7f) | (12<<24) | (4<<16) | (15<<8) | 7
                        assert g.read(0x49000448) == expected
                        result['shared_queue_config'] = hex(expected)
                        result['synthetic_eeq_depths'] = {'normal':16,'urgent':8}
                    if base_name in ('clock','fine'):
                        capture = 0x490010a8 if ble else 0x49001500
                        fine = 0x490010ac if ble else 0x49001504
                        assert not (g.read(capture) & 0x80000000)
                        expected = g.read(capture if base_name=='clock' else fine)
                        expected &= (0x7ffffff if ble else 0x3fffffff) if base_name=='clock' else (1023 if ble else 127)
                        assert returned == expected
                    if eeq_input is not None:
                        assert returned == min((12-occupancy)&255,3)
                        result['interpretation'] = ('synthetic occupancy exceeds capacity; software unsigned wrap, not proof of reachable hardware overflow' if occupancy>12 else 'vendor software capacity calculation')
                    if name == 'em-offsets':
                        assert g.read(0x490014bc) == 0x1090
                        assert g.read(0x490015c0) == 0x38f4
                        assert g.read(0x49001114) == 0xa5a02340
                        assert g.read(0x49001118) == 0xa5a02380
                    if queue_input is not None:
                        expected_idx = 1 if base_name == 'queue-skip' else 0
                        assert returned == queue_base + expected_idx*8
                        assert g.read(queue_state+4,1) == expected_idx
                        result['consumer_after'] = expected_idx
                        result['returned_slot_valid'] = bool(g.read(returned) & 0x80000000)
                        assert result['returned_slot_valid'] == (base_name != 'queue-empty')
                    results.append(result)
                finally:
                    if g:
                        g.sock.close()
                    proc.terminate()
                    proc.wait(timeout=5)
    report = {'claim': ('reconstructed C' if replacement else 'vendor') + ' function execution with synthetic state; no radio/boot proof',
              'replacement_elf_sha256':sha(replacement) if replacement else None,
              'rom_sha256': sha(args.rom), 'elf_sha256': sha(args.elf),
              'qemu_sha256': sha(args.qemu), 'cases': results}
    (out / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': len(results), 'report': str(out / 'results.json')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('qemu', 'rom', 'elf', 'out'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--replacement-elf')
    parser.add_argument('--nm',default='nm')
    run(parser.parse_args())
