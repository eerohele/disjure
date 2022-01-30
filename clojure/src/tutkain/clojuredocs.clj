(ns tutkain.clojuredocs
  (:require [clojure.edn :as edn]
            [clojure.java.io :as io]
            [tutkain.backchannel :refer [handle respond-to]]
            [tutkain.format :refer [pp-str]])
  (:import (java.io PushbackReader)))

(defmethod handle :examples
  [{:keys [source-path ns sym] :as message}]
  (respond-to message
    (if-some [qualified-symbol (some-> (ns-resolve ns sym) symbol)]
      (with-open [reader (PushbackReader. (io/reader source-path))]
        {:symbol qualified-symbol
         :examples (-> reader edn/read qualified-symbol)})
      {:symbol sym})))
