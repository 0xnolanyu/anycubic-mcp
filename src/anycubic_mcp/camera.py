"""Agora camera capture — wraps the native kx_cam.exe puller (see native/).

Flow per capture (no resident process):
  1. cloud.camera_open(printer_id)  -> shengwang params (fresh every call)
  2. write params json (ensure_ascii=False! the exe can't parse \\uXXXX)
  3. run kx_cam.exe (path from config) -> writes a JPEG frame
  4. cloud.camera_close(printer_id)
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time


class CameraError(RuntimeError):
    pass


def capture_frame(cloud, printer_id: int, kx_cam_exe: str,
                  workdir: str | None = None, timeout_s: int = 40) -> str:
    """Returns path to the captured JPEG frame."""
    workdir = workdir or os.path.dirname(kx_cam_exe)
    sw = cloud.camera_open(printer_id)
    if not sw:
        raise CameraError("camera_open returned no shengwang data (printer asleep?)")
    cfg_path = os.path.join(workdir, "cfg.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(sw, f, ensure_ascii=False)
    try:
        out = os.path.join(workdir, "cam_cpp.jpg")
        if os.path.exists(out):
            os.remove(out)
        r = subprocess.run([kx_cam_exe], cwd=workdir, capture_output=True,
                           text=True, timeout=timeout_s)
        if not os.path.exists(out) or os.path.getsize(out) < 2048:
            raise CameraError(f"kx_cam produced no frame (exit={r.returncode}, "
                              f"stderr tail: {r.stderr[-200:] if r.stderr else 'none'})")
        return out
    finally:
        try:
            cloud.camera_close(printer_id)
        except Exception:
            pass
