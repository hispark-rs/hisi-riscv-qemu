#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Synthetic SFC startup and realized-property guards; no hardware qualification.

Runs only local QEMU configuration checks, with no guest firmware or NOR command.
The caller supplies the executable and a new artifact directory. Binary/source
hashes and argv/QMP/stdout/stderr records identify the tested build; no particular
local path or previously built binary is required.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

DEVICE = "/machine/sfc-command"
CONFLICT = "ws53: SFC synthetic command profile and flashboot-handoff are mutually exclusive"
STATE = ("assume-profile", "commands-started", "commands-completed", "rejections",
         "peer-operations", "peer-commits", "peer-busy", "peer-wel", "peer-volatile-enable",
         "peer-sr1", "peer-sr2", "peer-nv-sr1", "peer-nv-sr2", "peer-sha256", "peer-power-cycles")


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def connect(path, process):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        require(process.poll() is None, "QEMU exited before QMP connection")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect(str(path))
            return sock
        except (FileNotFoundError, ConnectionRefusedError):
            sock.close()
            time.sleep(0.01)
    raise TimeoutError("QMP connection deadline")


def execute_case(qemu, output, name, *, enabled, conflict=False):
    directory = output / name
    directory.mkdir(exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="sg-") as temporary:
        qt, qm = Path(temporary) / "qt", Path(temporary) / "qm"
        argv = [str(qemu), "-M", "ws53,flashboot-handoff=on" if conflict else "ws53",
                "-accel", "qtest", "-S", "-display", "none", "-monitor", "none",
                "-serial", "null", "-nic", "none", "-d", "guest_errors",
                "-qtest", f"unix:{qt},server=on,wait=off", "-qtest-log", "/dev/null",
                "-qmp", f"unix:{qm},server=on,wait=off"]
        if enabled:
            argv += ["-global", "ws53-sfc-command.assume-profile=on"]
        (directory / "argv.json").write_text(json.dumps(argv, indent=2) + "\n")
        with (directory / "stdout.log").open("wb") as stdout, \
                (directory / "stderr.log").open("wb") as stderr:
            process = subprocess.Popen(argv, stdout=stdout, stderr=stderr)
            try:
                if conflict:
                    rc = process.wait(timeout=5)
                    require(rc == 1, f"conflict must reject startup: exit {rc}")
                    diagnostic = (directory / "stderr.log").read_text()
                    require(CONFLICT in diagnostic, "startup failed without expected mutual-exclusion diagnostic")
                    return {"name": name, "passed": True, "expected_startup_exit": rc,
                            "expected_diagnostic": CONFLICT, "commands_submitted": 0}
                with connect(qm, process) as control, control.makefile("rwb", buffering=0) as stream, \
                        (directory / "qmp.jsonl").open("w") as trace:
                    def receive():
                        raw = stream.readline()
                        require(bool(raw), "QMP disconnected")
                        trace.write(json.dumps({"received": raw.decode().rstrip()}) + "\n")
                        trace.flush()
                        return json.loads(raw)

                    def qmp(command, arguments=None, *, error=False):
                        message = {"execute": command}
                        if arguments is not None:
                            message["arguments"] = arguments
                        raw = json.dumps(message)
                        trace.write(json.dumps({"sent": raw}) + "\n")
                        trace.flush()
                        stream.write(raw.encode() + b"\n")
                        while True:
                            reply = receive()
                            if "event" in reply:
                                continue
                            if error:
                                require("error" in reply and "return" not in reply, "same-value set was accepted")
                                return reply["error"]
                            require("return" in reply and "error" not in reply, f"QMP failure: {reply}")
                            return reply["return"]

                    require("QMP" in receive(), "QMP greeting absent")
                    qmp("qmp_capabilities")
                    def snapshot():
                        return {key: qmp("qom-get", {"path": DEVICE, "property": key}) for key in STATE}
                    before = snapshot()
                    require(before["assume-profile"] is enabled, "initial profile differs")
                    failure = qmp("qom-set", {"path": DEVICE, "property": "assume-profile", "value": enabled}, error=True)
                    require(failure.get("class") == "GenericError", "wrong QMP failure class")
                    text = failure.get("desc", "")
                    require("assume-profile" in text and "realiz" in text.lower(), "failure does not establish realize guard")
                    after = snapshot()
                    require(after == before, "rejected same-value configuration changed state")
                    require(after["commands-started"] == after["commands-completed"] == after["peer-commits"] == 0,
                            "configuration test unexpectedly ran a NOR command")
                    qmp("quit")
                    process.wait(timeout=5)
                    require(process.returncode == 0, "QEMU failed during clean shutdown")
                    return {"name": name, "passed": True, "enabled": enabled, "same_value_rejected": True,
                            "error": failure, "before": before, "after": after, "commands_submitted": 0}
            finally:
                stop(process)


def run(qemu, output):
    output.mkdir(parents=True, exist_ok=False)
    report_path = output / "report.json"
    report = {"passed": False, "scope": "QEMU configuration guards only",
              "binary_sha256": sha(qemu), "script_sha256": sha(Path(__file__)),
              "hardware_accessed": False, "guest_firmware_executed": False,
              "board_nor_qualified": False, "cases": []}
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    try:
        report["cases"].append(execute_case(qemu, output, "startup-mutual-exclusion", enabled=True, conflict=True))
        report["cases"].append(execute_case(qemu, output, "same-value-enabled", enabled=True))
        report["cases"].append(execute_case(qemu, output, "same-value-disabled", enabled=False))
        report["passed"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report["artifacts_sha256"] = {str(p.relative_to(output)): sha(p) for p in sorted(output.rglob("*"))
                                      if p.is_file() and p != report_path}
        pending = output / "report.pending.json"
        pending.write_text(json.dumps(report, indent=2) + "\n")
        os.replace(pending, report_path)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new directory for reports and raw logs")
    args = parser.parse_args()
    report = run(args.qemu.resolve(strict=True), args.output.resolve())
    report_path = args.output.resolve() / "report.json"
    print(json.dumps({"passed": report["passed"], "case_count": len(report["cases"]),
                      "report": str(report_path), "sha256": sha(report_path)}, indent=2))


if __name__ == "__main__":
    main()
