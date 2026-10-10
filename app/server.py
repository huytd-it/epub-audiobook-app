"""Uvicorn loop factory: --loop app.server:loop_factory.

Python 3.11's Windows Proactor closes the listening socket when AcceptEx raises
WinError 64 (a client disconnect during accept). The process can stay alive but
stop accepting HTTP. Use the selector loop for this HTTP server on Windows.
Worker subprocesses use synchronous subprocess APIs in their worker threads;
this does not change the loop policy for TTS workers' own asyncio.run calls.
"""
from __future__ import annotations

import asyncio
import sys


def loop_factory() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()
