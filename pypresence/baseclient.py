import asyncio
import inspect
import json
import os
import struct
import sys
import tempfile
from typing import Union

from .exceptions import *
from .payloads import Payload


class BaseClient:

    def __init__(self, client_id: str, pipe=0, loop=None, handler=None, isasync=False):
        self.is_async = isasync

        client_id = str(client_id)
        if sys.platform == 'linux' or sys.platform == 'darwin':
            tempdir = (os.environ.get('XDG_RUNTIME_DIR') or tempfile.gettempdir())
            snap_path = '{0}/snap.discord'.format(tempdir)
            pipe_file = 'discord-ipc-{0}'.format(pipe)
            if os.path.isdir(snap_path):
                self.ipc_path = '{0}/{1}'.format(snap_path, pipe_file)
            else:
                self.ipc_path = '{0}/{1}'.format(tempdir, pipe_file)
        elif sys.platform == 'win32':
            self.ipc_path = r'\\?\pipe\discord-ipc-' + str(pipe)

        if loop is not None:
            self.update_event_loop(loop)
        else:
            self.update_event_loop(self.get_event_loop())

        self.sock_reader = None  # type: asyncio.StreamReader
        self.sock_writer = None  # type: asyncio.StreamWriter

        self.client_id = client_id

        if handler is not None:
            if not inspect.isfunction(handler):
                raise PyPresenceException('Error handler must be a function.')
            args = inspect.getfullargspec(handler).args
            if args[0] == 'self':
                args = args[1:]
            if len(args) != 2:
                raise PyPresenceException('Error handler should only accept two arguments.')

            if self.is_async:
                if not inspect.iscoroutinefunction(handler):
                    raise InvalidArgument('Coroutine', 'Subroutine', 'You are running async mode - '
                                                                     'your error handler should be awaitable.')
                err_handler = self._async_err_handle
            else:
                err_handler = self._err_handle

            loop.set_exception_handler(err_handler)
            self.handler = handler

        if getattr(self, "on_event", None):  # Tasty bad code ;^)
            self._events_on = True
        else:
            self._events_on = False

    @staticmethod
    def get_event_loop(force_fresh=False):
        if sys.platform == 'linux' or sys.platform == 'darwin':
            if force_fresh:
                return asyncio.new_event_loop()
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                return asyncio.new_event_loop()
            return loop
        elif sys.platform == 'win32':
            if force_fresh:
                return asyncio.ProactorEventLoop()
            loop = asyncio.get_event_loop()
            if isinstance(loop, asyncio.ProactorEventLoop) and not loop.is_closed():
                return loop
            return asyncio.ProactorEventLoop()

    def update_event_loop(self, loop):
        self.loop = loop
        asyncio.set_event_loop(self.loop)

    def _err_handle(self, loop, context: dict):
        result = self.handler(context['exception'], context['future'])
        if inspect.iscoroutinefunction(self.handler):
            loop.run_until_complete(result)

    async def _async_err_handle(self, loop, context: dict):
        await self.handler(context['exception'], context['future'])

    async def read_output(self):
        try:
            data = await self.sock_reader.read(1024)
        except BrokenPipeError:
            raise InvalidID
        status_code, length = struct.unpack('<II', data[:8])
        payload = json.loads(data[8:].decode('utf-8'))
        if payload["evt"] == "ERROR":
            raise ServerError(payload["data"]["message"])
        return payload

    def send_data(self, op: int, payload: Union[dict, Payload]):
        if isinstance(payload, Payload):
            payload = payload.data
        payload = json.dumps(payload)
        self.sock_writer.write(
            struct.pack(
                '<II',
                op,
                len(payload)) +
            payload.encode('utf-8'))

    async def handshake(self):
        if sys.platform == 'linux' or sys.platform == 'darwin':
            self.sock_reader, self.sock_writer = await asyncio.open_unix_connection(self.ipc_path, loop=self.loop)
        elif sys.platform == 'win32' or sys.platform == 'win64':
            self.sock_reader = asyncio.StreamReader(loop=self.loop)
            reader_protocol = asyncio.StreamReaderProtocol(self.sock_reader, loop=self.loop)
            try:
                self.sock_writer, _ = await self.loop.create_pipe_connection(lambda: reader_protocol, self.ipc_path)
            except FileNotFoundError:
                raise InvalidPipe
        self.send_data(0, {'v': 1, 'client_id': self.client_id})
        data = await self.sock_reader.read(1024)
        code, length = struct.unpack('<ii', data[:8])
        if self._events_on:
            self.sock_reader.feed_data = self.on_event
