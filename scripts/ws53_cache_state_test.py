#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""True TCG vendor-CSR admission tests; no cache/coherency/drain simulation."""
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
from ws53_branch_immediate_test import li, store

OBSERVED = 'observed-flash-baseline'
KNOWN = {0x7c0: 6, 0x7c1: 0, 0x7d8: 0x33333333}
POISON = 0xa5a5a5a5
NOTICE = ('qemu-system-riscv32: ws53-cache: observed Flash CSR profile ICCTL=6 DCCTL=0 '
          'MEMATTRL=0x33333333; state only, no cache/coherency/drain')
STARTUP_ONLY = 'ws53: cache-state-profile is a startup-only property'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def case(name, number, value=0, funct=2, source=0, rd=6):
    return dict(name=name, csr=number, value=value, funct=funct, source=source, rd=rd)


def scenarios(profile):
    # Every previously RAZ/WI CSR is tested, not just the known whitelist.
    rows = [case(f'read-{n:03x}', n) for n in range(0x7c0, 0x800)]
    for n, value in KNOWN.items():
        rows += [case(f'write-same-{n:03x}', n, value, 1, 5),
                 case(f'csrw-rd0-{n:03x}', n, value, 1, 5, 0),
                 case(f'csrrc-x0-{n:03x}', n, 0, 3, 0),
                 case(f'csrrs-x0-rd0-{n:03x}', n, 0, 2, 0, 0),
                 case(f'csrrc-x0-rd0-{n:03x}', n, 0, 3, 0, 0),
                 case(f'csrrsi-zero-{n:03x}', n, 0, 6, 0),
                 case(f'csrrci-zero-{n:03x}', n, 0, 7, 0)]
    if profile == 'unknown':
        return rows
    for n, values in ((0x7c0, (0, 1, 2, 3, 4, 5, 7, 8, 0x80000006)),
                      (0x7c1, (1, 2, 3, 4, 5, 6, 7, 8, 0x80000000))):
        rows += [case(f'unsupported-control-{n:03x}-{v:x}', n, v, 1, 5) for v in values]
    for n, values in ((0x7c2, (0, 4, 5, 0xffffffff)),
                      (0x7c3, (0, 4, 5, 8, 9, 12, 13, 0xffffffff)),
                      (0x7c4, (0, 0x20050000, 0x20050003)),
                      (0x7c5, (0, 0x20050000, 0x20050003)),
                      (0x7c6, (0, 1, 15)), (0x7c7, (0, 17, 255)),
                      (0x7d9, (0, 0x33333333)), (0x7ff, (0, 0xffffffff))):
        rows += [case(f'unsupported-write-{n:03x}-{v:x}', n, v, 1, 5) for v in values]
    for nibble in range(16):
        if nibble != 3:
            rows.append(case(f'memattr-low-{nibble:x}', 0x7d8, 0x33333330 | nibble, 1, 5))
    for nibble in (0, 2, 7, 15):
        rows.append(case(f'memattr-high-{nibble:x}', 0x7d8, (nibble << 28) | 0x3333333, 1, 5))
    rows += [case('rmw-set-icache', 0x7c0, 1, 2, 5),
             case('rmw-clear-size', 0x7c0, 2, 3, 5),
             case('rmw-set-dcache', 0x7c1, 1, 2, 5),
             case('rmw-set-dcache-rd0', 0x7c1, 1, 2, 5, 0),
             case('rmw-clear-size-rd0', 0x7c0, 2, 3, 5, 0),
             case('csrw-enable-rd0', 0x7c1, 1, 1, 5, 0),
             case('csrw-maintenance-rd0', 0x7c3, 12, 1, 5, 0),
             case('csrwi-enable-rd0', 0x7c1, 0, 5, 1, 0),
             case('csrwi-maintenance-rd0', 0x7c3, 0, 5, 12, 0),
             case('rmw-set-memattr', 0x7d8, 4, 2, 5),
             case('rmw-clear-memattr', 0x7d8, 1, 3, 5),
             case('rmw-same-set', 0x7c0, 2, 2, 5),
             case('rmw-same-clear', 0x7c0, 1, 3, 5),
             case('immediate-enable', 0x7c1, 0, 5, 1),
             case('immediate-set', 0x7c1, 0, 6, 1),
             case('immediate-clear', 0x7c0, 0, 7, 2),
             case('immediate-same-icctl', 0x7c0, 0, 5, 6),
             case('immediate-same-dcctl', 0x7c1, 0, 5, 0)]
    return rows


def operation(row):
    source = row['source'] if row['funct'] >= 5 else (row['value'] if row['source'] else 0)
    kind = row['funct'] & 3
    if kind == 1:
        return source, 0xffffffff
    if kind == 2:
        # QEMU trans_csrrs/trans_csrrsi pass all-ones as the new value and
        # the source as the mask. The x0/zimm0 form instead calls do_csrr.
        return (0xffffffff if row['source'] else 0), source
    return 0, source


def expected(row, profile, legacy=False):
    value, mask = operation(row)
    old = 0 if legacy else KNOWN.get(row['csr'])
    accepted = legacy or (profile == OBSERVED and old is not None and
                          ((old & ~mask) | (value & mask)) == old)
    return accepted, (old if accepted and row['rd'] else POISON), value, mask


def reason(row, profile):
    if profile == 'unknown':
        return 'unknown-inherited-state'
    number = row['csr']
    if number in KNOWN:
        return {0x7c0: 'instruction-cache-change-not-modeled',
                0x7c1: 'data-cache-change-not-modeled',
                0x7d8: 'memory-attribute-change-not-modeled'}[number]
    if number == 0x7d9:
        return 'unobserved-memattrh'
    if number in (0x7c2, 0x7c3):
        return 'maintenance-not-modeled'
    if number in (0x7c4, 0x7c5):
        return 'maintenance-address-not-modeled'
    if number in (0x7c6, 0x7c7):
        return 'prefetch-not-modeled'
    return 'vendor-csr-not-modeled'


def program(base, rows, observe):
    output, vector = base + 0x8000, base + 0x7000
    # WS53 output is in the last DTCM bank, not beyond ITCM.
    if base == 0x28000:
        output = 0x20050000
    code = li(5, vector) + [csr(0x305, 5)] + li(9, 0)
    expected_pcs = []
    for index, row in enumerate(rows):
        code += li(8, output + 16 + index * 32) + li(6, POISON) + li(5, row['value'])
        word = csr(row['csr'], row['source'], row['funct'], row['rd'])
        expected_pcs.append((base + len(code) * 4, word))
        code += [word, store(6, 8, 0)]
        if observe:
            for offset, number in enumerate(KNOWN):
                code += [csr(number, 0, 2, 7), store(7, 8, 16 + offset * 4)]
        code += [store(9, 8, 28)]
    done = output + 16 + len(rows) * 32 + 16
    code += li(8, done) + li(6, 1) + [store(6, 8, 0), 0x6f]
    handler = [csr(0x342, 0, 2, 10), store(10, 8, 4),
               csr(0x341, 0, 2, 10), store(10, 8, 8),
               csr(0x343, 0, 2, 11), store(11, 8, 12),
               0x00450513, csr(0x341, 10), 0x00148493, 0x30200073]
    assert len(code) * 4 < 0x7000
    pack = lambda values: struct.pack('<' + 'I' * len(values), *values)
    memory = pack([POISON] * ((done - output) // 4)) + bytes(4)
    return output, done, expected_pcs, [(base, pack(code)), (vector, pack(handler)), (output, memory)]


@contextmanager
def session(qemu, machine, base, profile=None, options=()):
    with tempfile.TemporaryDirectory(prefix='ws53-cache-') as temporary:
        directory = Path(temporary)
        qt, qm = directory/'qt', directory/'qm'
        selected = machine + (f',cache-state-profile={profile}' if profile is not None else '')
        command = [str(qemu), '-M', selected, '-accel', 'tcg', '-S', '-display', 'none',
                   '-monitor', 'none', '-serial', 'null', '-nic', 'none',
                   '-device', f'loader,addr={base:#x},cpu-num=0',
                   '-qtest', f'unix:{qt},server=on,wait=off', '-qtest-log', '/dev/null',
                   '-qmp', f'unix:{qm},server=on,wait=off', '-d', 'guest_errors', *options]
        with (directory/'stderr').open('w+b') as errors:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors)
            diagnostics = []
            try:
                with connect(qt, process) as bus, bus.makefile('rwb', buffering=0) as io, \
                        connect(qm, process) as control, control.makefile('rwb', buffering=0) as monitor:
                    assert 'QMP' in json.loads(monitor.readline())
                    def execute(name, arguments=None, error=None):
                        request = {'execute': name}
                        if arguments is not None:
                            request['arguments'] = arguments
                        monitor.write(json.dumps(request).encode() + b'\n')
                        while True:
                            raw = monitor.readline()
                            assert raw, 'QMP disconnected'
                            reply = json.loads(raw)
                            if 'error' in reply:
                                assert error is not None and reply['error'] == {'class': 'GenericError', 'desc': error}, reply
                                return reply
                            if 'return' in reply:
                                assert error is None, ('expected QMP rejection', request, reply)
                                return reply['return']
                    def bus_command(command):
                        io.write(command.encode() + b'\n')
                        reply = io.readline().split()
                        assert reply and reply[0] == b'OK', reply
                        return reply[1:]
                    execute('qmp_capabilities')
                    try:
                        yield execute, bus_command, diagnostics
                    finally:
                        if process.poll() is None:
                            execute('quit')
                assert process.wait(timeout=5) == 0
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                errors.seek(0)
                diagnostics.extend(errors.read().decode().splitlines())


def run_profile(qemu, machine, base, profile, legacy=False):
    rows = scenarios(profile)
    rejected = sum(not expected(row, profile, legacy)[0] for row in rows)
    rounds, qmp_rejections = [], 0
    with session(qemu, machine, base, None if legacy or profile == 'unknown' else profile) as (execute, bus, diagnostics):
        def get(name):
            return execute('qom-get', {'path': '/machine', 'property': name})
        def read(address):
            return int(bus(f'readl {address:#x}')[0], 0)
        maps = execute('human-monitor-command', {'command-line': 'info mtree'})
        for reset_round in range(2):
            execute('system_reset')
            assert not execute('query-status')['running']
            if not legacy:
                assert get('cache-state-profile') == profile
                assert get('cache-csr-rejections') == reset_round * rejected
                assert get('cache-csr-epoch-rejections') == 0 and not get('cache-csr-tainted')
                bus('writel 0x2005f000 0x13579bdf')
                for requested in ('unknown', OBSERVED):
                    execute('qom-set', {'path': '/machine', 'property': 'cache-state-profile', 'value': requested}, STARTUP_ONLY)
                    qmp_rejections += 1
                    assert get('cache-state-profile') == profile
                    assert read(0x2005f000) == 0x13579bdf
                    assert get('cache-csr-epoch-rejections') == 0
                    assert execute('human-monitor-command', {'command-line': 'info mtree'}) == maps
            output, done, positions, regions = program(base, rows, profile == OBSERVED and not legacy)
            for address, raw in regions:
                bus(f'write {address:#x} {len(raw)} 0x{raw.hex()}')
            execute('cont')
            deadline = time.monotonic() + 5
            while read(done) != 1:
                if time.monotonic() >= deadline:
                    execute('stop')
                    raise AssertionError((machine, profile, 'TCG deadline',
                        execute('human-monitor-command', {'command-line': 'info registers'}),
                        [read(output + index * 4) for index in range(36)]))
                time.sleep(.005)
            execute('stop')
            assert not execute('query-status')['running']
            raw = bytes.fromhex(bus(f'read {output:#x} {done-output}')[0].decode().removeprefix('0x'))
            words = struct.unpack('<'+'I'*(len(raw)//4), raw)
            assert words[:4] == words[-4:] == (POISON,) * 4
            traps, observations = 0, []
            for index, (row, (pc, instruction)) in enumerate(zip(rows, positions)):
                actual = words[4+index*8:12+index*8]
                accepted, rd, _, _ = expected(row, profile, legacy)
                traps += not accepted
                trap = (POISON, POISON, POISON) if accepted else (2, pc, instruction)
                state = tuple(KNOWN.values()) if profile == OBSERVED and not legacy else (POISON,)*3
                assert actual == (rd, *trap, *state, traps), (row, actual, rd, trap, state, traps)
                observations.append(dict(name=row['name'], csr=hex(row['csr']), instruction=hex(instruction),
                    accepted=accepted, rd=hex(actual[0]), exception_cause=actual[1] if not accepted else None,
                    exception_pc=hex(actual[2]) if not accepted else None,
                    exception_instruction=hex(actual[3]) if not accepted else None))
            assert traps == rejected
            if not legacy:
                assert get('cache-csr-rejections') == (reset_round+1) * rejected
                assert get('cache-csr-epoch-rejections') == rejected
                assert get('cache-csr-tainted') is bool(rejected)
                assert read(0x2005f000) == 0x13579bdf
            rounds.append(dict(reset_round=reset_round, operations=len(rows), exceptions=traps,
                guards_intact=True, atomic_rd_and_csr_preserved=True, observations=observations))
        # Reset changes the diagnostic epoch, never the selected profile or the
        # lifetime rejection history; no cache or physical-drain claim follows.
        execute('system_reset')
        if not legacy:
            assert get('cache-state-profile') == profile
            assert get('cache-csr-rejections') == 2*rejected
            assert get('cache-csr-epoch-rejections') == 0 and not get('cache-csr-tainted')
    wanted = []
    if legacy:
        if machine == 'ws63':
            wanted.append('qemu-system-riscv32: warning: nic ws63-netmac.0 has no peer')
    else:
        if profile == OBSERVED:
            wanted.append(NOTICE)
        for _ in range(2):
            for row in rows:
                accepted, _, value, mask = expected(row, profile)
                if not accepted:
                    wanted.append(f"ws53-cache: unsupported csr=0x{row['csr']:03x} op={'write' if mask else 'read'} "
                        f'value=0x{value:08x} mask=0x{mask:08x} profile={profile} reason={reason(row,profile)} '
                        '(model limitation, not silicon illegal)')
    assert diagnostics == wanted, (diagnostics, wanted)
    return dict(machine=machine, profile=profile if not legacy else 'legacy-RAZ/WI', rounds=rounds,
        tcg_operations=2*len(rows), expected_exceptions=2*rejected,
        qmp_startup_only_rejections=qmp_rejections, exact_diagnostics=len(diagnostics),
        state_only=True, hardware_accessed=False, dma_or_coherency_qualified=False)


def cache_state_test(qemu):
    qemu = Path(qemu).resolve()
    results = [run_profile(qemu, 'ws53', 0x28000, profile) for profile in ('unknown', OBSERVED)]
    for machine, base in (('ws63', 0xa00000), ('bs20', 0x100000), ('bs21', 0x100000),
                          ('bs21e', 0x100000), ('bs22', 0x100000)):
        results.append(run_profile(qemu, machine, base, OBSERVED, legacy=True))
    for profile in ('enabled', 'observed'):
        result = subprocess.run([str(qemu), '-M', 'ws53,cache-state-profile='+profile,
            '-S', '-display', 'none', '-monitor', 'none', '-serial', 'null'],
            capture_output=True, timeout=5)
        assert result.returncode == 1 and not result.stdout
        assert result.stderr.decode().splitlines() == [
            'qemu-system-riscv32: ws53: cache-state-profile must be unknown or '+OBSERVED+' or instruction-experiment']
    here = Path(__file__).resolve().parent
    return dict(passed=True, qemu_sha256=sha(qemu),
        source_sha256={name:sha(here/name) for name in
            ('ws53_cache_state_test.py','hisi_priority_test.py','ws53_branch_immediate_test.py')},
        profiles=results, machine_count=6, startup_invalid_profiles_rejected=2,
        tcg_operations=sum(row['tcg_operations'] for row in results),
        expected_exceptions=sum(row['expected_exceptions'] for row in results),
        qmp_startup_only_rejections=sum(row['qmp_startup_only_rejections'] for row in results),
        hardware_accessed=False, data_cache_simulated=False, physical_drain_verified=False,
        dma_safety_qualified=False,
        scope='TCG CSR state and explicit unsupported-operation rejection, not silicon illegal-op behavior')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    print(json.dumps(cache_state_test(parser.parse_args().qemu), indent=2))
