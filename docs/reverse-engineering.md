# Reverse-engineering notes — Anycubic cloud platform

Everything below was derived from the official Windows client
(`AnycubicSlicerNext`, app version 2.0.0.6, `cloud_sdk_cpp.dll`) using static
analysis (Ghidra), runtime memory inspection, and live API probing with a
legitimate logged-in account. Published for interoperability; no credentials
are included. The salts are trivially extractable from any official binary.

## 1. HTTP API basics

- **Base**: `https://cloud-platform.anycubic.com` + public prefix `/p/p/workbench/api`
- **Auth**: session token via header `XX-Token`
- **Device type header**: `Xx-Device-Type: web` (the desktop client uses
  `pcf`; both are accepted by the endpoints below — the *camera* order
  endpoint requires web/android and rejects pcf)
- Optional but sent by the client: `Xx-Is-Cn: 1`, `XX-LANGUAGE: zh-CN`

## 2. Request signature (`XX-Signature`)

Every request carries four headers assembled in one DLL function
(`FUN_1800d3920` @ `cloud_sdk_cpp.dll`):

| Header | Value |
| --- | --- |
| `XX-Timestamp` | Unix time in **milliseconds** |
| `XX-Nonce` | random UUID v4 string (36 chars) |
| `XX-Signature` | see below |
| `XX-Token` | session token |

```
signature = UPPERCASE_HEX( MD5(
    SALT1 + timestamp_ms + APP_VERSION + SALT2 + nonce + SALT1
) )
```

- `SALT1 = "f9b3528877c94d5c9c5af32245db46ef"`
- `SALT2 = "0cf75926606049a3937f56b0373b99fb"`
- `APP_VERSION = "2.0.0.6"` — the *client* version string, read from a
  global `std::string` in the DLL; the third-party PyPI `anycubic-cloud-mcp`
  library signs with `1.0.0` and works for some endpoints, but pairing the
  version with the client you extracted the token from is the safe choice.

Two-level tokens:

1. `access_token` — long-lived JWT held by the logged-in client
2. `session token` — `POST /v3/public/loginWithAccessToken`
   `{"access_token": "...", "device_type": "web"}` → use this for calls

## 3. Endpoint map (verified on Kobra X)

| Purpose | Method & path (under `/p/p/workbench/api`) |
| --- | --- |
| Printer status | `GET /work/printer/printersStatus?printer_ids=<id>` |
| Send order (start/cancel/camera...) | `POST /work/operation/sendOrder` |
| Reserve upload slot | `POST /v2/cloud_storage/lockStorageSpace {name,size}` |
| Finish upload | `POST /v2/profile/newUploadFile {user_lock_space_id}` |
| Release slot | `POST /v2/cloud_storage/unlockStorageSpace {id}` |
| Print history | `GET /v2/project/printHistory?page&limit` |
| Project detail | `GET /work/project/getProjects` |
| G-code slice info | `GET /work/gcode/infoFdm?id=<gcode_id>` (3MF-derived ids only) |
| Video thumbnail list | `GET /v3/printer/getVideoThumbnailList?device_id=<printer key>` |

File upload itself is a plain `PUT` to a pre-signed OBS URL returned by
`lockStorageSpace` (Huawei OBS).

### sendOrder

`POST /work/operation/sendOrder` body:

```json
{ "order_id": "<id>", "printer_id": <id>, "project_id": 0,
  "order_data": {...}, "ams_box_mapping": [...] }
```

Known `order_id` values (string keys of an enum in the DLL):

| id | Meaning |
| --- | --- |
| `1001` | camera open — returns Agora params (`data.shengwang`) |
| `1002` | camera close |
| `1003` | start print (`order_data = start-print request`) |
| `1004` | cancel print |

Start-print `order_data` (cloud file): `{"file_id": <cloud_file_id>,
"is_delete_file": false}` plus `ams_box_mapping` (required when the printer
has an ACE box — "Must supply box mapping when using ACE"). Each mapping
entry: `spool_index`, `filament_used`, `material_type`, `color_red/green/blue`,
`paint_index`.

> Fresh uploads can briefly fail with `No slicer found` — the cloud has not
> indexed the file yet. Retry with backoff (5 × 3 s observed sufficient).

> The server rejects new orders while printing ("Device busy, operation
> unavailable") but does **not** check bed cleanliness — the vision precheck in
> this project is the actual guard.

### Farm queue (Next "Farm Mode")

`/farm/...` family, partially mapped:

- `POST /farm/group/list {page,limit}` — printer groups
- `POST /farm/message/list`, `/farm/message/statistics {aggregate_mode:1,...}`
- `POST /farm/printer/setCleanPlate` — remotely set the clean-plate flag
- `POST /farm/{device,printer}/createBulkTask {printer_ids[], is_start_print,
  data[], print_number, work_project_group_id}` — native print queue
- The client string table (file offsets 0x646b00–0x646fb0 in
  cloud_sdk_cpp.dll, `.rdata`) clusters all JSON keys the serializer emits.
  Outer: `printer_ids, is_start_print, print_number, work_project_group_id,
  data`. data[] items (draft, live-probe pending): `file_id, filetype,
  project_type, project_ids, evoke_from, ai_detect, auto_leveling,
  vibration_compensation, flow_calibration, dry_mode, camera_timelapse,
  task_settings, ams_info{use_ams, ams_box_mapping,
  solt_color_material_match}`. C++ structs: `BatchPrintTask`,
  `CloudClient::requestBatchPrint(BatchPrintTask const*)`,
  `TaskListRequest`. Response/history keys nearby: `print_number, user_id,
  status, print_time, estimate, total_layers, supplies_usage`.
- Live probe result (2026-11-12): all 13 farm endpoints enumerated;
  message/{list,latest,info,statistics}, group/list respond correctly, but
  both createBulkTask variants return `51000 missing mandatory public
  parameter` regardless of data[] shape, extra body keys (page/limit/user_id),
  headers (Request-Id/Trace-Id/App-Version), device types (web/pcf/android)
  or signature versions (1.0.0/2.0.0.6). The missing "public parameter" is
  not in the probed space — schema still unknown. Full string-table dump of
  the serializer block (file offsets 0x646800-0x647400) is preserved above;
  candidate keys not yet placed: `drying_settings, slice_result, project_ids,
  md5, is_temp_file, empty_group_id, group_type, get_all, thumbnail`.
  Until the schema is cracked, use the local queue (queue.py) which is fully
  functional; farm native queue remains a stretch goal.

## 4. Camera (Agora RTC)

`sendOrder` `1001` returns `data.shengwang`:

```json
{ "appid": "6fc7f83702e44c67a0e1104933b01d4f", "channel": "<printer key>",
  "client_uid": 580469115, "encryption_mode": "AES_256_GCM2",
  "encryption_key": "...", "encryption_kdf_salt": "<base64, 32 bytes>",
  "rtc_token": "007eJxS...", "event_id": ... }
```

- Join as audience/broadcast with `autoSubscribeVideo`; the printer publishes
  as a stable uid (seen: 442216), 1280×720.
- Encryption must be enabled **before** joining: `enableEncryption(
  AES_256_GCM2, key, kdfSalt)` where the salt is the base64-decoded 32 bytes
  (passing the string unchanged → `INVALID_PARAMS` and a black stream).
- Params are per-call (fresh `rtc_token` each time); close with order `1002`.
- Server-side thumbnails: `getVideoThumbnailList` (only populated during
  prints/timelapse; `device_id` = printer `key`, *not* MAC — MAC returns
  code 1007).

## 5. Client config quirks (for the setup wizard)

- The desktop client stores its cloud section obfuscated in
  `%APPDATA%\AnycubicSlicerNext\AnycubicSlicerNext.conf`; the access token is
  a plain JWT inside the process memory while logged in — scan for
  `eyJ…\.eyJ…\.…` of plausible length instead of parsing the config.
- Never ship or log tokens; keep them under `~/.anycubic-mcp/`.
