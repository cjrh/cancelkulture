# cancelkulture
Cancellable subprocesses in Python (including for asyncio run_in_executor)

## ⚠️  Warning - texperimental ⚠️

Do not use this for anything important if you don't
understand what it's doing.

## Demo

You know how in `asyncio` if you run something in a process pool executor
using `run_executor()`, and then you try to cancel the task, but the
underlying process doesn't actually stop?

LET'S FIX THAT

```python
import time
import asyncio
import cancelkulture


exe = cancelkulture.ProcessPoolExecutor()


async def awork():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        exe,
        partial(time.sleep, 10.0),
        cancelkulture.ProcessCancelTimeout(0.0),
    )


async def main():
    t = asyncio.create_task(awork())

    # Let's wait a bit and cancel that task 
    await asyncio.sleep(5.0)
    t.cancel()

    try:
        await t
    except asyncio.CancelledError:
        pass

    # Here, the job inside the executor is really gone,
    # after 5 seconds, not 10.


asyncio.run(main())
```

## How can this work?? Cancellation requires cooperative multitasking...

Haha lolno, we just `SIGKILL` the process running that task.

You're going to want to make sure your jobs are idempotent because
in practice they will sometimes need to be restarted.

I know it sounds bad to SIGKILL a running process, but in
reality, in production systems hardware and software
(and humans) cause hard resets all the time. An extremely common
example is simply redeploying a new version of a running
application. Yes, your app will get a SIGTERM from Kubernetes,
but if you have blocking CPU code running within many nested
processes, it's a lot of work to orchestrate a controlled
shutdown. It's much easier to define idempotent work and
design for restarts as necessary.

## What exactly is provided in this library?

The first and most important thing is that this library is designed
to compose with whatever you already have. Because I also have 
existing code and I can't just replace the world with every new
thing.

Broadly, there are 3 features, and 3 supported use-cases. It's easier to go
through the use-cases:

1. You have an `asyncio` program, and you need a `ProcessPoolExecutor`, and
   you'd like the executor jobs to actually get cancelled when you cancel a 
   `loop.run_in_executor()` future.
2. You're working with regular sync code (not `asyncio`), and you need a
   `ProcessPoolExecutor`, and you would like to kill executor jobs. In particular
   during shutdown scenarios, e.g. for production redeployments of microservices.
3. You're stuck with using the built-in `ProcessPoolExecutor` from the
   `concurrent.futures`, but you would still like to be able to actually
   cancel running executor jobs. 

We'll step throught the scenarios one-by-one.

### 1. asyncio: Cancellable executor jobs

We covered this in the demo section. But I just want to highlight a few
things:

```python
import cancelkulture


exe = cancelkulture.ProcessPoolExecutor()


async def awork():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        exe,

        # Look here
        partial(time.sleep, 10.0),

        # And here
        cancelkulture.ProcessCancelTimeout(0.0),
    )
```

I've made a subclass of the `ProcessPoolExecutor` from the 
`concurrent.futures` stdlib module. It is pretty close to the 
original, but it does some...extra things inside the `submit()`
method to get the job done. You don't really have to worry about
that, but what you do have to pay attention to is how to pass
arguments. Parameters that are part of `cancelkulture` must
be separate, they cannot be part of a `partial`.

This is also a good time to discuss why the timeouts are
classes, like `cancelkulture.ProcessCancelTimeout` above. Usually,
in such situations one would use `kwargs` to specify special 
parameters used by wrapper functions, and then inside the wrapper
function you'd just omit them when calling the inner function.
In my case my inner function is the original
`ProcessPoolExecutor.submit()` method. Unfortunately, the
design of `asyncio` is such that only `*args` can be passed
to `run_in_executor()`. The docs specifically say to
"use a partial" if we need to pass `kwargs`. That doesn't work
for my wrapper use-case. So, instead I've made a new type
for each timeout parameter, and then in my wrapper `submit()`,
I check every value in `*args` to see whether the type matches
on of the types used by `cancelkulture`. So that's why
the classes.

### 2. A richer ProcessPoolExecutor 

The subclass provided by this package should be a drop-in
replacement for the one in `concurrent.futures`, but it
has 2 extra tricks. The first is that jobs can be 
cancelled.

This is an example from one of the tests. It uses a background
thread to cancel a running future.

```python
import time
import pytest
import threading
import cancelkulture


def delayed_cancel(fut, delay):
    time.sleep(delay)
    fut.cancel()


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

```

The job submitted to the executor would like to run for 10 seconds, but
we cancel the future after 0.5 seconds.

### 3. Getting cancellation in stdlib ProcessPoolExecutor

This one is messy, but it also leads a path to explain how `cancelkulture`
is made. The proverbial "sausage".

Again, an example from the tests:


```python
import concurrent.futures
import multiprocessing
import time
import cancelkulture


ctx = multiprocessing.get_context("spawn")
with concurrent.futures.ProcessPoolExecutor(
    mp_context=ctx,
    initializer=initializer,
) as exe:
    fut = exe.submit(
        cancelkulture.killable_wrapper,
        time.sleep,
        10.0,
        killable_wrapper_timeout=5.0,
    )

    try:
        fut.result()
    except cancelkulture.ProcessTimeoutError:
        print('This happens after 5 seconds')

```

Observations:
- We're using the vanilla stdlib `ProcessPoolExecutor` here
- We're using the "spawn" context for subprocesses. All three methods
  provided by the multiprocessing module work, but I'm highlighting
  "spawn" because that one is the recommended one.

Here you can see we're passing `cancelkulture.killable_wrapper` as
the `target` to the executor. This wrapper will be responsible for
executing the actual function, in this example `time.sleep()`.

