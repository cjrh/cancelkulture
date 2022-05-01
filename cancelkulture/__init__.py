"""
TBD

"""
import logging
import multiprocessing as mp
from multiprocessing.connection import Connection
import concurrent.futures
from typing import Optional
import time

import psutil


__version__ = "0.0.1"
__all__ = ["ProcessCancelledError", "ProcessTimeoutError", "killable_wrapper"]
logger = logging.getLogger(__name__)


class ProcessCancelledError(Exception):
    """Raised when the killable_wrapper process terminates itself due
    to a shutdown event.

    This must NOT be considered a job failure. Such jobs should be restarted
    when the application restarts.
    """
    pass


class ProcessTimeoutError(concurrent.futures.TimeoutError):
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
        killable_wrapper_cancel_pipe: Optional[Connection] = None,
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

        cancel_sender, cancel_receiver = mp.Pipe()

        def signal_handler(signum, frame):
            cancel_sender.send(
                dict(cancel_timeout=5.0)

            )

        signal.signal(signal.SIGTERM, handler)

        def work(duration):
            time.sleep(duration)

        with ProcessPoolExecutor() as exe:
            fut = exe.submit(
                killable_wrapper,

                work,       <- This is the actual work function
                90.0        <- params for the work function

                killable_wrapper_timeout=90,
                killable_wrapper_shutdown_pipe=cancel_receiver,
            )
            try:
                result = fut.result()
            except ProcessCancelledError:
                print("Task ended because the system is shutting down")
            except ProcessTimeoutError:
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
    exe = concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx)

    sdpipe = killable_wrapper_cancel_pipe  # alias for brevity
    fut = exe.submit(fn, *args, **kwargs)
    t0 = time.monotonic()

    # The time at which the cancellation request was received
    t0_cancel = 0
    # The duration to wait before hard-killing the subprocess
    cancel_timeout = 0

    def time_left() -> float:
        return time.monotonic() - t0 < killable_wrapper_timeout

    def time_left_shutdown() -> float:
        if not t0_cancel:
            return True
        else:
            return time.monotonic() - t0_cancel < cancel_timeout

    try:
        while time_left() and time_left_shutdown():
            try:
                # Wait for a result in slices of 1 second. This is a form
                # of polling, which gives us the opportunity to assess
                # timeouts or the shutdown signal.
                return fut.result(timeout=1.0)
            except concurrent.futures.TimeoutError:
                # This timeout is just on the polling `fut.result`, it isn't
                # an error. This is our chance to check whether the
                # shutdown pipe has received any data.
                if not t0_cancel:
                    if sdpipe and sdpipe.poll():
                        payload = sdpipe.recv()
                        logger.warning(f"{payload=}")
                        t0_cancel = time.monotonic()
                        cancel_timeout = payload["cancel_timeout"]
                continue
        else:
            if not time_left_shutdown():
                logger.info("Shutting down jobs task")
                raise ProcessCancelledError
            else:
                logger.error(f"Jobs task hit timeout")
                raise ProcessTimeoutError
    finally:
        _kill_all_processes_and_subprocesses(exe)
        exe.shutdown(False, cancel_futures=True)


def _kill_all_processes_and_subprocesses(
    executor: concurrent.futures.ProcessPoolExecutor,
):
    for pid, p in executor._processes.items():
        for child in psutil.Process(pid).children(recursive=True):
            logger.debug(f"Killing child {child.pid} of {pid}")
            child.kill()

        logger.debug(f"Killing process {pid}")
        p.kill()


class ProcessTimeout:
    def __init__(self, value):
        self.value = value


class ProcessCancelTimeout:
    def __init__(self, value):
        self.value = value


class ProcessPoolExecutor(
    concurrent.futures.ProcessPoolExecutor,
):
    def __init__(
        self,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._shutdown_timeout = None

    def submit(
        self,
        fn,
        /,
        *args,
        timeout_=ProcessTimeout(3600.0),
        after_cancel_timeout_=ProcessCancelTimeout(0.0),
        **kwargs,
    ):
        timeout_ = timeout_.value
        after_cancel_timeout_ = after_cancel_timeout_.value

        # Because `asyncio.run_in_executor` only allows
        # passing *args, and it literally calls this `submit`
        # method with those *args, we'll have to fish out
        # timeouts, if set, and overwrite the defaults.
        tmp_args = []
        for a in args:
            if isinstance(a, ProcessTimeout):
                timeout_ = a.value
            elif isinstance(a,  ProcessCancelTimeout):
                after_cancel_timeout_ = a.value
            else:
                tmp_args.append(a)

        args = tmp_args

        task_sender, task_receiver = mp.Pipe()

        fut = super().submit(
            killable_wrapper,
            fn, *args, **kwargs,
            killable_wrapper_timeout=timeout_,
            killable_wrapper_cancel_pipe=task_receiver,
        )

        parent = self

        def custom_cancel_method(self):
            logger.warning(f"Calling my custom cancel")
            from concurrent.futures._base import (
                RUNNING,
                FINISHED,
                CANCELLED,
                CANCELLED_AND_NOTIFIED,
            )
            """Cancel the future if possible.

            Returns True if the future was cancelled, False otherwise. A future
            cannot be cancelled if it is running or has already completed.
            """
            with self._condition:
                if self._state in [RUNNING, FINISHED]:
                    ############## MY CUSTOMIZATIONS ###############
                    if self._state in [RUNNING]:
                        if parent._shutdown_timeout is not None:
                            timeout = parent._shutdown_timeout
                        else:
                            timeout = after_cancel_timeout_

                        task_sender.send(
                            dict(cancel_timeout=timeout),
                        )
                    ############## MY CUSTOMIZATIONS ###############

                    return False

                if self._state in [CANCELLED, CANCELLED_AND_NOTIFIED]:
                    return True

                self._state = CANCELLED
                self._condition.notify_all()

            self._invoke_callbacks()
            return True

        from types import MethodType
        # YOLO
        fut.cancel = MethodType(custom_cancel_method, fut)
        return fut

    def shutdown(self, wait=True, *, cancel_futures=False, timeout=5.0):
        self._shutdown_timeout = timeout
        super().shutdown(wait, cancel_futures=cancel_futures)
