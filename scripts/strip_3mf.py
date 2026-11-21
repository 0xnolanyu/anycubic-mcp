"""从 3mf 的 project_settings.config 里剔除「自带脚本/G-code/连接」类键，生成可被 Remote API 打开的副本。
用法: python strip_3mf.py <src.3mf> <dst.3mf>
"""
import json, os, sys, zipfile

BLOCK_KEYS = [
    "change_filament_gcode", "filament_end_gcode", "filament_start_gcode", "filename_format",
    "host_type", "layer_change_gcode", "machine_end_gcode", "machine_pause_gcode",
    "machine_start_gcode", "printer_model", "printer_technology", "printhost_authorization_type",
    "printhost_ssl_ignore_revoke", "time_lapse_gcode",
]


def strip(src: str, dst: str, extra=None) -> dict:
    keys = list(BLOCK_KEYS) + list(extra or [])
    zin = zipfile.ZipFile(src)
    out = {}
    removed = []
    for item in zin.infolist():
        data = zin.read(item.filename)
        if item.filename == "Metadata/project_settings.config":
            cfg = json.loads(data.decode("utf-8"))
            for k in keys:
                if k in cfg:
                    cfg.pop(k)
                    removed.append(k)
            # 动态补扫：任何 *_gcode 脚本键（如 wrapping_detection_gcode）都会触发远端 API 的
            # blocked_by_remote_api_policy，值非空即剔除（空值不拦，留着无害）
            for k, v in list(cfg.items()):
                if k.endswith("_gcode") and isinstance(v, str) and v.strip() and k not in removed:
                    cfg.pop(k)
                    removed.append(k)
            data = json.dumps(cfg, ensure_ascii=False).encode("utf-8")
        out[item.filename] = data
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in out.items():
            z.writestr(name, data)
    return {"dst": dst, "removed": removed, "entries": len(out)}


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    r = strip(src, dst)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(r, ensure_ascii=False))
