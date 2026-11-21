"""把一个 3mf 工程切成单板、逐件（by object）的多色 gcode，并做颜色机械核对。

用法（在 06_脚本 目录下）：
    python slice_model.py <name> <3mf路径> <remap.json> [--support 0|1] [--out gcode]

remap.json 形如：
{
  "colors": ["#009639", "#8000FF", "#212721", "#DCF478"],   # 4 个槽的颜色（按槽序）
  "slots":  [3, 0, 2, 1],                                    # T i -> 物理槽（0 基）
  "map": {"1": 1, "3": 2, "4": 3}                            # 原 extruder -> 新 extruder(1..4)
}

流程：strip 掉脚本键 -> 打开 -> 按 map 改 extruder -> 全部搬到 plate0 -> arrange
      -> 写打印参数 -> 切片 -> 取 plate0 gcode -> 写 sidecar -> colour_map gate
"""
import sys, os, json, time, requests, importlib.util

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API = os.environ.get("ORCA_API", "http://127.0.0.1:13130/api/v1")
H = {"X-Api-Token": os.environ.get("ORCA_API_TOKEN", "")}
HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.abspath(os.environ.get("KX_WS") or os.path.dirname(HERE))
TMPDIR = os.path.abspath(os.environ.get("KX_TMP") or os.path.join(WS, "_tmp3d"))
os.makedirs(TMPDIR, exist_ok=True)
sys.path.insert(0, HERE)
import strip_3mf  # noqa: E402


def _load_colour_map():
    spec = importlib.util.spec_from_file_location("colour_map", os.path.join(HERE, "colour_map.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def api(method, path, **kw):
    r = requests.request(method, API + path, headers=H, timeout=kw.pop("timeout", 120), **kw)
    return r


def patch_extruders(src, remap):
    """把 3mf 里 >4 的 extruder 编号静态改写成 1..4。

    Orca 按当前工程槽数裁剪，ext6 打开后会变成 1，颜色就丢了；必须在打开前改。
    """
    import zipfile, re
    if not remap:
        return src
    zin = zipfile.ZipFile(src)
    ents = {i.filename: zin.read(i.filename) for i in zin.infolist()}
    name = "Metadata/model_settings.config"
    txt = ents[name].decode("utf-8")
    txt = re.sub(r'key="extruder" value="(\d+)"',
                 lambda m: 'key="extruder" value="%s"' % remap.get(m.group(1), m.group(1)),
                 txt)
    ents[name] = txt.encode("utf-8")
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in ents.items():
            z.writestr(k, v)
    return src


def patch_plate_sequence(src):
    """把 model_settings.config 里 plate 级 print_sequence 从 by layer 改成 by object。

    不 patch 的话 PUT /config 的 print_sequence 会被 plate 元数据压过，切片仍是逐层。
    """
    import zipfile
    zin = zipfile.ZipFile(src)
    ents = {i.filename: zin.read(i.filename) for i in zin.infolist()}
    for name in ("Metadata/model_settings.config",):
        if name in ents:
            txt = ents[name].decode("utf-8")
            new = txt.replace('key="print_sequence" value="by layer"', 'key="print_sequence" value="by object"')
            ents[name] = new.encode("utf-8")
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in ents.items():
            z.writestr(k, v)
    return src


def pack_on_plate0(gap=None, bed=255.0, margin=15.0):
    """两列货架排版：列间距 50mm、行间距 50mm，满足 by object 的龙门避让要求。"""
    gap = float(os.environ.get("PACK_GAP", 80))
    objs = list_objects()
    items = []
    for o in objs:
        w = o["bbox_max"][0] - o["bbox_min"][0]
        h = o["bbox_max"][1] - o["bbox_min"][1]
        items.append([o, w, h])
    items.sort(key=lambda t: -t[2])
    maxw = max(i[1] for i in items)
    cols = [margin, margin + maxw + gap]           # 左列 / 右列 起始 x
    cursors = [margin, margin]                     # 每列当前 y
    for it in items:
        o, w, h = it
        order = [0, 1] if cursors[0] <= cursors[1] else [1, 0]
        placed = False
        for c in order:
            if cursors[c] + h <= bed:
                x = cols[c]
                y = cursors[c]
                r = api("POST", f"/objects/{o['id']}/transform",
                        json={"translate": [x - o["bbox_min"][0], y - o["bbox_min"][1], 0]}, timeout=60)
                print(f"  pack obj {o['id']}: {w:.1f}x{h:.1f} -> col{c} x={x:.1f} y={y:.1f} {r.status_code}")
                cursors[c] = y + h + gap
                placed = True
                break
        if not placed:
            print(f"  !! obj {o['id']} {w:.1f}x{h:.1f} 放不下（cursors={cursors}）")
    for o in list_objects():
        print(f"  final obj {o['id']} bb {[round(v,1) for v in o['bbox_min']]} .. {[round(v,1) for v in o['bbox_max']]}")


def select_presets():
    names = [("printer", "Anycubic Kobra X 0.4 nozzle"),
             ("print", "0.16mm High Quality @Anycubic Kobra X 0.4 nozzle"),
             ("filament", "Anycubic PLA @Anycubic Kobra X 0.4 nozzle")]
    for t, n in names:
        r = api("PUT", "/preset", json={"type": t, "name": n}, timeout=120)
        print(f"[preset] {t} {r.status_code} {r.text[:100]}")


EXTRA_REMAP = {}


def open_project(three_mf, clean_path):
    strip_3mf.strip(three_mf, clean_path)
    patch_plate_sequence(clean_path)
    patch_extruders(clean_path, EXTRA_REMAP.get("map", {}))
    r = api("POST", "/project/open", json={"path": clean_path, "if_unsaved": "discard"}, timeout=300)
    if r.status_code != 200:
        raise SystemExit(f"open failed {r.status_code}: {r.text[:300]}")
    return r.json()


def list_objects():
    return api("GET", "/objects", timeout=60).json()["objects"]


def set_extruder(oid, n):
    r = api("PUT", f"/objects/{oid}/config", json={"extruder": n}, timeout=60)
    if r.status_code != 200:
        raise SystemExit(f"set extruder {oid} -> {n} failed: {r.status_code} {r.text[:200]}")


def move_to_plate(oid, plate=0):
    r = api("POST", f"/objects/{oid}/plate", json={"plate": plate}, timeout=60)
    return r.status_code, r.text[:150]


def main():
    name = sys.argv[1]
    three_mf = sys.argv[2]
    cfg_path = sys.argv[3]
    support = 1
    out = None
    keep = None
    ext_size = []
    keep_size = None
    args = sys.argv[4:]
    for i, a in enumerate(args):
        if a == "--support":
            support = int(args[i + 1])
        if a == "--out":
            out = args[i + 1]
        if a == "--keep":
            keep = [int(x) for x in args[i + 1].split(",") if x.strip()]
        if a == "--ext-size":
            ext_size = [x.strip() for x in args[i + 1].split(",") if x.strip()]
        if a == "--keep-size":
            keep_size = []
            for part in args[i + 1].split(","):
                if "x" in part:
                    w, h = part.split("x")
                    keep_size.append((float(w), float(h)))

    cfg = json.load(open(cfg_path, encoding="utf-8"))
    colors, slots, remap = cfg["colors"], cfg["slots"], cfg["map"]
    EXTRA_REMAP["map"] = remap
    clean = os.path.join(TMPDIR, f"{name}_clean.3mf")

    info = open_project(three_mf, clean)
    select_presets()
    print(f"[open] plates={info.get('plates')} objects={info.get('objects')}")

    objs = list_objects()
    print(f"[objects] {len(objs)} 个")
    todo = []
    for o in objs:
        orig = int(o["config"].get("extruder") or 1)
        plat = o.get("plate")
        size = [round(x, 1) for x in o["size_mm"]]
        todo.append((o["id"], orig, plat, size, [round(x, 1) for x in o["transform"]["offset"]]))
        print(f"  id={o['id']} orig_ext={orig} plate={plat} size={size}")
    print(f"[plan] 原 extruder -> 新 extruder: {remap}")

    for oid, orig, plat, size, off in todo:
        hit = None
        if keep_size is not None:
            for t in keep_size:
                if abs(size[0] - t[0]) < 0.6 and abs(size[1] - t[1]) < 0.6:
                    hit = t
                    break
        if keep_size is not None and hit is None:
            r = api("DELETE", f"/objects/{oid}", timeout=60)
            print(f"  obj {oid}: size {size[0]}x{size[1]} 不在 keep-size -> 删除 {r.status_code}")
            continue
        if keep_size is None and keep is not None and orig not in keep:
            r = api("DELETE", f"/objects/{oid}", timeout=60)
            print(f"  obj {oid}: ext {orig} 不在 keep={keep} -> 删除 {r.status_code}")
            continue
        new = remap.get(str(orig))
        if not new:
            print(f"  !! 缺映射 ext={orig} (obj {oid})")
            continue
        if ext_size:
            for spec in ext_size:
                sz, ti = spec.split(":")
                parts = [float(v) for v in sz.split("x")]
                if (abs(size[0] - parts[0]) < 0.8 and abs(size[1] - parts[1]) < 0.8):
                    print(f"  obj {oid}: size {size} -> extruder {ti}（按尺寸覆盖）")
                    set_extruder(oid, int(ti))
        st, body = move_to_plate(oid, 0)
        print(f"  obj {oid}: ext={orig} move {st} {body[:80]}")

    # 让 Orca 重排，避免搬到同板后重叠
    # 自己排版：Orca 的 arrange 会把对象挪到别的板（我们只要 plate0），所以手工货架排
    pack_on_plate0()

    # 打印参数：工程里的耗材槽数可能是 10（原工程）而不是 4，per-filament 键必须跟槽数等长
    nf = 4
    try:
        st = api("GET", "/status", timeout=30).json()
        nf = max(1, len((st.get("presets") or {}).get("filaments") or [])) or 4
    except Exception:
        pass
    pad = colors + [colors[-1]] * (nf - len(colors)) if nf > len(colors) else colors[:nf]
    print(f"[filament] 工程耗材槽数={nf}, 下发色数={len(pad)}")
    body = {
        "filament_colour": ";".join(pad),
        "nozzle_temperature": "205",
        "hot_plate_temp": "60",
        "layer_height": "0.16",
        "initial_layer_print_height": "0.2",
        "wall_loops": 3,
        "sparse_infill_density": "15%",
        "outer_wall_speed": 100,
        "inner_wall_speed": 120,
        "top_surface_speed": 100,
        "sparse_infill_speed": 170,
        "initial_layer_speed": 45,
        "brim_type": "outer_only",
        "brim_width": 5,
        "enable_support": "1" if support else "0",
        "support_type": "tree(auto)",
        "support_threshold_angle": 25,
        "print_sequence": "by object",
    }
    r = api("PUT", "/config", json=body, timeout=120)
    if r.status_code != 200:
        # 每槽一个值时温度必须给 N 个；退化为逐槽列
        n = len(colors)
        body["nozzle_temperature"] = ",".join(["205"] * n)
        body["hot_plate_temp"] = ",".join(["60"] * n)
        r = api("PUT", "/config", json=body, timeout=120)
    print(f"[config] {r.status_code} {r.text[:260]}")

    # 切片
    for attempt in range(3):
        r = api("POST", "/slice", json={}, timeout=180)
        print(f"[slice] try{attempt} {r.status_code} {r.text[:120]}")
        if r.status_code in (200, 202):
            break
        time.sleep(4)

    for rnd in range(3):
        for _ in range(120):
            s = api("GET", "/status", timeout=30).json()
            if not s.get("slicing") and s.get("slice_result_valid"):
                break
            time.sleep(5)
        st = api("GET", "/status", timeout=30).json()
        if st.get("slice_result_valid"):
            break
        print(f"[slice] valid=False，第 {rnd+1} 次重试（Orca 打开工程后首切常空转）")
        time.sleep(3)
        api("POST", "/slice", json={}, timeout=180)
    print(f"[slice done] valid={st.get('slice_result_valid')} keys={sorted(st.keys())}")
    print(f"[estimate] {json.dumps({k: v for k, v in st.items() if 'time' in k or 'est' in k or 'filament' in k}, ensure_ascii=False)[:300]}")

    g = api("GET", "/gcode?plate=0", timeout=300)
    if g.status_code != 200 or len(g.content) < 1000:
        raise SystemExit(f"gcode fetch failed {g.status_code} bytes={len(g.content)} {g.text[:200]}")
    out = out or os.path.join(WS, "00_待打印", f"{name}.gcode")
    open(out, "wb").write(g.content)
    print(f"[gcode] {out} {len(g.content)} bytes")

    hdr = g.content.decode("utf-8", errors="replace")[:200000]
    seq = [ln for ln in hdr.splitlines() if "print_sequence" in ln][:1]
    col = [ln for ln in hdr.splitlines() if ln.strip().startswith("; filament_colour")][:1]
    ts = sorted({int(ln.split("T")[1].split()[0]) for ln in hdr.splitlines() if ln.startswith("T") and ln.split("T")[1][:1].isdigit()})
    print(f"  seq={seq} colour={col} T={ts}")

    # sidecar 只记来源与计划（件名在 gcode 里都是「组合体_id_N」，不能当 key）
    side = {"source_3mf": os.path.basename(three_mf), "colors": colors, "slots": slots, "remap": remap}
    open(out + ".colourmap.json", "w", encoding="utf-8").write(json.dumps(side, ensure_ascii=False, indent=2))

    cm = _load_colour_map()
    try:
        res = cm.gate(out, slots)
        print("[gate]", json.dumps({k: res.get(k) for k in ("ok", "fails", "warns")}, ensure_ascii=False))
    except Exception as e:
        print("[gate] skipped:", repr(e)[:80])
    print("[path]", out)


if __name__ == "__main__":
    main()
