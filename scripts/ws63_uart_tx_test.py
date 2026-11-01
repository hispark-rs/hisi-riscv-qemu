#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Synthetic TX backpressure/reset tests; no physical FIFO or serializer claim."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import tempfile
from ws53_gpio_test import running, connect


def uart_path(qmp, chardev):
    matches = []
    for item in qmp('qom-list', {'path': '/machine/unattached'}):
        if item['type'] == 'child<ws63-uart>':
            path = '/machine/unattached/' + item['name']
            if qmp('qom-get', {'path': path, 'property': 'chardev'}) == chardev:
                matches.append(path)
    assert len(matches) == 1, matches
    return matches[0]


def receive(peer, expected):
    actual = b''
    while len(actual) < len(expected):
        part = peer.recv(len(expected)-len(actual))
        assert part, 'unexpected EOF'
        actual += part
    assert actual == expected, (actual, expected)


def empty(peer):
    peer.settimeout(.02)
    try:
        assert not peer.recv(1), 'unexpected transmitted byte'
    except socket.timeout:
        pass
    finally:
        peer.settimeout(2)


def test(qemu, machine, capacity):
    bases = ([0x44010000, 0x44011000, 0x44012000] if machine == 'ws63'
             else [0x52081000, 0x52080000, 0x52082000])
    with tempfile.TemporaryDirectory(prefix='hisi-uart-tx-') as tmp:
        paths = [Path(tmp)/str(i) for i in range(3)]
        extra = ['-global', f'ws63-uart.tx-fifo-size={capacity}']
        serial = []
        for i, path in enumerate(paths):
            extra += ['-chardev', f'socket,id=tx{i},path={path},server=on,wait=off']
            serial += ['-serial', f'chardev:tx{i}']
        with running(qemu, False, extra, serial_options=serial, machine=machine) as (cmd, _, reset, diagnostics):
            for port, base in enumerate(bases):
                path = uart_path(cmd.qmp, f'tx{port}')
                def stall(value):
                    cmd.qmp('qom-set', {'path': path, 'property': 'tx-stalled', 'value': value})
                def read(off): return int(cmd(f'readl {base+off:#x}')[0], 0)
                def write(off, val): cmd(f'writel {base+off:#x} {val:#x}')
                with connect(paths[port]) as peer:
                    peer.settimeout(2)
                    stall(True)
                    write(0x24, 0x10)
                    # Repeated filling/draining crosses the backing ring boundary.
                    for batch in range(2048//capacity+1):
                        payload = bytes((batch+i) % 256 for i in range(capacity))
                        assert read(0x44) & 3 == 2
                        for i, byte in enumerate(payload):
                            write(4, byte)
                            assert read(0x44) & 3 == (1 if i == capacity-1 else 0)
                        assert read(0x34) & 0xc0 == 0
                        if batch == 0:
                            write(4, 0xee)  # Invalid producer; diagnosed and dropped.
                            empty(peer)
                        stall(False)
                        receive(peer, payload)
                        assert read(0x44) & 3 == 2 and read(0x34) & 0xc0 == 0xc0
                        stall(True)
                    write(4, 0xa5)
                    write(0x24, 0x30)  # Explicit TX reset discards only TX contents.
                    assert read(0x24) == 0x10 and read(0x44) & 3 == 2
                    stall(False); empty(peer)
                    stall(True); write(4, 0x5a)
                    reset()
                    assert read(0x24) == 0 and read(0x44) & 3 == 2
                    # Host fault remains across guest reset; FIFO-disabled capacity=1.
                    write(4, 0x53)
                    assert read(0x44) & 3 == 1
                    empty(peer)
                    stall(False); receive(peer, b'\x53'); empty(peer)
        assert diagnostics == ['ws63-uart: write to full TX FIFO\n'*3], diagnostics
    return dict(machine=machine, capacity=capacity, ports=3, passed=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True, type=Path)
    args = parser.parse_args()
    results = [test(str(args.qemu), m, n) for m in ('ws63', 'bs20', 'bs21', 'bs21e', 'bs22') for n in (1, 16)]
    print(json.dumps(dict(passed=True, results=results, hardware_accessed=False,
        physical_fifo_depth_verified=False, serializer_timing_verified=False,
        tx_irq_supported=False, qemu_sha256=hashlib.sha256(args.qemu.read_bytes()).hexdigest()), indent=2))
