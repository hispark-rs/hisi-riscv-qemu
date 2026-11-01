#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""TCG stale/new instruction execution, maintenance, FENCE.I and reset tests.

The explicit experiment uses synthetic initial state/geometry/lookahead.
No physical cache, Flash programming, precise timing or DMA qualification.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import time

from hisi_priority_test import csr
from ws53_branch_immediate_test import branch, li, store
from ws53_cache_state_test import session, STARTUP_ONLY

BASE, OUTPUT, VECTOR = 0x28000, 0x20050000, 0x2f000
A, B = 0x4ff000, 0x4ff100
C, D = 0x4ff200, 0x4ffffc  # 48-bit instruction crosses a page and cache line.
D_VALUES = (0x12340001,0x23450002,0x34560003,0x45670004,0x56780005)
PROFILE = 'instruction-experiment'


def program():
    code = li(8, OUTPUT) + li(5, VECTOR) + [csr(0x305, 5)] + li(9, 0)
    results, faults = [], []

    def write(number, value):
        code.extend(li(5, value) + [csr(number, 5)])

    def call(address):
        offset = len(results) * 4
        results.append(offset)
        code.extend(li(5, address) + [0x000280e7, store(10, 8, offset)])

    def stage(number):
        code.extend(li(5, number) + [store(5, 8, 0x100)])
        # Wait until the stopped-CPU host test has changed the backing word.
        loop = len(code)
        code.extend([0x10442303] + li(7, number))  # lw t1,0x104(s0)
        code.append(branch(6, 7, 1, (loop-len(code))*4))

    def calls(number):
        for address in (A,B,C,D):
            call(address)
        stage(number)

    write(0x7c2, 4)
    write(0x7c0, 7)
    write(0x7c6, 7)
    code.append(0x0000100f)
    for number in (0x7c0, 0x7c6, 0x7c1):
        code.extend([csr(number, 0, 2, 6), store(6, 8, 0x110 + (number == 0x7c6)*4 + (number == 0x7c1)*8)])
    calls(1)
    calls(2)
    write(0x7c2, 4)
    calls(3)
    code.append(0x0000100f)
    calls(4)
    write(0x7c0, 6)
    calls(5)
    write(0x7c0, 7)
    calls(6)
    write(0x7c4, A)
    write(0x7c2, 5)
    calls(7)
    code.append(0x0000100f)
    calls(8)
    negatives = [
        (0x7c2, 0, 2, 0, 6),   # maintenance read
        (0x7c2, 4, 1, 5, 6),   # write-only command with rd
        (0x7c0, 0x100, 2, 5, 6),
        (0x7c6, 16, 1, 5, 6),
        (0x7c0, 3, 1, 5, 6),  # resize while enabled
        (0x7c1, 1, 1, 5, 6),
        (0x7c3, 12, 1, 5, 0),
        (0x7d8, 0, 1, 5, 6),
        (0x7c4, A+1, 1, 5, 0),
        (0x7c2, 0, 1, 5, 0),
    ]
    for index, (number, value, funct, rs, rd) in enumerate(negatives):
        code.extend(li(18, OUTPUT + 0x400 + index*16) + li(5, value) + li(6, 0x53aa5aa5))
        instruction = csr(number, rs, funct, rd)
        faults.append((BASE + len(code)*4, instruction))
        code.extend([instruction, store(6, 18, 12)])
    code.extend([store(9, 8, 0x120)])
    for offset, number in enumerate((0x7c0, 0x7c6, 0x7c1)):
        code.extend([csr(number, 0, 2, 6), store(6, 8, 0x124 + offset*4)])
    stage(9)
    code.append(0x6f)
    handler = [csr(0x342, 0, 2, 10), store(10, 18, 0),
               csr(0x341, 0, 2, 10), store(10, 18, 4),
               csr(0x343, 0, 2, 11), store(11, 18, 8),
               0x00450513, csr(0x341, 10), 0x00148493, 0x30200073]
    assert len(code)*4 < VECTOR-BASE
    pack = lambda words: struct.pack('<'+'I'*len(words), *words)
    return code, [(BASE, pack(code)), (VECTOR, pack(handler))], faults


def run(qemu):
    code, regions, faults = program()
    rounds = []
    with session(qemu, 'ws53,flashboot-handoff=on', BASE, PROFILE,
                 options=('-global','ws53-wdt.assume-timing=on')) as (execute, bus, diagnostics):
        def get(name):
            return execute('qom-get', {'path':'/machine', 'property':name})
        def read(address):
            return int(bus(f'readl {address:#x}')[0], 0)
        def patch(address, word):
            value = address << 32 | word
            execute('qom-set', {'path':'/machine','property':'icache-debug-patch','value':value})
            assert get('icache-debug-patch') == value
            assert read(address) == word
        def load_return(address, value):
            patch(address, (value << 20) | 0x513)
            patch(address+4, 0x00008067)
        def inputs(version):
            load_return(A,version)
            load_return(B,version+9)
            # C.LI a0,(19+version); C.JR ra.
            patch(C,0x80820000 | 0x4501 | ((19+version) << 2))
            # HiSilicon 48-bit l.li a0,imm32; C.JR ra.
            immediate = D_VALUES[version-1]
            patch(D,((immediate & 0xffff) << 16) | (10 << 7) | 0x1f)
            patch(D+4,0x80820000 | (immediate >> 16))
        counters = ('icache-valid-lines','icache-translation-fetches','icache-translation-hits',
                    'icache-line-fills','icache-prefetch-fills','icache-invalidations','icache-fence-i')
        for reset_round in range(2):
            execute('system_reset')
            assert not execute('query-status')['running']
            assert get('cache-state-profile') == PROFILE
            assert get('cache-csr-rejections') == reset_round*10
            assert get('cache-csr-epoch-rejections') == 0
            assert all(get(name) == 0 for name in counters)
            bus(f'write {OUTPUT:#x} {0x600} 0x'+bytes(0x600).hex())
            for address, raw in regions:
                bus(f'write {address:#x} {len(raw)} 0x{raw.hex()}')
            inputs(1)
            execute('qom-set',{'path':'/machine','property':'cache-state-profile','value':'unknown'},STARTUP_ONLY)
            assert get('cache-state-profile') == PROFILE
            previous_patch = get('icache-debug-patch')
            for bad in (BASE, A+1, 0x3ffffc, 0x800000):
                execute('qom-set', {'path':'/machine','property':'icache-debug-patch','value':bad << 32},
                        'ws53-cache: debug patch requires stopped CPU and aligned Flash address')
                assert get('icache-debug-patch') == previous_patch
                assert read(A) == (1 << 20)|0x513
            execute('cont')
            stages = []
            for stage in range(1, 10):
                deadline = time.monotonic() + 5
                while read(OUTPUT+0x100) != stage:
                    if time.monotonic() > deadline:
                        execute('stop')
                        raise AssertionError(('stage timeout',stage,read(OUTPUT+0x100),execute('human-monitor-command',{'command-line':'info registers'})))
                    time.sleep(.002)
                if stage == 1:
                    assert execute('query-status')['running']
                    execute('qom-set', {'path':'/machine','property':'icache-debug-patch','value':A << 32},
                            'ws53-cache: debug patch requires stopped CPU and aligned Flash address')
                    assert read(A) == (1 << 20)|0x513
                execute('stop')
                assert not execute('query-status')['running']
                snapshot = {name:get(name) for name in counters}
                if stage <= 8:
                    actual = tuple(read(OUTPUT+(stage-1)*16+i*4) for i in range(4))
                    expected = [(1,10,20,D_VALUES[0]),(1,10,20,D_VALUES[0]),
                                (2,11,21,D_VALUES[1]),(3,12,22,D_VALUES[2]),
                                (4,13,23,D_VALUES[3]),(3,12,22,D_VALUES[2]),
                                (5,12,22,D_VALUES[2]),(5,14,24,D_VALUES[4])][stage-1]
                    assert actual == expected, (reset_round,stage,actual,expected,snapshot)
                    stages.append(dict(stage=stage,executed=list(actual),counters=snapshot))
                if stage in (1,3,4,5):
                    inputs({1:2,3:3,4:4,5:5}[stage])
                if stage == 1:
                    assert [read(OUTPUT+x) for x in (0x110,0x114,0x118)] == [7,7,0]
                if stage == 9:
                    break
                bus(f'writel {OUTPUT+0x104:#x} {stage}')
                execute('cont')
            first = stages[0]['counters']
            assert first['icache-prefetch-fills'] > 0 and first['icache-valid-lines'] > 0
            assert read(OUTPUT+0x120) == 10
            assert [read(OUTPUT+x) for x in (0x124,0x128,0x12c)] == [7,7,0]
            for index,(pc,instruction) in enumerate(faults):
                actual = tuple(read(OUTPUT+0x400+index*16+n*4) for n in range(4))
                assert actual == (2,pc,instruction,0x53aa5aa5), (index,actual,pc,instruction)
            assert get('cache-csr-epoch-rejections') == 10 and get('cache-csr-tainted')
            assert get('cache-csr-rejections') == (reset_round+1)*10
            rounds.append(dict(reset_round=reset_round,stages=stages,precise_exceptions=10,
                               fault_rd_preserved=True,control_and_data_cache_unchanged=True))
        execute('system_reset')
        assert get('cache-csr-rejections') == 20 and get('cache-csr-epoch-rejections') == 0
        assert not get('cache-csr-tainted') and all(get(name) == 0 for name in counters)
    assert diagnostics[:2] == [
        'qemu-system-riscv32: ws53-cache: instruction experiment; synthetic initial state, direct-mapped Flash lines, no timing/D-cache/drain proof',
        'ws53-wdt: assumed timing low-byte=255 expiry=count+1 stage=reload clock=32000'], diagnostics
    reasons = ['instruction-command-requires-direct-write']*2 + [
        'unsupported-instruction-size-transition','unsupported-instruction-prefetch-bits',
        'unsupported-instruction-size-transition','data-cache-change-not-modeled',
        'maintenance-not-modeled','memory-attribute-change-not-modeled',
        'unsupported-instruction-line-address','unsupported-instruction-command']
    assert len(diagnostics) == 22, diagnostics
    for row, reason in zip(diagnostics[2:],reasons*2):
        assert row.startswith('ws53-cache: unsupported csr=') and f'profile={PROFILE} reason={reason} (' in row, row
    rejected = subprocess.run([str(qemu),'-M','ws53,cache-state-profile='+PROFILE,
        '-S','-display','none','-monitor','none','-serial','null'],capture_output=True,timeout=5)
    assert rejected.returncode == 1 and not rejected.stdout
    assert rejected.stderr.decode().splitlines() == [
        'qemu-system-riscv32: ws53-cache: instruction-experiment requires flashboot-handoff=on']
    here = Path(__file__).resolve().parents[1]
    return dict(passed=True,qemu_sha256=hashlib.sha256(Path(qemu).read_bytes()).hexdigest(),
                source_sha256={name:hashlib.sha256((here/name).read_bytes()).hexdigest() for name in (
                    'scripts/ws53_instruction_cache_test.py','scripts/ws53_cache_state_test.py',
                    'scripts/ws53_branch_immediate_test.py','scripts/hisi_priority_test.py',
                    'src/hw/riscv/ws53_instruction_cache.c.inc','src/hw/riscv/ws53_cache_state.c.inc',
                    'src/hw/riscv/ws53.c','patches/v10.0.0/0011-target-riscv-instruction-visibility.patch')},
                instruction_widths=[16,32,48],cross_page_and_line_48bit=True,
                startup_missing_flash_profile_refused=True,startup_profile_qmp_changes_refused=2,
                rounds=rounds,host_debug_patch_refusals=10,hardware_accessed=False,physical_cache_geometry_verified=False,
                physical_maintenance_completion_verified=False,dma_safety_qualified=False,
                scope='explicit synthetic Flash instruction visibility experiment, not silicon timing')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu',required=True)
    args = parser.parse_args()
    print(json.dumps(run(Path(args.qemu).resolve()),indent=2))
