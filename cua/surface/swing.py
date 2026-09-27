"""Surface driver for Java Swing apps through the in-process accessibility bridge (bridge/)."""
from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from .base import Observation

ROOT = Path(__file__).resolve().parents[2]


from .base import SurfaceError  # noqa: E402  (re-exported for existing imports)


class SwingSurface:
    kind = "java-swing"

    def __init__(self, port: int = 8740):
        self.base = f"http://127.0.0.1:{port}"
        self.http = httpx.Client(base_url=self.base, timeout=15.0)

    # -- perception
    def observe(self) -> Observation:
        r = self.http.get("/tree")
        if r.status_code != 200:
            raise SurfaceError(f"tree failed: {r.text}")
        return Observation.from_json(r.json())

    def screenshot(self) -> tuple[bytes, tuple[int, int]]:
        r = self.http.get("/screenshot")
        if r.status_code != 200:
            raise SurfaceError(f"screenshot failed: {r.text}")
        ox, oy = (int(v) for v in r.headers.get("X-Origin", "0,0").split(","))
        return r.content, (ox, oy)

    def settle(self, timeout_s: float = 3.0, quiet_s: float = 0.35) -> Observation:
        """Poll until the tree stops changing for quiet_s, or timeout. Returns latest observation."""
        deadline = time.monotonic() + timeout_s
        last = self.observe()
        last_fp, stable_since = last.fingerprint(), time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.12)
            cur = self.observe()
            fp = cur.fingerprint()
            if fp != last_fp:
                last_fp, stable_since = fp, time.monotonic()
            elif time.monotonic() - stable_since >= quiet_s:
                return cur
            last = cur
        return last

    # -- action
    def act(self, op: str, **kwargs: Any) -> None:
        r = self.http.post("/act", json={"op": op, **kwargs})
        if r.status_code != 200:
            raise SurfaceError(r.json().get("error", r.text))

    # -- control seam
    def set_control(self, mode: str, holder: str = "") -> dict[str, Any]:
        return self.http.post("/control", json={"mode": mode, "holder": holder}).json()

    def control(self) -> dict[str, Any]:
        return self.http.post("/control", json={}).json()

    def events(self, since: int = 0) -> tuple[list[dict[str, Any]], int]:
        d = self.http.get("/events", params={"since": since}).json()
        return d["events"], d["next"]

    def healthy(self) -> bool:
        try:
            return self.http.get("/health", timeout=1.0).status_code == 200
        except httpx.HTTPError:
            return False


def java_bin() -> str:
    tools = ROOT / ".tools"
    for d in sorted(tools.glob("jdk-*")) if tools.exists() else []:
        exe = d / "bin" / ("java.exe" if os.name == "nt" else "java")
        if exe.exists():
            return str(exe)
    return "java"


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def wait_port_free(port: int, timeout_s: float = 15.0) -> None:
    """A just-killed JVM can hold its listening socket for a moment; never start a second app on top of it."""
    deadline = time.monotonic() + timeout_s
    while not port_free(port):
        if time.monotonic() > deadline:
            raise SurfaceError(f"port {port} is still in use (another CoreLink/bridge running?). "
                               "Close CoreLink windows or kill java.exe, then retry.")
        time.sleep(0.25)


def launch_corelink(tenant: str = "heritage", port: int = 8740, faults_file: Path | None = None,
                    idle_timeout_s: int = 900, log_file: Path | None = None) -> tuple[subprocess.Popen, SwingSurface]:
    """Start the CoreLink mock with the bridge attached and wait until the bridge answers."""
    jar = Path(os.environ.get("CUA_BRIDGE_JAR", str(ROOT / "bridge" / "cua-bridge.jar")))  # override for testing
    classes = ROOT / "mockcore" / "out"
    if not jar.exists() or not classes.exists():
        raise SurfaceError("build first: scripts/build.ps1 (missing bridge jar or mockcore classes)")
    faults = faults_file or (ROOT / "runs" / "faults.properties")
    cmd = [java_bin(), f"-javaagent:{jar}=port={port}", f"-Dcorelink.faults={faults}",
           f"-Dcorelink.idleTimeoutSec={idle_timeout_s}", "-cp", str(classes), "com.corelink.Main",
           f"--tenant={tenant}"]
    wait_port_free(port)
    log_start = Path(log_file).stat().st_size if log_file and Path(log_file).exists() else 0
    out = open(log_file, "ab") if log_file else subprocess.DEVNULL
    proc = subprocess.Popen(cmd, stdout=out, stderr=out, cwd=str(ROOT))
    surface = SwingSurface(port)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SurfaceError("CoreLink exited during startup")
        if log_file and "BindException" in Path(log_file).read_bytes()[log_start:].decode(errors="ignore"):
            proc.kill()
            raise SurfaceError(f"bridge could not bind port {port}: another instance is running")
        if surface.healthy():
            try:
                if surface.observe().windows:
                    return proc, surface
            except SurfaceError:
                pass
        time.sleep(0.3)
    proc.kill()
    raise SurfaceError("bridge did not come up within 20s")
