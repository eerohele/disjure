(ns tutkain.analyzer.jvm
  (:require
   [clojure.tools.analyzer.jvm :as analyzer.jvm]
   [clojure.tools.analyzer.passes :as passes]
   [clojure.tools.analyzer.passes.source-info :as source-info]
   [clojure.tools.analyzer.passes.uniquify :as uniquify]
   [tutkain.analyzer :as analyzer]
   [tutkain.base64 :refer [base64-reader]]
   [tutkain.rpc :refer [respond-to]]))

(def ^:private analyzer-passes
  {:local-instances (passes/schedule #{#'source-info/source-info #'uniquify/uniquify-locals})
   :local-symbols (passes/schedule #{#'source-info/source-info})})

(defn ^:private parse-namespace
  [ns]
  (or (some-> ns symbol find-ns) (the-ns 'user)))

(def reader-opts
  {:features #{:clj :t.a.jvm} :read-cond :allow})

(defn analyze
  [start-line start-column reader]
  (analyzer/analyze
    :start-line start-line
    :start-column start-column
    :reader reader
    :reader-opts reader-opts
    :analyzer analyzer.jvm/analyze))

(defmethod analyzer/local-instances :default
  [{:keys [enclosing-sexp file ns start-column start-line] :as message}]
  (try
    (binding [*file* file
              *ns* (parse-namespace ns)]
      (with-open [reader (base64-reader enclosing-sexp)]
        (let [nodes (binding [analyzer.jvm/run-passes (analyzer-passes :local-instances)]
                      (analyze start-line start-column reader))
              positions (analyzer/local-positions nodes (analyzer/position message))]
          (respond-to message {:positions positions}))))
    (catch Throwable ex
      (respond-to message {:tag :ret :debug true :val (pr-str (Throwable->map ex))}))))

(defn local-symbols
  [{:keys [enclosing-sexp file ns start-line start-column line column]}]
  (when (and enclosing-sexp (pos-int? start-column) (pos-int? start-line) (pos-int? line) (pos-int? column))
    (binding [*file* file
              *ns* ns]
      (try
        (let [nodes (binding [analyzer.jvm/run-passes (analyzer-passes :local-symbols)]
                      (with-open [reader (base64-reader enclosing-sexp)]
                        (analyze start-line start-column reader)))]
          (vec (analyzer/local-symbols line column nodes)))
        ;; Ignore syntax errors in enclosing-sexp.
        (catch IllegalArgumentException _
          nil)))))
