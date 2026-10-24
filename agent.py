#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PC Monitor agent
- Meet CPU / RAM / schijven / netwerk / sensoren (via LibreHardwareMonitor)
- Serveert dashboard + stats.json lokaal (http://PC-IP:8765)
- Pusht stats.json periodiek naar een 'data'-branch op GitHub

Vereist:  pip install psutil   (+ git geinstalleerd + LibreHardwareMonitor met Remote Web Server aan)
"""
import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

import psutil

# ====================== CONFIG ======================
GITHUB_REPO_URL = "https://github.com/JOUWNAAM/pc-monitor.git"  # <-- aanpassen
GITHUB_BRANCH = "data"          # branch waar stats.json op komt (NIET je Pages-branch)
ENABLE_GITHUB_PUSH = True
PUSH_INTERVAL = 30              # seconden tussen pushes naar GitHub
SAMPLE_INTERVAL = 1.0           # seconden tussen metingen
HTTP_PORT = 8765                # lokale server (dashboard + stats.json)
LHM_URL = "http://localhost:8085/data.json"   # LibreHardwareMonitor Remote Web Server
PUBLISH_PROCESSES = False       # procesnamen publiek op GitHub zetten? (liever niet)
# ====================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data-repo")
LOG_FILE = os.path.join(BASE_DIR, "agent.log")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

LOCK = threading.Lock()
LATEST = {}
SPECS = {}
_state = {"net": None, "io": None, "t": None, "proc": [], "proc_t": 0.0}


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run(cmd, timeout=60):
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=NO_WINDOW, encoding="utf-8", errors="replace",
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


# ====================== SPECS (eenmalig) ======================
PS_SCRIPT = r"""
$ErrorActionPreference='SilentlyContinue'
$o=[ordered]@{
 cpu=@(Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,L2CacheSize,L3CacheSize)
 gpu=@(Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,DriverVersion,CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate)
 ram=@(Get-CimInstance Win32_PhysicalMemory | Select-Object Manufacturer,PartNumber,Capacity,Speed,ConfiguredClockSpeed)
 board=@(Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product)
 bios=@(Get-CimInstance Win32_BIOS | Select-Object Manufacturer,SMBIOSBIOSVersion)
 os=@(Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,OSArchitecture,BuildNumber)
 disks=@(Get-CimInstance Win32_DiskDrive | Select-Object Model,Size,InterfaceType)
 system=@(Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model)
}
$o | ConvertTo-Json -Depth 4 -Compress
"""


def collect_specs():
    specs = {
        "system": "",
        "os": platform.platform(),
        "cpu": {
            "name": platform.processor(),
            "cores": psutil.cpu_count(logical=False),
            "threads": psutil.cpu_count(logical=True),
            "max_mhz": None, "l2_kb": None, "l3_kb": None,
        },
        "gpus": [],
        "ram": {"total": psutil.virtual_memory().total, "modules": []},
        "board": "",
        "bios": "",
        "disks": [],
    }
    if sys.platform != "win32":
        return specs

    rc, out, err = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SCRIPT])
    try:
        raw = json.loads(out)
    except Exception:
        log("Specs ophalen via PowerShell mislukt: " + (err or out)[:200])
        return specs

    s = as_list(raw.get("system"))
    if s:
        specs["system"] = f"{s[0].get('Manufacturer') or ''} {s[0].get('Model') or ''}".strip()

    c = as_list(raw.get("cpu"))
    if c:
        c = c[0]
        specs["cpu"].update({
            "name": (c.get("Name") or specs["cpu"]["name"]).strip(),
            "cores": c.get("NumberOfCores") or specs["cpu"]["cores"],
            "threads": c.get("NumberOfLogicalProcessors") or specs["cpu"]["threads"],
            "max_mhz": c.get("MaxClockSpeed"),
            "l2_kb": c.get("L2CacheSize"),
            "l3_kb": c.get("L3CacheSize"),
        })

    for g in as_list(raw.get("gpu")):
        res = ""
        if g.get("CurrentHorizontalResolution"):
            res = f"{g['CurrentHorizontalResolution']}x{g['CurrentVerticalResolution']}"
            if g.get("CurrentRefreshRate"):
                res += f" @ {g['CurrentRefreshRate']} Hz"
        specs["gpus"].append({
            "name": (g.get("Name") or "").strip(),
            "vram_bytes": g.get("AdapterRAM"),   # WMI kapt dit af op 4 GB; zie nvidia-smi hieronder
            "driver": g.get("DriverVersion") or "",
            "resolution": res,
        })

    # Nauwkeurige VRAM voor NVIDIA-kaarten
    rc, out, _ = run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], timeout=10)
    if rc == 0:
        for line in out.splitlines():
            name, _, mem = line.partition(",")
            name = name.strip().lower()
            try:
                mb = int(mem.strip())
            except ValueError:
                continue
            for g in specs["gpus"]:
                gn = g["name"].lower()
                if name and (name in gn or gn in name):
                    g["vram_bytes"] = mb * 1024 * 1024

    for m in as_list(raw.get("ram")):
        specs["ram"]["modules"].append({
            "size": m.get("Capacity"),
            "speed": m.get("ConfiguredClockSpeed") or m.get("Speed"),
            "maker": (m.get("Manufacturer") or "").strip(),
            "part": (m.get("PartNumber") or "").strip(),
        })

    b = as_list(raw.get("board"))
    if b:
        specs["board"] = f"{b[0].get('Manufacturer') or ''} {b[0].get('Product') or ''}".strip()

    bi = as_list(raw.get("bios"))
    if bi:
        specs["bios"] = f"{bi[0].get('Manufacturer') or ''} {bi[0].get('SMBIOSBIOSVersion') or ''}".strip()

    o = as_list(raw.get("os"))
    if o:
        specs["os"] = f"{o[0].get('Caption') or ''} {o[0].get('OSArchitecture') or ''} (build {o[0].get('BuildNumber') or '?'})".strip()

    for d in as_list(raw.get("disks")):
        specs["disks"].append({
            "model": (d.get("Model") or "").strip(),
            "size": d.get("Size"),
            "iface": d.get("InterfaceType") or "",
        })
    return specs


# ====================== LIBREHARDWAREMONITOR ======================
NUM_RE = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*(.*)$")


def fetch_lhm():
    try:
        with urlopen(LHM_URL, timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def hw_kind(name, image):
    s = (image or "").lower()
    n = (name or "").lower()
    if "cpu" in s or re.search(r"ryzen|intel\(r\) core|core i\d|core ultra|xeon|athlon|threadripper|pentium|celeron|\bcpu\b", n):
        return "cpu"
    if any(k in s for k in ("nvidia", "ati", "gpu")) or re.search(r"geforce|radeon|\brtx\b|\bgtx\b|arc a\d|quadro", n):
        return "gpu"
    if "mainboard" in s or "chip" in s:
        return "board"
    if "hdd" in s or "nvme" in n or "ssd" in n:
        return "storage"
    if "ram" in s or "memory" in n:
        return "ram"
    return "other"


def walk(node, path, hw, out):
    text = node.get("Text", "")
    p = path + [text]
    if len(p) == 3:  # Sensor > Computer > Hardware
        hw = {"name": text, "kind": hw_kind(text, node.get("ImageURL", ""))}
    children = node.get("Children") or []
    if children:
        for c in children:
            walk(c, p, hw, out)
    elif hw is not None and len(p) >= 5:
        m = NUM_RE.match(node.get("Value", "") or "")
        if m:
            out.append({
                "hw": hw["name"], "kind": hw["kind"], "group": p[-2], "name": text,
                "value": float(m.group(1).replace(",", ".")),
                "unit": m.group(2).strip(),
            })


def parse_lhm(tree):
    flat = []
    walk(tree, [], None, flat)
    keep = []
    for s in flat:
        if s["unit"] in ("°C", "RPM", "W", "MHz", "GHz") or (s["unit"] == "%" and s["kind"] == "gpu"):
            keep.append(s)
    return keep


def pick(sensors, kind, unit, prefs):
    cand = [s for s in sensors if s["kind"] == kind and s["unit"] == unit]
    for p in prefs:
        for s in cand:
            if p in s["name"].lower():
                return s["value"]
    return max((s["value"] for s in cand), default=None)


def avg_core_clock(sensors):
    vals = []
    for s in sensors:
        if s["kind"] == "cpu" and s["name"].lower().startswith("core #") and s["unit"] in ("MHz", "GHz"):
            vals.append(s["value"] * (1000 if s["unit"] == "GHz" else 1))
    return sum(vals) / len(vals) if vals else None


# ====================== METEN ======================
def top_processes(n=8):
    cores = psutil.cpu_count() or 1
    agg = {}
    for p in psutil.process_iter(["name", "cpu_percent", "memory_info"]):
        try:
            i = p.info
            name = i["name"]
            if not name or name == "System Idle Process":
                continue
            mi = i["memory_info"]
            a = agg.setdefault(name, {"name": name, "cpu": 0.0, "mem": 0, "count": 0})
            a["cpu"] += (i["cpu_percent"] or 0) / cores
            a["mem"] += mi.rss if mi else 0
            a["count"] += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    rows = sorted(agg.values(), key=lambda r: (r["cpu"], r["mem"]), reverse=True)[:n]
    for r in rows:
        r["cpu"] = round(r["cpu"], 1)
    return rows


def sample():
    now = time.time()
    dt = (now - _state["t"]) if _state["t"] else None

    cpu_total = psutil.cpu_percent(interval=None)
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    # Sensoren
    sensors = []
    lhm_ok = False
    tree = fetch_lhm()
    if tree:
        sensors = parse_lhm(tree)
        lhm_ok = True

    cpu_temp = pick(sensors, "cpu", "°C", ["cpu package", "tctl/tdie", "core max", "tdie", "tctl", "average"])
    if cpu_temp is None and hasattr(psutil, "sensors_temperatures"):
        try:
            allt = [t.current for lst in psutil.sensors_temperatures().values() for t in lst]
            cpu_temp = max(allt) if allt else None
        except Exception:
            pass
    cpu_power = pick(sensors, "cpu", "W", ["cpu package", "package"])
    gpu_temp = pick(sensors, "gpu", "°C", ["gpu core"])
    gpu_load = pick(sensors, "gpu", "%", ["gpu core"])

    freq = avg_core_clock(sensors)
    if freq is None:
        f = psutil.cpu_freq()
        freq = f.current if f else None

    # Netwerk + schijf-IO snelheid
    net = psutil.net_io_counters()
    io = psutil.disk_io_counters()
    down = up = rd = wr = None
    if dt and _state["net"] and dt > 0:
        down = max(0, (net.bytes_recv - _state["net"].bytes_recv) / dt)
        up = max(0, (net.bytes_sent - _state["net"].bytes_sent) / dt)
    if dt and _state["io"] and io and dt > 0:
        rd = max(0, (io.read_bytes - _state["io"].read_bytes) / dt)
        wr = max(0, (io.write_bytes - _state["io"].write_bytes) / dt)
    _state["net"], _state["io"], _state["t"] = net, io, now

    # Partities
    disks = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append({"mount": part.mountpoint, "total": u.total, "used": u.used, "percent": u.percent})
        except Exception:
            pass

    # Top processen (elke 3 s)
    if now - _state["proc_t"] >= 3:
        try:
            _state["proc"] = top_processes()
        except Exception:
            pass
        _state["proc_t"] = now

    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "epoch": round(now, 1),
        "host": socket.gethostname(),
        "lhm": lhm_ok,
        "cpu": {
            "percent": cpu_total, "per_core": per_core, "freq_mhz": freq,
            "temp_c": cpu_temp, "power_w": cpu_power,
        },
        "gpu": {"temp_c": gpu_temp, "load": gpu_load},
        "ram": {"total": mem.total, "used": mem.used, "available": mem.available, "percent": mem.percent},
        "swap": {"total": swap.total, "used": swap.used, "percent": swap.percent},
        "disks": disks,
        "disk_io": {"read_bps": rd, "write_bps": wr},
        "net": {"down_bps": down, "up_bps": up},
        "uptime_s": int(now - psutil.boot_time()),
        "sensors": sensors,
        "processes": _state["proc"],
    }


def get_payload(public=False):
    with LOCK:
        data = dict(LATEST)
    if not data:
        return {}
    data["specs"] = SPECS
    if public:
        data.pop("host", None)
        if not PUBLISH_PROCESSES:
            data.pop("processes", None)
    return data


# ====================== LOKALE SERVER ======================
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/stats.json":
            self._send(200, json.dumps(get_payload(False)).encode("utf-8"), "application/json; charset=utf-8")
        elif path in ("/", "/index.html"):
            try:
                with open(os.path.join(BASE_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(404, b"index.html niet gevonden naast agent.py", "text/plain")
        else:
            self._send(404, b"Not found", "text/plain")


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def serve():
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
        log(f"Lokaal dashboard: http://{local_ip()}:{HTTP_PORT}")
        srv.serve_forever()
    except Exception as e:
        log(f"Lokale server kon niet starten: {e}")


# ====================== GITHUB PUSH ======================
def git(*args):
    return run(["git", "-C", DATA_DIR, *args], timeout=120)


def init_repo():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.isdir(os.path.join(DATA_DIR, ".git")):
        git("init")
        git("symbolic-ref", "HEAD", f"refs/heads/{GITHUB_BRANCH}")
        git("remote", "add", "origin", GITHUB_REPO_URL)
        git("config", "user.name", "pc-monitor")
        git("config", "user.email", "pc-monitor@users.noreply.github.com")
    else:
        git("remote", "set-url", "origin", GITHUB_REPO_URL)


def push_once():
    payload = get_payload(public=True)
    if not payload:
        return
    with open(os.path.join(DATA_DIR, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    git("add", "stats.json")
    has_head = git("rev-parse", "--verify", "HEAD")[0] == 0
    if has_head:
        # amend + force push: de repo blijft 1 commit groot, geen geschiedenis die opzwelt
        rc, _, err = git("commit", "--amend", "--allow-empty", "-q", "-m", "update")
    else:
        rc, _, err = git("commit", "-q", "-m", "init")
    if rc != 0:
        log("git commit mislukt: " + err[:300])
        return
    rc, _, err = git("push", "-f", "-q", "origin", GITHUB_BRANCH)
    if rc != 0:
        log("git push mislukt: " + err[:300])


def pusher():
    while not LATEST:
        time.sleep(1)
    init_repo()
    while True:
        try:
            push_once()
        except Exception as e:
            log(f"Push-fout: {e}")
        time.sleep(PUSH_INTERVAL)


# ====================== MAIN ======================
def main():
    global SPECS
    log("Agent gestart, specs verzamelen...")
    SPECS = collect_specs()
    psutil.cpu_percent(None)
    psutil.cpu_percent(None, percpu=True)

    threading.Thread(target=serve, daemon=True).start()
    if ENABLE_GITHUB_PUSH:
        threading.Thread(target=pusher, daemon=True).start()

    while True:
        t0 = time.time()
        try:
            data = sample()
            with LOCK:
                LATEST.clear()
                LATEST.update(data)
        except Exception as e:
            log(f"Meetfout: {e}")
        time.sleep(max(0.05, SAMPLE_INTERVAL - (time.time() - t0)))


if __name__ == "__main__":
    main()