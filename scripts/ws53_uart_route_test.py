#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""SDK L0 command-to-IRQ42 model; no silicon reset/readback claims."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import time

from ws53_gpio_test import running, connect
from ws53_branch_immediate_test import li, load, store
from ws53_tsensor_domain_test import csr_read, csr_write, addi

ROUTE = 0x57000048
UART = 0x57014000
OBJECT = '/machine/uart-l0-route'
PROFILE = ('-global', 'ws53-uart-l0-route.assume-profile=on')
NOTICE = ('ws53-uart-l0-route: explicit command1-to-acore profile; '
          'initial route, ccore destination and physical readback are not modeled')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2)+'\n')


def check_diagnostics(text, errors, enabled):
    # QEMU prefixes error_report with the executable basename, not log_mask.
    lines = text.splitlines()
    notices = [line for line in lines if line.endswith(NOTICE)]
    assert len(notices) == int(enabled), lines
    rest = [line for line in lines if line not in notices]
    assert len(rest) == errors and all('ws53-uart-l0-route: unsupported ' in line for line in rest), lines


def routing(qemu, out):
    with running(qemu, False, PROFILE) as (cmd, irq, reset, diagnostics):
        def read(addr): return int(cmd(f'readl {addr:#x}')[0], 0)
        def write(addr, value): cmd(f'writel {addr:#x} {value:#x}')
        def prop(name): return cmd.qmp('qom-get', {'path': OBJECT, 'property': name})
        def pin(name, level): cmd(f'set_irq_in {OBJECT} {name} 0 {int(level)}')
        def level():
            cmd('readl 0x28000')  # Drain synchronous qtest IRQ messages.
            return irq.get(42, False)
        cmd('irq_intercept_in /machine/intc')
        assert prop('commands') == 0 and not prop('routed') and not level()
        # Use the actual UART RX FIFO and IER path, not only a synthetic IRQ.
        write(UART+0x24, 0x10); write(UART+0x2c, 2); write(UART+0x18, 4)
        write(UART+4, 0x2a)
        assert read(UART+0x4c) == 1 and prop('source-pending') and not level()
        # Peers still deliver independently of the L0 routing command.
        for base, number in ((0x52081000, 39), (0x52080000, 41)):
            write(base+0x24, 0x10); write(base+0x2c, 2); write(base+0x18, 4)
            write(base+4, 0x53)
            assert irq.get(number, False) and read(base+0x4c) == 1
            assert read(base+4) == 0x53 and not irq.get(number, False)
        write(ROUTE, 1)
        assert prop('routed') and prop('commands') == 1 and level()
        assert read(UART+0x4c) == 1, 'route consumed queued RX data'
        write(ROUTE, 1)
        assert prop('commands') == 2 and level()
        pin('block-route', True)
        assert prop('blocked') and prop('source-pending') and not level()
        write(ROUTE, 1)
        assert prop('commands') == 3 and not level(), 'command erased fault'
        pin('block-route', False)
        assert level() and read(UART+4) == 0x2a and not level()
        for _ in range(2):
            pin('block-route', True)
            reset()
            assert not prop('routed') and prop('commands') == 0 and prop('blocked')
            pin('source', True)
            write(ROUTE, 1)
            assert not level()
            pin('block-route', False)
            assert level()
            pin('source', False)
            assert not level()
    check_diagnostics(diagnostics[0], 0, True)
    (out/'routing.stderr').write_text(diagnostics[0])
    # Old RAM/model users retain the legacy pass-through profile by default.
    with running(qemu, False) as (cmd, irq, reset, diagnostics):
        cmd('irq_intercept_in /machine/intc')
        cmd(f'set_irq_in {OBJECT} source 0 1')
        assert irq.get(42, False)
        cmd(f'writel {ROUTE:#x} 1')
        assert irq.get(42, False)
        cmd(f'set_irq_in {OBJECT} source 0 0')
        assert not irq.get(42, False)
    check_diagnostics(diagnostics[0], 1, False)
    (out/'legacy.stderr').write_text(diagnostics[0])
    return dict(real_uart_rx_fifo_irq=True, peers_unaffected=True,
                pending_fifo_preserved=True, fault_survives_command_and_reset=True,
                reset_rounds=2, legacy_pass_through_preserved=True)


def denied_accesses(qemu, out):
    operations = [f'{verb} {ROUTE:#x}' for verb in ('readb', 'readw', 'readl', 'readq')]
    operations += [f'{verb} {ROUTE:#x} 1' for verb in ('writeb', 'writew', 'writeq')]
    operations += [f'writel {ROUTE:#x} {value:#x}' for value in (0, 2, 3, 7, 0xffffffff)]
    operations += [f'writel {ROUTE+offset:#x} 1' for offset in (1, 4)]
    with running(qemu, False, PROFILE) as (cmd, irq, _, diagnostics):
        cmd('irq_intercept_in /machine/intc')
        cmd(f'set_irq_in {OBJECT} source 0 1')
        for operation in operations:
            cmd(operation)
            assert not cmd.qmp('qom-get', {'path': OBJECT, 'property': 'routed'})
            assert cmd.qmp('qom-get', {'path': OBJECT, 'property': 'commands'}) == 0
            assert not irq.get(42, False), operation
        cmd(f'writel {ROUTE:#x} 1')
        assert irq.get(42, False)
    check_diagnostics(diagnostics[0], len(operations), True)
    (out/'denied.stderr').write_text(diagnostics[0])
    return dict(operations=operations, rejected_whole_without_route_side_effects=True,
                qtest_return_value_is_not_memtx_success_evidence=True)


def cpu_faults(qemu, out, enabled):
    entry, trap, result, records = 0x28000, 0x28400, 0x29000, 0x29100
    code = li(8, result)+li(18, records)+li(9, 0)+li(5, trap)+[csr_write(0x305, 5)]
    code += li(10, ROUTE)+li(12, 1)
    faults = []
    # qtest discards MemTxResult; actual CPU traps establish rejection.
    accesses = [(load(5, 10, 0), 5), (store(12, 10, 0) & ~(7 << 12), 7),
                ((store(12, 10, 0) & ~(7 << 12)) | (1 << 12), 7)]
    if not enabled:
        accesses.append((store(12, 10, 0), 7))
    for instruction, cause in accesses:
        faults.append((cause, entry+4*len(code), ROUTE))
        code.append(instruction)
    if enabled:
        code += [store(12, 10, 0)]
    code += [store(9, 8, 4)]+li(5, 0x52544531)+[store(5, 8, 0), 0x0000006f]
    handler = [csr_read(5, 0x342), store(5, 18, 0), csr_read(5, 0x341), store(5, 18, 4),
               csr_read(6, 0x343), store(6, 18, 8), addi(5, 5, 4), csr_write(0x341, 5),
               addi(18, 18, 12), addi(9, 9, 1), 0x30200073]
    image = lambda words: struct.pack('<'+'I'*len(words), *words)
    label = 'profile' if enabled else 'legacy'
    transcript = []
    with tempfile.TemporaryDirectory(prefix='ws53-route-cpu-') as tmp:
        tmp = Path(tmp)
        argv = [str(qemu), '-M', 'ws53', '-S', '-display', 'none', '-monitor', 'none',
                '-serial', 'null', '-qtest-log', '/dev/null', '-qtest', f'unix:{tmp}/qt,server=on,wait=off',
                '-qmp', f'unix:{tmp}/qm,server=on,wait=off', '-d', 'guest_errors',
                *(PROFILE if enabled else ())]
        save(out/f'cpu-{label}-command.json', argv)
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with connect(tmp/'qt') as qs, qs.makefile('rwb', buffering=0) as qt, \
                 connect(tmp/'qm') as ms, ms.makefile('rwb', buffering=0) as qm:
                json.loads(qm.readline())
                def qmp(op, arguments=None):
                    request = {'execute': op}
                    if arguments is not None: request['arguments'] = arguments
                    qm.write(json.dumps(request).encode()+b'\n')
                    while True:
                        answer = json.loads(qm.readline())
                        assert 'error' not in answer, answer
                        if 'return' in answer: return answer['return']
                def cmd(text):
                    qt.write(text.encode()+b'\n')
                    answer = qt.readline().split()
                    assert answer and answer[0] == b'OK', (text, answer)
                    transcript.append([text, [v.decode() for v in answer]])
                    return answer[1:]
                qmp('qmp_capabilities')
                for addr, data in ((entry, image(code)), (trap, image(handler)),
                                   (result, bytes(8)), (records, bytes(12*len(faults)))):
                    cmd(f'write {addr:#x} {len(data)} 0x{data.hex()}')
                qmp('cont')
                deadline = time.monotonic()+5
                while int(cmd(f'readl {result:#x}')[0], 0) != 0x52544531:
                    assert time.monotonic() < deadline, 'route CPU fixture timeout'
                    time.sleep(.01)
                qmp('stop')
                assert int(cmd(f'readl {result+4:#x}')[0], 0) == len(faults)
                raw = bytes.fromhex(cmd(f'read {records:#x} {12*len(faults)}')[0].decode()[2:])
                observed = [struct.unpack_from('<3I', raw, 12*i) for i in range(len(faults))]
                assert observed == faults, (observed, faults)
                commands = qmp('qom-get', {'path': OBJECT, 'property': 'commands'})
                assert commands == int(enabled)
                qmp('quit')
            stdout, stderr = proc.communicate(timeout=5)
            assert proc.returncode == 0 and not stdout, (proc.returncode, stdout, stderr)
        finally:
            if proc.poll() is None: proc.terminate()
            _, stderr = proc.communicate(timeout=5)
            (out/f'cpu-{label}.stderr').write_bytes(stderr)
            save(out/f'cpu-{label}-transcript.json', transcript)
    check_diagnostics(stderr.decode(), len(faults), enabled)
    return dict(actual_tcg_execution=True, traps=observed, commands=commands,
                hardware_bus_fault_semantics_verified=False)


def uart_route_test(qemu):
    """Include the explicit profile and legacy compatibility in the main suite."""
    with tempfile.TemporaryDirectory(prefix='ws53-uart-route-suite-') as tmp:
        out = Path(tmp)
        return dict(passed=True, hardware_accessed=False, routing=routing(qemu, out),
                    denied=denied_accesses(qemu, out),
                    cpu=[cpu_faults(qemu, out, enabled) for enabled in (False, True)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(); args.qemu = args.qemu.resolve()
    assert not args.output.exists(), 'fresh output required'
    args.output.mkdir(parents=True)
    report = dict(passed=False, hardware_accessed=False, qemu_sha256=digest(args.qemu),
                  source_sha256={name: digest(Path(__file__).with_name(name)) for name in
                    (Path(__file__).name, 'ws53_gpio_test.py', 'ws53_branch_immediate_test.py',
                     'ws53_tsensor_domain_test.py')}, silicon_reset_route_known=False,
                  ccore_route_modeled=False, flash_rust_fixture_executed=False)
    try:
        report['routing'] = routing(args.qemu, args.output)
        report['denied'] = denied_accesses(args.qemu, args.output)
        report['cpu'] = [cpu_faults(args.qemu, args.output, enabled) for enabled in (False, True)]
        report['passed'] = True
    except BaseException as error:
        report.update(error=str(error), error_type=type(error).__name__)
        raise
    finally:
        save(args.output/'report.json', report)
    print(json.dumps(report, indent=2))


if __name__ == '__main__': main()
