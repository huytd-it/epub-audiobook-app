import asyncio
import socket
import struct
import sys

import uvicorn


def test_http_listener_survives_aborted_clients():
    # Resolve the factory exactly as the desktop/dev Uvicorn commands do.
    factory = uvicorn.Config("app.main:app", loop="app.server:loop_factory").get_loop_factory()
    loop = factory()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    class HttpProtocol(asyncio.Protocol):
        def connection_made(self, transport):
            self.transport = transport

        def data_received(self, data):
            self.transport.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            self.transport.close()

    async def exercise():
        if sys.platform == "win32":
            assert isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)
        server = await loop.create_server(HttpProtocol, "127.0.0.1", 0)
        address = server.sockets[0].getsockname()

        def abort_clients():
            for _ in range(40):
                with socket.create_connection(address, timeout=2) as client:
                    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                      struct.pack("hh" if sys.platform == "win32" else "ii", 1, 0))

        try:
            await asyncio.to_thread(abort_clients)
            for _ in range(5):
                reader, writer = await asyncio.open_connection(*address)
                writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                assert (await asyncio.wait_for(reader.read(), timeout=2)).endswith(b"\r\n\r\nok")
                writer.close()
                await writer.wait_closed()
            assert server.is_serving()
            assert not [error for error in errors if "accept" in error.get("message", "").lower()]
        finally:
            server.close()
            await server.wait_closed()

    try:
        loop.run_until_complete(asyncio.wait_for(exercise(), timeout=15))
        loop.run_until_complete(loop.shutdown_default_executor())
    finally:
        loop.close()
