# anycubic-mcp

**Unofficial Anycubic cloud 3D-printer MCP server.** Send print jobs, check the
bed with the printer camera + AI vision, manage a queue, query print history —
all through Model Context Protocol tools, from any MCP client (Claude Desktop,
Cursor, dsh, ...).

Works with printers that use the Anycubic cloud platform (verified on
**Kobra X** with ACE multi-color; same API family should apply to other cloud
models — reports welcome).

> ⚠️ Unofficial, reverse-engineered, MIT-licensed. Not affiliated with Anycubic.
> The platform may change its protocol at any time. Use at your own risk.

## Features

- ✅ **Cloud print pipeline** — upload G-code, start/cancel prints, live status, temps, firmware
- ✅ **Camera bed-check** — Agora RTC puller (native, no resident process) + strict `true/false` AI vision verdict; refuses to print on a dirty bed
- ✅ **Safety prechecks** — busy check → vision (authoritative) → server clean-plate flag (fallback); duplicate guard via history
- ✅ **Queue** — cloud farm bulk-task API where available, local JSON queue as fallback, timer-friendly `queue_process`
- ✅ **Print history** — merged view so agents can tell printed from unprinted
- ✅ **One-command setup** — `anycubic-setup` scans a logged-in Anycubic app for your token, lists printers, writes config

## Quick start

```bash
pipx install anycubic-mcp          # or: uv tool install anycubic-mcp
anycubic-setup                     # interactive: token -> printer -> config
```

Then register in your MCP client:

```json
{
  "mcpServers": {
    "anycubic": { "command": "anycubic-mcp" }
  }
}
```

Camera bed-check additionally needs `kx_cam.exe`, built once from `native/`
(Agora C++ SDK download instructions included), and any OpenAI-compatible
multimodal endpoint for the vision verdict (local Ollama works).

## Tools

| Tool | Purpose |
| --- | --- |
| `printer_status` | busy/idle, temps, clean-plate flag, firmware |
| `bed_photo` | capture one frame; optional AI verdict `true/false` |
| `verify_gcode` | first-layer scatter + tool-sequence check — catches stacked plates & wrong colors **before** sending |
| `upload_gcode` | upload a file, returns `cloud_file_id` |
| `send_print_job` | prechecks → upload → start (the safe default); `dry_run=True` returns the checklist without sending |
| `cancel_print` | cancel current job |
| `print_history` | recent jobs to avoid duplicates |
| `queue_add/list/remove/process` | local queue (timer-friendly) |

### Slot numbering (1-based, human semantics)

`slots` everywhere means **human numbering**: `[1]` = first spool slot, `[3]` =
third. The 0-based wire format is an internal detail — every send/queue result
echoes a `slot_map` so you can see exactly which spool was used. This exists
because a 0/1-based mix-up caused two real misprints.

### dry_run — look before you send

`send_print_job(..., dry_run=True)` runs every gate (busy / vision / duplicate
history / first-layer verify) and returns a checklist. Nothing is uploaded or
started. Point it at any new G-code before committing filament.

## Documentation

- [docs/reverse-engineering.md](docs/reverse-engineering.md) — signature
  algorithm, endpoint list, Agora camera params, farm queue probing notes
- [native/README.md](native/README.md) — building `kx_cam.exe`
- [examples/dsh/](examples/dsh/) — dsh `mcp-registry` relay shim

## Roadmap

- `PrinterAdapter` protocol: the queue executor talks to a printer only via
  status/upload/start/cancel/history. Anycubic cloud is the first adapter;
  Bambu/Klipper/Moonraker adapters can reuse the same queue, safety checks
  (vision bed-photo veto) and MCP surface as-is.
- Frida-assisted capture of the farm `createBulkTask` body (community help
  welcome) — the local queue already covers single-printer workflows.

## License

MIT. The signature salts are extractable from any official client binary;
nothing here contains credentials. Keep your tokens in `~/.anycubic-mcp/`.
