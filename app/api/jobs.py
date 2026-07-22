"""Bounded in-process async job runtime for the API service.

It deliberately separates *accepted clients* from *executing workers*: up to
50 jobs may be visible/streaming while a small fixed worker pool protects CPU,
filesystem and the upstream LLM service from a thundering herd.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


MAX_ACTIVE_JOBS = int(os.getenv("API_MAX_ACTIVE_JOBS", "50"))
MAX_WORKERS = int(os.getenv("API_MAX_WORKERS", "8"))


@dataclass
class Job:
    id: str
    kind: str
    created_at: float = field(default_factory=time.time)
    status: str = "queued"
    result: dict | None = None
    error: str | None = None
    events: asyncio.Queue[dict] = field(default_factory=asyncio.Queue)


class AsyncJobManager:
    def __init__(self, max_active: int = MAX_ACTIVE_JOBS, max_workers: int = MAX_WORKERS):
        self.max_active = max_active
        self.jobs: dict[str, Job] = {}
        self._workers = asyncio.Semaphore(max_workers)

    async def submit(self, job_id: str, kind: str,
                     work: Callable[[Callable[[str, Any], None]], Any]) -> Job:
        if len([j for j in self.jobs.values() if j.status in {"queued", "running"}]) >= self.max_active:
            raise RuntimeError("job capacity reached")
        job = Job(id=job_id, kind=kind)
        self.jobs[job_id] = job
        await self._emit(job, "queued", {"position": "accepted"})
        asyncio.create_task(self._run(job, work))
        return job

    async def _run(self, job: Job, work) -> None:
        async with self._workers:
            job.status = "running"
            await self._emit(job, "running", {})
            loop = asyncio.get_running_loop()

            def progress(event: str, data: Any) -> None:
                loop.call_soon_threadsafe(job.events.put_nowait, {"event": event, "data": data})

            try:
                job.result = await asyncio.to_thread(work, progress)
                job.status = "completed"
                await self._emit(job, "result", job.result)
            except Exception as exc:
                job.error = str(exc)
                job.status = "failed"
                await self._emit(job, "error", {"message": job.error})
            finally:
                await self._emit(job, "end", {"status": job.status})

    async def _emit(self, job: Job, event: str, data: Any) -> None:
        await job.events.put({"event": event, "data": data})

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)
