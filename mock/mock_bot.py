"""Mock bot that simulates Telegram connection without real credentials."""

from __future__ import annotations


class MockBot:
    def __init__(self) -> None:
        self._connected = True

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def client(self):
        return self

    async def send_message(self, user_id: int, text: str) -> None:
        print(f"[MOCK TG] Would send to {user_id}: {text[:80]}...")

    async def start(self) -> None:
        print("[MOCK] Bot started (simulated)")

    async def stop(self) -> None:
        self._connected = False
        print("[MOCK] Bot stopped")
