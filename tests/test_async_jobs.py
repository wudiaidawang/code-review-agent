"""AsyncJobManager（异步任务管理器）回归测试。"""
import asyncio

from app.api.jobs import AsyncJobManager


def test_job_streams_plan_then_result():
    async def scenario():
        manager = AsyncJobManager(max_active=2, max_workers=1)

        def work(progress):
            progress("plan", {"steps": ["resolve_symbol"]})
            return {"answer": "done"}

        job = await manager.submit("job_1", "investigate", work)
        events = []
        while True:
            item = await asyncio.wait_for(job.events.get(), timeout=2)
            events.append(item)
            if item["event"] == "end":
                break
        assert job.status == "completed"
        assert job.result == {"answer": "done"}
        assert any(e["event"] == "plan" for e in events)
        assert events[-1]["data"]["status"] == "completed"

    asyncio.run(scenario())


def test_active_job_capacity_is_enforced():
    async def scenario():
        manager = AsyncJobManager(max_active=1, max_workers=1)
        release = asyncio.Event()

        def work(progress):
            # Keep the first task active while the capacity check runs.
            while not release.is_set():
                import time
                time.sleep(0.01)
            return {"ok": True}

        await manager.submit("job_1", "review", work)
        try:
            await manager.submit("job_2", "review", work)
            assert False, "second active job must be rejected"
        except RuntimeError:
            pass
        release.set()

    asyncio.run(scenario())
