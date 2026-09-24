# Example client: power-cycle a board BOOTS times through the relayed TAC REST API,
# watch the relayed serial console, and classify each boot as login / lost / timeout.
#
# Written to root-cause the lemans-evk (RB8) Debian early-boot power loss (PMIC OCP on
# PMIC C LDO4 = vreg_l4c, UFS VCCQ): after a loss the board is powered on with a short
# power-key press first (a TAC power cycle cuts the supply and wipes the PMIC fault
# record), so XBL's "OCP Occured" report can be read. It also counts ufshcd probe
# attempts per boot (a deferred first probe shows up as a second
# "Unable to find vdd-hba-supply").
#
# Runs inside a LAVA docker test action. The console (ser2net) and the TAC REST API
# (tac-api) are reached through this repository's relay test service, published on
# the docker bridge gateway (CONSOLE_PORT/TAC_PORT, default 17085/17080).
# Environment: TAC_SERIAL (required: the board's TAC serial), BOOTS, BOOT_TIMEOUT,
# SILENCE, MARKER.
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.request

TAC_SERIAL = os.environ["TAC_SERIAL"]
CONSOLE_PORT = int(os.environ.get("CONSOLE_PORT", "17085"))
TAC_PORT = int(os.environ.get("TAC_PORT", "17080"))
BOOTS = int(os.environ.get("BOOTS", "60"))
BOOT_TIMEOUT = int(os.environ.get("BOOT_TIMEOUT", "180"))
# console silence after /init that counts as a power loss
SILENCE = int(os.environ.get("SILENCE", "30"))
MARKER = os.environ.get("MARKER", "")


def gateway():
    for line in open("/proc/net/route").read().splitlines()[1:]:
        f = line.split()
        if f[1] == "00000000":
            return socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
    raise RuntimeError("no default route")


class Tac:
    def __init__(self, host, port, serial):
        self.base = f"http://{host}:{port}/{serial}"

    def _put(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method="PUT")
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()

    def quick(self, name):
        self._put(f"/quick/{name}")

    def pin(self, command, value):
        # pytactl reads the value from a JSON body; a bare query string gets 415
        self._put(f"/command/{command}", {"value": int(value)})


class Console:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(1)
        self.lock = threading.Lock()
        self.lines = []
        self.partial = b""
        self.last_rx = time.monotonic()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            chunk = re.sub(rb"\xff[\xfb-\xfe].|\xff[\xf0-\xfa]", b"", chunk)
            self.last_rx = time.monotonic()
            with self.lock:
                *done, self.partial = (self.partial + chunk).split(b"\n")
                for raw in done:
                    line = raw.decode("utf-8", "replace").rstrip("\r")
                    self.lines.append(line)
                    print(f"[console] {line}", flush=True)

    def mark(self):
        with self.lock:
            return len(self.lines)

    def since(self, idx):
        with self.lock:
            tail = [self.partial.decode("utf-8", "replace")] if self.partial else []
            return list(self.lines[idx:]) + tail


def log(msg):
    print(f"### {time.strftime('%H:%M:%S')} {msg}", flush=True)


def case(name, result, measurement=None):
    extra = f" MEASUREMENT={measurement}" if measurement is not None else ""
    print(f"<LAVA_SIGNAL_TESTCASE TEST_CASE_ID={name} RESULT={result}{extra}>", flush=True)


def boot_once(tac, con, n):
    """Return (outcome, lines): outcome is "login", "lost" or "timeout"."""
    log(f"boot {n}: power cycle")
    start = con.mark()
    tac.quick("powerOff")
    time.sleep(3)
    tac.quick("powerOn")
    t0 = time.monotonic()
    seen_init = False
    while time.monotonic() - t0 < BOOT_TIMEOUT:
        time.sleep(1)
        out = con.since(start)
        kernel = next((i for i, l in enumerate(out) if "Linux version" in l), None)
        if kernel is not None and any("login:" in l for l in out[kernel:]):
            log(f"boot {n}: reached login in {time.monotonic() - t0:.0f}s")
            return "login", out
        seen_init = seen_init or any("Run /init" in l for l in out)
        if seen_init and time.monotonic() - con.last_rx > SILENCE:
            log(f"boot {n}: POWER LOSS / FREEZE; last console lines:")
            for line in out[-6:]:
                print(f"    {line!r}", flush=True)
            return "lost", out
    log(f"boot {n}: timeout")
    return "timeout", con.since(start)


def main():
    gw = gateway()
    con = Console(gw, CONSOLE_PORT)
    tac = Tac(gw, TAC_PORT, TAC_SERIAL)
    counts = {"login": 0, "lost": 0, "timeout": 0}
    ocp = 0
    marked = 0
    multi_probe = 0
    for n in range(1, BOOTS + 1):
        outcome, out = boot_once(tac, con, n)
        counts[outcome] += 1
        probes = sum("Unable to find vdd-hba-supply" in l for l in out)
        if probes > 1:
            multi_probe += 1
        log(f"boot {n}: {outcome}, ufshcd probe attempts seen: {probes}")
        if MARKER and any(MARKER in l for l in out):
            marked += 1
        if outcome == "lost":
            # power on with the key (no supply cut) so XBL prints the PMIC fault
            start = con.mark()
            tac.pin("kpd_pwr", 1)
            time.sleep(1)
            tac.pin("kpd_pwr", 0)
            time.sleep(15)
            hits = [l for l in con.since(start) if "OCP Occured" in l or "VREG_OCP" in l]
            if hits:
                ocp += 1
            log(f"boot {n}: after key power-on, XBL fault lines: {hits}")
    log(f"summary: {counts}, losses with XBL OCP report: {ocp}, boots with >1 ufshcd probe: {multi_probe}, marker seen: {marked}/{BOOTS}")
    case("boots-login", "pass", counts["login"])
    case("boots-lost", "pass" if counts["lost"] == 0 else "fail", counts["lost"])
    case("boots-timeout", "pass" if counts["timeout"] == 0 else "fail", counts["timeout"])
    case("xbl-ocp-reports", "pass" if ocp == 0 else "fail", ocp)
    case("ufshcd-multi-probe", "pass" if multi_probe == 0 else "fail", multi_probe)
    if MARKER:
        case("marker-seen", "pass" if marked == BOOTS else "fail", marked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
