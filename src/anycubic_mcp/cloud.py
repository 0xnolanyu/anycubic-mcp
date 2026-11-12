"""Anycubic cloud API client — pure requests, zero third-party reverse-engineered deps.

All algorithms and endpoints were reverse-engineered from the official
Anycubic Slicer Next client (cloud_sdk_cpp.dll) and verified against the
live cloud platform. See docs/reverse-engineering.md.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

import requests

BASE = "https://cloud-platform.anycubic.com/p/p/workbench/api"

# Salts embedded in every official client binary (extractable via strings).
SALT1 = "f9b3528877c94d5c9c5af32245db46ef"
SALT2 = "0cf75926606049a3937f56b0373b99fb"

# Client version string mixed into the signature. Verified working: 2.0.0.6
# (matches Anycubic Slicer Next 2.0.0.x). Bump when the platform rejects.
CLIENT_VERSION = "2.0.0.6"


def sign(ts_ms: str, nonce: str, version: str = CLIENT_VERSION) -> str:
    """XX-Signature = uppercase-hex MD5(SALT1 + ts_ms + version + SALT2 + nonce + SALT1)."""
    raw = f"{SALT1}{ts_ms}{version}{SALT2}{nonce}{SALT1}"
    return hashlib.md5(raw.encode()).hexdigest().upper()


class AnycubicCloud:
    def __init__(self, token: str, base: str = BASE, version: str = CLIENT_VERSION):
        self.token = token
        self.base = base.rstrip("/")
        self.version = version
        self.s = requests.Session()

    # -- low level ---------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        nonce = str(uuid.uuid4())
        return {
            "XX-Token": self.token,
            "XX-Nonce": nonce,
            "XX-Timestamp": ts,
            "XX-Signature": sign(ts, nonce, self.version),
            "Xx-Device-Type": "pcf",
            "Xx-Is-Cn": "1",
            "XX-Version": self.version,
            "XX-LANGUAGE": "zh-CN",
            "Content-Type": "application/json",
        }

    def call(self, method: str, path: str, *, params: dict | None = None,
             json_body: Any = None, timeout: int = 120) -> dict:
        r = self.s.request(method, self.base + path, params=params,
                           json=json_body, headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        return r.json()

    # -- auth --------------------------------------------------------------
    @staticmethod
    def login_with_access_token(access_token: str) -> str:
        """Exchange the long-lived access token (from app memory/config) for a
        session token accepted by /work/* endpoints."""
        r = requests.post(BASE + "/v3/public/loginWithAccessToken",
                          json={"access_token": access_token, "device_type": "web"},
                          timeout=60)
        r.raise_for_status()
        j = r.json()
        if j.get("code") != 1:
            raise RuntimeError(f"login failed: {j.get('msg')}")
        return j["data"]["token"]

    # -- printers ----------------------------------------------------------
    def printers_status(self, printer_ids: list[int]) -> list[dict]:
        j = self.call("GET", f"/work/printer/printersStatus?printer_ids={','.join(map(str, printer_ids))}")
        if j.get("code") != 1:
            raise RuntimeError(f"printersStatus failed: {j.get('msg')}")
        return j.get("data") or []

    # -- files / printing ---------------------------------------------------
    def lock_storage_space(self, name: str, size: int) -> dict:
        j = self.call("POST", "/v2/cloud_storage/lockStorageSpace", json_body={"name": name, "size": size})
        if j.get("code") != 1:
            raise RuntimeError(f"lock failed: {j.get('msg')}")
        return j["data"]

    def unlock_storage_space(self, lock_id: int) -> None:
        self.call("POST", "/v2/cloud_storage/unlockStorageSpace", json_body={"id": lock_id})

    def claim_upload(self, lock_id: int) -> int:
        """newUploadFile: claim the uploaded object, returns cloud_file_id."""
        j = self.call("POST", "/v2/profile/newUploadFile", json_body={"user_lock_space_id": lock_id})
        if j.get("code") != 1:
            raise RuntimeError(f"claim failed: {j.get('msg')}")
        return j["data"]["id"]

    def upload_gcode(self, path: str) -> int:
        """Full upload flow: lock -> OBS PUT -> claim -> unlock. Returns cloud_file_id."""
        import os
        size = os.path.getsize(path)
        d = self.lock_storage_space(os.path.basename(path), size)
        r = requests.put(d["preSignUrl"], data=open(path, "rb").read(),
                         timeout=900, headers={"Content-Length": str(size)})
        if r.status_code != 200:
            raise RuntimeError(f"OBS PUT {r.status_code}")
        fid = self.claim_upload(d["id"])
        self.unlock_storage_space(d["id"])
        return fid

    def send_order(self, printer_id: int, order_id: str, order_data: dict,
                   ams_box_mapping: list[dict] | None = None) -> dict:
        body: dict[str, Any] = {
            "order_id": order_id,
            "printer_id": printer_id,
            "project_id": 0,
            "order_data": order_data,
        }
        if ams_box_mapping:
            body["ams_box_mapping"] = ams_box_mapping
        j = self.call("POST", "/work/operation/sendOrder", json_body=body)
        if j.get("code") != 1:
            raise RuntimeError(f"sendOrder failed: {j.get('msg')}")
        return j

    def start_print(self, printer_id: int, cloud_file_id: int,
                    ams_box_mapping: list[dict] | None = None,
                    retries: int = 5, retry_wait_s: float = 3.0) -> dict:
        """Start printing an uploaded cloud file. Retries on 'No slicer found'
        (the platform indexes new uploads asynchronously)."""
        order_data = {"file_id": cloud_file_id, "is_delete_file": 0}
        last = None
        for _ in range(retries):
            try:
                return self.send_order(printer_id, "1003", order_data, ams_box_mapping)
            except RuntimeError as e:
                if "No slicer" not in str(e):
                    raise
                last = e
                time.sleep(retry_wait_s)
        raise last  # type: ignore[misc]

    def cancel_print(self, printer_id: int) -> dict:
        return self.send_order(printer_id, "1004", {})

    # -- camera ------------------------------------------------------------
    def camera_open(self, printer_id: int) -> dict:
        """Request an Agora RTC stream session; returns shengwang params
        (appid/channel/uid/token/encryption)."""
        j = self.call("POST", "/work/operation/sendOrder",
                      json_body={"order_id": "1001", "printer_id": printer_id,
                                 "project_id": 0, "order_data": None})
        if j.get("code") != 1:
            raise RuntimeError(f"camera open failed: {j.get('msg')}")
        return j.get("data") or {}

    def camera_close(self, printer_id: int) -> dict:
        return self.send_order(printer_id, "1002", {})

    # -- history / projects --------------------------------------------------
    def print_history(self, page: int = 1, limit: int = 20) -> list[dict]:
        j = self.call("GET", f"/v2/project/printHistory?page={page}&limit={limit}")
        if j.get("code") != 1:
            raise RuntimeError(f"printHistory failed: {j.get('msg')}")
        d = j.get("data")
        if isinstance(d, dict):
            return d.get("list") or []
        return d or []

    def projects(self) -> list[dict]:
        j = self.call("GET", "/work/project/getProjects")
        if j.get("code") != 1:
            raise RuntimeError(f"getProjects failed: {j.get('msg')}")
        return j.get("data") or []

    # -- farm (cloud-native queue) -------------------------------------------
    def farm_group_list(self, page: int = 1, limit: int = 10) -> list[dict]:
        j = self.call("POST", "/farm/group/list", json_body={"page": page, "limit": limit})
        if j.get("code") != 1:
            raise RuntimeError(f"farm/group/list failed: {j.get('msg')}")
        return j.get("data") or []

    def farm_create_bulk_task(self, printer_ids: list[int], data: list[dict],
                              is_start_print: int = 0, print_number: int = 1,
                              work_project_group_id: int = 0) -> dict:
        """Create a cloud-queued farm task. Body shape partially probed; full
        field list documented in docs/reverse-engineering.md."""
        j = self.call("POST", "/farm/printer/createBulkTask",
                      json_body={"printer_ids": printer_ids, "data": data,
                                 "is_start_print": is_start_print,
                                 "print_number": print_number,
                                 "work_project_group_id": work_project_group_id})
        if j.get("code") != 1:
            raise RuntimeError(f"createBulkTask failed: {j.get('msg')}")
        return j.get("data") or {}

    @staticmethod
    def farm_task_item(file_id: int, ams_box_mapping: list[dict] | None = None,
                       evoke_from: str = "web", filetype: int = 1) -> dict:
        """Draft data[] item for createBulkTask — keys from the client DLL's
        string table (offsets 0x646b00-0x646fb0, see reverse-engineering.md);
        exact live shape pending an on-line probe. Extra keys observed next to
        the serializer: ai_detect, auto_leveling, vibration_compensation,
        flow_calibration, dry_mode, camera_timelapse, task_settings,
        project_type, project_ids, ams_info(use_ams, solt_color_material_match).
        """
        item = {"file_id": file_id, "filetype": filetype,
                "evoke_from": evoke_from,
                "ai_detect": False, "auto_leveling": True,
                "vibration_compensation": True, "flow_calibration": False,
                "dry_mode": False, "camera_timelapse": False}
        if ams_box_mapping:
            item["ams_info"] = {"use_ams": True,
                                "ams_box_mapping": ams_box_mapping}
        return item

    def farm_set_clean_plate(self, printer_id: int) -> dict:
        j = self.call("POST", "/farm/printer/setCleanPlate", json_body={"printer_ids": [printer_id]})
        if j.get("code") != 1:
            raise RuntimeError(f"setCleanPlate failed: {j.get('msg')}")
        return j.get("data") or {}
