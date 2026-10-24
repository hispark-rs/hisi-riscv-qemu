#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Run first-party CSR/interrupt code on six HiSilicon TCG machines.

No SDK, external ELF, peripherals or hardware transport is used. qtest supplies
two local input levels; real guest CSR instructions, traps and mret must deliver
the required order. This is shared model regression, not silicon evidence.
"""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time

from ws53_branch_immediate_test import branch, li, store

MACHINES = {'ws63': 0xa00000, 'bs20': 0x100000, 'bs21': 0x100000,
            'bs21e': 0x100000, 'bs22': 0x100000, 'ws53': 0x28000}
SCENARIOS = ((3, 1), (1, 3), (3, 3))
POISON = 0xa5a5a5a5


def csr(number, source, funct=1, dest=0):
    return number << 20 | source << 15 | funct << 12 | dest << 7 | 0x73


def payload(base, priorities):
    output, vector = base + 0x2000, base + 0x1000
    code = li(8, output) + li(9, 0) + li(5, vector) + [csr(0x305, 5)]
    for index in range(16):
        code += [csr(0xbc0 + index, 0, 2, 6), store(6, 8, index * 4)]
    code += [csr(0xbfe, 0, 2, 6), store(6, 8, 64)]
    # Adjacent IRQ32/33 use two nibbles in LOCIPRI0 (SDK irq - 26).
    # Real CSR read/modify/write must preserve all six untouched neighbors.
    for index, (irq, priority) in enumerate(zip((32, 33), priorities)):
        shift = (irq - 26) * 4
        code += li(5, 15 << shift) + [csr(0xbc0, 5, 3)]
        code += li(5, priority << shift) + [csr(0xbc0, 5, 2)]
        code += [csr(0xbc0, 0, 2, 6), store(6, 8, 68 + index * 4)]
    code += li(5, 3) + [csr(0xbe0, 5)]
    code += li(5, max(priorities)) + [csr(0xbfe, 5), csr(0x300, 8, 6)]
    code += [0x13] * 64  # equal priority must remain masked, even with MIE=1.
    code += [store(9, 8, 76)]
    # Handler writes cause values at output+128 and increments x9.
    code += li(8, output + 128)
    code += li(5, 1) + [csr(0xbfe, 5)]
    if priorities[0] != priorities[1]:
        code += li(7, 1) + [branch(9, 7, 1, 0)]
        code += [csr(0xbfe, 0)]  # now admit the lower-priority source.
    code += li(7, 2) + [branch(9, 7, 1, 0)]
    code += [csr(0x300, 8, 7)] + li(8, output)
    code += [store(9, 8, 80), csr(0xbe0, 0, 2, 6), store(6, 8, 84)]
    # Leave different state for the next explicit system_reset to clear.
    code += li(5, 7) + [csr(0xbfe, 5)]
    code += li(6, 1) + [store(6, 8, 200), 0x6f]
    handler = [csr(0x342, 0, 2, 10), store(10, 8, 0),
               0x00440413, 0x00148493,  # addi x8,x8,4; addi x9,x9,1
               0x01f57593, 0x00100613, 0x00b61633,  # mask = 1 << (cause & 31)
               csr(0xbe0, 12, 3), csr(0xbf0, 10), 0x30200073]
    packed = lambda words: struct.pack('<' + 'I' * len(words), *words)
    assert len(code) * 4 < 0x1000
    return output, ((base, packed(code)), (vector, packed(handler)))


def connect(path, process):
    deadline = time.monotonic() + 5
    while True:
        connection = socket.socket(socket.AF_UNIX)
        connection.settimeout(5)
        try:
            connection.connect(str(path))
            return connection
        except (FileNotFoundError, ConnectionRefusedError):
            connection.close()
            if process.poll() is not None or time.monotonic() >= deadline:
                raise
            time.sleep(.01)


def run_machine(qemu, machine, base):
    cases = []
    with tempfile.TemporaryDirectory(prefix='hisi-irq-') as temporary:
        directory = Path(temporary)
        qt, qm = directory / 'qt', directory / 'qm'
        command = [str(qemu), '-M', machine, '-accel', 'tcg', '-S',
            '-display', 'none', '-monitor', 'none', '-serial', 'null', '-nic', 'none',
            '-device', f'loader,addr={base:#x},cpu-num=0',
            '-qtest', f'unix:{qt},server=on,wait=off', '-qtest-log', '/dev/null',
            '-qmp', f'unix:{qm},server=on,wait=off', '-d', 'guest_errors']
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with connect(qt, process) as bus, bus.makefile('rwb', buffering=0) as io, \
                    connect(qm, process) as control, control.makefile('rwb', buffering=0) as monitor:
                assert 'QMP' in json.loads(monitor.readline())
                def execute(name, arguments=None):
                    request = dict(execute=name)
                    if arguments is not None:
                        request['arguments'] = arguments
                    monitor.write(json.dumps(request).encode() + b'\n')
                    while True:
                        raw = monitor.readline()
                        assert raw, 'QMP disconnected'
                        reply = json.loads(raw)
                        assert 'error' not in reply, reply
                        if 'return' in reply:
                            return reply['return']
                def bus_command(value):
                    io.write(value.encode() + b'\n')
                    reply = io.readline().split()
                    assert reply and reply[0] == b'OK', reply
                    return reply[1:]
                def read(address):
                    return int(bus_command(f'readl {address:#x}')[0], 0)
                execute('qmp_capabilities')
                paths = []
                for parent in ('/machine', '/machine/unattached'):
                    paths += [parent + '/' + row['name'] for row in execute('qom-list', {'path': parent})
                              if row['type'] == 'child<ws63-intc>']
                assert len(paths) == 1, paths
                intc = paths[0]
                for reset_round in range(2):
                    for priorities in SCENARIOS:
                        for irq in (32, 33):
                            bus_command(f'set_irq_in {intc} unnamed-gpio-in {irq} 0')
                        execute('system_reset')
                        assert not execute('query-status')['running']
                        output, regions = payload(base, priorities)
                        for address, raw in (*regions, (output, struct.pack('<64I', *([POISON] * 64)))):
                            bus_command(f'write {address:#x} {len(raw)} 0x{raw.hex()}')
                        for irq in (32, 33):
                            bus_command(f'set_irq_in {intc} unnamed-gpio-in {irq} 1')
                        execute('cont')
                        deadline = time.monotonic() + 5
                        while read(output + 200) != 1:
                            assert time.monotonic() < deadline, (machine, priorities, 'TCG deadline')
                            time.sleep(.005)
                        execute('stop')
                        assert not execute('query-status')['running']
                        words = [read(output + n * 4) for n in range(64)]
                        expected0 = (0x11111111 & ~(15 << 24)) | priorities[0] << 24
                        expected1 = (expected0 & ~(15 << 28)) | priorities[1] << 28
                        order = sorted((32, 33), key=lambda irq: (-priorities[irq - 32], irq))
                        assert words[:16] == [0x11111111] * 16, words[:16]
                        assert words[16:22] == [0, expected0, expected1, 0, 2, 0], words[16:22]
                        assert words[32:34] == [0x80000000 | irq for irq in order], words[32:34]
                        assert words[22:32] == [POISON] * 10
                        assert words[34:50] == [POISON] * 16 and words[51:] == [POISON] * 13
                        cases.append(dict(reset_round=reset_round, priorities=list(priorities),
                            delivery_order=order, reset_locipri=words[:16], reset_prithd=words[16],
                            rmw_words=words[17:19], equal_threshold_deliveries=words[19],
                            delivered=words[20], final_enabled=words[21], guards_intact=True))
                execute('quit')
            _, errors = process.communicate(timeout=5)
            # This machine always creates its netmac. -nic none deliberately
            # leaves it without a network peer; no other diagnostic is allowed.
            expected = b'qemu-system-riscv32: warning: nic ws63-netmac.0 has no peer\n' if machine == 'ws63' else b''
            assert process.returncode == 0 and errors == expected, errors.decode()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
    return dict(machine=machine, cases=cases, tcg_execution=True, guest_errors=0,
                expected_network_disabled_notice=(machine == 'ws63'))


def run(qemu, machines):
    assert machines and len(machines) == len(set(machines)), 'duplicate or empty machine selection'
    return dict(passed=True, hardware_accessed=False, vendor_firmware_used=False,
        qemu_sha256=hashlib.sha256(qemu.read_bytes()).hexdigest(),
        source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        instruction_helper_sha256=hashlib.sha256(Path(__file__).with_name('ws53_branch_immediate_test.py').read_bytes()).hexdigest(),
        machines=[run_machine(qemu, machine, MACHINES[machine]) for machine in machines],
        case_count=len(machines) * len(SCENARIOS) * 2,
        scope='TCG CSR read/RMW, priority/strict threshold/ties, traps/mret and system-reset regression; no physical timing or silicon claim')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--machines', nargs='+', choices=list(MACHINES), default=list(MACHINES),
                        help='explicit machine set; WS53 is only built on the v10.0.0 overlay')
    args = parser.parse_args()
    print(json.dumps(run(args.qemu.resolve(), args.machines), indent=2))
