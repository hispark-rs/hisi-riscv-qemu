#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""TCG: WS53 ITCM traps stay architectural; legacy ROM stubs still return."""
import argparse
import json
from pathlib import Path
import struct
import time

from hisi_priority_test import csr
from ws53_branch_immediate_test import li, store
from ws53_cache_state_test import POISON, session, sha


def pack(values):
    return struct.pack('<' + 'I' * len(values), *values)


def read(bus, address):
    return int(bus(f'readl {address:#x}')[0], 0)


def block(bus, address, length):
    return bytes.fromhex(bus(f'read {address:#x} {length}')[0].decode().removeprefix('0x'))


def load(rd, rs1, offset=0):
    return (offset << 20) | (rs1 << 15) | (2 << 12) | (rd << 7) | 3


def wait_done(execute, bus, address):
    execute('cont')
    deadline = time.monotonic() + 5
    while read(bus, address) != 1:
        if time.monotonic() >= deadline:
            execute('stop')
            raise AssertionError(('TCG deadline', execute('human-monitor-command',
                {'command-line': 'info registers'})))
        time.sleep(.005)
    execute('stop')
    assert not execute('query-status')['running']


def ws53_itcm(qemu, base):
    output, vector, done = 0x20050000, 0x2f000, 0x20050100
    # The zero encoding is a 16-bit illegal instruction; handler skips its
    # reserved four-byte fixture slot. ffffffff and the CSR each occupy 32 bits.
    instructions = (0, 0xffffffff, csr(0x7c0, 0, 2, 6))
    code = li(5, vector) + [csr(0x305, 5)] + li(9, 0)
    pcs = []
    for index, word in enumerate(instructions):
        code += li(8, output+16+index*32) + li(6, POISON) + li(10, 0x13579bdf) + li(1, 0x2468ace0)
        pcs.append(base+len(code)*4)
        code += [word, store(6, 8, 0), store(10, 8, 16), store(1, 8, 20), store(9, 8, 24)]
    code += li(8, done) + li(6, 1) + [store(6, 8, 0), 0x6f]
    assert base + len(code)*4 <= 0x30000
    handler = [csr(0x342, 0, 2, 12), store(12, 8, 4),
               csr(0x341, 0, 2, 13), store(13, 8, 8),
               csr(0x343, 0, 2, 14), store(14, 8, 12),
               0x00468693, csr(0x341, 13), 0x00148493, 0x30200073]
    rounds = []
    with session(qemu, 'ws53', base) as (execute, bus, diagnostics):
        for reset_round in range(2):
            execute('system_reset')
            for address, raw in ((base, pack(code)), (vector, pack(handler)),
                (output, pack([POISON]*32)), (done, bytes(4))):
                bus(f'write {address:#x} {len(raw)} 0x{raw.hex()}')
            wait_done(execute, bus, done)
            words = struct.unpack('<32I', block(bus, output, 128))
            assert words[:4] == words[-4:] == (POISON,)*4
            actual = []
            for index, (pc, word) in enumerate(zip(pcs, instructions)):
                row = words[4+index*8:12+index*8]
                assert row == (POISON, 2, pc, word, 0x13579bdf, 0x2468ace0, index+1, POISON), row
                actual.append(dict(pc=hex(pc), instruction=hex(word), cause=row[1],
                    mtval=hex(row[3]), a0_unchanged=True, ra_unchanged=True, returned_via_mret=True))
            assert execute('qom-get', {'path':'/machine','property':'cache-csr-rejections'}) == reset_round+1
            assert execute('qom-get', {'path':'/machine','property':'cache-csr-epoch-rejections'}) == 1
            rounds.append(dict(reset_round=reset_round, traps=actual, guards_intact=True))
    assert diagnostics == ['ws53-cache: unsupported csr=0x7c0 op=read value=0x00000000 '
        'mask=0x00000000 profile=unknown reason=unknown-inherited-state '
        '(model limitation, not silicon illegal)']*2, diagnostics
    return dict(machine='ws53', code_base=hex(base), rounds=rounds, exception_count=6,
        legacy_bs21_overlap_verified=True)


def legacy_rom(qemu, machine, base):
    vector, output, done = base+0x7000, base+0x8000, base+0x8100
    source, dest = base+0x9000, base+0x9040
    # Current modeled addresses, not a claim that every BS2X silicon has this ABI.
    memset, memcpy = (0x10b7f6, 0x10aeb4) if machine == 'ws63' else (0x3d1dc, 0x3da4e)
    calls = [('memset', memset, 0x41, 16, 0, b'A'*16),
             ('memcpy', memcpy, source, 16, 0, bytes(range(16))),
             ('memset-reject', memset, 0x42, 17, 0xffffffff, bytes(range(16)))]
    code = li(5, vector) + [csr(0x305, 5)] + li(9, 0)
    for index, (_, entry, third, length, _, _) in enumerate(calls):
        code += li(10, dest) + li(11, 16) + li(12, third) + li(13, length) + li(5, entry)
        code += [0x000280e7]  # jalr ra,0(t0); ROM interception must return here.
        code += li(8, output+16+index*24) + [store(10,8,0), store(9,8,4)] + li(5,dest)
        for offset in range(4):
            code += [load(6,5,offset*4), store(6,8,8+offset*4)]
    code += li(8,done) + li(6,1) + [store(6,8,0),0x6f]
    # Any delivered trap instead of a ROM return is a deterministic failure.
    handler = [0x00148493,0x6f]
    rounds=[]
    with session(qemu,machine,base) as (execute,bus,diagnostics):
        for reset_round in range(2):
            execute('system_reset')
            for address,raw in ((base,pack(code)),(vector,pack(handler)),
                (output,pack([POISON]*26)),(done,bytes(4)),(source,bytes(range(16))),
                (dest-16,pack([POISON]*12))):
                bus(f'write {address:#x} {len(raw)} 0x{raw.hex()}')
            wait_done(execute,bus,done)
            raw=block(bus,output,104)
            assert raw[:16] == raw[-16:] == pack([POISON]*4)
            observations=[]
            for index,(name,entry,_,_,expected_return,expected_bytes) in enumerate(calls):
                actual=raw[16+index*24:40+index*24]
                assert actual == pack([expected_return,0])+expected_bytes,(machine,name,actual.hex())
                observations.append(dict(name=name,entry=hex(entry),result=hex(expected_return),
                    destination_hex=expected_bytes.hex(),returned_to_caller=True,unexpected_traps=0))
            assert block(bus,dest-16,16) == block(bus,dest+16,16) == pack([POISON]*4)
            assert block(bus,source,16) == bytes(range(16))
            rounds.append(dict(reset_round=reset_round,calls=observations,guards_intact=True,source_intact=True))
    wanted=['qemu-system-riscv32: warning: nic ws63-netmac.0 has no peer'] if machine=='ws63' else []
    assert diagnostics == wanted, diagnostics
    return dict(machine=machine,rounds=rounds,rom_calls=6,
        scope='existing modeled ROM address/return/memory semantics; not silicon ABI qualification')


def rom_trap_test(qemu):
    qemu=Path(qemu).resolve()
    ws53=[ws53_itcm(qemu,base) for base in (0x20000,0x28000,0x2ff00)]
    legacy=[legacy_rom(qemu,machine,base) for machine,base in
        (('ws63',0xa00000),('bs20',0x100000),('bs21',0x100000),('bs21e',0x100000),('bs22',0x100000))]
    here=Path(__file__).resolve().parent
    return dict(passed=True,qemu_sha256=sha(qemu),
        source_sha256={name:sha(here/name) for name in ('ws53_rom_trap_test.py','ws53_cache_state_test.py',
            'hisi_priority_test.py','ws53_branch_immediate_test.py')},
        ws53=ws53,legacy=legacy,ws53_expected_exceptions=18,legacy_rom_calls=30,
        hardware_accessed=False,dma_safety_qualified=False,
        ws63_hook_ws53_scope='guard statically checked; WS53 has no mapped WS63 ROM range')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu',required=True)
    print(json.dumps(rom_trap_test(parser.parse_args().qemu),indent=2))
