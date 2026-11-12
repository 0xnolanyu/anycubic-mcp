# native/ — kx_cam: Agora camera frame puller

A tiny C++ exe that joins the printer's Agora RTC channel (params issued
per-session by the cloud API), waits for the printer's video stream, grabs one
frame, writes `cam_cpp.jpg`, and exits. No resident process, no viewer.

## Build (Windows, MSVC)

1. Download the **Agora Native SDK for Windows v4.7.0** (video SDK, FULL package):
   https://docs.agora.io/en/video-calling/get-started/get-started-sdk?platform=windows
   (direct link pattern: `https://download.agora.io/sdk/release/Agora_Native_SDK_for_Windows_v4.7.0_FULL.zip`)
2. Extract and note the `sdk/` folder (contains `high_level_api/include`,
   `low_level_api/include`, `x86_64/dll`, `x86_64/lib`... actually libs are in
   `dll/agora_rtc_sdk.dll.lib`).
3. From a *Native Tools Command Prompt for VS*:

   ```bat
   cd native
   build.bat C:\path\to\Agora_Native_SDK_for_Windows_FULL\sdk
   ```

4. Copy every DLL from the SDK's `x86_64/dll` next to the built `kx_cam.exe`
   (the exe is tiny; the DLLs are the runtime — do not commit them).

## Runtime contract

`kx_cam.exe` reads `cfg.json` **in its own directory** (UTF-8,
`ensure_ascii=False` — the parser does not understand `\uXXXX` escapes) with
the `shengwang` object returned by `sendOrder` order 1001:

```json
{ "appid": "...", "channel": "<printer key>", "client_uid": 580469115,
  "encryption_mode": "AES_256_GCM2", "encryption_key": "...",
  "encryption_kdf_salt": "<base64 32B>", "rtc_token": "..." }
```

On success it writes `cam_cpp.jpg` (1280x720) next to itself and prints
`frames: N` / `JOINED` / `user_published <uid>` log lines to stderr.

Notes:
- The salt must be converted to a `Uint8Array(32)`-style buffer
  (base64-decoded 32 bytes) before `enableEncryption` — passing the raw string
  yields INVALID_PARAMS and a black stream.
- The printer publishes as a fixed uid per device (seen: 442216).
- `#include <objidl.h>` must precede GDI+ headers (MSVC quirk).
