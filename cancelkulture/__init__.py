"""
TBD

"""
import logging
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
from typing import Optional
import time

import psutil


__version__ = "0.0.1"
logger = logging.getLogger(__name__)


class JobsShutdownError(Exception):
    """Raised when the killable_wrapper process terminates itself due
    to a shutdown event.

    This must NOT be considered a job failure. Such jobs should be restarted
    when the application restarts.
    """
    pass


class JobsTimeoutError(TimeoutError):
    """Raised when the killable_wrapper process terminates itself due
    to hitting a timeout limit.

    This MUST be considered a job failure. If the job were to be restarted
    it is likely to hit the same timeout error.
    """
    pass


def killable_wrapper(
        fn,
        *args,
        killable_wrapper_timeout: float = 3600 * 8,
        killable_wrapper_shutdown_pipe: Optional[mp.Pipe] = None,
        killable_wrapper_shutdown_timeout: float = 5.0,
        **kwargs
):
    """This is a "wrapper" function that can be used to wrap other functions
    being given to a ProcessPoolExecutor for execution. This wrapper provides
    two features to the calling context:

        1. During shutdown, you can ask this process to kill itself by putting
           something (anything!) on the pipe.

        2. An extra feature is the timeout parameter. It will kill itself if it
           exceeds the timeout parameter, in seconds.

    This is an example of how it might be used:

        from concurrent.futures import ProcessPoolExecutor
        import multiprocessing as mp
        import signal

        shutdown_pipe = mp.Pipe()

        def signal_handler(signum, frame):
            shutdown_pipe.send(None)

        signal.signal(signal.SIGTERM, handler)

        def work(duration):
            time.sleep(duration)

        with ProcessPoolExecutor() as exe:
            fut = exe.submit(
                killable_wrapper,

                work,       <- This is the actual work function
                90.0        <- params for the work function

                killable_wrapper_timeout=90,
                killable_wrapper_shutdown_pipe=shutdown_pipe,
            )
            try:
                result = fut.result()
            except JobsShutdownError:
                print("Task ended because the system is shutting down")
            except JobsTimeoutError:
                print("Task ended because it timed out")

    """
    # We only need to run a single process, but we're using an executor instance
    # because it makes it easy to get return values and capture raised
    # exceptions. The mp.Process class does not provide these things.
    #
    # Secondly, because the intent of this wrapper is to be a target_fn
    # inside some other ProcessPoolExecutor, which should have been
    # configured with the "spawn" start method, it is safe to use "fork"
    # here (it'll be forking from an empty process), and it'll be convenient
    # in certain situations like where the parent PPE might have had an
    # initializer function to set up global configuration (e.g. tracing)
    # which we would like to reuse here.
    ctx = mp.get_context("fork")
    exe = ProcessPoolExecutor(max_workers=1, mp_context=ctx)

    sdpipe = killable_wrapper_shutdown_pipe  # alias for brevity
    fut = exe.submit(fn, *args, **kwargs)
    t0 = time.monotonic()

    t0_shutdown = 0

    def time_left() -> float:
        return time.monotonic() - t0 < killable_wrapper_timeout

    def time_left_shutdown() -> float:
        return time.monotonic() - t0_shutdown < killable_wrapper_shutdown_timeout

    try:
        while time_left() and time_left_shutdown():
            print(f"{time.monotonic()=}")
            try:
                # Wait for a result in slices of 1 second. This is a form
                # of polling, which gives us the opportunity to assess
                # timeouts or the shutdown signal.
                return fut.result(timeout=1.0)
            except TimeoutError:
                # This timeout is just on the polling `fut.result`, it isn't
                # an error. This is our chance to check whether the
                # shutdown pipe has received any data.
                if not t0_shutdown:
                    if sdpipe and sdpipe.poll():
                        t0_shutdown = time.monotonic()
                continue
        else:
            if not time_left_shutdown():
                logger.info("Shutting down jobs task")
                raise JobsShutdownError
            else:
                logger.error(f"Jobs task hit timeout")
                raise JobsTimeoutError
    finally:
        _kill_all_processes_and_subprocesses(exe)
        exe.shutdown(False, cancel_futures=True)


def _kill_all_processes_and_subprocesses(executor: ProcessPoolExecutor):
    for pid, p in executor._processes.items():
        for child in psutil.Process(pid).children(recursive=True):
            logger.debug(f"Killing child {child.pid} of {pid}")
            child.kill()

        logger.debug(f"Killing process {pid}")
        p.kill()

