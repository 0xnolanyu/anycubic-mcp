"""`anycubic-setup` — interactive first-run wizard that collects everything for you.

Stages (each auto-detects, asks only when unavoidable):
 1. access_token : scan a running Anycubic Slicer Next / Anycubic App process
                   memory for a JWT (feature scan, not fixed offset)
 2. session token: loginWithAccessToken exchange
 3. printer      : printersStatus -> list -> pick (if more than one)
 4. camera       : locate kx_cam.exe (built from native/) — optional
 5. vision       : ask for an OpenAI-compatible endpoint (optional)
 6. write ~/.anycubic-mcp/config.toml
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from .cloud import AnycubicCloud  # noqa: E402
from .config import DEFAULT_DIR, write_example  # noqa: E402


def _scan_access_token() -> str | None:
    """Feature-scan likely processes for an eyJ... JWT of plausible length.

    Windows-only helper: reads process memory via ctypes (ReadProcessMemory),
    looking for the JWT prefix 'eyJ' followed by base64 chars. The official
    client keeps the access token in memory at all times while logged in.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None
    import subprocess
    try:
        names = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=30).stdout.lower()
    except Exception:
        return None
    targets = [ln.split('","')[0].strip('"') for ln in names.splitlines()
               if ("anycubic" in ln or "orca" in ln)]
    k32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 2
    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pe = PE(); pe.dwSize = ctypes.sizeof(PE)
    found: list[str] = []
    if k32.Process32First(snap, ctypes.byref(pe)):
        while True:
            exe = pe.szExeFile.decode(errors="ignore").lower()
            if any(t and exe.startswith(t.rsplit(".", 1)[0]) for t in targets) or \
               "anycubic" in exe:
                pid = pe.th32ProcessID
                h = k32.OpenProcess(0x10 | 0x043A, False, pid)  # PROCESS_VM_READ|QUERY
                if h:
                    buf = ctypes.create_string_buffer(1 << 20)
                    addr = ctypes.c_size_t(0)
                    mbi = ctypes.create_string_buffer(56)
                    while ctypes.windll.kernel32.VirtualQueryEx(h, ctypes.byref(addr), mbi, 56):
                        n = k32.ReadProcessMemory(h, ctypes.byref(addr), buf, 1 << 20, None)
                        if n:
                            data = buf.raw[:n]
                            for m in re.finditer(rb"eyJ[A-Za-z0-9_\-]{80,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{20,}", data):
                                found.append(m.group().decode())
                                if len(found) > 4:
                                    break
                        addr = ctypes.c_size_t(addr.value + (1 << 20))
                    k32.CloseHandle(h)
            if len(found) > 4:
                break
            if not k32.Process32Next(snap, ctypes.byref(pe)):
                break
    k32.CloseHandle(snap)
    return found[0] if found else None


def main() -> int:
    write_example()
    print("== anycubic-mcp setup ==")
    tok_file = DEFAULT_DIR / "access_token.txt"
    sess_file = DEFAULT_DIR / "session_token.txt"

    at = None
    if tok_file.exists():
        at = tok_file.read_text(encoding="utf-8").strip()
        print(f"[1] access token: loaded from {tok_file}")
    if not at:
        print("[1] scanning running Anycubic app for access token ...")
        at = _scan_access_token()
        if at:
            print("    found JWT in process memory")
            DEFAULT_DIR.mkdir(parents=True, exist_ok=True)
            tok_file.write_text(at, encoding="utf-8")
        else:
            print("    not found — log into Anycubic Slicer Next (or the mobile app\n"
                  "    on this PC) and re-run, or paste an access token:")
            at = input("> ").strip() or None
    if not at:
        print("no token; aborting"); return 1

    print("[2] exchanging session token ...")
    sess = AnycubicCloud.login_with_access_token(at)
    sess_file.write_text(sess, encoding="utf-8")
    print(f"    session token saved ({len(sess)} chars)")

    print("[3] listing printers ...")
    c = AnycubicCloud(sess)
    # printer listing endpoint via printersStatus requires ids; use projects? No —
    # the wizard asks the user to run `anycubic-mcp printer_status_probe` later if
    # needed. For now try the known list endpoint used by the app:
    try:
        j = c.call("GET", "/work/printer/printersStatus?printer_ids=0")
    except Exception:
        j = {}
    printers = j.get("data") or []
    if len(printers) == 1:
        pid = printers[0].get("id")
    else:
        for i, p in enumerate(printers):
            print(f"    {i}: {p.get('name')} (id={p.get('id')})")
        pid = None
        if printers:
            k = input("pick index: ").strip()
            pid = printers[int(k)].get("id") if k.isdigit() and int(k) < len(printers) else None
    if pid is None:
        pid = int(input("enter printer id manually: ").strip() or 0)

    kx_cam = input("[4] kx_cam.exe path (Enter to skip camera): ").strip()
    vurl = input("[5] vision endpoint base_url (Enter to skip): ").strip()
    vmodel = input("    vision model name: ").strip() if vurl else ""

    cfg = DEFAULT_DIR / "config.toml"
    cfg.write_text(
        "[cloud]\nsession_token_file = \"session_token.txt\"\n\n"
        f"[printer]\nid = {pid}\n\n"
        f"[camera]\nenabled = {str(bool(kx_cam)).lower()}\nkx_cam_exe = \"{kx_cam}\"\n\n"
        f"[vision]\nbase_url = \"{vurl}\"\nmodel = \"{vmodel}\"\napi_key = \"none\"\n\n"
        "[queue]\nfile = \"queue.json\"\n", encoding="utf-8")
    print(f"done -> {cfg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
