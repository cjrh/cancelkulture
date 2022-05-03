import asyncio
from functools import partial
import time
import concurrent.futures
from contextlib import contextmanager
import multiprocessing as mp
import itertools

import pytest
import cancelkulture


def work(duration: float) -> int:
    g = itertools.count()
    t0 = time.monotonic()
    while time.monotonic() - t0 < duration:
        print(f"work: {next(g)}")
        time.sleep(1.0)
    return 123


def work_cpubound(duration: float) -> int:
    t0 = time.monotonic()
    while time.monotonic() - t0 < duration:
        continue
    return 123


def work_with_subprocess(duration: float) -> int:
    p = mp.Process(target=work, args=(duration,))
    p.start()
    p.join()
    return 123


def work_with_internal_executor(duration: float) -> int:
    with concurrent.futures.ProcessPoolExecutor(max_workers=10) as exe:
        results = exe.map(work, [duration] * 10)

    return 123


def initializer():
    import logging
    logging.basicConfig(level="DEBUG")


@contextmanager
def pool(
    worker,
    worker_arg,
    killable_wrapper_timeout,
    killable_wrapper_shutdown_pipe,
    mp_context = "spawn",
):
    ctx = mp.get_context(mp_context)
    with concurrent.futures.ProcessPoolExecutor(
        mp_context=ctx,
        initializer=initializer,
    ) as exe:
        fut = exe.submit(
            cancelkulture.killable_wrapper,
            worker,
            worker_arg,
            killable_wrapper_timeout=killable_wrapper_timeout,
            killable_wrapper_cancel_pipe=killable_wrapper_shutdown_pipe,
        )

        yield fut


@pytest.mark.parametrize('mp_context', [
    "fork",
    "forkserver",
    "spawn",
])
@pytest.mark.parametrize('worker_fn', [
    work,
    work_cpubound,
    work_with_subprocess,
    work_with_internal_executor,
])
def test_success(mp_context, worker_fn):
    with pool(worker_fn, 0.1, 2.0, None, mp_context=mp_context) as fut:
            assert fut.result() == 123


@pytest.mark.parametrize('mp_context', [
    "fork",
    "forkserver",
    "spawn",
])
@pytest.mark.parametrize('worker_fn', [
    work,
    work_cpubound,
    work_with_subprocess,
    work_with_internal_executor,
])
def test_timeout(mp_context, worker_fn):
    with pool(worker_fn, 10.0, 0.5, None, mp_context=mp_context) as fut:
        with pytest.raises(cancelkulture.ProcessTimeoutError):
            _ = fut.result()


@pytest.mark.parametrize('mp_context', [
    "fork",
    "forkserver",
    "spawn",
])
@pytest.mark.parametrize('worker_fn', [
    work,
    work_cpubound,
    work_with_subprocess,
    work_with_internal_executor,
])
def test_shutdown(mp_context, worker_fn):
    sender, receiver = mp.Pipe()
    with pool(worker_fn, 100.0, 1000.0, receiver, mp_context=mp_context) as fut:
        time.sleep(0.1)
        sender.send(dict(cancel_timeout=0.5))
        with pytest.raises(cancelkulture.ProcessCancelledError):
            _ = fut.result()


def test_new_executor_success():
    with cancelkulture.ProcessPoolExecutor() as exe:
        fut = exe.submit(work, 0.1)
        assert fut.result() == 123


def test_new_executor_timeout():
    with cancelkulture.ProcessPoolExecutor() as exe:
        fut = exe.submit(
            work,
            10.0,
            timeout_=cancelkulture.ProcessTimeout(0.5),
        )
        with pytest.raises(cancelkulture.ProcessTimeoutError):
            _ = fut.result()


def delayed_cancel(fut, delay):
    time.sleep(delay)
    fut.cancel()


def test_new_executor_cancel():
    import threading

    with cancelkulture.ProcessPoolExecutor() as exe:
        fut = exe.submit(
            work,
            10.0,
            after_cancel_timeout_=cancelkulture.ProcessCancelTimeout(0.0),
        )
        t = threading.Thread(target=delayed_cancel, args=(fut, 0.5,), daemon=True)
        t.start()
        with pytest.raises(cancelkulture.ProcessCancelledError):
            _ = fut.result()


@pytest.mark.parametrize('args', [
    (partial(work, 0.1),),
    (work, 0.1),
])
def test_asyncio_success(args):
    exe = cancelkulture.ProcessPoolExecutor()

    async def awork():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            exe,
            *args
        )

    async def main():
        assert await awork() == 123

    asyncio.run(main())


def test_asyncio_timeout():
    import asyncio
    from functools import partial

    exe = cancelkulture.ProcessPoolExecutor()

    async def awork():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            exe,
            partial(work, 10.0),
            cancelkulture.ProcessTimeout(3.0),
        )

    async def main():
        with pytest.raises(cancelkulture.ProcessTimeoutError):
            assert await awork() == 123

    asyncio.run(main())


def test_asyncio_cancel():
    exe = cancelkulture.ProcessPoolExecutor()

    async def awork():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            exe,
            partial(work, 10.0),
            cancelkulture.ProcessCancelTimeout(0.0),
        )

    async def main():
        t = asyncio.create_task(awork())
        await asyncio.sleep(5.0)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(main())
