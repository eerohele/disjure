(ns tutkain.java
  (:require
   [clojure.java.io :as io]
   [clojure.main :as main]
   [clojure.repl :as repl]
   [tutkain.rpc :refer [handle respond-to]])
  (:import
   (java.io File)
   (java.net URL)))

(def source-zip
  (constantly
    (->>
      [(io/file (System/getProperty "tutkain.java.src.zip"))
       (io/file (System/getProperty "java.home") "lib/src.zip")
       (io/file (System/getProperty "java.home") "src.zip")]
      (drop-while #(or (nil? %) (not (.exists ^File %))))
      (first))))

(defn class-url->source-url
  "Given the URL of an artifact in the classpath providing Java classes, return
  the URL of the JAR that contains the source code of those classes.

  If the URL has the \"jrt\" protocol, attempt a heuristic to figure out the
  path to the ZIP file that contains the JDK source files. Otherwise, assumes
  that the source JAR has been generated by the Maven Source plugin.

  (TODO: Read actual path from artifact POM file?)

  See https://maven.apache.org/plugins/maven-source-plugin/examples/configureplugin.html."
  [^URL url]
  (when (some? url)
    (let [new-url (if (= "jrt" (.getProtocol url))
                    (str "jar:file:" (source-zip) "!" (.getFile url))
                    (->
                      (.toString url)
                       ;; Strip nested class part from filename
                      (.replaceAll "\\$.+?\\." "\\.")
                      (.replace ".jar!" "-sources.jar!")))]
      (URL. (.replace new-url ".class" ".java")))))

(defn qualified-class-name
  [^Class class]
  (let [class-name (.getSimpleName class)]
    (if-some [package-name (some-> class .getPackage .getName)]
      (str package-name "." class-name)
      class-name)))

(defn resolve-class
  "Given an ns symbol and a symbol, if the symbol resolves to a class in the
  context of the given namespace, return that class (java.lang.Class)."
  ^Class [ns sym]
  (try (let [val (ns-resolve ns sym)]
         (when (class? val) val))
    (catch Exception e
      (when (not= ClassNotFoundException
              ;; FIXME
              (class #?(:bb e :clj (main/repl-exception e))))
        (throw e)))))

(comment (resolve-class 'clojure.core 'String) ,)

(defn resolve-stacktrace
  "Given a java.lang.Throwable, return a seq of maps representing its resolved
  stack trace.

  A \"resolved\" stack trace is one where each element of the stack trace has
  an absolute path to the source file of the stack trace element.

  For Java sources, presumes that the Java source files are next to the main
  artifact in the local Maven repository."
  [^Throwable ex]
  (when (instance? Throwable ex)
    (let [cl (.getContextClassLoader (Thread/currentThread))]
      (map (fn [^StackTraceElement el]
             (let [class-name (.getClassName el)
                   file-name (.getFileName el)
                   java? (.endsWith file-name ".java")
                   url (if java?
                         (class-url->source-url (.getResource cl (str (.replace class-name "." "/") ".class")))
                         (or
                           ;; TODO: This appears to work, but is it right?
                           (.getResource (Class/forName class-name) file-name)
                           (.getResource cl file-name)))]
               (cond->
                 {:file-name file-name
                  :column 1
                  :name (if java?
                          (str (.getClassName el) "/" (.getMethodName el))
                          (repl/demunge class-name))
                  :native? (.isNativeMethod el)
                  :line (.getLineNumber el)}
                 url (assoc :file (str url)))))
        (.getStackTrace ex)))))

(defmethod handle :resolve-stacktrace
  [{:keys [thread-bindings] :as message}]
  (respond-to message {:stacktrace (resolve-stacktrace (get @thread-bindings #'*e))}))
