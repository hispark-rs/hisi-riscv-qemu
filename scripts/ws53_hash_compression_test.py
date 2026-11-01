#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Check actual model compression against hashlib, not MMIO or hardware."""
import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import random
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / 'src/hw/riscv/ws53_hash_compress.h'
PROFILES = {
    'sha1': (10, 0, 64, 20, '67452301efcdab8998badcfe10325476c3d2e1f0'),
    'sha224': (11, 0, 64, 28, 'c1059ed8367cd5073070dd17f70e5939ffc00b316858151164f98fa7befa4fa4'),
    'sha256': (11, 1, 64, 32, '6a09e667bb67ae853c6ef372a54ff53a510e527f9b05688c1f83d9ab5be0cd19'),
    'sha384': (11, 2, 128, 48, 'cbbb9d5dc1059ed8629a292a367cd5079159015a3070dd17152fecd8f70e593967332667ffc00b318eb44a8768581511db0c2e0d64f98fa747b5481dbefa4fa4'),
    'sha512': (11, 3, 128, 64, '6a09e667f3bcc908bb67ae8584caa73b3c6ef372fe94f82ba54ff53a5f1d36f1510e527fade682d19b05688c2b3e6c1f1f83d9abfb41bd6b5be0cd19137e2179'),
    'sm3': (12, 0, 64, 32, '7380166f4914b2b9172442d7da8a0600a96f30bc163138aae38dee4db0fb0e4e'),
}


def constants():
    primes = []
    n = 2
    while len(primes) < 80:
        if all(n % p for p in primes if p*p <= n):
            primes.append(n)
        n += 1
    values = []
    for p in primes:
        # Integer cube root of p*2^192, exact floor without float precision.
        value = p << 192
        lo, hi = 0, 1 << ((value.bit_length()+2)//3)
        while lo+1 < hi:
            mid = (lo+hi)//2
            if mid**3 <= value:
                lo = mid
            else:
                hi = mid
        assert lo**3 <= value < (lo+1)**3
        values.append(lo & ((1 << 64)-1))
    return values


def pad(data, block):
    suffix = 16 if block == 128 else 8
    zeros = (-len(data)-1-suffix) % block
    return data + b'\x80' + bytes(zeros) + (len(data)*8).to_bytes(suffix, 'big')


def initial(profile):
    iv = bytes.fromhex(profile[4])
    return (ctypes.c_uint32*16)(*(int.from_bytes(iv[i:i+4], 'little') for i in range(0, len(iv), 4)))


def run(write_constants=False):
    text = HEADER.read_text()
    expected = constants()
    if write_constants:
        assert text.count('WS53_HASH_K_CONSTANTS') == 1
        rows = [', '.join(f'UINT64_C(0x{v:016x})' for v in expected[i:i+3]) for i in range(0, 80, 3)]
        HEADER.write_text(text.replace('WS53_HASH_K_CONSTANTS', ',\n    '.join(rows)))
        text = HEADER.read_text()
    assert [int(x, 16) for x in re.findall(r'UINT64_C\(0x([0-9a-f]{16})\)', text)] == expected
    assert expected[0] == 0x428a2f98d728ae22 and expected[-1] == 0x6c44198c4a475817
    counts = dict(digest_cases=0, split_cases=0, changed_state_cases=0, rejected_calls=0)
    with tempfile.TemporaryDirectory(prefix='ws53-hash-compress-') as temporary:
        directory = Path(temporary)
        (directory / 'wrapper.c').write_text('''#include "ws53_hash_compress.h"
int compress(unsigned a,unsigned m,uint32_t *s,const uint8_t *p,size_t n) {
 return ws53_hash_compress(a,m,s,p,n);
}
''')
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-fPIC', '-shared',
                        '-fsanitize=undefined', '-fsanitize-undefined-trap-on-error',
                        '-I', str(HEADER.parent), str(directory/'wrapper.c'), '-o', str(directory/'model.so')], check=True)
        lib = ctypes.CDLL(str(directory/'model.so'))
        fn = lib.compress
        fn.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint32), ctypes.c_char_p, ctypes.c_size_t]
        fn.restype = ctypes.c_int
        rng = random.Random(0x57533533)
        for name, profile in PROFILES.items():
            algorithm, mode, block, result, _ = profile
            # Empty, exact blocks, both padding thresholds, random data and a
            # million-byte vector exercise all rounds and state chaining.
            messages = [b'abc', b'abcd'*16, b'a'*1000000]
            messages += [rng.randbytes(n) for n in [*range(0, 301), 511, 512, 513, 1023, 1024, 1025, 65535]]
            for data in messages:
                encoded = pad(data, block)
                state = initial(profile)
                assert fn(algorithm, mode, state, encoded, len(encoded)) == 1
                output = b''.join(word.to_bytes(4, 'little') for word in state)
                assert output[:result] == hashlib.new(name, data).digest(), (name, len(data))
                counts['digest_cases'] += 1
                if len(data) < 1100:
                    split = initial(profile)
                    for i in range(0, len(encoded), block):
                        assert fn(algorithm, mode, split, encoded[i:i+block], block) == 1
                    assert list(split) == list(state)
                    counts['split_cases'] += 1
            for _ in range(12):
                state = (ctypes.c_uint32*16)(*(rng.getrandbits(32) for _ in range(16)))
                split = (ctypes.c_uint32*16)(*state)
                data = rng.randbytes(block*3)
                before = list(state)
                assert fn(algorithm, mode, state, data, len(data)) == 1
                for i in range(0, len(data), block):
                    assert fn(algorithm, mode, split, data[i:i+block], block) == 1
                assert list(state) == list(split) and list(state) != before
                counts['changed_state_cases'] += 1
            for length in (0, 1, block-1, block+1):
                state = initial(profile)
                before = list(state)
                assert fn(algorithm, mode, state, bytes(block*2), length) == 0
                assert list(state) == before
                counts['rejected_calls'] += 1
        for algorithm in range(16):
            for mode in range(16):
                if (algorithm, mode) in {(p[0],p[1]) for p in PROFILES.values()}:
                    continue
                state = initial(PROFILES['sha512'])
                before = list(state)
                assert fn(algorithm, mode, state, bytes(128), 128) == 0 and list(state) == before
                counts['rejected_calls'] += 1
    # Published SM3 examples, independent of the generated/random test corpus.
    assert hashlib.new('sm3', b'abc').hexdigest() == '66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0'
    assert hashlib.new('sm3', b'abcd'*16).hexdigest() == 'debe9ff92275b8a138604889c18e5a4d6fdb70e5387e5765293dcba39c0c5732'
    return dict(passed=True, **counts, exact_generated_round_constants=80,
                header_sha256=hashlib.sha256(HEADER.read_bytes()).hexdigest(), ubsan_trap=True,
                qemu_executed=False, hardware_executed=False,
                limitation='Compression only: no register, descriptor, interrupt, clock or DMA timing model checked')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-constants', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.write_constants), indent=2))
