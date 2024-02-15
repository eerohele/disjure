import sublime

from ..api import edn
from . import base64, dialects, forms, namespace, selectors, sexp, state

KINDS = {
    "function": sublime.KIND_FUNCTION,
    "var": sublime.KIND_VARIABLE,
    "macro": (sublime.KIND_ID_FUNCTION, "m", "Macro"),
    "multimethod": (sublime.KIND_ID_FUNCTION, "u", "Multimethod"),
    "namespace": sublime.KIND_NAMESPACE,
    # "package": (sublime.KIND_ID_NAMESPACE, "p", "Package"),
    "field": sublime.KIND_VARIABLE,
    "class": sublime.KIND_TYPE,
    "special-form": (sublime.KIND_ID_FUNCTION, "s", "Special form"),
    "method": (sublime.KIND_ID_FUNCTION, "i", "Instance method"),
    "static-method": (sublime.KIND_ID_FUNCTION, "c", "Static method"),
    "keyword": sublime.KIND_KEYWORD,
    "protocol": sublime.KIND_TYPE,
    "navigation": sublime.KIND_NAVIGATION,
    "local": (sublime.KIND_ID_VARIABLE, "l", "Local"),
}


def completion_item(item):
    details = ""

    if klass := item.get(edn.Keyword("class")):
        details = f"on <code>{klass}</code>"

        if return_type := item.get(edn.Keyword("return-type")):
            details += f", returns <code>{return_type}</code>"
    elif edn.Keyword("doc") in item:
        d = {}

        for k, v in item.items():
            d[k.name] = v.name if isinstance(v, edn.Keyword) else v

        details = f"""<a href="{sublime.command_url("tutkain_show_popup", args={"item": d})}">More</a>"""

    type = item.get(edn.Keyword("type"))
    candidate = item.get(edn.Keyword("candidate"))
    trigger = item.get(edn.Keyword("trigger"), candidate + " ")

    if type == edn.Keyword("navigation"):
        candidate = candidate + "/"
    # elif type == edn.Keyword("package"):
    #     # In imports
    #     candidate = candidate + " "

    arglists = item.get(edn.Keyword("arglists"), [])
    annotation = ""

    if type in {edn.Keyword("method"), edn.Keyword("static-method")}:
        annotation = "(" + ", ".join(arglists) + ")"
    else:
        annotation += " ".join(arglists)

    kind = KINDS.get(type.name, sublime.KIND_AMBIGUOUS)

    return sublime.CompletionItem(
        trigger=trigger,
        completion=candidate,
        completion_format=sublime.COMPLETION_FORMAT_SNIPPET,
        kind=kind,
        annotation=annotation,
        details=details,
    )


def enclosing_sexp_sans_prefix(view, expr, prefix):
    """Given a view, an S-expression, and a prefix, return the S-expression
    with the prefix removed.

    The prefix is unlikely to resolve, so we must remove it from the
    S-expression to be able to analyze it on the server.
    """
    before = sublime.Region(expr.open.region.begin(), prefix.begin())
    after = sublime.Region(prefix.end(), expr.close.region.end())
    return view.substr(before) + view.substr(after)


def handler(completion_list, response):
    if completions := response.get(edn.Keyword("completions"), []):
        completion_list.set_completions(
            map(
                completion_item,
                completions,
            ),
            flags=sublime.AutoCompleteFlags.INHIBIT_WORD_COMPLETIONS
            | sublime.AutoCompleteFlags.INHIBIT_REORDER,
        )


def get_completions(view, prefix, point):
    # The AC widget won't show up after typing a character that's in word_separators.
    #
    # Removing the forward slash from word_separators is a no go, though. Therefore,
    # we're gonna do this awful hack where we remove the forward slash from
    # word_separators for the duration of the AC interaction.
    word_separators = view.settings().get("word_separators")

    try:
        view.settings().set("word_separators", word_separators.replace("/", ""))

        if (
            view.match_selector(
                point,
                "source.clojure & (meta.symbol - meta.function.parameters) | constant.other.keyword",
            )
            and (dialect := dialects.for_point(view, point))
            and (client := state.get_client(view.window(), dialect))
        ):
            if scope := selectors.expand_by_selector(
                view, point, "meta.symbol | constant.other.keyword"
            ):
                prefix = view.substr(scope)

            completion_list = sublime.CompletionList()

            op = {
                "op": edn.Keyword("completions"),
                "prefix": prefix,
                "ns": namespace.name(view),
                "dialect": dialect,
            }

            if (outermost := sexp.outermost(view, point)) and (
                "analyzer.clj" in client.capabilities
            ):
                code = enclosing_sexp_sans_prefix(view, outermost, scope)
                start_line, start_column = view.rowcol(outermost.open.region.begin())
                line, column = view.rowcol(point)
                op["file"] = view.file_name() or "NO_SOURCE_FILE"
                op["start-line"] = start_line + 1
                op["start-column"] = start_column + 1
                op["line"] = line + 1
                op["column"] = column + 1
                op["enclosing-sexp"] = base64.encode(code.encode("utf-8"))

            client.send_op(
                op, handler=lambda response: handler(completion_list, response)
            )

            return completion_list
    finally:
        view.settings().set("word_separators", word_separators)
