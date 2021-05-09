from unittest import TestCase

from Tutkain.api import edn
from Tutkain.package import source_root, start_logging, stop_logging
from Tutkain.src.repl.client import Client

from .mock import Server


class TestClient(TestCase):
    @classmethod
    def setUpClass(self):
        start_logging(False)

    @classmethod
    def tearDownClass(self):
        stop_logging()

    def test_smoke(self):
        with Server(greeting="user=> ") as server:
            with Client(source_root(), server.host, server.port, wait=False) as client:
                # Client starts sub-REPL
                server.recv()

                with Server() as backchannel:
                    server.send({
                        edn.Keyword("tag"): edn.Keyword("ret"),
                        edn.Keyword("val"): f"""{{:host "localhost", :port {backchannel.port}}}""",
                    })

                    for filename in ["lookup.clj", "completions.clj", "load_blob.clj", "test.clj"]:
                        response = edn.read(backchannel.recv())
                        self.assertEquals(edn.Keyword("load-base64"), response.get(edn.Keyword("op")))
                        self.assertEquals(filename, response.get(edn.Keyword("filename")))

                    self.assertEquals(
                        Client.handshake_payloads["print_version"],
                        server.recv().rstrip()
                    )

                    server.send({
                        edn.Keyword("tag"): edn.Keyword("out"),
                        edn.Keyword("val"): "Clojure 1.11.0-alpha1"
                    })

                    server.send({
                        edn.Keyword("tag"): edn.Keyword("ret"),
                        edn.Keyword("val"): "nil",
                        edn.Keyword("ns"): "user",
                        edn.Keyword("ms"): 0,
                        edn.Keyword("form"): Client.handshake_payloads["print_version"]
                    })

                    self.assertEquals({
                        "printable": "Clojure 1.11.0-alpha1\n",
                        "response": {
                            edn.Keyword("tag"): edn.Keyword("out"),
                            edn.Keyword("val"): "Clojure 1.11.0-alpha1"
                        }
                    }, client.printq.get(timeout=1))

                    client.eval("(inc 1)")

                    self.assertEquals(
                        {edn.Keyword("op"): edn.Keyword("set-eval-context"),
                         edn.Keyword("id"): 5,
                         edn.Keyword("file"): "NO_SOURCE_FILE",
                         edn.Keyword("ns"): edn.Symbol("user"),
                         edn.Keyword("line"): 1,
                         edn.Keyword("column"): 1},
                        edn.read(backchannel.recv())
                    )

                    backchannel.send({
                        edn.Keyword("id"): 5,
                        edn.Keyword("file"): None,
                        edn.Keyword("ns"): edn.Symbol("user")
                    })

                    self.assertEquals("(inc 1)\n", server.recv())

                    response = {
                        edn.Keyword("tag"): edn.Keyword("ret"),
                        edn.Keyword("val"): "2",
                        edn.Keyword("ns"): "user",
                        edn.Keyword("ms"): 1,
                        edn.Keyword("form"): "(inc 1)"
                    }

                    server.send(response)

                    self.assertEquals(
                        {"printable": "2\n", "response": response},
                        client.printq.get(timeout=1)
                    )

        self.assertEquals(":repl/quit\n", server.recv())
