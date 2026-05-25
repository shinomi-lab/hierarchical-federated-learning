import functools
import logging
import time
from typing import Callable, Optional

def _short_repr(arg, maxlen=200):
    try:
        s = repr(arg)
    except Exception:
        return "<unrepr>"
    if len(s) > maxlen:
        return s[:maxlen] + "..."
    return s

def log_calls(logger: Optional[logging.Logger] = None):
    """Decorator to log function entry/exit with timing. Works for sync and async."""
    def _decorator(func: Callable):
        lg = logger or logging.getLogger(func.__module__)

        if hasattr(func, "__call__") and hasattr(func, "__name__"):
            pass

        @functools.wraps(func)
        def _sync(*args, **kwargs):
            try:
                lg.debug(f"ENTER {func.__name__} args={[_short_repr(a) for a in args]} kwargs={ {k:_short_repr(v) for k,v in kwargs.items()} }")
                t0 = time.time()
                res = func(*args, **kwargs)
                dur = time.time() - t0
                lg.debug(f"EXIT  {func.__name__} duration={dur:.4f}s")
                return res
            except Exception as e:
                lg.exception(f"EXCEPT {func.__name__}: {e}")
                raise

        async def _async(*args, **kwargs):
            try:
                lg.debug(f"ENTER {func.__name__} args={[_short_repr(a) for a in args]} kwargs={ {k:_short_repr(v) for k,v in kwargs.items()} }")
                t0 = time.time()
                res = await func(*args, **kwargs)
                dur = time.time() - t0
                lg.debug(f"EXIT  {func.__name__} duration={dur:.4f}s")
                return res
            except Exception as e:
                lg.exception(f"EXCEPT {func.__name__}: {e}")
                raise

        if callable(func):
            if hasattr(func, "__code__") and (func.__code__.co_flags & 0x80):
                # unlikely branch; fallback
                return _async
            try:
                import inspect

                if inspect.iscoroutinefunction(func):
                    return functools.wraps(func)(_async)
            except Exception:
                pass
        return functools.wraps(func)(_sync)

    return _decorator
