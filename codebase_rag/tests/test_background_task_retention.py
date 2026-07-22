"""Background helper tasks must stay referenced until they finish.

A bare ``asyncio.create_task`` result that nobody stores can be garbage
collected mid-flight, silently killing the task (python:S7502).
"""

import asyncio

from codebase_rag.main import _background_tasks, _spawn_background


def test_spawned_task_is_retained_until_done() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def work() -> None:
            await release.wait()

        before = set(_background_tasks)
        _spawn_background(work())
        pending = _background_tasks - before
        assert len(pending) == 1

        task = next(iter(pending))
        release.set()
        await task
        assert task not in _background_tasks

    asyncio.run(scenario())
