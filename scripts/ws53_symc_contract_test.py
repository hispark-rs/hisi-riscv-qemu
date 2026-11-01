#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["gmssl==3.2.2", "pycryptodomex==3.23.0"]
# ///
"""Adversarial SYMC profiles and concurrent HASH/SYMC IRQ59 ownership."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
from ws53_gpio_test import running
from ws53_hash_test import Hash, PROFILE as HASH_PROFILE, NOTICE as HASH_NOTICE
from ws53_hash_compression_test import pad
from ws53_symc_test import Symc, BASE, PROFILE, NOTICES
from ws53_symc_reference import reference


def diagnostics_count(messages, count):
    lines = messages[0].splitlines()
    assert lines[:2] == NOTICES, lines
    assert len(lines[2:]) == count, lines
    assert all(line.startswith('ws53-symc: unimplemented/unavailable') for line in lines[2:]), lines


def unchanged(s, ch, length=32):
    assert s.get('pending-reads') == s.get('pending-writes') == 0
    assert not s.read(0xc000) & (1 << ch)
    assert s.bytes(s.addresses(ch)[1], length) == b'X'*length


def contract_test(qemu):
    result = dict(configurations=[], descriptors=[], permissions=[], runtime_loss=[],
                  invalid_accesses=0, submission_order=0, shared_irq=0, late_reuse=0)
    for ch in (1, 2):
        with running(qemu, False, PROFILE) as (cmd, irq, reset, messages):
            s = Symc(cmd)
            s.load(0, 2, b'K'*16)
            i, o = s.bank(ch), s.bank(ch, True)
            key = (2 << 16) | (3 << 20) | (1 << 24)
            configurations = [('disabled', i, 0xaa00), ('input-security', i, 0x8000a500),
                ('output-security', i, 0x80005a00), ('copy-enabled', i+4, 1), ('copy-other', i+4, 2)]
            configurations += [(f'algorithm-{a}', i+0x10, (key & ~(15 << 16)) | (a << 16)) for a in (0, 1, 3, 4, 15)]
            configurations += [(f'mode-{m}', i+0x10, (key & ~(15 << 20)) | (m << 20)) for m in (0, 2, 4, 5, 9, 15)]
            configurations += [('no-key-size', i+0x10, key & ~(3 << 24))]
            configurations += [(f'sm4-size-{n}', i+0x10, (5 << 16) | (3 << 20) | (n << 24)) for n in (2, 3)]
            configurations += [(f'width-{n}', i+0x10, key | (n << 26)) for n in (1, 2, 3)]
            for output, bank in ((False, i), (True, o)):
                configurations += [(f'{output}-high', bank+0x20, 1)]
                configurations += [(f'{output}-base-{a:x}', bank+0x24, a) for a in (0x2005e001, 0x20000000, 0x2005ffe0 if not output else 0x20060000, 0xffffffe0)]
                configurations += [(f'{output}-depth-{n}', bank+(0x28 if output else 0x2c), n) for n in (0, 1, 3, 255)]
            configurations += [('overlapping-tables', o+0x24, s.addresses(ch)[2]+32)]
            for name, offset, value in configurations:
                s.configure(ch, 0, 2, 1, 3)
                s.descriptors(ch, b'A'*32, bytes(16))
                s.write(offset, value)
                before = s.get('rejections')
                s.submit(ch); s.step(100000)
                assert s.get('rejections') == before+2, name
                unchanged(s, ch)
                result['configurations'].append(f'{ch}:{name}')
            rejects = s.get('rejections')
        diagnostics_count(messages, rejects)

        with running(qemu, False, PROFILE) as (cmd, irq, reset, messages):
            s = Symc(cmd); s.load(0, 2, b'K'*16)
            source, dest, table = s.addresses(ch)
            malformed = [(f'flags-{n:x}', 0, n) for n in (0, 1, 2, 7, 0x103, 0x403)]
            malformed += [(f'length-{n:x}', 4, n) for n in (0, 1, 15, 17, 0x50010, 0xfffffff0)]
            malformed += [('input-high', 12, 1), ('output-high', 76, 1), ('output-flags', 64, 3), ('unequal-length', 68, 16)]
            for off, name in ((8, 'source'), (72, 'dest')):
                malformed += [(f'{name}-{a:x}', off, a) for a in (source+1, 0x20000000, 0x20060000, 0xffffffe0, table, table+64)]
            malformed += [('in-place', 72, source), ('source-inside-output', 8, dest)]
            for name, offset, value in malformed:
                s.configure(ch, 0, 2, 1, 3)
                raw = bytearray(s.descriptors(ch, b'A'*32, bytes(16)))
                struct.pack_into('<I', raw, offset, value)
                s.memory(table, raw)
                before = s.get('rejections')
                s.submit(ch); s.step(100000)
                assert s.get('rejections') == before+1, name
                unchanged(s, ch)
                assert s.bytes(table, 96) == raw
                result['descriptors'].append(f'{ch}:{name}')
            rejects = s.get('rejections')
        diagnostics_count(messages, rejects)

        # Loaded bytes alone do not authorize an engine, direction, half, or slot.
        permissions = ('no-key', 'wrong-slot', 'hmac-namespace', 'decrypt-only', 'encrypt-only',
                       'wrong-engine', 'wrong-size', 'odd-half-missing', 'odd-long-key',
                       'incomplete-long-key', 'flushed-key', 'foreign-owner',
                       'km-clock', 'loader-error', 'controller-error')
        for name in permissions:
            with running(qemu, False, PROFILE) as (cmd, irq, reset, messages):
                s = Symc(cmd)
                size, parity, decrypt = 1, 0, name == 'encrypt-only'
                if name != 'no-key':
                    s.load(0, 5 if name == 'wrong-engine' else 2,
                           b'K'*(32 if name == 'odd-long-key' else 16),
                           usage=1 if name == 'encrypt-only' else 2 if name == 'decrypt-only' else 3)
                if name in ('wrong-size', 'odd-long-key', 'incomplete-long-key'): size = 3
                if name in ('odd-half-missing', 'odd-long-key'): parity = 1
                if name == 'incomplete-long-key':
                    s.km.stage(0, b'K'*16, size=3); s.km.step(); s.km.write(0x1044, 0x4000)
                if name in ('flushed-key', 'foreign-owner'):
                    s.km.release(0); s.km.step()
                    if name == 'foreign-owner': s.km.claim(0, 2)
                if name == 'km-clock': s.km.clock(False)
                if name.endswith('-error'): s.km.inject(name, 1)
                slot = 1 if name == 'wrong-slot' else 0x100 if name == 'hmac-namespace' else 0
                s.configure(ch, slot, 2, size, 3, decrypt)
                s.descriptors(ch, b'A'*32, bytes(16), parity)
                s.submit(ch); s.step(100000)
                assert s.get('rejections') == 1, name
                unchanged(s, ch)
                result['permissions'].append(f'{ch}:{name}')
            diagnostics_count(messages, 1)

        # Loss after an input bus read was accepted must prevent computation.
        for name in ('owner', 'km-clock', 'key-flush', 'ree-cause', 'tee-cause', 'loader-error'):
            with running(qemu, False, PROFILE) as (cmd, irq, reset, messages):
                s = Symc(cmd); s.load(0, 2, b'K'*16); s.configure(ch, 0, 2, 1, 3)
                s.descriptors(ch, b'A'*32, bytes(16)); s.submit(ch); s.step(1000)
                assert s.get('pending-reads') == 1
                if name == 'owner': s.fault(ch, 6)
                elif name == 'km-clock': s.km.clock(False)
                elif name == 'key-flush':
                    s.fault(ch, 8); s.km.release(0); s.km.step(); s.fault(ch, 8, False)
                elif name == 'loader-error': s.km.inject(name, 1)
                else: cmd.qmp('qom-set', dict(path='/machine/symc', property='inject-'+name, value=1))
                s.step(100000); assert s.get('rejections') == 1, name
                assert s.get('bytes-read') == 16 and s.get('bytes-written') == 0
                unchanged(s, ch)
                result['runtime_loss'].append(f'{ch}:{name}')
            diagnostics_count(messages, 1)

        with running(qemu, False, PROFILE) as (cmd, irq, reset, messages):
            s = Symc(cmd); s.load(0, 2, b'K'*16)
            for first in ('input', 'output-twice', 'wrong-pointer', 'mutate', 'unlock'):
                s.configure(ch, 0, 2, 1, 3); s.descriptors(ch, b'A'*32, bytes(16))
                before = s.get('rejections'); i, o = s.bank(ch), s.bank(ch, True)
                if first == 'input': s.write(i+0x30, 1)
                elif first == 'wrong-pointer': s.write(o+0x30, 2)
                else:
                    s.write(o+0x30, 1)
                    if first == 'output-twice': s.write(o+0x30, 1)
                    elif first == 'mutate': s.write(i+0x10, 0)
                    else:
                        s.write(0x20, 0); assert s.read(0x20) == 1 << (4*ch)
                assert s.get('rejections') == before+(first != 'unlock'), first
                assert s.read(i+0x30) == 0
                unchanged(s, ch)
                # The failed operation must not poison an otherwise valid ring.
                if first in ('input', 'wrong-pointer'): s.write(o+0x30, 1)
                s.write(i+0x30, 1); s.step(100000)
                assert s.get('transfers') == result['submission_order'] % 5+1
                result['submission_order'] += 1
            rejects = s.get('rejections')
        diagnostics_count(messages, rejects)

    with running(qemu, False, PROFILE) as (cmd, irq, reset, messages):
        s = Symc(cmd)
        commands = [f'{op} {BASE+off:#x}'+(' 0' if op.startswith('write') else '')
                    for op in ('readb', 'readw', 'readq', 'writeb', 'writew', 'writeq')
                    for off in (0x20, 0x4100, 0xc000, 0xd000)]
        commands += [f'writel {BASE+off:#x} 1' for off in (0x8060, 0x8064, 0x8070, 0x8074, 0x9080, 0x4108, 0xd034)]
        commands += [f'readl {BASE+off:#x}' for off in (0x4108, 0x4101, 0x9080+16, 0xd02c)]
        commands += [f'writel {BASE+off:#x} 1' for off in (8, 0x60, 0x8610, 0xc000)]
        for text in commands:
            before = s.get('rejections'); cmd(text)
            assert s.get('rejections') == before+1, text
        result['invalid_accesses'] = len(commands)
    diagnostics_count(messages, len(commands))

    result['shared_irq'] = shared_irq_test(qemu)
    result['late_reuse'] = late_reuse_test(qemu)
    result['passed'] = True
    return result


def shared_irq_test(qemu):
    count = 0
    for order in ('hash-first', 'symc-first'):
        with running(qemu, False, PROFILE+HASH_PROFILE) as (cmd, irq, reset, messages):
            s, h = Symc(cmd), Hash(cmd)
            # Keep all four channels' data and descriptors independent.
            h.addresses = lambda ch: (0x20030000+ch*0x1000, 0x2003f000+ch*32)
            cmd('irq_intercept_in /machine/intc')
            for ch in (1, 2):
                s.load(ch-1, 2, bytes([ch])*16)
                s.configure(ch, ch-1, 2, 1, 3)
                s.descriptors(ch, b'A'*32, bytes(16))
                h.configure(ch, 'sha256'); h.input(ch, pad(b'abc', 64))
            h.write(0x8604, 6)
            for ch in (1, 2): s.submit(ch); h.write(h.ch(ch, 0x30), 1)
            s.step(100000)
            assert h.read(0x8600) == s.read(0x8610) == s.read(0xc000) == 6 and irq[59]
            for ch in (1, 2):
                assert h.output(ch, 32) == hashlib.sha256(b'abc').digest()
                assert s.bytes(s.addresses(ch)[1], 32) == reference(2, bytes([ch])*16, 3, b'A'*32, bytes(16))[0]
            h.write(4, 0); assert not irq[59]
            h.write(4, 1); assert irq[59]
            # Neither local clock/mask nor another engine's ACK consumes status.
            s.clock(False); assert irq[59]
            h.write(0x8604, 0); assert not irq[59]
            s.clock(True); assert irq[59]
            h.write(0x8604, 6)
            s.write(0x8614, 0); s.write(0xc004, 0); assert irq[59]
            s.write(0x8614, 6); s.write(0xc004, 6)
            acks = [(0x8600, 2), (0x8600, 4)]
            symc_acks = [(off, bit) for off in (0x8610, 0xc000) for bit in (2, 4)]
            acks = acks+symc_acks if order == 'hash-first' else symc_acks+acks
            for index, (off, bit) in enumerate(acks):
                h.write(off, bit)
                assert irq[59] == (index != len(acks)-1), (order, index)
            assert h.read(0x8600) == s.read(0x8610) == s.read(0xc000) == 0
            reset(); s.step(100000); assert not irq[59]
            assert h.count('rejections') == s.get('rejections') == 0
            count += 1
        assert messages[0].splitlines() == NOTICES+[HASH_NOTICE], messages
    return count


def late_reuse_test(qemu):
    count = 0
    for ch in (1, 2):
        for writing in (False, True):
            with running(qemu, False, PROFILE) as (cmd, irq, reset, messages):
                s = Symc(cmd); s.load(0, 2, b'K'*16); s.configure(ch, 0, 2, 1, 1)
                s.descriptors(ch, b'A'*32, bytes(16)); s.submit(ch); s.step(3000 if writing else 1000)
                s.fault(ch, 8); s.write(0x60, 1 << ch); s.step(5000)
                before = s.get('rejections')
                s.write(s.bank(ch)+0x24, 0x20010000)
                s.write(s.bank(ch, True)+0x30, 1)
                assert s.get('rejections') == before+2
                assert s.get('pending-writes' if writing else 'pending-reads') == 1
                s.fault(ch, 8, False); s.step(1000)
                assert s.get('writes-after-reset' if writing else 'reads-after-reset') == 1
                # Only after the synthetic bus retired is fresh ownership safe.
                s.configure(ch, 0, 2, 1, 1)
                descriptors = s.descriptors(ch, b'B'*32, bytes(16)); s.submit(ch); s.step(100000)
                expected, iv = reference(2, b'K'*16, 1, b'B'*32, bytes(16))
                s.verify(ch, b'B'*32, descriptors, expected, iv)
                count += 1
            diagnostics_count(messages, 2)
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = contract_test(str(args.qemu.resolve()))
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({k: len(v) if isinstance(v, list) else v for k, v in report.items()}, indent=2))
