"""把 3mf 压成单挤出机单色：model_settings 里所有 extruder -> 1，project_settings 只留 1 个 filament。
用法: python _patch_single.py <src.3mf> <dst.3mf> <hex>
"""
import json, re, sys, zipfile

def run(src, dst, hex_colour):
    zin = zipfile.ZipFile(src)
    out, info = {}, []
    for item in zin.infolist():
        data = zin.read(item.filename)
        if item.filename == "Metadata/model_settings.config":
            txt = data.decode("utf-8")
            txt, n = re.subn(r'(key="extruder" value=")(\d+)(")', r'\g<1>1\g<3>', txt)
            info.append(f"extruder patched {n}")
            data = txt.encode("utf-8")
        elif item.filename == "Metadata/project_settings.config":
            cfg = json.loads(data.decode("utf-8"))
            for k in list(cfg):
                if k.startswith("filament_"):
                    v = cfg[k]
                    if isinstance(v, list) and len(v) >= 1:
                        cfg[k] = [v[0]] if k in ("filament_type", "filament_vendor", "filament_soluble", "filament_is_support") else None
                    elif isinstance(v, str) and ";" in v:
                        cfg[k] = v.split(";")[0]
            for k in [k for k, v in cfg.items() if v is None]:
                pass
            cfg["filament_colour"] = [hex_colour]
            cfg["filament_type"] = [cfg.get("filament_type") or ["PLA"]][0]
            data = json.dumps(cfg, ensure_ascii=False).encode("utf-8")
        out[item.filename] = data
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in out.items():
            z.writestr(name, data)
    print("OK", dst, info)

if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3])
