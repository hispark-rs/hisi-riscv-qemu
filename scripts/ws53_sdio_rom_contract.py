#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Optional execution audit of two unmodified WS53 SDIO ROM leaf functions.

Requires the private, already captured ROM slice; never connects to a board.
Only position-independent leaf bytes are relocated into QEMU RAM. This tests
software ABI/validation, not SDIO MMIO, ADMA transfers or hardware behavior.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
from ws53_branch_immediate_test import execute_ram, li, load, store, branch

START, END = 0x1e872, 0x1f1e4
SHA256 = 'a1ac5c03c613112f973b45bdfbfd399d3f3017b315b61d1f6b0d2a07d2ce4671'
CODE, LEAF, DATA, DONE = 0x28000, 0x20000, 0x20010000, 0x2005fff0


def execute(qemu, leaf, records, stride):
    code=li(8,DATA)+li(9,DATA+len(records))+li(18,LEAF)
    loop=len(code)*4
    code += [load(r,8,(r-10)*4) for r in range(10,14)]
    code += [0x000900e7,store(10,8,16), (stride<<20)|(8<<15)|(8<<7)|0x13]
    code += [branch(8,9,6,loop-len(code)*4)]
    code += li(5,DONE)+[0x00100313,store(6,5,0),0x0000006f]
    assert stride<2048 and DATA+len(records)<DONE
    return execute_ram(qemu,((CODE,struct.pack('<'+'I'*len(code),*code)),(LEAF,leaf),(DATA,records)),DATA,len(records),DONE)


def audit(qemu, rom):
    image=rom.read_bytes()
    assert len(image)==END-START and hashlib.sha256(image).hexdigest()==SHA256,'ROM identity mismatch'
    def leaf(start,end): return image[start-START:end-START]
    # SDK acore.sym: hal_sdio_v151_set_adma_table; relative branches only.
    records=bytearray(); cases=[]; stride=1080
    for index in (0,1,127,128,129,255,0xffffffff):
        for length in (0,1,4,65535,65536,65537,0xffffffff):
            for address in (0,0x20050001):
                table=DATA+len(records)+32
                record=bytearray(b'\xa5'*stride)
                struct.pack_into('<IIIII',record,0,table,index,address,length,0xdeadbeef)
                records.extend(record); cases.append((table,index,address,length))
    for index,length in ((0,1),(128,65535),(129,1)):
        record=bytearray(b'\xa5'*stride)
        struct.pack_into('<IIIII',record,0,0,index,0x20050000,length,0xdeadbeef)
        records.extend(record); cases.append((0,index,0x20050000,length))
    actual=execute(qemu,leaf(0x1e8f4,0x1e918),records,stride)
    truncations=0
    for i,(table,index,address,length) in enumerate(cases):
        expected=bytearray(records[i*stride:(i+1)*stride])
        valid=bool(table and index<129 and address and length)
        struct.pack_into('<I',expected,16,0 if valid else 0x80003251)
        if valid:
            struct.pack_into('<HHI',expected,32+8*index,0x21,length&65535,address)
            truncations += length>65535
        assert actual[i*stride:(i+1)*stride]==expected, f'ADMA case {i}: {cases[i]}'
    # SDK acore.sym: sdio_cmd53_rw_bytes_cnt, also a relative-only leaf.
    records=bytearray(); lengths=[]
    for block_size in (0,1,64,512,2048,4095):
        for mode in (0,1):
            for count in range(512):
                argument=(mode<<27)|count
                records.extend(struct.pack('<IIIIII',block_size,argument,0,0,0xdeadbeef,0xa5a5a5a5))
                expected=(count*block_size or 0xffffffff) if mode else (count or 512)
                lengths.append(expected)
    actual=execute(qemu,leaf(0x1ee2a,0x1ee4a),records,24)
    for i,expected in enumerate(lengths):
        record=bytearray(records[i*24:(i+1)*24]); struct.pack_into('<I',record,16,expected)
        assert actual[i*24:(i+1)*24]==record, f'CMD53 case {i}'
    return {'rom_sha256':SHA256,'sdk_commit':'163d9150ab94a02d09a05f623351f9facee3025e',
            'adma_cases':len(cases),'accepted_length_truncations':truncations,'cmd53_length_cases':len(lengths),
            'adma_stride':8,'adma_transfer_flags':33,'adma_max_index':128,
            'zero_block_count_returns_error_sentinel':True,'byte_mode_zero_is_512':True,
            'unaligned_data_pointer_accepted_by_rom':True,'leaf_bytes_unmodified':True,
            'relocated_into_qemu_ram':True,'mmio_tested':False,'physical_hardware_accessed':False,
            'adma_transfer_or_drain_verified':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--qemu',required=True); p.add_argument('--rom',required=True,type=Path)
    a=p.parse_args(); print(json.dumps(audit(a.qemu,a.rom),indent=2))
