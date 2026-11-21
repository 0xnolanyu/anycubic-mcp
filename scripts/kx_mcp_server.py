#!/usr/bin/env python3
"""
kx_mcp_server.py — Anycubic Kobra X 云端直传 MCP server (stdio)
工具:
  kx_status()                打印机实时状态（含忙碌/清盘/温度）
  kx_send(file, slots, force_clean, skip_vision)  上传 G-code 并启动打印（默认四查，含视觉清盘）
  kx_bed_photo(vision)       开摄像头截帧识别床面（需 viewer 常驻 8793）
  kx_upload(file)            只上传不打印，返回 cloud_file_id
  kx_print_cloud(file_id, slots)     启动已上传的云文件
token: 环境变量 ANYCUBIC_TOKEN 或脚本内默认文件
"""
import hashlib, json, os, re, sys, time, uuid
import requests
from fastmcp import FastMCP

SALT1 = "f9b3528877c94d5c9c5af32245db46ef"
SALT2 = "0cf75926606049a3937f56b0373b99fb"
APP_VERSION = "2.0.0.6"
PRINTER_ID = int(os.environ.get("KX_PRINTER_ID", "0") or 0)
BASE = "https://cloud-platform.anycubic.com/p/p/workbench/api"
WS = os.path.abspath(os.environ.get("KX_WS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
TOKDIR = os.path.abspath(os.environ.get("KX_TOKEN_DIR") or os.path.join(WS, "_tmp3d"))
os.makedirs(TOKDIR, exist_ok=True)
TOKFILE = os.path.join(TOKDIR, "anycubic_session_token.txt")
ACCESS_TOKEN_FILE = os.path.join(TOKDIR, "anycubic_token.txt")

mcp = FastMCP("kx-cloud")

def _tok():
    return (os.environ.get("ANYCUBIC_TOKEN") or open(TOKFILE).read()).strip()

def _hdr(tok):
    ts = str(int(time.time() * 1000)); n = str(uuid.uuid4())
    sig = hashlib.md5((SALT1 + ts + APP_VERSION + SALT2 + n + SALT1).encode()).hexdigest().upper()
    return {"Xx-Device-Type": "pcf", "Xx-Is-Cn": "1", "XX-Version": APP_VERSION,
            "Xx-Nonce": n, "Xx-Signature": sig, "Xx-Timestamp": ts,
            "XX-Token": tok, "XX-LANGUAGE": "zh-CN", "Content-Type": "application/json"}

def _api(method, path, **kw):
    r = getattr(requests, method)(BASE + path, headers=_hdr(_tok()), timeout=kw.pop("timeout", 60), **kw)
    try: return r.json()
    except Exception: return {"code": -1, "msg": r.text[:200]}

@mcp.tool
def _rgb_hex(c):
    if not isinstance(c, (list, tuple)) or len(c) < 3 or any((not isinstance(x, int)) or x < 0 for x in c[:3]):
        return None
    return "#{:02X}{:02X}{:02X}".format(*c[:3])


@mcp.tool
def kx_rack() -> dict:
    """云端直读真实料架槽色（打印机上报，换料即刷新）。"""
    j = _api("get", f"/work/printer/getPrinters?printer_id={PRINTER_ID}")
    if j.get("code") != 1 or not j.get("data"):
        return {"ok": False, "msg": j.get("msg")}
    colors = j["data"][0].get("color") or []
    slots = {}
    for i in range(4):
        slots[f"槽{i+1}"] = _rgb_hex(colors[i]) if i < len(colors) else None
    return {"ok": True, "slots": slots, "raw": colors[:4]}


def _rack_cloud() -> dict:
    """槽号(1基)->hex；失败返回空。"""
    try:
        r = kx_rack()
        if r.get("ok"):
            return {int(k.replace("槽", "")): v for k, v in r["slots"].items() if v}
    except Exception:
        pass
    return {}


def kx_status() -> dict:
    """KX 实时状态: 忙碌/清盘/温度/原因"""
    j = _api("get", f"/work/printer/printersStatus?printer_ids={PRINTER_ID}")
    if j.get("code") != 1 or not j.get("data"):
        return {"ok": False, "msg": j.get("msg")}
    d = j["data"][0]
    return {"ok": True, "is_printing": d.get("is_printing"), "is_clean_plate": d.get("is_clean_plate"),
            "ready_status": d.get("ready_status"), "reason": d.get("reason"),
            "nozzle_temp": d.get("parameter", {}).get("curr_nozzle_temp"),
            "bed_temp": d.get("parameter", {}).get("curr_hotbed_temp"),
            "print_count": d.get("print_count"), "firmware": d.get("version", {}).get("firmware_version")}


def _norm_name(n: str) -> str:
    """归一化文件名：去扩展名、去 (1)(2) 重复后缀，用于和历史比对。"""
    n = os.path.basename(n or "")
    n = re.sub(r"\.gcode$", "", n, flags=re.I)
    n = re.sub(r"\s*\(\d+\)$", "", n)
    # 去掉“已修版/修正版/已修/修正/v2”等修饰词：改个后缀不能绕过重复检查
    n = re.sub(r"(已修版|修正版|已修|修正|_final|_fixed)", "", n, flags=re.I)
    n = re.sub(r"_+", "_", n).strip("_")
    return n.strip().lower()


def _dup_check(path: str, force_repeat: bool = False) -> dict:
    """开打前查历史：同一文件是否已经成功打完过。已打过则拒绝，除非 force_repeat。"""
    if force_repeat:
        return {"ok": True, "msg": "force_repeat 跳过重复检查"}
    try:
        h = kx_history()
    except Exception as e:
        return {"ok": True, "msg": f"历史查询失败({e})，放行但请人工确认"}
    want = _norm_name(path)
    hits = [it for it in (h.get("history") or []) if _norm_name(it.get("name", "")) == want]
    # 先扫全部：只要有过一次成功打完，就拒绝（避免后来被取消的 0% 记录把已完成的记录盖掉）
    done = [it for it in hits
            if it.get("state") == "finished" and (it.get("progress") or 0) >= 95]
    if done:
        it = done[0]
        return {"ok": False,
                "msg": (f"该文件已成功打印过：{it.get('name')} "
                        f"{it.get('start')}–{it.get('end')} 进度{it.get('progress')}%。"
                        f"确实要重打请传 force_repeat=True")}
    stopped = [it for it in hits if it.get("state") == "stoped"]
    if stopped:
        pr = max((it.get("progress") or 0) for it in stopped)
        return {"ok": True, "msg": f"历史上有中断记录(最高{pr}%)，且无完成记录，视为需要重打"}
    return {"ok": True, "msg": "历史中无同名完成记录"}

def _precheck(force_clean: bool, vision: bool = True) -> dict:
    if force_clean:
        return {"ok": True, "msg": "force_clean 跳过"}
    j = _api("get", f"/work/printer/printersStatus?printer_ids={PRINTER_ID}")
    if j.get("code") != 1 or not j.get("data"):
        return {"ok": False, "msg": f"状态查询失败: {j.get('msg')}"}
    d = j["data"][0]
    # 实测语义: is_printing 2=忙碌(有任务)；1=空闲待命；0=离线。忙碌才拒。
    if d.get("is_printing") == 2 or "任务" in (d.get("reason") or ""):
        return {"ok": False, "msg": f"机器忙碌(is_printing={d.get('is_printing')}): {d.get('reason') or ''}"}
    # 视觉为权威清盘信号；is_clean_plate(人工确认)降级为视觉故障时的后备
    if vision:
        try:
            v = _vision_bed_clear(_cpp_frame())
            if v.get("bed_clear") is False:
                return {"ok": False, "msg": f"视觉检查发现床面有物品: {v.get('desc')}", "vision": v}
            return {"ok": True, "msg": "前置检查通过(视觉确认床空)", "vision": v}
        except Exception as e:
            if d.get("is_clean_plate") in (1, "1"):
                return {"ok": True, "msg": f"视觉核查失败({e})，以服务器清盘标志放行"}
            return {"ok": False, "msg": f"视觉核查失败且未确认清盘(is_clean_plate=0): {e}"}
    if d.get("is_clean_plate") in (0, "0"):
        return {"ok": False, "msg": "未确认清盘(is_clean_plate=0)且未做视觉检查，拒绝发送。"}
    return {"ok": True, "msg": "前置检查通过(服务器清盘标志)"}

def _upload(path: str) -> int:
    size = os.path.getsize(path)
    j = _api("post", "/v2/cloud_storage/lockStorageSpace",
             json={"name": os.path.basename(path), "size": size})
    if j.get("code") != 1: raise RuntimeError(f"lock 失败: {j.get('msg')}")
    lock_id = j["data"]["id"]
    r = requests.put(j["data"]["preSignUrl"], data=open(path, "rb").read(),
                     timeout=900, headers={"Content-Length": str(size)})
    if r.status_code != 200: raise RuntimeError(f"OBS PUT {r.status_code}")
    j2 = _api("post", "/v2/profile/newUploadFile", json={"user_lock_space_id": lock_id})
    if j2.get("code") != 1: raise RuntimeError(f"认领失败: {j2.get('msg')}")
    _api("post", "/v2/cloud_storage/unlockStorageSpace", json={"id": lock_id})
    return j2["data"]["id"]

def _rack():
    """读料架台账 kx_rack.json：槽号(1基)→颜色名（用户换料时口述维护）。"""
    try:
        d = json.loads(open(_RACK_FILE, encoding='utf-8').read())
        return {s['slot']: s['name'] for s in d['slots']}
    except Exception:
        return {}

_RACK_FILE = __import__('os').path.join(__import__('os').path.dirname(__import__('os').path.abspath(__file__)), 'kx_rack.json')

def _colour_dist(a, b):
    """两个 #RRGGBB 的粗略色距(0-441)。"""
    try:
        pa = [int(a[i:i+2], 16) for i in (1, 3, 5)]
        pb = [int(b[i:i+2], 16) for i in (1, 3, 5)]
        return sum((x - y) ** 2 for x, y in zip(pa, pb)) ** 0.5
    except Exception:
        return 441.0


def _slot_map(slots) -> list:
    """T几 -> 槽几(颜色)。颜色优先云端真色，失败回退台账 kx_rack.json。"""
    cloud = _rack_cloud()
    if not cloud:
        try:
            rack = json.load(open(_RACK_PATH, encoding="utf-8"))
            cloud = {int(k.replace("槽", "")): v.get("hex") for k, v in rack.items()}
        except Exception:
            cloud = {}
    out = []
    for i, s in enumerate(slots):
        hexv = cloud.get(s + 1)
        out.append(f"T{i} -> 槽{s+1}({hexv or '未知色'})")
    return out

def _start_rest(cloud_file_id: int, slots: list[int]) -> str:
    """REST 直发启动打印（2026-11-18：uv 库 access_token 通道随 token 过期死亡，
    本函数用 _api 的 XX-Token 直发 /work/operation/sendOrder，复刻库 payload 结构）。
    乱序 slots 语义（slots[i] 0基槽 ↔ 云端 material_list[i] 的 paint_index=工具号 i）
    经 11-17 四色板 slots=[0,3,2,1] 视觉核查 T1绿茎→槽4绿 实机验证有效。"""
    import time as _t
    # 1) 等切片元数据就绪
    for _ in range(10):
        j = _api("post", "/work/index/getUploadStatus", json={"id": int(cloud_file_id)})
        if (j.get("data") or {}).get("status") == 1:
            break
        _t.sleep(2)
    # 2) 拉云端切片元数据（material_list 带 filament_used/material_type/paint_index）
    j2 = _api("get", f"/work/gcode/infoFdm?id={cloud_file_id}")
    mats = ((j2.get("data") or {}).get("slice_material_info_list")) or []
    # 3) 云端真色（打印机上报）
    rack = kx_rack()
    colors = rack.get("raw") if rack.get("ok") else None
    if not colors or len(colors) < 4:
        raise RuntimeError("云端料架颜色读取失败，无法构造 ams_box_mapping")
    mapping = []
    if mats:  # 多色：按 material_list 逐工具映射
        for x, mat in enumerate(mats):
            if x >= len(slots):
                break
            si = int(slots[x])
            c = colors[si]
            mapping.append({
                "ams_color": c, "ams_index": si,
                "filament_used": mat.get("filament_used", 1),
                "material_type": mat.get("material_type", "PLA"),
                "paint_color": c, "paint_index": mat.get("paint_index", x),
            })
    else:  # fallback：云端按「paint_index 值 == T号」对齐读 ams_index 选槽。
        # paint_index 必须填循环计数 i（=工具号 T i），ams_index 填 slots[i]；
        # 11-18 事故实证：paint_index 误填 si 会让乱序 slots 失效（T1 被指到 paint_index=1 项的 ams_index=1=槽2，绿茎打白）。
        for i, si in enumerate([int(s) for s in slots]):
            mapping.append({
                "ams_color": colors[si], "ams_index": si, "filament_used": 1,
                "material_type": "PLA", "paint_color": colors[si], "paint_index": i,
            })
    # 4) sendOrder（order_id=1 START_PRINT，复刻 AnycubicProjectCtrlOrderRequest 结构）
    payload = {
        "order_id": 1, "printer_id": PRINTER_ID, "project_id": 0,
        "data": {
            "filetype": 0, "file_key": "", "file_name": "",
            "task_settings": {"ai_detect": 0, "camera_timelapse": 0},
            "file_id": int(cloud_file_id), "hollow_param": None, "is_delete_file": 0,
            "matrix": "", "project_type": 1, "punching_param": None,
            "slice_param": None, "slice_size": None, "template_id": 0,
        },
        "ams_info": {"ams_box_mapping": mapping, "use_ams": True},
        "settings": None,
    }
    r = _api("post", "/work/operation/sendOrder", json=payload)
    if r.get("code") != 1 or not (r.get("data") or {}).get("msgid"):
        raise RuntimeError(f"sendOrder 失败: {str(r.get('msg'))[:150]}")
    return str(r["data"]["msgid"])


def _start(cloud_file_id: int, slots: list[int], bypass_precheck: bool = False) -> str:
    """启动云文件打印。默认强制四查（含视觉）；bypass_precheck 仅限明确知晓风险的试探。
    注：乱序 slots（如 [0,3,2,1]）是有意映射——T i 用槽 slots[i]+1，2026-11-17 四色板实机验证有效，勿 sorted。"""
    if not bypass_precheck:
        pre = _precheck(False, vision=True)
        if not pre["ok"]:
            raise RuntimeError(f"PRECHECK_BLOCKED: {pre['msg'][:200]}")
    return _start_rest(cloud_file_id, slots)


def _start_uv_deprecated(cloud_file_id: int, slots: list[int], bypass_precheck: bool = False) -> str:
    """旧 uv 库通道（依赖长期 access_token，过期即死）。保留备查。"""
    sys.path.insert(0, r"C:\Users\Administrator\AppData\Roaming\uv\tools\anycubic-cloud-mcp\Lib\site-packages")
    os.environ.setdefault("ANYCUBIC_BASE_DOMAIN", "cloud-platform.anycubic.com")
    import asyncio, logging
    from anycubic_mcp.client import AnycubicClient
    from anycubic_mcp.config import MCPConfig
    from pathlib import Path
    logging.basicConfig(level=logging.CRITICAL)
    async def _run():
        cfg = MCPConfig(auth_mode="web", auth_token=open(ACCESS_TOKEN_FILE).read().strip(),
                        device_id=None, config_path=Path("."), log_level="CRITICAL")
        c = AnycubicClient(cfg, logging.getLogger("kxmcp")); await c.start()
        try:
            p = await c.get_printer(printer_id=PRINTER_ID)
            apiobj = next(getattr(c, k) for k in dir(c)
                          if getattr(c, k, None) and hasattr(getattr(c, k), "print_with_cloud_file_id"))
            mapping = None
            try:
                proj = await apiobj.fetch_project_gcode_info_fdm(cloud_file_id)
                if proj and proj.slice_material_info_list:
                    mapping = p.build_mapping_for_material_list(
                        slot_index_list=slots, material_list=proj.slice_material_info_list)
            except Exception:
                pass
            if not mapping:  # ACE 必须给 box mapping；单色 fallback
                # paint_index 必须是目标槽号(0基)——KX 云端按 paint_index 选料位，
                # 填工具序号会导致单工具永远吃槽1（11-14 粒子炮台紫料事故实证）
                mapping = p.build_mapping_for_material_list(
                    slot_index_list=slots,
                    material_list=[{"filament_used": "1", "material_type": "PLA",
                                    "paint_index": s} for s in slots])
            # 云端索引探针（11-13 破解 /work/index/getUploadStatus）：status==1=切片元数据就绪
            try:
                from anycubic_cloud_api.models.http import HTTP_METHODS as _HM
                _api = apiobj
                _root = getattr(_api, "_public_api_root", None) or getattr(c.api, "_public_api_root")
                _h = c.api.anycubic_auth.get_auth_headers(with_token=True)
                for _ in range(10):
                    _r = await c.api._fetch_ext_resp(method=_HM.POST, base_url=_root + "/work/index/getUploadStatus",
                                                     query={}, params={"id": int(cloud_file_id)},
                                                     extra_headers=_h, is_json=True)
                    if (_r or {}).get("data", {}).get("status") == 1:
                        break
                    await asyncio.sleep(2)
            except Exception:
                pass
            last_err = None
            for _ in range(5):  # 兜底：仍报 No slicer 时盲等重试
                try:
                    return await apiobj.print_with_cloud_file_id(
                        printer=p, cloud_file_id=cloud_file_id, ams_box_mapping=mapping)
                except Exception as e:
                    if "No slicer" not in str(e):
                        raise
                    last_err = e
                    await asyncio.sleep(3)
            raise last_err
        finally:
            await c.close()
    return asyncio.run(_run())

@mcp.tool
def kx_upload(file: str) -> dict:
    """只上传 G-code 到云，返回 cloud_file_id（不打印）"""
    try:
        return {"ok": True, "cloud_file_id": _upload(file)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}

@mcp.tool
def kx_print_cloud(file_id: int, slots: list[int] = [1]) -> dict:
    """启动已上传的云文件（file_id 来自 kx_upload），slots 为 1 基色槽"""
    try:
        order = _start(file_id, slots)
        return {"ok": True, "order": order}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}

CAM_DIR = r"C:\Users\Administrator\.dsh\kxcam"

def _web_hdr(tok):
    """sendOrder(摄像头开/关)必须用 web 设备头 + /work/operation/sendOrder"""
    ts = str(int(time.time() * 1000)); n = str(uuid.uuid4())
    sig = hashlib.md5((SALT1 + ts + APP_VERSION + SALT2 + n + SALT1).encode()).hexdigest().upper()
    return {"Xx-Device-Type": "web", "Xx-Is-Cn": "1", "XX-Version": APP_VERSION,
            "Xx-Nonce": n, "Xx-Signature": sig, "Xx-Timestamp": ts,
            "XX-Token": tok, "XX-LANGUAGE": "zh-CN", "Content-Type": "application/json"}

def _send_order(order_id: str) -> dict:
    import requests as _rq
    r = _rq.post(BASE + "/work/operation/sendOrder", headers=_web_hdr(_tok()),
                 json={"order_id": order_id, "printer_id": PRINTER_ID}, timeout=30)
    return r.json()

def _cpp_frame(wait_secs: int = 40) -> str:
    """C++ 拉流器截帧：sendOrder(1001)→kx_cam.exe→sendOrder(1002)。返回 jpg 路径。"""
    import subprocess
    j = _send_order("1001")
    sw = (j.get("data") or {}).get("shengwang") or {}
    if not sw:
        raise RuntimeError(f"开流失败: {j.get('msg')}")
    outp = os.path.join(CAM_DIR, "cam_cpp.jpg")
    cfg = {"appid": sw["appid"], "token": sw["rtc_token"], "channel": sw["channel"],
           "uid": sw.get("client_uid", 0), "key": sw["encryption_key"],
           "salt_b64": sw["encryption_kdf_salt"], "out": outp, "wait_secs": wait_secs}
    cfgp = os.path.join(CAM_DIR, "cfg.json")
    try:
        json.dump(cfg, open(cfgp, "w"))
    except PermissionError:
        # 沙箱只允许写工作区，而 kx_cam.exe 只认 ASCII 路径的 cfg：
        # 不能退到工作区（中文路径 fopen 失败，“no config”），只能提示去授权/沙箱外跑。
        raise RuntimeError(
            "cfg.json 写入被拒（%s）；复用旧配置会拿到过期的黑帧。"
            "请以可写用户目录的权限运行，或手动执行 06_脚本/kx_snap_sys.py" % cfgp)
    try:
        cp = subprocess.run([os.path.join(CAM_DIR, "kx_cam.exe"), os.path.join(CAM_DIR, "cfg.json")],
                            capture_output=True, text=True, cwd=CAM_DIR, timeout=wait_secs + 30)
    finally:
        try: _send_order("1002")
        except Exception: pass
    if not os.path.exists(outp):
        raise RuntimeError("未收到视频帧: " + (cp.stdout or "").strip().splitlines()[-1][:120] if cp and cp.stdout else "未收到视频帧")
    return outp

def _vision_bed_clear(jpg: str) -> dict:
    """101 litellm deepseek-v4.1-flash 视觉判断床面（master key 内存取用不落盘）"""
    import base64, re, subprocess as sp
    vlm_ssh = os.environ.get("KX_VLM_SSH", "")
    vlm_url = os.environ.get("KX_VLM_URL", "")
    if not (vlm_ssh and vlm_url):
        return {"bed_clear": None, "verdict": "SKIP", "desc": "未配置 KX_VLM_SSH / KX_VLM_URL，跳过视觉核查"}
    key = sp.run(["ssh", "-o", "BatchMode=yes", vlm_ssh,
                  'grep -o "master_key.*" /tmp/lt.yaml | head -1'],
                 capture_output=True, text=True, timeout=15).stdout
    mk = re.search(r"sk-\S+", key).group(0)
    import requests as _rq
    img = base64.b64encode(open(jpg, "rb").read()).decode()
    r = _rq.post(vlm_url,
        headers={"Authorization": "Bearer " + mk},
        json={"model": os.environ.get("KX_VLM_MODEL", "deepseek-v4.1-flash-ark"), "messages": [{"role": "user", "content": [
            {"type": "text", "text": "这是3D打印机俯视摄像头画面。判断打印床面上是否存在任何非机器本身的物体（成品、半成品、测试块、工具、夹具、耗材残料都算；喷头和料架不算）。床面完全无外来物体=true，存在任何外来物体=false。第一行只输出 true 或 false（小写），第二行起中文简述依据。"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img}}]}]},
        timeout=120)
    desc = r.json()["choices"][0]["message"]["content"]
    first = desc.strip().splitlines()[0].strip().upper() if desc.strip() else ""
    return {"bed_clear": first.lower().startswith("true"),
            "verdict": first[:20], "desc": desc[:200]}


@mcp.tool
def kx_history() -> dict:
    """云端打印历史：每条含文件名/状态/起止时间/层数进度（判断哪件打过）"""
    try:
        h = _api("get", "/v2/project/printHistory", params={"page": 1, "limit": "20"}).get("data") or []
        g = _api("get", "/work/project/getProjects", params={"page": 1, "limit": "20"}).get("data") or []
        gmap = {}
        for p2 in g:
            try: gmap[str(json.loads(p2.get("device_message") or "{}").get("taskid"))] = p2
            except Exception: pass
        rows = []
        for x in h:
            dm = {}
            try:
                gp = gmap.get(str(x.get("task_id")))
                if gp: dm = json.loads(gp.get("device_message") or "{}")
            except Exception: pass
            rows.append({
                "name": dm.get("filename") or x.get("gcode_name"),
                "state": dm.get("state"), "progress": dm.get("progress"),
                "layers": dm.get("total_layers"),
                "start": time.strftime("%m-%d %H:%M", time.localtime(x.get("create_time") or 0)),
                "end": time.strftime("%m-%d %H:%M", time.localtime(x.get("end_time") or 0)) if x.get("end_time") else None})
        return {"ok": True, "count": len(rows), "history": rows}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@mcp.tool
def kx_bed_photo(vision: bool = True) -> dict:
    """C++ 拉流器截帧（无常驻进程）→ 可选 deepseek-v4.1 视觉判断床面。返回 {bed_clear, desc, frame}"""
    try:
        jpg = _cpp_frame()
        if not vision:
            return {"ok": True, "frame": jpg}
        v = _vision_bed_clear(jpg)
        return {"ok": True, **v, "frame": jpg}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}

@mcp.tool
def kx_rack_detail() -> dict:
    """ACE 多色盒完整信息：每槽颜色+余量百分比+耗材类型，含烘干状态/湿度/喷嘴温度（/v2/printer/getMultiColorBoxInfo）"""
    import asyncio, logging as _lg
    sys.path.insert(0, r"C:\Users\Administrator\AppData\Roaming\uv\tools\anycubic-cloud-mcp\Lib\site-packages")
    os.environ.setdefault("ANYCUBIC_BASE_DOMAIN", "cloud-platform.anycubic.com")
    from anycubic_mcp.client import AnycubicClient
    from anycubic_mcp.config import MCPConfig
    from anycubic_cloud_api.models.http import HTTP_METHODS as _HM
    from pathlib import Path as _P
    _lg.basicConfig(level=_lg.CRITICAL)
    async def _run():
        cfg = MCPConfig(auth_mode="web", auth_token=open(ACCESS_TOKEN_FILE).read().strip(),
                        device_id=None, config_path=_P("."), log_level="CRITICAL")
        c = AnycubicClient(cfg, _lg.getLogger("x")); await c.start()
        try:
            await c.ensure_auth()
            root = c.api._public_api_root
            h = c.api.anycubic_auth.get_auth_headers(with_token=True)
            r = await c.api._fetch_ext_resp(method=_HM.POST, base_url=root + "/v2/printer/getMultiColorBoxInfo",
                                            query={}, params={"id": int(PRINTER_ID)}, extra_headers=h, is_json=True)
            boxes = (r or {}).get("data") or []
            out = []
            for b in boxes:
                out.append({"box": b.get("box_name"), "model_id": b.get("model_id"),
                            "nozzle_temp": b.get("curr_nozzle_temp"), "humidity": b.get("humidity"),
                            "drying": b.get("drying_status"),
                            "slots": {f"槽{s.get('index')+1}": {
                                "hex": _rgb_hex(s.get("color")), "remain_pct": s.get("consumables_percent"),
                                "type": s.get("type"), "brand": s.get("brand_name"), "status": s.get("status")}
                                for s in (b.get("slots") or [])}})
            return {"ok": True, "boxes": out}
        finally:
            await c.close()
    return asyncio.run(_run())


@mcp.tool
def kx_send(file: str, slots: list[int] = [1], force_clean: bool = False,
            skip_vision: bool = False, force_repeat: bool = False,
            expect: dict | None = None, accept_any: list[int] | None = None,
            clean_confirmed: str = "") -> dict:
    """上传 + 启动打印。默认六查：忙碌/清盘/视觉床面/耗材映射/是否已经打过/换料碰撞。"""
    if not force_clean:
        dup = _dup_check(file, force_repeat=force_repeat)
        if not dup["ok"]:
            return {"ok": False, "stage": "dup_check", "msg": dup["msg"]}
    # 第六查: 换料横移碰撞(仅 by-object 多色板生效; BLOCK=高危剐蹭, 安全红线不可 force)
    try:
        import importlib.util as _ilu
        _gv_path = os.environ.get('KX_GCODE_VERIFY') or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)), 'src', 'anycubic_mcp', 'gcode_verify.py'))
        _spec = _ilu.spec_from_file_location('_gv_standalone', _gv_path)
        _gv = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_gv)
        sw = _gv.verify_switch_safety(file)
        if not sw.get('ok'):
            hits = [f"{s['tool']}:{'/'.join(s['origin_hits'] + s['path_hits'])}"
                    for s in sw['switches'] if s['status'] == 'BLOCK']
            return {"ok": False, "stage": "switch_collision",
                    "msg": f"换料碰撞高危: {'; '.join(hits)}。横梁扫过33mm带会剐到床上高件——把>33mm的件躺倒打/移到最后打/分板, 不可强制发送。"}
    except FileNotFoundError:
        pass
    # 第七查: 首层附着(2026-11-16 山竹绿叶粘头事故固化; BLOCK=细臂件无brim裸打, 安全红线不可 force)
    try:
        import importlib.util as _ilu2
        _gv_path2 = os.environ.get('KX_GCODE_VERIFY') or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)), 'src', 'anycubic_mcp', 'gcode_verify.py'))
        _spec2 = _ilu2.spec_from_file_location('_gv_standalone2', _gv_path2)
        _gv2 = _ilu2.module_from_spec(_spec2); _spec2.loader.exec_module(_gv2)
        fl = _gv2.verify_first_layer_anchoring(file)
        if not fl.get('ok'):
            details = '; '.join(f"{o['name']}(brim={o['brim']},body={o['body']})" for o in fl['objects'] if o['verdict'] == 'BLOCK')
            return {"ok": False, "stage": "first_layer",
                    "msg": f"首层附着高危: {details}。细臂件无压边裸打会在中途被撬起粘头——给该件加 brim_type=outer_only 8mm 重切, 不可强制发送。"}
    except FileNotFoundError:
        pass
    # 第八查: 件↔色↔槽 机械对齐(2026-11-19 笑脸/花瓣颜色对调事故固化; BLOCK,不可 force)
    try:
        import importlib.util as _ilu3
        _cm_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'colour_map.py')
        _spec3 = _ilu3.spec_from_file_location('_colour_map', _cm_path)
        _cm = _ilu3.module_from_spec(_spec3); _spec3.loader.exec_module(_cm)
        _g = _cm.gate(file, slots, accept_any=accept_any)
        if not _g['ok']:
            return {"ok": False, "stage": "colour_align",
                    "msg": "颜色机械对齐失败: " + "; ".join(_g['fails']) +
                           "。按原始工程的 extruder->filament_colour 顺序重切该件, 不可强制发送。",
                    "table": _g['table'], "colours": _g['colours'], "rack_source": _g['rack_source']}
    except FileNotFoundError:
        return {"ok": False, "stage": "colour_align", "msg": "找不到 colour_map.py——没有对齐能力等于没有保护, 拒绝发送。"}

    # 第九查: 五项自动检查(碰撞/支撑/逐件/间距/颜色) —— 2026-11-21 起每次开打强制
    try:
        import importlib.util as _ilu4
        _pg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'print_gate.py')
        _spec4 = _ilu4.spec_from_file_location('_print_gate', _pg_path)
        _pg = _ilu4.module_from_spec(_spec4); _spec4.loader.exec_module(_pg)
        _r = _pg.check(file, slots, accept_any or [])
        if not _r['ok']:
            return {"ok": False, "stage": "five_check",
                    "msg": "五项检查未通过: " + "; ".join(_r['fails']) + "。改切片或改摆放后重发, 不可强制发送。",
                    "fails": _r['fails'], "warns": _r['warns'], "info": _r['info']}
        _five_warns = _r['warns']
        # 第十查: 扫床路径 + 空盘凭证 —— 2026-11-22 撞机后固化
        # 起点段(z≈0.2 贴床直奔件起点)与归零/归位横扫不在件间抬Z的覆盖里：床上只要有残留就会被剐。
        # 只要检查报告 requires_empty_bed，跳过视觉就必须给显式凭证，否则拒发。
        if _r.get('requires_empty_bed'):
            if (force_clean or skip_vision) and not str(clean_confirmed or "").strip():
                return {"ok": False, "stage": "clean_unconfirmed",
                        "msg": "本次开打要求空盘(扫床检查: 起点贴床直奔件起点)，但你要跳过视觉核查却未给空盘凭证。"
                               "传 clean_confirmed 再发, 或去掉 force_clean/skip_vision 让视觉真的看一眼。",
                        "sweep": _r.get('sweep')}
            if str(clean_confirmed or "").strip():
                _five_warns = list(_five_warns) + ["[空盘凭证] " + str(clean_confirmed).strip()]
    except FileNotFoundError:
        return {"ok": False, "stage": "five_check", "msg": "找不到 print_gate.py——没有五项检查就等于没有保护, 拒绝发送。"}

    pre = _precheck(force_clean, vision=not skip_vision)
    if not pre["ok"]:
        return {"ok": False, "stage": "precheck", "msg": pre["msg"], "vision": pre.get("vision")}
    if expect:
        cloud = _rack_cloud()
        for k, want in expect.items():
            got = cloud.get(int(k))
            if got and _colour_dist(got, want) > 150:
                return {"ok": False, "stage": "rack_colour", "msg": f"槽色核对失败: 槽{k} 云端={got} 期望={want}。请确认换料完成或修正 expect。", "slot_map": _slot_map(slots)}
    try:
        fid = _upload(file)
        order = _start(fid, slots)
        return {"ok": True, "cloud_file_id": fid, "order": order, "slot_map": _slot_map(slots),
                "precheck": pre["msg"], "five_warns": _five_warns}
    except Exception as e:
        return {"ok": False, "stage": "upload/start", "error": str(e)[:300]}

QUEUE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kx_queue.json")

def _qload() -> list:
    if os.path.exists(QUEUE_FILE):
        try: return json.load(open(QUEUE_FILE, encoding="utf-8"))
        except Exception: return []
    return []

def _qsave(q: list):
    tmp = QUEUE_FILE + ".tmp"
    json.dump(q, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, QUEUE_FILE)

@mcp.tool
def kx_queue_add(file: str, slots: list[int] = [3], note: str = "", expect: dict | None = None) -> dict:
    """添加打印任务到本地队列（slots 1 基色槽，默认 3=白）"""
    if not os.path.exists(file):
        return {"ok": False, "error": f"文件不存在: {file}"}
    q = _qload()
    item = {"id": max([x["id"] for x in q], default=0) + 1, "file": file, "slots": slots,
            "note": note, "status": "pending", "cloud_file_id": None,
            "expect": expect, "added_at": time.strftime("%m-%d %H:%M")}
    q.append(item); _qsave(q)
    return {"ok": True, "queued": item, "queue_len": len(q)}

@mcp.tool
def kx_queue_list() -> dict:
    """查看队列全部任务及状态"""
    return {"ok": True, "queue": _qload()}

@mcp.tool
def kx_queue_remove(qid: int) -> dict:
    """移除队列任务"""
    q = _qload(); n = len(q)
    q = [x for x in q if x["id"] != qid]; _qsave(q)
    return {"ok": True, "removed": n - len(q)}

@mcp.tool
def kx_queue_process(vision: bool = True) -> dict:
    """推进队列：头任务 pending→四查后发送；running→检测完成（空闲+清盘）后标 done 并可续下一个。
    定时调用即可实现自动续打；取件由人工完成，未清盘绝不发下一任务。"""
    q = _qload()
    if not q:
        return {"ok": True, "action": "empty"}
    head = q[0]
    st = kx_status()
    # 状态查询失败（token 过期/网络）绝不能当成"打印结束"——否则误报取件、误判空闲
    if not st.get("ok"):
        return {"ok": False, "action": "status_error", "head": head["id"],
                "msg": "状态查询失败，未变更队列: " + str(st.get("msg"))[:120]}
    if head["status"] == "running":
        if st.get("is_printing") == 2:
            return {"ok": True, "action": "waiting_print", "head": head["id"], "note": head.get("note", "")}
        # 打完了（或异常停了）：等人工取件确认清盘
        if st.get("is_clean_plate") in (1, "1"):
            head["status"] = "done"; head["finished_at"] = time.strftime("%m-%d %H:%M")
            _qsave(q)
            return kx_queue_process(vision)  # 递归尝试下一个
        return {"ok": True, "action": "waiting_pickup", "head": head["id"],
                "msg": "打印已结束，等待取件（在打印机上确认清盘或取走成品）", "note": head.get("note", "")}
    # pending → 检查全过 → 置 ready_confirm 等人工确认（用户指令 m16135：发送前必须确认）
    if head["status"] == "pending":
        pre = _precheck(False, vision=vision)
        if not pre["ok"]:
            return {"ok": True, "action": "blocked", "head": head["id"], "msg": pre["msg"][:200]}
        exp = head.get("expect") or {}
        if exp:
            cloud = _rack_cloud()
            for k, want in exp.items():
                got = cloud.get(int(k))
                if got and _colour_dist(got, want) > 150:
                    return {"ok": True, "action": "blocked", "head": head["id"],
                            "msg": f"槽色核对失败: 槽{k} 云端={got} 期望={want}"}
        head["status"] = "ready_confirm"; head["slot_map"] = _slot_map(head["slots"])
        _qsave(q)
        return {"ok": True, "action": "ready_confirm", "head": head["id"],
                "note": head.get("note", ""), "slot_map": head["slot_map"],
                "msg": "五查+槽色核对全过，等用户确认后 kx_queue_confirm 发送"}

    # ready_confirm → 已在等确认
    if head["status"] == "ready_confirm":
        return {"ok": True, "action": "ready_confirm", "head": head["id"],
                "note": head.get("note", ""), "slot_map": head.get("slot_map", []),
                "msg": "等待用户确认（kx_queue_confirm）"}

    # 终态条目：跳过并移除（不再发送）
    if head["status"] in ("done", "completed", "cancelled"):
        q.pop(0)
        _qsave(q)
        return {"ok": True, "action": "skipped_done", "head": head["id"],
                "msg": f"队首任务已完成({head['status']})，已移除，队列剩余 {len(q)}"}

    # 任何未知状态一律不动（fail-closed）。发送只允许经 kx_queue_confirm。
    return {"ok": True, "action": "unknown_status", "head": head["id"],
            "msg": f"队首状态 {head['status']} 不在预期流程内，未做任何操作"}

@mcp.tool
def kx_queue_confirm(qid: int | None = None) -> dict:
    """用户确认后真正发送（跳过五查重跑，直接 upload+start）。不确认不发。"""
    q = _qload()
    if qid:
        head = next((x for x in q if x["id"] == qid), None)
    else:
        head = q[0] if q else None
    if not head:
        return {"ok": False, "error": "队列空或无此任务"}
    if head["status"] not in ("ready_confirm", "pending"):
        return {"ok": False, "error": f"任务状态 {head['status']} 不可确认发送"}
    try:
        fid = head.get("cloud_file_id") or _upload(head["file"])
        order = _start(fid, head["slots"])
        head.update(status="running", cloud_file_id=fid, order=order,
                    started_at=time.strftime("%m-%d %H:%M"))
        _qsave(q)
        return {"ok": True, "action": "started", "head": head["id"], "order": order,
                "slot_map": _slot_map(head["slots"]), "note": head.get("note", "")}
    except Exception as e:
        head["status"] = "failed"; head["error"] = str(e)[:200]; _qsave(q)
        return {"ok": False, "action": "failed", "head": head["id"], "error": str(e)[:200]}


@mcp.tool
def kx_cancel() -> dict:
    """取消当前打印任务（实测有效；状态滞后~30s，'terminated'报错即已取消）"""
    sys.path.insert(0, r"C:\Users\Administrator\AppData\Roaming\uv\tools\anycubic-cloud-mcp\Lib\site-packages")
    os.environ.setdefault("ANYCUBIC_BASE_DOMAIN", "cloud-platform.anycubic.com")
    import asyncio, logging
    from anycubic_mcp.client import AnycubicClient
    from anycubic_mcp.config import MCPConfig
    from pathlib import Path
    logging.basicConfig(level=logging.CRITICAL)
    async def _run():
        cfg = MCPConfig(auth_mode="web", auth_token=open(ACCESS_TOKEN_FILE).read().strip(),
                        device_id=None, config_path=Path("."), log_level="CRITICAL")
        c = AnycubicClient(cfg, logging.getLogger("kxmcp")); await c.start()
        try:
            p = await c.get_printer(printer_id=PRINTER_ID)
            await p.update_info_from_api(with_project=True)  # 关键：刷新 latest_project
            proj = p.latest_project
            if not proj:
                return "no_active_project（无任务可取消）"
            apiobj = next(getattr(c, k) for k in dir(c)
                          if getattr(c, k, None) and hasattr(getattr(c, k), "print_with_cloud_file_id"))
            return await apiobj.cancel_print(printer=p, project=proj)
        finally:
            await c.close()
    try:
        r = asyncio.run(_run())
        if r is None:
            return {"ok": False, "error": "cancel 返回 None（无 project？稍后重试）"}
        return {"ok": True, "cancel_order": r}
    except Exception as e:
        s = str(e)
        if "terminated" in s:
            return {"ok": True, "note": "任务已在终止流程中"}
        return {"ok": False, "error": s[:300]}

if __name__ == "__main__":
    mcp.run()
