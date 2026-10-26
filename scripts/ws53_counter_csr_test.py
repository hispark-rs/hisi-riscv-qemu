#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Real TCG CSR capability/atomicity tests, including unchanged older boards."""
import argparse
import json
from pathlib import Path
import struct
import time

from ws53_cache_state_test import POISON, case, program, session, sha, NOTICE


def scenarios():
    return [
        case('read-cycle-before', 0xb00), case('read-cycleh-before', 0xb80),
        case('read-inhibit', 0x320),
        case('read-inhibit-rd0', 0x320, rd=0),
        case('read-clear-inhibit', 0x320, funct=3),
        case('write-inhibit', 0x320, 5, 1, 5),
        case('write-inhibit-rd0', 0x320, 5, 1, 5, 0),
        case('set-inhibit', 0x320, 5, 2, 5),
        case('clear-inhibit', 0x320, 5, 3, 5),
        case('write-zero-inhibit-rd0', 0x320, 0, 1, 0, 0),
        case('write-immediate-inhibit', 0x320, funct=5, source=5),
        case('set-immediate-inhibit', 0x320, funct=6, source=5),
        case('clear-immediate-inhibit', 0x320, funct=7, source=5),
        case('read-immediate-set-zero', 0x320, funct=6),
        case('read-immediate-clear-zero', 0x320, funct=7),
        case('read-cycle-after', 0xb00), case('read-cycleh-after', 0xb80),
    ]


def run_profile(qemu, machine, base, profile=None, ws53=False):
    rows = scenarios()
    output, done, positions, regions = program(base, rows, False)
    rounds = []
    with session(qemu, machine, base, profile,
                 ('-icount', 'shift=8,align=off,sleep=off')) as (execute, bus, diagnostics):
        def read(address):
            return int(bus(f'readl {address:#x}')[0], 0)
        for reset_round in range(2):
            execute('system_reset')
            for address, data in regions:
                bus(f'write {address:#x} {len(data)} 0x{data.hex()}')
            execute('cont')
            deadline = time.monotonic() + 5
            while read(done) != 1:
                assert time.monotonic() < deadline, ('TCG deadline', machine, profile)
                time.sleep(.002)
            execute('stop')
            raw = bytes.fromhex(bus(f'read {output:#x} {done-output}')[0].decode().removeprefix('0x'))
            words = struct.unpack('<'+'I'*(len(raw)//4), raw)
            assert words[:4] == words[-4:] == (POISON,)*4
            observations, traps, inhibit, cycles = [], 0, 0, []
            for index, (row, (pc, instruction)) in enumerate(zip(rows, positions)):
                value = words[4+index*8:12+index*8]
                rejected = ws53 and row['csr'] == 0x320
                traps += rejected
                if rejected:
                    assert value[:4] == (POISON, 2, pc, instruction), (row, value)
                else:
                    assert value[1:4] == (POISON,)*3, (row, value)
                    if row['csr'] == 0x320:
                        assert value[0] == (inhibit if row['rd'] else POISON), (row, value, inhibit)
                        source = row['source'] if row['funct'] >= 5 else (row['value'] if row['source'] else 0)
                        operation = row['funct'] & 3
                        inhibit = (source if operation == 1 else inhibit | source if operation == 2
                                   else inhibit & ~source)
                    else:
                        assert value[0] != POISON, (row, value)
                        cycles.append(value[0])
                assert value[4:7] == (POISON,)*3 and value[7] == traps, (row, value, traps)
                observations.append(dict(name=row['name'], instruction=hex(instruction),
                    pc=hex(pc), words=list(value), rejected=rejected))
            before, after = cycles[0] | cycles[1] << 32, cycles[2] | cycles[3] << 32
            assert after > before and inhibit == 0, (before, after, inhibit)
            assert traps == (13 if ws53 else 0)
            rounds.append(dict(reset_round=reset_round, operations=len(rows), exceptions=traps,
                guards_intact=True, cycles_advance=True, counter_delta=after-before,
                observations=observations))
    expected = []
    if machine == 'ws63':
        expected = ['qemu-system-riscv32: warning: nic ws63-netmac.0 has no peer']
    elif profile == 'observed-flash-baseline':
        expected = [NOTICE]
    elif profile == 'instruction-experiment':
        expected = ['qemu-system-riscv32: ws53-cache: instruction experiment; synthetic initial '
                    'state, direct-mapped Flash lines, no timing/D-cache/drain proof']
    assert diagnostics == expected, (machine, profile, diagnostics, expected)
    return dict(machine=machine, profile=profile, rounds=rounds, diagnostics=diagnostics)


def run(qemu):
    qemu = Path(qemu).resolve()
    results = [run_profile(qemu, 'ws53'+(',flashboot-handoff=on' if profile == 'instruction-experiment' else ''),
               0x28000, profile, True) for profile in
               (None, 'observed-flash-baseline', 'instruction-experiment')]
    results += [run_profile(qemu, machine, base) for machine, base in
                (('ws63', 0xa00000), ('bs20', 0x100000), ('bs21', 0x100000),
                 ('bs21e', 0x100000), ('bs22', 0x100000))]
    return dict(passed=True, qemu_sha256=sha(qemu), producer_sha256=sha(__file__),
        cases=results, tcg_operations=sum(len(c['rounds'])*len(scenarios()) for c in results),
        tcg_execution=True, hardware_accessed=False, timing_qualified=False,
        silicon_write_behavior_measured=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    print(json.dumps(run(parser.parse_args().qemu), indent=2))
