import json
import os
import pathlib
import sublime
import urllib
import tempfile

from ..api import edn
from . import namespace
from . import selectors
from . import settings
from . import state


EXAMPLE_SOURCE_PATH = os.path.join(sublime.cache_path(), "Tutkain", "clojuredocs.edn")
EXAMPLE_URI = "https://clojuredocs.org/clojuredocs-export.json"


def refresh_cache(window, callback=lambda: None):
    window.status_message("Downloading ClojureDocs examples...")

    try:
        response = urllib.request.urlopen("https://clojuredocs.org/clojuredocs-export.json")
        encoding = response.info().get_content_charset("utf-8")
        data = json.loads(response.read().decode(encoding))

        with open(EXAMPLE_SOURCE_PATH, "w") as file:
            output = {}

            for var in data.get("vars"):
                if examples := var.get("examples"):
                    symbol = edn.Symbol(var.get("name"), var.get("ns"))
                    output[symbol] = list(map(lambda example: example.get("body"), examples))

            edn.write1(file, output)

        window.status_message("ClojureDocs examples downloaded.")
        callback()
    except urllib.error.URLError as error:
        sublime.error_message(f"[Tutkain] Error trying to fetch ClojureDocs examples from {EXAMPLE_URI}:\n\n {repr(error)}\n\nAre you connected to the internet?")
    except OSError as error:
        sublime.error_message(f"[Tutkain] Error trying to save ClojureDocs examples into {EXAMPLE_SOURCE_PATH}:\n {repr(error)}")


def send_message(window, client, ns, sym):
    client.backchannel.send({
        "op": edn.Keyword("examples"),
        "source-path": EXAMPLE_SOURCE_PATH,
        "ns": ns,
        "sym": sym
    }, lambda response: handler(window, client, response))


def handler(window, client, response):
    symbol = response.get(edn.Keyword("symbol"))

    if examples := response.get(edn.Keyword("examples")):
        descriptor, temp_path = tempfile.mkstemp(".clj")

        try:
            path = pathlib.Path(temp_path)

            with open(path, "w") as file:
                file.write(f";; ClojureDocs examples for {symbol}\n\n")

                for example in examples:
                    file.write(example)

            view = window.open_file(f"{path}", flags=sublime.ADD_TO_SELECTION | sublime.SEMI_TRANSIENT)

            view.settings().set("tutkain_temp_file", {
                "path": temp_path,
                "descriptor": descriptor,
                "name": f"{symbol.name}.clj"
            })

            view.set_scratch(True)

            # Switch to the symbol's namespace
            if (ns := symbol.namespace) and settings.load().get("auto_switch_namespace"):
                client.switch_namespace(ns)
        except:
            if os.path.exists(temp_path):
                os.close(descriptor)
                os.remove(temp_path)
    else:
        window.status_message(f"No examples found for {symbol}.")


def show(view):
    window = view.window()

    if client := state.get_client(window, edn.Keyword("clj")):
        point = view.sel()[0].begin()
        ns = edn.Symbol(namespace.name(view) or namespace.default(edn.Keyword("clj")))

        if region := selectors.expand_by_selector(view, point, "meta.symbol"):
            sym = edn.Symbol(view.substr(region))
            send_message(window, client, ns, sym)
        else:
            input_panel = view.window().show_input_panel(
                "Symbol: ",
                "",
                lambda sym: send_message(window, client, ns, edn.Symbol(sym)),
                lambda _: None,
                lambda: None
            )

            input_panel.assign_syntax("Packages/Tutkain/Clojure (Tutkain).sublime-syntax")
            input_panel.settings().set("auto_complete", True)
    else:
        view.window().status_message("ERR: Not connected to a Clojure REPL.")


def show_examples(view):
    if not os.path.exists(EXAMPLE_SOURCE_PATH):
        sublime.set_timeout_async(
            lambda: refresh_cache(view.window(), lambda: show(view)),
            0
        )
    else:
        show(view)
