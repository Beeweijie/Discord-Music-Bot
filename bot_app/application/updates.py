"""Prepare one update at a time while the live bot remains available."""
import asyncio

from bot_app.domain.updates import UpdateError


class UpdateService:
    def __init__(self, backend):
        self.backend = backend
        self.lock = asyncio.Lock()

    async def prepare(self, url=""):
        if self.lock.locked():
            raise UpdateError("An update is already being prepared. Please wait.")
        async with self.lock:
            return await asyncio.to_thread(self.backend.prepare, url)

    def activate(self, job, channel_id):
        self.backend.activate(job, channel_id)

    def cancel(self):
        self.backend.cancel()
