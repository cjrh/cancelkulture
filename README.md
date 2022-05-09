[![ci](https://github.com/cjrh/cancelkulture/workflows/Python%20application/badge.svg)](https://github.com/cjrh/cancelkulture/actions)
[![Coverage Status](https://coveralls.io/repos/github/cjrh/cancelkulture/badge.svg?branch=main)](https://coveralls.io/github/cjrh/cancelkulture?branch=main)
[![versions](https://img.shields.io/pypi/pyversions/cancelkulture.svg)](https://pypi.python.org/pypi/cancelkulture)
[![tags](https://img.shields.io/github/tag/cjrh/cancelkulture.svg)](https://img.shields.io/github/tag/cjrh/cancelkulture.svg)
[![pip](https://img.shields.io/badge/install-pip%20install%20cancelkulture-ff69b4.svg)](https://img.shields.io/badge/install-pip%20install%20cancelkulture-ff69b4.svg)
[![pypi](https://img.shields.io/pypi/v/cancelkulture.svg)](https://pypi.org/project/cancelkulture/)

# cancelkulture
Cancellable subprocesses with timeouts in Python (including for `asyncio.run_in_executor`)

## ⚠️  Warning - experimental ⚠️

Do not use this for anything important if you don't
understand what it's doing.

## Demo

You know how in `asyncio` if you run something in a process pool executor
using `run_executor()`, and then you try to cancel the task, but the
underlying process doesn't actually stop? Also, it's super annoying you 
can't set a timeout on an executor job?

LET'S FIX THAT

#### Timeouts


```python
import time
import asyncio
from cancelkulture import ProcessPoolExecutor, ProcessCancelTimeout


async def awork():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(ProcessPoolExecutor(), time.sleep, 10.0)


async def main():
    try:
        await asyncio.wait_for(awork(), 5.0)
    except asyncio.TimeoutError:
        print('Timed out!')


asyncio.run(main())
```

#### Cancellation

```python
import time
import asyncio
from cancelkulture import ProcessPoolExecutor, ProcessCancelTimeout


async def awork():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(ProcessPoolExecutor(), time.sleep, 10.0)


async def main():
    t = asyncio.create_task(awork())

    # Let's wait a bit and cancel that task 
    loop = asyncio.get_running_loop()
    loop.call_later(5.0, t.cancel)

    try:
        await t
    except asyncio.CancelledError:
        print('Cancelled!')


asyncio.run(main())
```

Actually, there is also support for cancelling subprocesses in
non-async code too.  Read on to explore further examples.

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
shutdown, especially within the narrow time window before
Kubernetes does a SIGKILL anyway.

It's much easier to define idempotent work and design for
restarts as necessary.

## What exactly is provided in this library?

I first want to point out that this library is designed to compose with
whatever you already have. Because I too have existing code and I can't just
replace the world with every new thing.

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

### 1. asyncio: Cancellable executor jobs (and timeouts!)

We covered this in the demo section. But I just want to highlight a few
things:

```python
from cancelkulture import ProcessPoolExecutor, ProcessCancelTimeout


async def work():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        ProcessPoolExecutor(),

        # Look here
        partial(time.sleep, 10.0),

        # And here
        ProcessCancelTimeout(5.0),
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

Now is also a good time to discuss what the two timeouts do:

- `ProcessTimeout`: This is the allowed time for the job.
- `ProcessCancelTimeout`: This is the allowed time for the job
  *after* the job has been cancelled (e.g. `fut.cancel()`).

### 2. A richer ProcessPoolExecutor 

The subclass provided by this package should be a drop-in
replacement for the one in `concurrent.futures`, but it
has 2 extra tricks. The first is that jobs can be 
cancelled.

#### Cancellation

This is an example from one of the tests. It uses a background
thread to cancel a running future. This example demonstrates
cancellation.

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
        time.sleep,
        10.0,
        cancelkulture.ProcessCancelTimeout(0.0),
    )
    t = threading.Thread(target=delayed_cancel, args=(fut, 0.5,), daemon=True)
    t.start()
    with pytest.raises(cancelkulture.ProcessCancelledError):
        _ = fut.result()

```

The job submitted to the executor would like to run for 10 seconds, but
we cancel the future after 0.5 seconds.

#### Timeouts

With timeouts, there's more nuance. First, an example that shows how
timeouts work, and then we'll discuss why I didn't use the existing
timeout API when getting the result in `fut.result(timeout)`.


```python
    with cancelkulture.ProcessPoolExecutor() as exe:
        fut = exe.submit(
            time.sleep,
            10.0,
            cancelkulture.ProcessTimeout(0.5),
        )
        with pytest.raises(cancelkulture.ProcessTimeoutError):
            _ = fut.result()
```

The above is the way it currently works. However, if you're familiar
with the stdlib `concurrent.futures` module, you will be aware that
there is an existing feature to set a `timeout` parameter on the
result call itself. For example, *I could have* gone with something
like this:

```python

    ### HYPOTHETICAL CODE ###

    with cancelkulture.ProcessPoolExecutor() as exe:
        fut = exe.submit(
            time.sleep,
            10.0,

                  <- See, no timeout parameter here

        )
        with pytest.raises(concurrent.futures.TimeoutError):
            _ = fut.result(timeout=5.0)
```

So why didn't I do this? It's because I didn't want to change
compatibility with the stdlib `ProcessPoolExecutor` code.
In the original, the timeout error that is raised does not
affect the running task at all. It doesn't kill it, the task
goes on running. My fear was that it might become difficult
to know which executor is being used where, in more complicated
programs with lots of moving parts.

Thus, the current design is that even for the `ProcessPoolExecutor`
in `cancelkulture`, using `fut.result(timeout)` will not kill the
task if that timeout is reached. It is to be used only as an
alert, just like in the stdlib version.

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

