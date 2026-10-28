#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Explicit opaque GPIO SW_CTL observation, error and IRQ34 contracts.

All source values and faults are model inputs. No selector encoding, silicon
reset value, electrical state, Flash application or hardware is qualified.
"""
import sys
sys.dont_write_bytecode = True
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import time

from ws53_gpio_test import BANKS, connect, gpio_test, running
from ws53_branch_immediate_test import li, load, store
from ws53_tsensor_domain_test import csr_read, csr_write, addi
from ws53_flash_handoff_test import NOTICE as WDT_NOTICE

PROFILE = ('-global', 'ws53-gpio.assume-source-control=on')
FLASH = ('-machine', 'flashboot-handoff=on', '-global', 'ws53-wdt.assume-timing=on')
NOTICES = [f'qemu-system-riscv32: ws53-gpio: opaque SW_CTL observation enabled (width={width}); '
           'selector encoding and silicon reset are not modeled' for _,width,_ in BANKS]
BASE, SOURCE = 0x5700c000, 0x5700c008
LINE, PEER = 1 << 17, 1 << 16
VALID = (1 << 23) - 1


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value): path.write_text(json.dumps(value, indent=2)+'\n')


def profile(value): return PROFILE + ('-global',f'ws53-gpio.source-control-value={value:#x}')


def observations(qemu, out):
    cases = []
    for value in (0,0x87654321,0xffffffff):
        with running(qemu, extra_args=FLASH+profile(value)) as (cmd, irq, reset, diagnostics):
            cmd('irq_intercept_in /machine/intc')
            def read(address): return int(cmd(f'readl {address:#x}')[0],0)
            def write(off,data): cmd(f'writel {BASE+off:#x} {data:#x}')
            def pin(n,level): cmd(f'set_irq_in /machine/gpio0 pin {n} {level}')
            def state():
                return [read(BASE+off) for off in (0,4,12,16,20,24,28,32,36,40)]
            # Baseline is Flash handoff CCORE=0; GPIO does not need TSENSOR's
            # separate domain release and this observer never writes that reset.
            assert read(0x5703096c)==0
            write(4,VALID);write(0xc,LINE|PEER);write(0x14,LINE|PEER)
            write(0x18,LINE|PEER);write(0x10,VALID&~PEER)
            pin(17,1);pin(16,1)
            assert read(BASE+0x24)==LINE|PEER and read(BASE+0x28)==PEER and irq[34]
            snapshot=state()
            for _ in range(8):
                for base,_,_ in BANKS:
                    assert read(base+8)==value  # full32; not bank-valid-bit masked
                assert state()==snapshot and irq[34]
            # An explicit model input changes only this bank's opaque snapshot.
            injected=value^0xffffffff
            cmd.qmp('qom-set',{'path':'/machine/gpio0','property':'source-control-observation','value':injected})
            assert read(SOURCE)==injected and state()==snapshot and irq[34]
            assert all(read(base+8)==value for base,_,_ in BANKS[1:])
            assert cmd.qmp('qom-get',{'path':'/machine/gpio0','property':'source-control-observation'})==injected
            # Cancel the selected latched masked wait; a different owner's
            # pending enabled line survives. Disable alone does not EOI.
            write(0x10,VALID&~PEER);write(0xc,PEER);write(0x2c,LINE)
            assert read(BASE+0x24)==PEER and read(BASE+0x28)==PEER and irq[34]
            write(0xc,PEER|LINE)
            assert read(BASE+0x24)==PEER, 'cancel left a selected stale edge'
            write(0x2c,PEER);assert not irq[34]
            pin(17,0);pin(17,1)
            assert read(BASE+0x24)==LINE and read(BASE+0x28)==0 and not irq[34]
            write(0x10,VALID&~LINE)
            assert irq[34] and read(BASE+0x28)==LINE, 'unmask lost a pending edge'
            write(0xc,PEER)
            assert not irq[34] and read(BASE+0x28)==0
            write(0x2c,LINE);write(0xc,PEER|LINE)
            assert read(BASE+0x24)==0 and not irq[34]
            # Dropped/immediately cancelled operation: transitions while EN=0
            # do not create a deferred event when a new wait acquires the line.
            write(0xc,PEER);pin(17,0);pin(17,1);write(0xc,PEER|LINE)
            assert read(BASE+0x24)==0 and not irq[34]
            for _ in range(2):
                reset()
                assert read(0x5703096c)==0 and read(SOURCE)==value
                assert read(BASE+4)==read(BASE+0x10)==VALID
                assert read(BASE+0xc)==read(BASE+0x24)==read(BASE+0x28)==0
                assert not irq[34]
            cases.append(dict(value=value,repeated_reads=24,reset_rounds=2,read_side_effects=False,
                injection_changed_only_snapshot=True,peer_latched_irq_preserved=True,
                cancel_ack_selected_line=True,disabled_transition_not_queued=True,
                masked_edge_delivered_on_unmask=True,irq=34,ccore_held_zero=True))
        lines=diagnostics[0].splitlines()
        (out/f'profile-{value:08x}.stderr').write_text(diagnostics[0])
        assert sorted(lines)==sorted([*NOTICES,WDT_NOTICE]),lines
    return cases


def rejected_accesses(qemu,out):
    startup=subprocess.run([str(qemu),'-M','ws53','-S','-display','none','-monitor','none',
        '-serial','null','-global','ws53-gpio.source-control-value=1'],
        capture_output=True,timeout=5)
    assert startup.returncode!=0 and not startup.stdout
    assert b'source-control-value requires assume-source-control=on' in startup.stderr
    (out/'invalid-startup.stderr').write_bytes(startup.stderr)
    with running(qemu) as (cmd,_,_,diagnostics):
        cmd(f'readl {SOURCE:#x}')
        cmd(f'writel {SOURCE:#x} 0')
        try:
            cmd.qmp('qom-set',{'path':'/machine/gpio0','property':'source-control-observation','value':7})
        except AssertionError as error:
            assert 'requires assume-source-control=on' in str(error)
        else: raise AssertionError('source injection without explicit profile accepted')
    lines=diagnostics[0].splitlines()
    assert len(lines)==2 and 'without explicit SW_CTL observation profile' in lines[0]
    assert 'SW_CTL selector writes are outside' in lines[1]
    (out/'unconfigured.stderr').write_text(diagnostics[0])
    accesses=[]
    with running(qemu,extra_args=profile(0xa55a5aa5)) as (cmd,_,_,diagnostics):
        def read(off): return int(cmd(f'readl {BASE+off:#x}')[0],0)
        offsets=(0,4,8,12,16,20,24,28,32,36,40)
        before=[read(off) for off in offsets]
        for operation in (f'writel {SOURCE:#x} 0',f'writel {SOURCE:#x} 0xffffffff',
                f'readb {SOURCE:#x}',f'readw {SOURCE:#x}',f'readq {SOURCE:#x}',
                f'writeb {SOURCE:#x} 0',f'writew {SOURCE:#x} 0',f'writeq {SOURCE:#x} 0xffffffffffffffff',
                f'readl {SOURCE+1:#x}',f'writel {SOURCE+1:#x} 0xffffffff'):
            cmd(operation)
            assert [read(off) for off in offsets]==before, operation
            accesses.append(operation)
        for value in (-1,1<<32,'not-an-integer',True):
            try: cmd.qmp('qom-set',{'path':'/machine/gpio0','property':'source-control-observation','value':value})
            except AssertionError: pass
            else: raise AssertionError('invalid model u32 accepted: '+repr(value))
            assert read(8)==0xa55a5aa5
    lines=diagnostics[0].splitlines()
    assert all(lines.count(line)==1 for line in NOTICES)
    errors=[line for line in lines if line not in NOTICES]
    assert len(errors)==len(accesses),errors
    assert sum('unimplemented write +0x8' in line for line in errors)==2
    assert sum('unsupported ' in line and 'width/alignment' in line for line in errors)==8
    (out/'rejected-accesses.stderr').write_text(diagnostics[0])
    return dict(unconfigured_read_and_write_rejected=True,invalid_startup_rejected=True,model_injection_requires_profile=True,
                invalid_model_values_rejected=4,guest_accesses_rejected=accesses,
                unchanged_neighbouring_registers=True,errors_are_model_policy=True)


def cpu_access(qemu,out,enabled):
    entry,trap,result,records=0x28000,0x28400,0x29000,0x29100
    value=0x87654321
    code=li(8,result)+li(18,records)+li(9,0)+li(5,trap)+[csr_write(0x305,5)]
    code+=li(10,SOURCE)+li(12,0x12345678)
    faults=[]
    if enabled:
        code += [load(5,10,0),store(5,8,4),load(5,10,0),store(5,8,8)]
    else:
        faults.append((5,entry+len(code)*4,SOURCE));code += [load(5,10,0)]
    for instruction,cause in ((store(12,10,0),7),
            (load(5,10,0)&~(7<<12),5),(store(12,10,0)&~(7<<12),7),
            ((load(5,10,0)&~(7<<12))|(1<<12),5),
            ((store(12,10,0)&~(7<<12))|(1<<12),7)):
        faults.append((cause,entry+len(code)*4,SOURCE));code.append(instruction)
    if enabled: code += [load(5,10,0),store(5,8,12)]
    code += [store(9,8,16)]+li(5,0x53524345)+[store(5,8,0),0x0000006f]
    handler=[csr_read(5,0x342),store(5,18,0),csr_read(5,0x341),store(5,18,4),
        csr_read(6,0x343),store(6,18,8),addi(5,5,4),csr_write(0x341,5),
        addi(18,18,12),addi(9,9,1),0x30200073]
    image=lambda words:struct.pack('<'+'I'*len(words),*words)
    label='enabled' if enabled else 'unconfigured'
    (out/f'cpu-{label}.bin').write_bytes(image(code))
    (out/f'cpu-{label}-trap.bin').write_bytes(image(handler))
    transcript=[]
    with tempfile.TemporaryDirectory(prefix='ws53-source-cpu-') as tmp:
        tmp=Path(tmp)
        argv=[str(qemu),'-M','ws53','-S','-display','none','-monitor','none','-serial','null',
            '-qtest-log','/dev/null','-qtest',f'unix:{tmp}/qt,server=on,wait=off',
            '-qmp',f'unix:{tmp}/qm,server=on,wait=off','-d','guest_errors',*(profile(value) if enabled else ())]
        save(out/f'cpu-{label}-command.json',argv)
        proc=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            with connect(tmp/'qt') as qs,qs.makefile('rwb',buffering=0) as qt, \
                 connect(tmp/'qm') as ms,ms.makefile('rwb',buffering=0) as qm:
                json.loads(qm.readline())
                def qmp(op):
                    qm.write(json.dumps({'execute':op}).encode()+b'\n')
                    while True:
                        answer=json.loads(qm.readline());assert 'error' not in answer,answer
                        if 'return' in answer:return answer['return']
                def cmd(text):
                    qt.write(text.encode()+b'\n');answer=qt.readline().split()
                    assert answer and answer[0]==b'OK',(text,answer)
                    transcript.append([text,[x.decode() for x in answer]])
                    return answer[1:]
                qmp('qmp_capabilities')
                for addr,data in ((entry,image(code)),(trap,image(handler)),
                                  (result,bytes(20)),(records,bytes(12*len(faults)))):
                    cmd(f'write {addr:#x} {len(data)} 0x{data.hex()}')
                qmp('cont');deadline=time.monotonic()+5
                while int(cmd(f'readl {result:#x}')[0],0)!=0x53524345:
                    assert time.monotonic()<deadline,'CPU access-fault fixture timeout'
                    time.sleep(.01)
                qmp('stop')
                data=bytes.fromhex(cmd(f'read {result:#x} 20')[0].decode()[2:])
                actual=struct.unpack('<5I',data)
                expected=(0x53524345,)+((value,value,value) if enabled else (0,0,0))+(len(faults),)
                assert actual==expected,actual
                raw=bytes.fromhex(cmd(f'read {records:#x} {12*len(faults)}')[0].decode()[2:])
                observed=[struct.unpack_from('<3I',raw,12*i) for i in range(len(faults))]
                assert observed==faults,(observed,faults)
                qmp('quit')
            stdout,stderr=proc.communicate(timeout=5)
            assert proc.returncode==0 and not stdout,(proc.returncode,stdout,stderr)
        finally:
            if proc.poll() is None:proc.terminate()
            _,stderr=proc.communicate(timeout=5)
            (out/f'cpu-{label}.stderr').write_bytes(stderr)
            save(out/f'cpu-{label}-transcript.json',transcript)
    lines=stderr.decode().splitlines()
    notices=NOTICES if enabled else []
    assert all(lines.count(line)==1 for line in notices)
    assert len([line for line in lines if line not in notices])==len(faults),lines
    return dict(profile=enabled,actual_tcg_load_store_execution=True,traps=observed,
        configured_read_value=value if enabled else None,source_after_rejected_write_unchanged=enabled)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();args.qemu=args.qemu.resolve()
    assert not args.output.exists(),'fresh output required'
    args.output.mkdir(parents=True)
    report=dict(passed=False,hardware_accessed=False,source_encoding_modeled=False,
        hardware_reset_value_known=False,source_write_semantics_modeled=False,
        cpu_async_fixture_executed=False,flash_application_qualified=False,
        qemu_sha256=sha(args.qemu),source_sha256={name:sha(Path(__file__).with_name(name)) for name in
            (Path(__file__).name,'ws53_gpio_test.py','ws53_branch_immediate_test.py','ws53_tsensor_domain_test.py')})
    try:
        report['observations']=observations(args.qemu,args.output)
        report['rejected']=rejected_accesses(args.qemu,args.output)
        report['cpu']=[cpu_access(args.qemu,args.output,enabled) for enabled in (False,True)]
        report['legacy_gpio']=gpio_test(str(args.qemu))
        report['passed']=True
    except BaseException as error:
        report['error']=repr(error);raise
    finally:save(args.output/'report.json',report)
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
