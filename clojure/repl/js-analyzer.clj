(ns repl.js-analyzer
  (:require
   [clojure.java.io :as io]
   [clojure.set :as set]
   [clojure.spec.alpha :as spec]
   [cljs.env :as env]
   [cognitect.transcriptor :as xr]
   [shadow.cljs.devtools.server :as server]
   [shadow.cljs.devtools.api :as shadow]
   [tutkain.analyzer :as analyzer]
   [tutkain.analyzer.js :as js]
   [tutkain.backchannel.test :refer [send-op string->base64 string->reader]]
   [tutkain.cljs :as cljs]))

(spec/def ::op
  #{:const :map :def :var})

(spec/def ::type
  #{:number :map :keyword :string})

(spec/def ::form
  any?)

(spec/def ::line nat-int?)
(spec/def ::end-line nat-int?)
(spec/def ::column nat-int?)
(spec/def ::end-column nat-int?)

(spec/def ::env
  (spec/keys :opt-un [::line ::end-line ::column ::end-column]))

(spec/def ::node
  (spec/keys
    :req-un [::op]
    :opt-un [::type ::form ::env]))

(spec/def ::position
  (spec/keys :req-un [::line
                      ::form
                      ::column
                      ::end-column]))

(def build-id :browser)

;; Start shadow-cljs watch

(xr/on-exit #(server/stop!))
(do (server/start!) (shadow/watch build-id))
(defn env [] (cljs/compiler-env build-id))

;; Touch the file to ensure the keywords in that file are in the compiler
;; environment. Not sure why I need to do this.
(run!
  #(-> (io/file %) (.setLastModified (System/currentTimeMillis)))
  ["dev/src/my/other.cljs" "dev/src/my/app.cljs"])
(Thread/sleep 3000)

;; Analyze multiple forms
(env/with-compiler-env (env)
  (seq (js/analyze 0 0 (string->reader "1 2"))))

(xr/check! (spec/coll-of ::node :min-count 2))

;; Line and column number
(def nodes
  (env/with-compiler-env (env)
    (set (js/analyze 1 2 (string->reader "(def x 1)")))))

(xr/check! (spec/coll-of ::node) nodes)
(xr/check! (partial set/subset? #{{:line 1 :column 2}})
  (set/project (map :env nodes) [:line :column]))

(->
  {:op :locals
   :dialect :cljs
   :build-id build-id
   :file "/path/to/my.cljs"
   :ns "cljs.user"
   :context (string->base64 "(defn f [x] (inc x))")
   :form "x"
   :start-line 0
   :start-column 0
   :line 0
   :column 9
   :end-column 10}
  send-op
  :positions
  set)

(xr/check!
  (partial set/subset? #{{:form 'x :line 0 :column 9 :end-column 10}
                         {:form 'x :line 0 :column 17 :end-column 18}}))

(->
  {:op :locals
   :dialect :cljs
   :build-id build-id
   :file "/path/to/my.cljs"
   :ns "cljs.user"
   ;; newline doesn't mess up :end-column
   :context (string->base64 "(defn f [x] (doto x\n  inc))")
   :form "x"
   :start-line 0
   :start-column 0
   :line 0
   :column 18
   :end-column 19}
  send-op
  :positions
  set)

(xr/check!
  (partial set/subset? #{{:form 'x :line 0 :column 9 :end-column 10}
                         {:form 'x :line 0 :column 18 :end-column 19}}))

(into #{}
  (mapcat
    (fn [[form [column end-column]]]
      (->
        {:op :locals
         :dialect :cljs
         :build-id build-id
         :file "/path/to/my.cljs"
         :ns "cljs.user"
         :context (string->base64 "(defn f [{:keys [x y z]}] (+ x y z))")
         :form form
         :start-line 0
         :start-column 0
         :line 0
         :column column
         :end-column end-column}
        send-op
        :positions)))
  {"x" [17 18] "y" [19 20] "z" [21 22]})

(xr/check!
  (partial set/subset?
    #{{:line 0 :column 17 :form 'x :end-column 18}
      {:line 0 :column 29 :form 'x :end-column 30}
      {:line 0 :column 19 :form 'y :end-column 20}
      {:line 0 :column 31 :form 'y :end-column 32}
      {:line 0 :column 21 :form 'z :end-column 22}
      {:line 0 :column 33 :form 'z :end-column 34}}))

(env/with-compiler-env (env)
  (analyzer/index-by-position
    (js/analyze 0 0
      (string->reader "(defn f [x] (+ x (let [x 2] (inc x)) x))"))))

(xr/check!
  (spec/every-kv ::position simple-symbol? :min-count 5 :max-count 5))

(->
  {:op :locals
   :dialect :cljs
   :build-id build-id
   :file "/path/to/my.cljs"
   :ns "cljs.user"
   :context (string->base64 "(defn foo [{:keys [bar/baz]}] baz)")
   :form "baz"
   :start-line 0
   :start-column 0
   :line 0
   :column 30
   :end-column 33}
  send-op
  :positions
  set)

;; TODO
#_(xr/check!
  (partial set/subset?
    #{{:line 0 :column 19 :form 'baz :end-column 26}
      {:line 0 :column 30 :form 'baz :end-column 33}}))

(->
  {:op :locals
   :dialect :cljs
   :build-id build-id
   :file "/path/to/my.cljs"
   :ns "cljs.user"
   :context (string->base64 "(defn foo [{:keys [bar/baz]}] baz)")
   :form "bar/baz"
   :start-line 0
   :start-column 0
   :line 0
   :column 19
   :end-column 26}
  send-op
  :positions
  set)

;; TODO
#_(xr/check!
  (partial set/subset?
    #{{:line 0 :column 19 :form 'baz :end-column 26}
      {:line 0 :column 30 :form 'baz :end-column 33}}))

(->
  {:op :locals
   :dialect :cljs
   :build-id build-id
   :file "/path/to/my.cljs"
   :ns "cljs.user"
   :context (string->base64 "(defn f [x] (inc x))")
   :form 'x
   :start-line 0
   :start-column 0
   :line 0
   :column 9
   :end-column 10}
  send-op
  :positions)

(xr/check! (spec/coll-of ::position :min-count 2))

;; *ns* is bound for read
(->
  {:op :locals
   :dialect :cljs
   :build-id build-id
   :file "/path/to/my.cljs"
   :ns "my.browser.app"
   :context (string->base64 "(defn f [x] (::other/keyword x))")
   :form 'x
   :start-line 0
   :start-column 0
   :line 0
   :column 9
   :end-column 10}
  send-op
  :positions)

(xr/check!
  (partial set/subset?
    #{{:line 0 :column 9 :form 'x :end-column 10}
      {:line 0 :column 29 :form 'x :end-column 30}}))
