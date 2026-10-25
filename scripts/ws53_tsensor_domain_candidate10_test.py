#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Independent negative diagnostic for frozen candidate10, never HIL qualification."""
import argparse
import json
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import time

from ws53_gpio_test import connect
from ws53_tsensor_domain_test import sha

CANDIDATE10 = "8950d0ee0597ef31ff89caee7279b920f28017c77063f84c647b6af9fabeb410"


def run(qemu, elf, out):
    assert sha(elf) == CANDIDATE10, "not frozen candidate10 ELF"
    symbols = {}
    for line in subprocess.check_output(["llvm-nm", "-n", str(elf)], text=True).splitlines():
        parts = line.split()
        if len(parts) == 3:
            symbols[parts[2]] = int(parts[0], 16)
    handler = symbols["default_exc_handler"]
    out.mkdir(parents=True, exist_ok=False)
    report = {"schema": "ws53-candidate10-domain-negative-diagnostic-v1",
              "passed": False, "hardware_accessed": False,
              "candidate10_qualified": False, "qemu": qemu, "qemu_sha256": sha(qemu),
              "candidate_elf": str(elf), "candidate_elf_sha256": CANDIDATE10,
              "test_sha256": sha(__file__),
              "access_fault_is_model_policy_not_silicon_behavior": True}
    try:
        with tempfile.TemporaryDirectory(prefix="ws53-domain-c10-") as tmp:
            tmp = Path(tmp)
            argv = [qemu, "-M", "ws53,flashboot-handoff=on", "-S", "-display", "none",
                    "-monitor", "none", "-serial", "null", "-serial", "null",
                    "-serial", f"file:{out}/uart.raw", "-kernel", str(elf),
                    "-global", "ws53-wdt.assume-timing=on",
                    "-global", "ws53-tsensor.assume-profile=on",
                    "-global", "ws53-tsensor.initial-code=933",
                    "-global", "ws53-tcxo.initial-count=0x123456780000fff0",
                    "-global", "ws53-pwm.assume-timing=on",
                    "-global", "ws53-pwm.counter-hz=2000000",
                    "-global", "ws53-mclken0.initial-value=0x7bcf",
                    "-global", "ws53-rtc.assume-timing=on",
                    "-global", "ws53-rtc.counter-hz=32000",
                    "-global", "ws53-rtc.ack-delay-ns=32000",
                    "-qtest", f"unix:{tmp}/qt,server=on,wait=off", "-qtest-log", "/dev/null",
                    "-qmp", f"unix:{tmp}/qm,server=on,wait=off", "-d", "guest_errors",
                    "-icount", "shift=8,align=off,sleep=off"]
            (out / "command.json").write_text(json.dumps(argv, indent=2)+"\n")
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                with connect(tmp / "qm") as ms, ms.makefile("rwb", buffering=0) as qm, \
                     connect(tmp / "qt") as qs, qs.makefile("rwb", buffering=0) as qt:
                    json.loads(qm.readline())
                    def qmp(op, arguments=None):
                        request = {"execute": op}
                        if arguments is not None: request["arguments"] = arguments
                        qm.write(json.dumps(request).encode()+b"\n")
                        while True:
                            reply = json.loads(qm.readline())
                            assert "error" not in reply, reply
                            if "return" in reply: return reply["return"]
                    def read(address, size):
                        qt.write(f"read {address:#x} {size}\n".encode())
                        reply = qt.readline().split()
                        assert len(reply) == 2 and reply[0] == b"OK" and reply[1].startswith(b"0x"), reply
                        data = bytes.fromhex(reply[1][2:].decode())
                        assert len(data) == size
                        return data
                    def regs():
                        text = qmp("human-monitor-command", {"command-line": "info registers"})
                        values = {}
                        for name in ("pc", "mtvec", "mcause", "mepc", "mtval"):
                            found = re.findall(r"\b"+name+r"\s+([0-9a-fA-F]+)", text)
                            assert len(found) == 1, (name, text)
                            values[name] = int(found[0], 16)
                        return values, text
                    qmp("qmp_capabilities")
                    qmp("cont")
                    deadline = time.monotonic()+30
                    while True:
                        values, dump = regs()
                        uart = (out / "uart.raw").read_bytes()
                        assert b"WS53 FLASH PASS irq=53" not in uart, "candidate unexpectedly passed"
                        if values["pc"] == handler:
                            break
                        assert time.monotonic() < deadline, ("candidate did not fault", values, uart[-1024:])
                        time.sleep(.02)
                    qmp("stop")
                    values, dump = regs()
                    assert values["pc"] == handler and values["mcause"] == 5
                    assert values["mtval"] == 0x59006304
                    assert values["mtvec"] == symbols["trap_vector"]
                    (out / "registers.txt").write_text(dump)
                    report["fault"] = values
                    state = struct.unpack("<18I", read(symbols["WS53_FLASH_TSENSOR_STATE"], 72))
                    windows = read(symbols["WS53_FLASH_TSENSOR_WINDOWS"], 72)
                    samples = read(symbols["WS53_FLASH_TSENSOR_SAMPLES"], 96)
                    assert not any(state) and not any(windows) and not any(samples)
                    assert read(0x5703096c, 4) == bytes(4)
                    count = qmp("qom-get", {"path": "/machine/tsensor", "property": "blocked-reads"})
                    assert count == 1
                    for name in ("blocked-writes", "starts", "samples", "reports"):
                        assert qmp("qom-get", {"path": "/machine/tsensor", "property": name}) == 0
                    # The default exception handler is an actual unconditional
                    # self-loop. Continue and verify it remains there, with no
                    # new UART, no final success, and no debugger initialization.
                    assert read(handler, 2) == b"\x01\xa0"  # compressed j .
                    before_uart = (out / "uart.raw").read_bytes()
                    qmp("cont")
                    time.sleep(.15)
                    qmp("stop")
                    again, _ = regs()
                    assert again == values and (out / "uart.raw").read_bytes() == before_uart
                    qmp("quit")
                stdout, stderr = proc.communicate(timeout=5)
                assert proc.returncode == 0 and not stdout
            finally:
                if proc.poll() is None: proc.terminate()
                _, stderr = proc.communicate(timeout=5)
                (out / "qemu.stderr").write_bytes(stderr)
        text = (out / "uart.raw").read_text()
        for marker in ("WS53 FLASH data/bss OK", "WS53 PASS tcxo ",
                       "WS53 PASS tcxo_nonblocking ", "WS53 PASS pwm0 ",
                       "WS53 RTC BEGIN 00000000", "WS53 RTC BEGIN 00000001",
                       "WS53 PASS rtc channels=2", "WS53 FLASH RTC gpio17_edges=8"):
            assert marker in text, marker
        assert "TSENSOR BEGIN" not in text and "WS53 FLASH PASS irq=53" not in text
        assert stderr.decode().count("inaccessible control domain read +0x4") == 1
        assert "unimplemented" not in stderr.decode().lower()
        source = subprocess.check_output(["riscv64-linux-gnu-addr2line", "-afi", "-e", str(elf),
                                          hex(values["mepc"])], text=True)
        assert "tsensor_flash.rs:" in source, source
        (out / "fault-source.txt").write_text(source)
        disassembly = subprocess.check_output(["riscv64-linux-gnu-objdump", "-d", "-S",
            f"--start-address={values['mepc']}", f"--stop-address={values['mepc']+4}", str(elf)], text=True)
        (out / "fault-instruction.txt").write_text(disassembly)
        assert sha(elf) == CANDIDATE10 and sha(qemu) == report["qemu_sha256"]
        report.update(passed=True, expected_negative_observed=True,
                      uart_completed_through_both_rtc=True, final_application_pass=False,
                      sensor_published_state_zero=True, sensor_start_count=0,
                      control_cpu_running=False, exception_self_loop_verified=True,
                      fault_source=source)
    except BaseException as error:
        report["error"] = repr(error)
        raise
    finally:
        report["artifacts"] = {p.name: sha(p) for p in sorted(out.iterdir()) if p.is_file()}
        (out / "report.json").write_text(json.dumps(report, indent=2)+"\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True, type=Path)
    parser.add_argument("--elf", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(str(args.qemu.resolve()), args.elf.resolve(), args.output.resolve()), indent=2))
