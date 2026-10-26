import asyncio


def spawn(app, coro) -> asyncio.Task:
    """Run a background task and keep a reference so it is not garbage-collected."""
    tasks = app.bot_data.setdefault("tasks", set())
    t = asyncio.create_task(coro)
    tasks.add(t)
    t.add_done_callback(tasks.discard)
    return t
