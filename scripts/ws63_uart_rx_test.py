#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Synthetic WS63/BS2X character FIFO and RX notification; no physical-depth claim."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import time
from ws53_gpio_test import running, connect


def test(qemu, machine='ws63', capacity=16):
    # All these existing machines use the same UART class/address map.
    bases = [0x44010000, 0x44011000, 0x44012000]
    if machine != 'ws63':
        # BS2X wrappers keep their default synthetic single-character capacity;
        # separate addresses are supplied from their existing machine mapping.
        bases = [0x52081000, 0x52080000, 0x52082000]
    with tempfile.TemporaryDirectory(prefix='ws63-uart-rx-') as tmp:
        paths = [Path(tmp)/str(i) for i in range(3)]
        extra = ['-global', f'ws63-uart.rx-fifo-size={capacity}']
        serial = []
        for i, path in enumerate(paths):
            extra += ['-chardev', f'socket,id=rx{i},path={path},server=on,wait=off']
            serial += ['-serial', f'chardev:rx{i}']
        with running(qemu, False, extra, serial_options=serial, machine=machine) as (cmd, irq, reset, diagnostics):
            if machine == 'ws63':
                cmd('irq_intercept_in /machine/intc')
            for port, base in enumerate(bases):
                def read(off): return int(cmd(f'readl {base+off:#x}')[0], 0)
                def write(off, value): cmd(f'writel {base+off:#x} {value:#x}')
                def level():
                    read(0x34)
                    return irq.get(53+port, False)
                def ready():
                    deadline = time.monotonic()+2
                    while not read(0x34) & 0x20:
                        assert time.monotonic() < deadline, 'RX never became ready'
                        time.sleep(.001)
                assert read(0x34) == 0xc0 and read(0) == 1
                write(0x24, 0x10)
                with connect(paths[port]) as peer:
                    peer.sendall(b'\x00\xff\x53'); ready()
                    assert read(0) == 1
                    write(0x18, 1)  # RX line-error enable must NOT enable RX data.
                    assert read(0) == 1
                    write(0x18, 4)
                    assert read(0) == 4
                    if machine == 'ws63': assert level()
                    write(0x18, 0)
                    assert read(0) == 1 and read(0x34) & 0x20
                    if machine == 'ws63': assert not level()
                    write(0x18, 4)  # No new byte needed to reassert.
                    assert read(0) == 4
                    values = []
                    for _ in range(3):
                        ready(); values.append(read(4))
                    assert bytes(values) == b'\x00\xff\x53'
                    assert read(0x34) == 0xc0 and read(0) == 1
                    # More than capacity exercises can_receive/accept_input and
                    # ring wrap without counting host buffering as chip storage.
                    payload = bytes(i % 256 for i in range(2*capacity+9))
                    peer.sendall(payload)
                    values = []
                    for _ in payload:
                        ready(); values.append(read(4))
                    assert bytes(values) == payload
                    peer.sendall(b'\x41'); ready()
                    write(0x24, 0x50)  # RX FIFO reset clears retained data and IRQ.
                    assert read(0x34) == 0xc0 and read(0) == 1
                    if machine == 'ws63': assert not level()
                    if capacity == 16:
                        write(0x24, 0x14)  # quarter trigger (4 bytes)
                        peer.sendall(b'123'); ready(); time.sleep(.01)
                        assert read(0) == 1
                        peer.sendall(b'4')
                        deadline = time.monotonic()+2
                        while read(0) != 4:
                            assert time.monotonic() < deadline
                            time.sleep(.001)
                        assert read(4) == ord('1') and read(0) == 1
                        write(0x24, 0x10)
                        assert read(0) == 4
                        assert bytes(read(4) for _ in range(3)) == b'234'
                    peer.sendall(b'\x42'); ready()
                    reset()
                    assert read(0x18) == 0 and read(0x34) == 0xc0 and read(0) == 1
            # Verify all three were reset, not only the last accessed port.
            assert all(int(cmd(f'readl {base+0x18:#x}')[0], 0) == 0 for base in bases)
        assert diagnostics == [''], diagnostics
    return dict(machine=machine, capacity=capacity, ports=3, passed=True,
                hardware_accessed=False, physical_fifo_depth_verified=False,
                physical_baud_verified=False, receive_count_alias_qualified=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True, type=Path)
    args = parser.parse_args()
    result = [test(str(args.qemu), capacity=n) for n in (1, 16)]
    result += [test(str(args.qemu), machine=m, capacity=1) for m in ("bs20", "bs21", "bs21e", "bs22")]
    print(json.dumps(dict(passed=True, results=result,
        qemu_sha256=hashlib.sha256(args.qemu.read_bytes()).hexdigest()), indent=2))
