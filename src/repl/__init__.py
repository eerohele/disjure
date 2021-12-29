from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
import io
import queue
import os
import pathlib
import posixpath
import socket
import types
import uuid
from threading import Lock

from . import backchannel
from ...api import edn
from ..log import log
from .. import base64
from .. import settings


def read_until_prompt(socket: socket.SocketType):
    """Given a socket, read bytes from the socket until `=> `, then return the
    read bytes."""
    bs = bytearray()

    while bs[-3:] != bytearray(b"=> "):
        bs.extend(socket.recv(1))

    return bs


BASE64_BLOB = "(def load-base64 (let [decoder (java.util.Base64/getDecoder)] (fn [blob file filename] (with-open [reader (-> decoder (.decode blob) (java.io.ByteArrayInputStream.) (java.io.InputStreamReader.) (clojure.lang.LineNumberingPushbackReader.))] (clojure.lang.Compiler/load reader file filename)))))"


class Client(ABC):
    @abstractmethod
    def dialect_name(self):
        pass

    """A `Client` connects to a Clojure socket server, then sends over code
    that a) starts a custom REPL on top of the default REPL and b) starts a
    backchannel socket server. The `Client` then connects to the backchannel
    socket server and uses that connection to communicate with the Clojure
    runtime.

    Tutkain uses the REPL for evaluations and the backchannel for everything
    else (auto-completion, metadata lookup, static analysis, etc.)"""

    def start_workers(self):
        self.executor.submit(self.send_loop)
        self.executor.submit(self.recv_loop)
        return self

    def write_line(self, line):
        """Given a string, write the string followed by a newline into the file object
        associated with the socket of this client."""
        self.buffer.write(line)
        self.buffer.write("\n")
        self.buffer.flush()

    def module_loaded(self, response):
        if response.get(edn.Keyword("result")) == edn.Keyword("ok"):
            self.capabilities.add(response.get(edn.Keyword("filename")))

    def load_modules(self, modules):
        for filename, requires in modules.items():
            path = os.path.join(settings.source_root(), filename)

            with open(path, "rb") as file:
                self.backchannel.send({
                    "op": edn.Keyword("load-base64"),
                    "path": path,
                    "filename": filename,
                    "blob": base64.encode(file.read()),
                    "requires": requires
                }, self.module_loaded)

    @abstractmethod
    def handshake(self):
        pass

    def read_greeting(self):
        greeting = read_until_prompt(self.socket)

        if not self.has_backchannel():
            self.printq.put(greeting.decode("utf-8"))

        return greeting

    def connect(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((self.host, self.port))
        self.buffer = self.socket.makefile(mode="rw")
        log.debug({"event": "client/connect", "host": self.host, "port": self.port})

        log.debug({
            "event": "client/handshake",
            "data": self.executor.submit(self.read_greeting).result(timeout=5)
        })

        return self

    def source_path(self, filename):
        """Given the name of a Clojure source file belonging to this package,
        return the absolute path to that source file as a POSIX path (for
        Windows compatibility)."""
        return posixpath.join(pathlib.Path(settings.source_root()).as_posix(), filename)

    def has_backchannel(self):
        return self.options.get("backchannel", {}).get("enabled", True)

    def __init__(self, host, port, name, options={}):
        self.id = str(uuid.uuid4())
        self.host = host
        self.port = port
        self.name = name
        self.sendq = queue.Queue()
        self.printq = queue.Queue()
        self.executor = ThreadPoolExecutor(thread_name_prefix=f"{self.name}.{self.id}")
        self.backchannel = types.SimpleNamespace(send=lambda *args, **kwargs: None, halt=lambda *args: None)
        self.options = options
        self.capabilities = set()
        self.lock = Lock()
        self.ready = False
        self.on_close = lambda: None

    def send_loop(self):
        """Start a loop that reads strings from `self.sendq` and sends them to
        the Clojure runtime this client is connected to for evaluation."""
        while item := self.sendq.get():
            log.debug({"event": "client/send", "item": item})
            self.buffer.write(item)
            self.buffer.write("\n")
            self.buffer.flush()

        self.buffer.write(":repl/quit")
        self.buffer.flush()
        self.socket.shutdown(socket.SHUT_RDWR)
        self.on_close()
        log.debug({"event": "thread/exit"})

    @abstractmethod
    def evaluate(self, code, options={"file": "NO_SOURCE_FILE", "line": 0, "column": 0}):
        """Given a string of Clojure code, send it for evaluation to the
        Clojure runtime this client is connected to.

        Accepts a map of options:
        - `file`: the absolute path to the source file this evaluation is
                  associated with (default `"NO_SOURCE_FILE"`)
        - `line`: the line number the code is positioned at (default `0`)
        - `column`: the column number the code is positioned at (default `0`)"""
        pass

    @abstractmethod
    def switch_namespace(self, ns):
        """Given the name of a Clojure namespace as a string, ask the Clojure
        runtime this client is connected to to switch to that namespace."""
        pass

    def print(self, item):
        self.printq.put(item)

    def handle(self, item):
        self.print(item.replace("\r", ""))

    def recv_loop(self):
        """Start a loop that reads evaluation responses from a socket and puts
        them in a print queue."""
        try:
            while response := self.socket.recv(io.DEFAULT_BUFFER_SIZE):
                item = response.decode("utf-8")
                log.debug({"event": "client/recv", "item": item})
                self.handle(item)
        except OSError as error:
            log.error({"event": "error", "error": error})
        finally:
            self.print(
                edn.kwmap({
                    "tag": edn.Keyword("err"),
                    "val": f"[Tutkain] Disconnected from {self.dialect_name()} runtime at {self.host}:{self.port}.\n"
                })
            )

            # Put a None into the queue to tell consumers to stop reading it.
            self.print(None)

            log.debug({"event": "thread/exit"})

            try:
                self.buffer.close()
                self.socket.close()
                log.debug({"event": "client/disconnect"})
            except OSError as error:
                log.debug({"event": "error", "exception": error})

    def eval_context_message(self, options):
        message = {
            "op": edn.Keyword("set-eval-context"),
            "file": options.get("file", "NO_SOURCE_FILE"),
            "line": options.get("line", 0) + 1,
            "column": options.get("column", 0) + 1,
        }

        if response := options.get("response"):
            message["response"] = edn.kwmap(response)

        return message

    def halt(self):
        """Halt this client."""
        log.debug({"event": "client/halt"})

        # Feed poison pill to input queue.
        self.sendq.put(None)
        self.executor.shutdown(wait=False)
        self.backchannel.halt()


class JVMClient(Client):
    def dialect_name(self):
        return "Clojure"

    def handshake(self):
        self.write_line("(ns tutkain.bootstrap)")
        self.buffer.readline()
        self.write_line(BASE64_BLOB)
        self.buffer.readline()

        for filename in ["format.clj", "backchannel.clj", "base64.clj", "repl.clj"]:
            path = self.source_path(filename)

            with open(path, "rb") as file:
                blob = base64.encode(file.read())
                self.write_line(f"""(load-base64 "{blob}" "{path}" "{os.path.basename(path)}")""")

            self.buffer.readline()

        backchannel_opts = self.options.get("backchannel", {})
        backchannel_port = backchannel_opts.get("port", 0)
        backchannel_bind_address = backchannel_opts.get("bind_address", "localhost")
        self.write_line(f"""(try (tutkain.repl/repl {{:port {backchannel_port} :bind-address "{backchannel_bind_address}"}}) (catch Exception ex (.toString ex)))""")
        line = self.buffer.readline()

        if not line.startswith('{'):
            self.print(edn.kwmap({
                "tag": edn.Keyword("err"),
                "val": "Couldn't connect to Clojure REPL.\n"
            }))

            self.print(edn.kwmap({
                "tag": edn.Keyword("err"),
                "val": line + "\n"
            }))

            self.print(edn.kwmap({
                "tag": edn.Keyword("err"),
                "val": "NOTE: Tutkain requires Clojure 1.10.0 or newer.\n"
            }))
        else:
            ret = edn.read(line)

            if (host := ret.get(edn.Keyword("host"))) and (port := ret.get(edn.Keyword("port"))):
                self.backchannel = backchannel.Client(self.print).connect(self.id, host, port)
                self.print(ret.get(edn.Keyword("greeting")))
            else:
                self.print(ret)

        self.load_modules({
            "java.clj": [],
            "lookup.clj": [],
            "completions.clj": [],
            "load_blob.clj": [],
            "test.clj": [],
            "query.clj": [],
            "analyzer.clj": [
                edn.Symbol("clojure.tools.reader"),
                edn.Symbol("clojure.tools.analyzer.ast")
            ],
            "analyzer/jvm.clj": [
                edn.Symbol("tutkain.analyzer"),
                edn.Symbol("clojure.tools.analyzer.jvm")
            ]
        })

    def __init__(self, host, port, options={}):
        super().__init__(host, port, "tutkain.clojure.client", options=options)

    def connect(self):
        super().connect()

        if self.has_backchannel():
            # Start a promptless REPL so that we don't need to keep sinking the prompt.
            self.write_line("""(clojure.main/repl :prompt (constantly "") :need-prompt (constantly false))""")
            self.handshake()

        self.start_workers()
        return self

    def switch_namespace(self, ns):
        self.sendq.put(f"(tutkain.repl/switch-ns {ns})")

    def evaluate(self, code, options={"file": "NO_SOURCE_FILE", "line": 0, "column": 0}):
        if self.has_backchannel():
            self.backchannel.send(
                self.eval_context_message(options),
                lambda _: self.sendq.put(code)
            )
        else:
            self.sendq.put(code)


class JSClient(Client):
    def dialect_name(self):
        return "ClojureScript"

    def __init__(self, host, port, prompt_for_build_id, options={}):
        super().__init__(host, port, "tutkain.cljs.client", options=options)
        self.prompt_for_build_id = prompt_for_build_id

    def connect(self):
        super().connect()
        # Start a promptless REPL so that we don't need to keep sinking the prompt.
        self.write_line('(clojure.main/repl :prompt (constantly "") :need-prompt (constantly false))')
        self.write_line("""(sort (shadow.cljs.devtools.api/get-build-ids))""")
        build_id_options = edn.read_line(self.buffer)

        self.executor.submit(
            self.prompt_for_build_id,
            build_id_options,
            lambda index: self.handshake(build_id_options[index])
        )

        return self

    def handshake(self, build_id):
        self.write_line("(ns tutkain.bootstrap)")
        self.buffer.readline()
        self.write_line(BASE64_BLOB)
        self.buffer.readline()

        for filename in ["format.clj", "backchannel.clj", "base64.clj", "shadow.clj"]:
            path = self.source_path(filename)

            with open(path, "rb") as file:
                blob = base64.encode(file.read())
                self.write_line(f"""(load-base64 "{blob}" "{path}" "{os.path.basename(path)}")""")

            self.buffer.readline()

        backchannel_port = self.options.get("backchannel", {}).get("port", 0)
        self.write_line(f"""(tutkain.shadow/repl {{:build-id {build_id} :port {backchannel_port}}})""")

        ret = edn.read_line(self.buffer)
        host = ret.get(edn.Keyword("host"))
        port = ret.get(edn.Keyword("port"))
        self.backchannel = backchannel.Client(self.print).connect(self.id, host, port)
        greeting = ret.get(edn.Keyword("greeting"))
        self.print(greeting)

        # NOTE: If you make changes to module loading, make sure you manually
        # test connecting to a ClojureScript runtime *without* connecting to
        # a Clojure runtime first to make sure we're loading everything we
        # need.
        self.load_modules({
            "lookup.clj": [],
            "java.clj": [],
            "completions.clj": [],
            "query.clj": [],
            "cljs.clj": [],
            "shadow.clj": [],
            "analyzer.clj": [
                edn.Symbol("clojure.tools.reader"),
                edn.Symbol("clojure.tools.analyzer.ast")
            ],
            "analyzer/js.clj": [
                edn.Symbol("tutkain.analyzer")
            ]
        })

        self.start_workers()

    def switch_namespace(self, ns):
        self.sendq.put(f"(in-ns '{ns})")

    def evaluate(self, code, options={"file": "NO_SOURCE_FILE", "line": 0, "column": 0}):
        if self.has_backchannel():
            self.backchannel.send(
                self.eval_context_message(options),
                lambda _: self.sendq.put(code)
            )
        else:
            self.sendq.put(code)


class BabashkaClient(Client):
    def dialect_name(self):
        return "Babashka"

    def __init__(self, host, port):
        super().__init__(host, port, "tutkain.bb.client")

    def handshake(self):
        self.write_line("""(println "Babashka" (System/getProperty "babashka.version"))""")
        self.print(self.buffer.readline())
        self.buffer.readline()
        self.start_workers()

    def connect(self):
        super().connect()
        self.handshake()
        return self

    def has_backchannel(self):
        return False

    def switch_namespace(self, ns):
        self.sendq.put(f"(in-ns '{ns})")

    def print(self, item):
        # Babashka currently has no backchannel we can use for err and out, so
        # we just print everything without syntax highlighting.
        self.printq.put(edn.kwmap({"tag": edn.Keyword("out"), "val": item}))

    def evaluate(self, code, options={"file": "NO_SOURCE_FILE", "line": 0, "column": 0}):
        self.sendq.put(code)
