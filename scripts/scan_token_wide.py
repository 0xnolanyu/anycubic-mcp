# -*- coding: utf-8 -*-
"""UTF-16LE 版本：扫 pid 内存里的宽字符 JWT（Windows 上 QString/std::wstring 常见）"""
import ctypes, ctypes.wintypes as wt, json, os, re, sys, time
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

PID = int(sys.argv[1])

class MBI64(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", wt.DWORD), ("__a1", wt.DWORD), ("RegionSize", ctypes.c_ulonglong),
                ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD), ("__a2", wt.DWORD)]

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI64), ctypes.c_size_t]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

h = k32.OpenProcess(0x10 | 0x043A, False, PID)
if not h:
    print("OPEN_FAIL", ctypes.get_last_error()); sys.exit(1)

# 宽字符 JWT：每个 ASCII 字符后跟 \x00
PAT_W = re.compile(rb"e\x00y\x00J\x00[A-Za-z0-9_\-\.\x00]{60,900}")
PAT_ASCII = re.compile(rb"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}")

mbi = MBI64(); addr = 0; scanned = 0; found = []
t0 = time.time()
while addr < 0x7FFFFFFFFFFF:
    if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        break
    size = mbi.RegionSize
    if size == 0:
        break
    prot = mbi.Protect
    readable = (mbi.State == 0x1000) and (prot & 0x02 or prot & 0x04 or prot & 0x20 or prot & 0x40) and not (prot & 0x01)
    if readable and size <= 256 * 1024 * 1024:
        off = 0
        while off < size:
            chunk = min(4 * 1024 * 1024, size - off)
            buf = ctypes.create_string_buffer(chunk)
            rd = ctypes.c_size_t(0)
            if k32.ReadProcessMemory(h, ctypes.c_void_p(mbi.BaseAddress + off), buf, chunk, ctypes.byref(rd)):
                data = buf.raw[:rd.value]
                scanned += len(data)
                for m in PAT_W.finditer(data):
                    try:
                        s = m.group(0).decode("utf-16-le", "ignore")
                    except Exception:
                        continue
                    if 120 <= len(s) <= 400 and s not in found:
                        found.append(s)
                        print("WIDE HIT", len(s), s[:26], flush=True)
                for m in PAT_ASCII.finditer(data):
                    s = m.group(0).decode("ascii", "ignore")
                    if 200 <= len(s) <= 320 and s not in found:
                        found.append(s)
                        print("ASCII HIT", len(s), s[:26], flush=True)
            off += chunk
    addr = mbi.BaseAddress + size

k32.CloseHandle(h)
print(f"DONE scanned={scanned/1e6:.0f}MB {time.time()-t0:.0f}s hits={len(found)}")
if found:
    import tempfile, pathlib
    p = os.path.join(os.environ.get("KX_TMP", tempfile.gettempdir()), f"token_wide_{PID}.json")
    json.dump(found, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("WROTE", p)
