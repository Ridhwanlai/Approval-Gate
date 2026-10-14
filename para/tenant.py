"""Which firm the current request or job belongs to (hosted mode).

Set once per request by the server and carried into job threads with contextvars. In hosted mode,
code that reads or writes firm data calls require() and refuses to run without a firm, so a missed
hand-off fails closed instead of falling back to shared data.
"""
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

from . import config

_firm = contextvars.ContextVar("para_firm_id", default=None)


def set_firm(firm_id):
    return _firm.set(firm_id)


def current():
    return _firm.get()


def require():
    firm_id = _firm.get()
    if config.HOSTED and not firm_id:
        raise RuntimeError("no firm in context")
    return firm_id


def thread(target, *args):
    """A daemon thread that runs with a copy of the caller's context (and so its firm)."""
    ctx = contextvars.copy_context()
    return threading.Thread(target=ctx.run, args=(target, *args), daemon=True)


def submit(pool: ThreadPoolExecutor, fn, *args):
    """pool.submit with the caller's context; each task gets its own copy."""
    return pool.submit(contextvars.copy_context().run, fn, *args)
