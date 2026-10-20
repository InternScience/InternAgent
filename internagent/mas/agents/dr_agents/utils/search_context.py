"""Conversation identity for web research, independent of cached tool clients."""

from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4


_search_session = ContextVar("search_session", default=None)


@contextmanager
def search_run():
    """Reuse an enclosing research run, or start a fresh standalone run."""
    token = None
    if _search_session.get() is None:
        token = _search_session.set(str(uuid4()))
    try:
        yield
    finally:
        if token is not None:
            _search_session.reset(token)


def search_session_id():
    """Direct tool calls outside a workflow are independent standalone runs."""
    return _search_session.get() or str(uuid4())
