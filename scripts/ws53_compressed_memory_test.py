#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Vendor-assembled xlinx compressed byte/halfword memory regression.

Sweep all eight base/data registers and all 32 legal offsets per instruction.
Execute vendor-verified encodings in QEMU with full memory/guard comparisons.
Ordinary regression needs neither ROM nor SDK. --tool-prefix independently
regenerates the complete frozen encoding corpus with the local vendor assembler.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
from ws53_branch_immediate_test import execute_ram, li, load, store, branch

CODE, FUNCTIONS, TABLE, CELL, DONE = 0x28000, 0x20000, 0x20010000, 0x20041004, 0x2005fff0
BASELINE = b'\xa5'*4 + bytes((0x81+i*13) & 255 for i in range(64)) + b'\x5a'*4
STRIDE, BATCH = 80, 1024
CORPUS_SHA256 = '556cc5e93d374932cae66a1853e6a16183943f7a762040dc6cc53e93911f019a'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def opcode(is_store, half, rd, rs1, offset):
    # Independent vendor-assembler round trip checks this explanation before it
    # can be used to make a passing TCG claim. Halfword bit12 is byte offset bit5.
    high = (offset >> 5) & 1 if half else offset & 1
    return ((0xa000 if is_store else 0x2000) | (2 if half else 0) | ((rd-8) << 2) |
            ((rs1-8) << 7) | (high << 12) | ((offset & 2) << 4) |
            ((offset & 4) << 4) | ((offset & 8) << 7) | ((offset & 16) << 7))


def make_cases():
    cases, lines = [], ['.text', '.option rvc', '.option norelax']
    for mnemonic, is_store, half in (('lbu', False, False), ('lhu', False, True),
                                      ('sb', True, False), ('sh', True, True)):
        for rd in range(8, 16):
            for rs1 in range(8, 16):
                for index in range(32):
                    off = index*2 if half else index
                    cases.append(dict(op=mnemonic, store=is_store, half=half, rd=rd, rs1=rs1, offset=off))
                    lines.append(f'{mnemonic} x{rd},{off}(x{rs1})')
    for case in cases:
        case['instruction'] = opcode(case['store'], case['half'], case['rd'], case['rs1'], case['offset'])
    raw = b''.join(struct.pack('<H', case['instruction']) for case in cases)
    assert hashlib.sha256(raw).hexdigest() == CORPUS_SHA256, 'frozen vendor encoding corpus changed'
    return cases, lines, raw


def corpus(prefix, output):
    cases, lines, raw = make_cases()
    assembly, obj, binary = (output/name for name in ('corpus.s', 'corpus.o', 'corpus.bin'))
    assembly.write_text('\n'.join(lines)+'\n')
    if prefix is not None:
        subprocess.run([str(prefix)+'as', '-march=rv32imfc_xlinxma1p0_xlinxmb1p0_xlinxmc1p0',
                        '-enable-c-lbu-sb', '-enable-c-lhu-sh', str(assembly), '-o', str(obj)], check=True)
        subprocess.run([str(prefix)+'objcopy', '-O', 'binary', '-j', '.text', str(obj), str(binary)], check=True)
        assert binary.read_bytes() == raw, 'vendor assembler differs from frozen encodings'
    else:
        binary.write_bytes(raw)
    (output/'cases.json').write_text(json.dumps(cases, indent=2)+'\n')
    return cases


def execute_batch(qemu, cases):
    functions, records = bytearray(), bytearray()
    for case in cases:
        pointer = FUNCTIONS+len(functions)
        words = li(case['rd'], 0xa1b2c3d4) if case['store'] else []
        words += li(case['rs1'], CELL)
        functions += struct.pack('<'+'I'*len(words), *words)
        functions += struct.pack('<H', case['instruction'])
        functions += struct.pack('<II', (case['rd'] << 15) | (10 << 7) | 0x13, 0x00008067)
        record = bytearray(b'\xa5'*STRIDE)
        struct.pack_into('<I', record, 0, pointer)
        records += record
    assert FUNCTIONS+len(functions) <= CODE
    code = li(20, TABLE)+li(21, TABLE+len(records))
    loop = len(code)*4
    code += li(5, CELL-4)
    for off in range(0, len(BASELINE), 4):
        code += li(6, struct.unpack_from('<I', BASELINE, off)[0])+[store(6, 5, off)]
    code += [load(5, 20, 0), 0x000280e7, store(10, 20, 4)]
    code += li(5, CELL-4)
    for off in range(0, len(BASELINE), 4):
        code += [load(6, 5, off), store(6, 20, 8+off)]
    code += [(STRIDE << 20) | (20 << 15) | (20 << 7) | 0x13]
    code += [branch(20, 21, 6, loop-len(code)*4)]
    code += li(5, DONE)+[0x00100313, store(6, 5, 0), 0x0000006f]
    actual = execute_ram(str(qemu.resolve()), ((CODE, struct.pack('<'+'I'*len(code), *code)),
                         (FUNCTIONS, functions), (TABLE, records)), TABLE, len(records), DONE)
    failures = []
    for i, case in enumerate(cases):
        row = actual[i*STRIDE:(i+1)*STRIDE]
        expected = bytearray(BASELINE)
        width = 2 if case['half'] else 1
        offset = case['offset']+4
        if case['store']:
            value = CELL if case['rd'] == case['rs1'] else 0xa1b2c3d4
            expected[offset:offset+width] = value.to_bytes(4, 'little')[:width]
        else:
            value = int.from_bytes(expected[offset:offset+width], 'little')
        good = row[:4] == records[i*STRIDE:i*STRIDE+4] and row[8:] == expected
        good &= struct.unpack_from('<I', row, 4)[0] == value
        if not good:
            failures.append(dict(case=case, actual_result=struct.unpack_from('<I', row, 4)[0],
                                 expected_result=value, actual_memory=row[8:].hex(), expected_memory=expected.hex()))
    return actual, failures


def run(qemu):
    """SDK-free aggregate test; frozen encodings were checked by vendor assembly."""
    cases, _, _ = make_cases()
    failures = []
    for start in range(0, len(cases), BATCH):
        _, bad = execute_batch(Path(qemu), cases[start:start+BATCH])
        failures.extend(bad)
    assert not failures, {'failed': len(failures), 'examples': failures[:4]}
    return dict(passed=True, cases=len(cases), encoding_corpus_sha256=CORPUS_SHA256,
                full_memory_and_guards_checked=True, hardware_accessed=False)


def main(args):
    assert not args.output.exists(), 'use a new output directory'
    args.output.mkdir(parents=True)
    cases = corpus(args.tool_prefix, args.output)
    failures = []
    for start in range(0, len(cases), BATCH):
        actual, bad = execute_batch(args.qemu, cases[start:start+BATCH])
        (args.output/f'raw-{start//BATCH:02}.bin').write_bytes(actual)
        failures.extend(bad)
    (args.output/'failures.json').write_text(json.dumps(failures, indent=2)+'\n')
    report = dict(passed=not failures, cases=len(cases), failures=len(failures),
                  all_base_and_data_registers=True, aliasing_base_data_registers=True,
                  load_zero_extension_checked=True, full_memory_and_guards_checked=True,
                  vendor_assembler=str(args.tool_prefix)+'as' if args.tool_prefix else None,
                  vendor_assembler_sha256=sha(Path(str(args.tool_prefix)+'as')) if args.tool_prefix else None,
                  vendor_reassembled_this_run=args.tool_prefix is not None,
                  assembly_sha256=sha(args.output/'corpus.s'), opcodes_sha256=sha(args.output/'corpus.bin'),
                  qemu_sha256=sha(args.qemu), script_sha256=sha(Path(__file__)),
                  executor_sha256=sha(Path(__file__).with_name('ws53_branch_immediate_test.py')),
                  hardware_accessed=False)
    (args.output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('qemu', 'output'):
        parser.add_argument('--'+option, type=Path, required=True)
    parser.add_argument('--tool-prefix', type=Path)
    main(parser.parse_args())
