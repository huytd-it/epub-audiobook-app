from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from fastapi import Request


@contextmanager
def locked_conn(request: Request):
    """Acquire the shared db_lock and yield the shared sqlite3 connection, mirroring how the
    worker guards every access to the same connection (see app/worker.py)."""
    conn: sqlite3.Connection = request.app.state.conn
    lock: threading.Lock = request.app.state.db_lock
    with lock:
        yield conn
