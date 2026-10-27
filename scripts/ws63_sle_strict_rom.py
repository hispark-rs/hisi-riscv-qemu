#!/usr/bin/env python3
"""Opt-in experiment guard for an isolated, already patched QEMU source tree.

Run after build.sh has applied the normal patches, then rebuild QEMU. The guard
only changes behavior when WS63_SLE_STRICT_ROM is set: no ROM illegal instruction
may silently become a successful stub call. Never use on a shared build tree.
"""
from pathlib import Path
import sys

path = Path(sys.argv[1]) / 'target/riscv/cpu_helper.c'
text = path.read_text()
needle = '        ws63_rom_call(env);'
guard = '''        if (getenv("WS63_SLE_STRICT_ROM")) {
            error_report("WS63 strict ROM: illegal instruction at 0x%08x", (unsigned)env->pc);
            abort();
        }
'''
if 'WS63_SLE_STRICT_ROM' not in text:
    assert text.count(needle) == 1, 'Unsupported QEMU source: ROM hook not unique'
    path.write_text(text.replace(needle, guard + needle))
print(path)
