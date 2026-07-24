"""
Throttled progress reporter that edits a Telegram message at most every N seconds.
Used by yt-dlp / spotdl progress hooks.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from telegram import Message


@dataclass
class ProgressReporter:
    message: Message
    loop: asyncio.AbstractEventLoop
    interval: float = 2.0
    _last_edit: float = field(default=0.0, init=False)
    _last_text: str = field(default="", init=False)

    def hook(self, d: dict) -> None:
        """yt-dlp compatible progress hook (called from worker thread)."""
        if d.get("status") == "downloading":
            pct = d.get("_percent_str", "?").strip()
            speed = d.get("_speed_str", "?").strip()
            eta = d.get("_eta_str", "?").strip()
            text = f"⬇️ Downloading…  {pct}  •  {speed}  •  ETA {eta}"
        elif d.get("status") == "finished":
            text = "✅ Download finished, processing…"
        else:
            return
        self._maybe_edit(text)

    def _maybe_edit(self, text: str) -> None:
        now = time.monotonic()
        if text == self._last_text or now - self._last_edit < self.interval:
            return
        self._last_edit = now
        self._last_text = text
        asyncio.run_coroutine_threadsafe(
            self._safe_edit(text), self.loop
        )

    async def _safe_edit(self, text: str) -> None:
        try:
            await self.message.edit_text(text)
        except Exception:
            # ignore "message not modified" / flood errors
            pass
