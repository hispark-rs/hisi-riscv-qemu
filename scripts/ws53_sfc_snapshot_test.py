#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Explicit synthetic SFC configured-snapshot contract and real TCG checks.

No hardware, physical clocks, cache/drain or ownership qualification. Three raw
startup values are test inputs, not measured reset values. CPU read scenarios
prove the command engine's byte effects without any mutating NOR command.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import time
from hisi_priority_test import csr
from ws53_branch_immediate_test import li, load, store, branch
from ws53_sfc_contract_test import Peer, connect, require, sha, config, PROPERTIES

DEVICE = '/machine/sfc-command'
BASE, OUTPUT, VECTOR = 0x28000, 0x20050000, 0x2f000
CLOCK, GLOBAL, DMA = 0x52000570, 0x53000100, 0x53000240
POISON = 0xa5a5a5a5
GOOD = dict(global_value=0xa5a503fa, dma=0x5a5aaab2, clock=0x1234abcd)


def flags(values=None, *, assume=True, enabled=True):
    result = ['-global', 'ws53-sfc-command.assume-profile=on'] if assume else []
    if enabled:
        result += ['-global', 'ws53-sfc-command.configured-handoff=on']
    if values is not None:
        for key, name in (('global_value', 'global'), ('dma', 'dma'), ('clock', 'clock'),
                          ('status3', 'status3')):
            if key in values:
                value = values[key]
                result += ['-global', f'ws53-sfc-command.snapshot-{name}={hex(value) if type(value) is int else value}']
    return result


@contextmanager
def session(binary, folder, *, values=None, assume=True, enabled=True, tcg=False):
    folder.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='sfc-snap-') as tmp:
        qt, qm = Path(tmp)/'qt', Path(tmp)/'qm'
        args = [str(binary), '-M', 'ws53', '-accel', 'tcg' if tcg else 'qtest',
                '-display', 'none', '-monitor', 'none', '-serial', 'null', '-nic', 'none',
                '-qtest', f'unix:{qt},server=on,wait=off', '-qtest-log', '/dev/null',
                '-qmp', f'unix:{qm},server=on,wait=off', '-d', 'guest_errors']
        if tcg:
            args += ['-S', '-device', f'loader,addr={BASE:#x},cpu-num=0']
        args += flags(values, assume=assume, enabled=enabled)
        (folder/'command.json').write_text(json.dumps(args, indent=2)+'\n')
        with (folder/'stderr.log').open('wb') as err, (folder/'traffic.jsonl').open('w') as traffic:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=err)
            try:
                with connect(qt, proc) as bus, connect(qm, proc) as monitor, \
                     bus.makefile('rwb', buffering=0) as io, monitor.makefile('rwb', buffering=0) as qmp:
                    require('QMP' in json.loads(qmp.readline()), 'QMP greeting absent')
                    peer = Peer(io, qmp, traffic)
                    peer.qmp('qmp_capabilities')
                    yield peer
                    peer.qmp('quit')
                    require(proc.wait(timeout=5) == 0, 'QEMU exit failed')
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)


def check_diagnostics(folder, *, assumed, enabled, rejections, status3=False):
    lines = (folder/'stderr.log').read_text().splitlines()
    notice = [s for s in lines if s.startswith('ws53-sfc-command: assumed synthetic ')]
    snapshot = [s for s in lines if s.startswith('ws53-sfc-command: assumed configured snapshot ')]
    raw_status = [s for s in lines if s.startswith('ws53-sfc-command: assumed immutable synthetic STATUS3=')]
    rejects = [s for s in lines if s.startswith('ws53-sfc-command: rejected ')]
    require(len(notice) == int(assumed) and len(snapshot) == int(enabled)
            and len(raw_status) == int(status3) and len(rejects) == rejections
            and len(lines) == len(notice)+len(snapshot)+len(raw_status)+len(rejects),
            'missing/unexpected guest diagnostics: '+repr(lines))


def snapshot(p, *, configured=True):
    result = p.snapshot()
    result['mutating-commands-started'] = p.get('mutating-commands-started')
    if configured:
        result['raw-snapshot'] = [p.rd(0x100), p.rd(0x240), int(p.qt(f'readl {CLOCK:#x}')[0], 0)]
    return result


def atomic_reject(p, operation, *, configured=True):
    before = snapshot(p, configured=configured)
    operation()
    after = snapshot(p, configured=configured)
    require(after.pop('rejections') == before.pop('rejections')+1, 'rejection counter did not increase once')
    require(after == before, 'rejected access changed model state')


def startup_rejections(binary, out):
    parse_error = 'requires an explicit unsigned 32-bit raw snapshot'
    cases = [('dependency', flags(GOOD, assume=False), 'configured-handoff requires assume-profile')]
    for field in GOOD:
        name = 'global' if field == 'global_value' else field
        diagnostic = f'snapshot-{name} {parse_error}'
        cases.append(('missing-'+field, flags({k: v for k, v in GOOD.items() if k != field}), diagnostic))
        for value in ('-1', '+1', '0x100000000', '0xdjunk', ''):
            cases.append(('bad-'+field+'-'+str(len(cases)), flags(GOOD | {field: value}), diagnostic))
    cases.append(('stray-snapshot', flags(GOOD, enabled=False), 'raw snapshots require configured-handoff'))
    cases.append(('flashboot-conflict', flags(GOOD)+['-M', 'ws53,flashboot-handoff=on'],
                  'SFC synthetic command profile and flashboot-handoff are mutually exclusive'))
    cases.append(('status3-dependency', flags({'status3': 0}, assume=False, enabled=False),
                  'snapshot-status3 requires assume-profile'))
    for index, value in enumerate(('-1', '+1', '256', '0x100000000', '0xajunk', '')):
        diagnostic = ('exceeds unsigned 8-bit raw status' if value == '256' else parse_error)
        cases.append((f'bad-status3-{index}', flags({'status3': value}, enabled=False),
                      f'snapshot-status3 {diagnostic}'))
    results = []
    for name, extra, diagnostic in cases:
        argv = [str(binary), '-M', 'ws53', '-S', '-display', 'none', '-monitor', 'none',
                '-serial', 'null', '-nic', 'none']+extra
        result = subprocess.run(argv, capture_output=True, timeout=5)
        require(result.returncode == 1 and diagnostic.encode() in result.stderr,
                'startup rejection absent or from unrelated cause: '+name)
        (out/(name+'.stderr')).write_bytes(result.stderr)
        results.append(dict(name=name, argv=argv, exit_code=result.returncode, expected_diagnostic=diagnostic))
    return results


def contract(binary, out):
    results = []
    for source in (0, 6):
        folder = out/f'contract-source{source}'
        values = GOOD | {'clock': (GOOD['clock'] & ~15) | 1 | (source << 1)}
        with session(binary, folder, values=values) as p:
            initial = snapshot(p)
            for _ in range(4):
                require([p.rd(0x100), p.rd(0x240), int(p.qt(f'readw {CLOCK:#x}')[0], 0),
                         int(p.qt(f'readl {CLOCK:#x}')[0], 0)] ==
                        [values['global_value'], values['dma'], values['clock'] & 0xffff, values['clock']],
                        'raw/alias read differs')
            require(snapshot(p) == initial, 'reads changed state')
            for name, value in [('configured-handoff', True), ('configured-handoff', False),
                                ('snapshot-global', hex(values['global_value'])), ('snapshot-dma', '0'),
                                ('snapshot-clock', '1')]:
                before = snapshot(p)
                p.qmp('qom-set', {'path': DEVICE, 'property': name, 'value': value}, expect_error=True)
                require(snapshot(p) == before, 'post-realize property mutation changed state')
            # Both values and exact same-value writes are refused; no opaque
            # upper-bit mutation or halfword RMW is silently accepted.
            for addr in (GLOBAL, DMA):
                for value in (0, 0xffffffff, values['global_value' if addr == GLOBAL else 'dma']):
                    atomic_reject(p, lambda a=addr, v=value: p.qt(f'writel {a:#x} {v:#x}'))
                for suffix in ('b', 'w'):
                    atomic_reject(p, lambda a=addr, s=suffix: p.qt(f'read{s} {a:#x}'))
                atomic_reject(p, lambda a=addr: p.qt(f'readl {a+1:#x}'))
            for suffix, value in (('b', 0), ('w', values['clock'] & 0xffff), ('l', values['clock'])):
                atomic_reject(p, lambda s=suffix, v=value: p.qt(f'write{s} {CLOCK:#x} {v:#x}'))
            for suffix, offset in (('b', 0), ('w', 1), ('w', 2), ('b', 3)):
                atomic_reject(p, lambda s=suffix, o=offset: p.qt(f'read{s} {CLOCK+o:#x}'))
            for off in (0x120, 0x124, 0x128, 0x12c, 0x130, 0x200, 0x244, 0x248, 0x24c, 0x250):
                atomic_reject(p, lambda o=off: p.rd(o))
            # Actual controller transfer and synthetic NOR WIP stay distinct.
            p.program(0x100, b'\x12\x34\x56\x78')
            p.wren()
            p.start(0x02, address=0x104, data=b'\x80\x40')
            p.done()
            require(p.rd(0x300) & 1 == 0 and p.get('peer-busy'), 'START incorrectly means NOR idle')
            p.qmp('system_reset')
            require(p.get('peer-busy'), 'system reset incorrectly canceled peer operation')
            require(snapshot(p)['raw-snapshot'] == initial['raw-snapshot'], 'reset did not replay configured snapshot')
            p.step(200_000)
            require(p.read(0x100, 6) == bytes.fromhex('123456788040'), 'peer pending data lost across reset')
            p.wren()
            pending = p.stage(0x02, address=0x106, data=b'\x00')
            p.wr(0x300, pending)
            p.qmp('system_reset')
            p.step(300_000)
            require(p.read(0x106, 1) == b'\xff', 'uncommitted controller operation survived reset')
            rejected = p.get('rejections')
            results.append(dict(name=f'contract-source{source}', passed=True, rejections=rejected,
                                values=values, no_read_side_effects=True, synthetic_reset_not_por=True))
        check_diagnostics(folder, assumed=True, enabled=True, rejections=rejected)
    unsupported = [('dma-busy', GOOD | {'dma': GOOD['dma'] | 1}),
                   ('clock-off', GOOD | {'clock': GOOD['clock'] & ~1}),
                   ('spi-mode-field1', GOOD | {'global_value': GOOD['global_value'] | 1}),
                   ('four-byte', GOOD | {'global_value': GOOD['global_value'] | 4})]
    unsupported += [(f'source{source}', GOOD | {'clock': 1 | (source << 1)})
                    for source in (1, 2, 3, 4, 5, 7)]
    for name, values in unsupported:
        folder = out/name
        with session(binary, folder, values=values) as p:
            for _ in range(2):
                p.qmp('system_reset')
                require(p.rd(0x240) == values['dma'], 'reset cleared supplied DMA state')
                word = p.stage(0x9f, read=True, length=3)
                atomic_reject(p, lambda: p.wr(0x300, word))
                p.step(1_000_000)
                require(p.get('commands-started') == p.get('commands-completed') == 0,
                        'unqualified snapshot started command')
            rejected = p.get('rejections')
            results.append(dict(name=name, passed=True, rejections=rejected, values=values))
        check_diagnostics(folder, assumed=True, enabled=True, rejections=rejected)
    return results


def status3_contract(binary, out):
    """Optional immutable synthetic byte, not a real NOR status/NV layout."""
    results = []
    for seed in (None, 0, 0xa7, 0xff):
        folder = out/('status3-unconfigured' if seed is None else f'status3-{seed:02x}')
        with session(binary, folder, values=None if seed is None else {'status3': seed},
                     enabled=False) as p:
            if seed is None:
                word = p.stage(0x15, read=True, length=1)
                atomic_reject(p, lambda: p.wr(0x300, word), configured=False)
            else:
                for value in (hex(seed), '0x55'):
                    before = snapshot(p, configured=False)
                    p.qmp('qom-set', {'path': DEVICE, 'property': 'snapshot-status3', 'value': value},
                          expect_error=True)
                    require(snapshot(p, configured=False) == before, 'STATUS3 runtime mutation changed state')
                for kwargs in ({'read': True, 'length': 0}, {'read': True, 'length': 2},
                               {'read': True, 'length': 1, 'address': 0}, {'length': 1}):
                    word = p.stage(0x15, **kwargs)
                    atomic_reject(p, lambda: p.wr(0x300, word), configured=False)
                # DATA is unchanged until controller completion and only byte0
                # changes; an explicit zero seed is distinct from absent input.
                p.start(0x15, read=True, data=b'\xa7'*64, length=1)
                require(p.words() == [0xa7a7a7a7]*16, 'STATUS3 changed DATA before completion')
                p.done()
                require(struct.pack('<16I', *p.words()) == bytes([seed])+b'\xa7'*63,
                        'STATUS3 byte/tail transfer differs')
                # Reading raw STATUS3 during WIP does not imply idle. A
                # controller reset preserves submitted NOR work; a synthetic
                # peer power cycle cancels it, but the immutable seed remains.
                p.wren()
                p.start(0x02, address=0x100, data=b'\x00')
                p.done()
                p.fault('peer-stall', True)
                require(p.get('peer-busy'), 'expected pending NOR operation')
                for reset in (False, True):
                    if reset:
                        p.qmp('system_reset')
                    p.command(0x15, read=True, length=1)
                    require(p.words()[0] & 255 == seed and p.get('peer-busy'),
                            'STATUS3 seed or pending NOR work lost')
                    p.command(0x05, read=True, length=1)
                    require(p.words()[0] & 1 and p.get('peer-commits') == 0,
                            'STATUS3 read concealed WIP or committed NOR')
                p.power_cycle()
                p.fault('peer-stall', False)
                p.step(1_000_000)
                p.command(0x15, read=True, length=1)
                require(p.words()[0] & 255 == seed and not p.get('peer-busy'),
                        'immutable seed changed after synthetic power cycle')
                require(p.read(0x100, 1) == b'\xff' and p.get('peer-commits') == 0,
                        'canceled NOR write committed later')
            rejected = p.get('rejections')
            results.append(dict(seed=seed, passed=True, rejections=rejected,
                                synthetic_immutable_input=True, real_status_bits_qualified=False))
        check_diagnostics(folder, assumed=True, enabled=False, rejections=rejected,
                          status3=seed is not None)
    return results


def access_program(values, configured):
    rows = []
    for addr, width, expected in ((GLOBAL, 4, values['global_value']), (DMA, 4, values['dma']),
                                 (CLOCK, 2, values['clock'] & 0xffff), (CLOCK, 4, values['clock'])):
        rows.append(dict(address=addr, width=width, write=False, accepted=configured, expected=expected))
        rows.append(dict(address=addr, width=width, write=True, accepted=False, expected=expected))
    for addr, width in ((GLOBAL, 1), (DMA, 2), (CLOCK, 1), (CLOCK+2, 2)):
        rows.append(dict(address=addr, width=width, write=False, accepted=False, expected=POISON))
    code = li(5, VECTOR)+[csr(0x305, 5)]+li(9, 0)
    for i, row in enumerate(rows):
        value = row['expected'] if row['write'] else POISON
        code += li(8, OUTPUT+16+24*i)+li(5, row['address'])+li(6, value)
        row['pc'] = BASE+4*len(code)
        width = {1: 0, 2: 1, 4: 2}[row['width']]
        word = ((6 << 20) | (5 << 15) | (width << 12) | 0x23) if row['write'] else (
               (5 << 15) | ((width if width == 2 else width+4) << 12) | (6 << 7) | 3)
        code += [word, store(6, 8, 0), store(9, 8, 16)]
    done = OUTPUT+16+24*len(rows)+16
    code += li(8, done)+li(6, 1)+[store(6, 8, 0), 0x6f]
    handler = [csr(0x342, 0, 2, 10), store(10, 8, 4), csr(0x341, 0, 2, 10), store(10, 8, 8),
               csr(0x343, 0, 2, 11), store(11, 8, 12), 0x00450513, csr(0x341, 10), 0x00148493, 0x30200073]
    pack = lambda values: struct.pack('<'+'I'*len(values), *values)
    return rows, done, [(BASE, pack(code)), (VECTOR, pack(handler)),
        (OUTPUT, pack([POISON]*((done-OUTPUT)//4))+bytes(4))]


def execute(p, regions, done):
    for address, data in regions:
        p.qt(f'write {address:#x} {len(data)} 0x{data.hex()}')
    p.qmp('cont')
    deadline = time.monotonic()+5
    while int(p.qt(f'readl {done:#x}')[0], 0) != 1:
        require(time.monotonic() < deadline, 'bounded TCG completion deadline')
        time.sleep(.005)
    p.qmp('stop')
    raw = bytes.fromhex(p.qt(f'read {OUTPUT:#x} {done-OUTPUT}')[0].removeprefix('0x'))
    return struct.unpack('<'+'I'*(len(raw)//4), raw)


def cpu_access(binary, out):
    results = []
    for assume, configured in ((False, False), (True, False), (True, True)):
        folder = out/f'cpu-{int(assume)}-{int(configured)}'
        rows, done, regions = access_program(GOOD, configured)
        with session(binary, folder, values=GOOD if configured else None, assume=assume,
                     enabled=configured, tcg=True) as p:
            rounds = []
            for iteration in range(2):
                p.qmp('system_reset')
                data = execute(p, regions, done)
                require(data[:4] == data[-4:] == (POISON,)*4, 'TCG guard corruption')
                traps = 0
                for i, row in enumerate(rows):
                    expected = row['expected'] if row['write'] or row['accepted'] else POISON
                    if not row['accepted']:
                        traps += 1
                    trap = (POISON,)*3 if row['accepted'] else (7 if row['write'] else 5, row['pc'], row['address'])
                    require(data[4+6*i:10+6*i] == (expected, *trap, traps, POISON),
                            'CPU cause/mepc/mtval/rd mismatch: '+repr(row))
                rounds.append(dict(iteration=iteration, operations=rows, faults=traps, guards_preserved=True))
            rejected = p.get('rejections')
            require(rejected == traps*2, 'CPU fault diagnostic count differs')
        check_diagnostics(folder, assumed=assume, enabled=configured, rejections=rejected)
        results.append(dict(assume=assume, configured=configured, rounds=rounds, rejections=rejected))
    return results


def read_program(status3=False):
    # Standalone CPU client, no HAL implementation is borrowed. Each polling
    # loop has a finite instruction budget; timeout writes zero over the poison
    # success status. A trap writes mcause and never publishes completion.
    code = li(5, VECTOR)+[csr(0x305, 5)]+li(8, OUTPUT)
    for i, addr in enumerate((GLOBAL, DMA, CLOCK)):
        code += li(5, addr)+[load(6, 5, 0), store(6, 8, i*4)]
    commands = [(0x9f, 3, None), (0x05, 1, None), (0x35, 1, None)]
    if status3:
        commands.append((0x15, 1, None))
    commands += [(0x03, 64, 0), (0x03, 64, 0x3fffc0)]
    for index, (opcode, length, address) in enumerate(commands):
        code += li(5, 0x53000308)+li(6, opcode)+[store(6, 5, 0)]
        if address is not None:
            code += li(5, 0x5300030c)+li(6, address)+[store(6, 5, 0)]
        code += li(5, 0x53000300)+li(6, config(read=True, length=length, address=address is not None))
        code += [store(6, 5, 0)]+li(7, 1_000_000)
        # if START clear skip 5 instructions to data capture; otherwise retry
        code += [load(6, 5, 0), 0x00137313, branch(6, 0, 0, 20), 0xfff38393,
                 branch(7, 0, 1, -16)]
        code += [store(7, 8, 12), 0x6f]
        code += li(5, 0x53000400)
        for word in range(16):
            code += [load(6, 5, word*4), store(6, 8, 16+index*64+word*4)]
    done = OUTPUT+16+64*len(commands)
    code += li(5, done)+li(6, 1)+[store(6, 5, 0), 0x6f]
    handler = li(8, OUTPUT)+[csr(0x342, 0, 2, 6), store(6, 8, 12), 0x6f]
    pack = lambda values: struct.pack('<'+'I'*len(values), *values)
    return commands, done, [(BASE, pack(code)), (VECTOR, pack(handler)),
                            (OUTPUT, pack([POISON]*((done-OUTPUT)//4))+bytes(4))]


def cpu_reads(binary, out):
    results = []
    for seed in (None, 0, 0xa7, 0xff):
        folder = out/('cpu-read-only' if seed is None else f'cpu-read-only-status3-{seed:02x}')
        commands, done, regions = read_program(seed is not None)
        immutable = ('peer-sha256', 'peer-operations', 'peer-commits', 'peer-wel', 'peer-volatile-enable',
                     'peer-sr1', 'peer-sr2', 'peer-nv-sr1', 'peer-nv-sr2', 'mutating-commands-started')
        values = GOOD if seed is None else GOOD | {'status3': seed}
        with session(binary, folder, values=values, tcg=True) as p:
            before = {name: p.get(name) for name in immutable}
            for iteration in range(2):
                p.qmp('system_reset')
                words = execute(p, regions, done)
                require(words[:4] == (GOOD['global_value'], GOOD['dma'], GOOD['clock'], POISON),
                        'CPU snapshot reads or trap/timeout status differ')
                data = struct.pack('<'+'I'*len(words[4:]), *words[4:])
                require(data[:3] == bytes.fromhex('7f5301') and data[64] == data[128] == 0,
                        'CPU JEDEC/status transfer differs')
                if seed is not None:
                    require(data[192] == seed, 'CPU raw STATUS3 transfer differs')
                require(data[-128:] == b'\xff'*128, 'CPU array first/last 64 bytes differ')
                after = {name: p.get(name) for name in immutable}
                require(after == before, 'read-only CPU client changed NOR/enable state')
                require(p.get('commands-started') == p.get('commands-completed') == len(commands)*(iteration+1)
                        and p.get('rejections') == 0, 'CPU command accounting differs')
                results.append(dict(iteration=iteration, status3_seed=seed, commands=[c[0] for c in commands],
                                    command_count=len(commands), nor_before=before, nor_after=after,
                                    real_tcg=True, hal_implementation_executed=False))
        check_diagnostics(folder, assumed=True, enabled=True, rejections=0, status3=seed is not None)
    return results


def main(args):
    binary, out = args.qemu.resolve(), args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    report = dict(passed=False, hardware_accessed=False, ci_receipt=False,
        physical_clock_or_drain_qualified=False, ownership_qualified=False, synthetic_reset_not_por=True,
        qemu_sha256=sha(binary), source_sha256={name: sha(Path(__file__).parent/name) for name in
        ('ws53_sfc_snapshot_test.py', 'ws53_sfc_contract_test.py', 'ws53_branch_immediate_test.py', 'hisi_priority_test.py')})
    try:
        report['startup_rejections'] = startup_rejections(binary, out)
        report['contracts'] = contract(binary, out)
        report['status3_contracts'] = status3_contract(binary, out)
        report['tcg_access'] = cpu_access(binary, out)
        report['tcg_read_only'] = cpu_reads(binary, out)
        report['passed'] = True
    except BaseException as error:
        report['error'] = repr(error)
        raise
    finally:
        (out/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(dict(passed=True, output=str(out/'report.json'), startup_rejections=len(report['startup_rejections']),
        contracts=len(report['contracts']), status3_contracts=len(report['status3_contracts']),
        tcg_access_operations=sum(len(r['operations']) for group in report['tcg_access'] for r in group['rounds']),
        tcg_read_commands=sum(r['command_count'] for r in report['tcg_read_only'])), indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    main(parser.parse_args())
