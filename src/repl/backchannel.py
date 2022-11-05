import itertools
import socket
from queue import Queue
from threading import Lock, Thread
from typing import IO

from ...api import edn
from ..log import log


class Client:
    """A backchannel client.

    Connects to a backchannel server to allow consumers to send messages to
    the server and register callbacks to be called on responses to those
    messages."""

    def __init__(self, default_handler):
        """Given a default response message handler function, initialize a new
        backchannel client."""
        self.default_handler = default_handler
        self.sendq = Queue()
        self.handlers = {}
        self.message_id = itertools.count(1)
        self.lock = Lock()

    def send_loop(self, sock: socket.SocketType, buffer: IO):
        """Given a socket and a file object, start a loop that gets items from
        the send queue of this backchannel client and writes them as EDN into
        the file object.

        Attempts to shut down the socket upon exiting the loop."""
        try:
            while message := self.sendq.get():
                log.debug({"event": "backchannel/send", "message": message})
                edn.write_line(buffer, message)
        except OSError as error:
            log.error({"event": "send_error", "error": error})
        finally:
            try:
                sock.shutdown(socket.SHUT_RDWR)
                sock.close()
                buffer.close()
                log.debug({"event": "backchannel/disconnect"})
            except OSError as e:
                log.debug({"event": "send_error", "exception": e})

            log.debug({"event": "thread/exit"})

    def recv_loop(self, buffer: IO):
        """Given a file object, start a loop that reads EDN messages from the
        file object and calls the handler function of this backchannel client
        on every message."""
        try:
            while message := edn.read_line(buffer):
                log.debug({"event": "backchannel/recv", "message": message})
                self.handle(message)
        except OSError as error:
            log.error({"event": "recv_error", "error": error})
        finally:
            log.debug({"event": "thread/exit"})

    def connect(self, id, host, port):
        """Given a host and a port number, connect this backchannel client to
        the backchannel server listening on host:port."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((host, port))
        buffer = sock.makefile(mode="rw")

        log.debug({"event": "backchannel/connect", "host": host, "port": port})

        send_loop = Thread(daemon=True, target=lambda: self.send_loop(sock, buffer))
        send_loop.name = f"tutkain.backchannel.{id}.send_loop"
        send_loop.start()

        recv_loop = Thread(daemon=True, target=lambda: self.recv_loop(buffer))
        recv_loop.name = f"tutkain.backchannel.{id}.recv_loop"
        recv_loop.start()

        return self

    def register_handler(self, id, handler):
        with self.lock:
            self.handlers[id] = handler

    def send(self, message, handler=None):
        """Given a message (a dict) and, optionally, a handler function, put
        the message into the send queue of this backchannel client and register
        the handler to be called on the message response."""
        message = edn.kwmap(message)
        message_id = next(self.message_id)
        message[edn.Keyword("id")] = message_id

        if handler:
            self.register_handler(message_id, handler)

        self.sendq.put(message)

    def handle(self, message):
        """Given a message, call the handler function registered for the
        message in this backchannel instance.

        If there's no handler function for the message, call the default
        handler function instead."""
        try:
            id = message.get(edn.Keyword("id"))

            try:
                with self.lock:
                    handler = self.handlers.get(id, self.default_handler)

                handler.__call__(message)
            except AttributeError as error:
                log.error({"event": "error", "message": message, "error": error})
            finally:
                with self.lock:
                    self.handlers.pop(id, None)
        except AttributeError as error:
            raise ValueError(f"Got invalid message: {message}")

    def halt(self):
        """Halt this backchannel client."""
        log.debug({"event": "backchannel/halt"})
        self.sendq.put(None)
