"""Async helpers for the project's blocking third-party libraries."""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, TypeVar


BLOCKING_IO_CONCURRENCY = 8

_Result = TypeVar("_Result")
_BLOCKING_IO_EXECUTOR = ThreadPoolExecutor(
    max_workers=BLOCKING_IO_CONCURRENCY,
    thread_name_prefix="sf-reader-io",
)


async def run_blocking(
    function: Callable[..., _Result],
    /,
    *args: Any,
    **kwargs: Any,
) -> _Result:
    """Run blocking work without stalling the event loop.

    A dedicated executor keeps total blocking I/O bounded across all fetchers.
    ``run_in_executor`` propagates the function's exception to the awaiting task;
    cancelling the await also remains observable as ``CancelledError``.
    """
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    call = partial(function, *args, **kwargs)
    return await loop.run_in_executor(
        _BLOCKING_IO_EXECUTOR,
        context.run,
        call,
    )
