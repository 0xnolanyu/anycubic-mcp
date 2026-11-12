"""Vision bed-check: send a camera frame to any OpenAI-compatible endpoint
(local Ollama, LiteLLM, cloud API) and parse a strict true/false verdict.

Protocol (enforced): the model must answer exactly `true` or `false` on line 1
(bed fully free of foreign objects = true), rationale in Chinese from line 2.
"""
from __future__ import annotations

import base64
import os

import requests

PROMPT = (
    "这是3D打印机俯视摄像头画面。判断打印床面上是否存在任何非机器本身的物体"
    "（成品、半成品、测试块、工具、夹具、耗材残料都算；喷头和料架不算）。"
    "床面完全无外来物体=true，存在任何外来物体=false。"
    "第一行只输出 true 或 false（小写），第二行起中文简述依据。"
)


class VisionChecker:
    def __init__(self, base_url: str, model: str, api_key: str = "none",
                 prompt: str = PROMPT):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.prompt = prompt

    def check(self, jpeg_path: str, timeout: int = 120) -> dict:
        img = base64.b64encode(open(jpeg_path, "rb").read()).decode()
        r = requests.post(
            f"{self.base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": [{"role": "user", "content": [
                {"type": "text", "text": self.prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + img}},
            ]}]},
            timeout=timeout,
        )
        r.raise_for_status()
        desc = r.json()["choices"][0]["message"]["content"] or ""
        first = desc.strip().splitlines()[0].strip().lower() if desc.strip() else ""
        return {"bed_clear": first.startswith("true"),
                "verdict": first[:20], "desc": desc[:400]}


def from_config(cfg: dict) -> VisionChecker | None:
    """Build from config dict {vision_base_url, vision_model, vision_api_key?}."""
    if not cfg.get("vision_base_url") or not cfg.get("vision_model"):
        return None
    return VisionChecker(
        base_url=cfg["vision_base_url"],
        model=cfg["vision_model"],
        api_key=cfg.get("vision_api_key") or os.environ.get("VISION_API_KEY", "none"),
    )
