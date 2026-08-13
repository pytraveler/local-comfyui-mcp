// Exercise the console capture in mcp_bridge.js without a browser.
//
// The rest of the bridge JS only makes sense against a live litegraph graph, but the
// capture section deliberately touches nothing except `console`, `window` and
// `performance` - so it can be lifted out and run against stubs, which is the only
// automated coverage the browser half has. Worth having: the shape-not-instanceof
// check below was written because `instanceof Error` silently lost a real error to
// `JSON.stringify`, which renders one as `{}`.
//
//   node scripts/check_console.mjs
//
// Exit codes: 0 passed, 1 an expectation failed, 2 the section could not be found,
// 3 there is no source in this checkout - see below. `tests/test_console_capture.py`
// runs this too, and skips when node is not on PATH or when this exits 3.

import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

// src/, never web/: web/ holds the minified build, and the section below is found
// by a comment marker, which minification deletes.
const here = dirname(fileURLToPath(import.meta.url));
const source = join(here, "..", "comfy_node", "comfyui_mcp_bridge", "src", "mcp_bridge.js");

// A release ships the built bundle alone, without src/ or the build scripts. There
// is then nothing here to check, and that is not a fault: a red line meaning "you
// installed the release rather than the development tree" is worse than no line,
// which is the same reason node's absence is a skip rather than a failure.
if (!existsSync(source)) {
  console.log(`no ${source}; this is a built checkout, nothing to check here`);
  process.exit(3);
}

const src = readFileSync(source, "utf8");
const OPEN = "// --- the browser console";
const CLOSE = "captureConsole();";
const start = src.indexOf(OPEN);
const end = src.indexOf(CLOSE) + CLOSE.length;
if (start < 0 || end < start) {
  console.error(`could not find the capture section (${OPEN} ... ${CLOSE}) in ${source}`);
  process.exit(2);
}

const results = [];
const passthrough = [];
const listeners = {};

const sandbox = {
  window: { addEventListener: (name, fn) => (listeners[name] = fn) },
  performance: { now: () => 1234.6 },
  console: Object.fromEntries(
    ["log", "warn", "error", "info", "debug"].map((name) => [
      name,
      (...args) => passthrough.push([name, args]),
    ]),
  ),
};
vm.createContext(sandbox);
vm.runInContext(`${src.slice(start, end)}\nglobalThis.__captured = consoleLog;`, sandbox);

const buf = sandbox.__captured;
const { console: patched } = sandbox;
const ok = (name, condition) => results.push([Boolean(condition), name]);

// --- what gets captured, and what still reaches devtools ---
patched.warn("plain warning");
ok("a warning is captured", buf.all.at(-1).text === "plain warning");
ok("a warning also lands in the problems ring", buf.problems.at(-1).text === "plain warning");
ok("and it still reached the real console", passthrough.at(-1)[0] === "warn");

patched.log("a log");
ok("console.log is captured as INFO", buf.all.at(-1).level === "INFO");
ok("but an ordinary log is not a problem", buf.problems.at(-1).text === "plain warning");

// --- arguments of every awkward shape ---
patched.error("Error loading extension", "/extensions/foo/bar.js", new Error("boom"));
const joined = buf.all.at(-1).text;
ok("an Error from another realm renders as its stack", joined.includes("Error: boom") && joined.includes("at "));
ok("several arguments are joined", joined.startsWith("Error loading extension /extensions/foo/bar.js"));

patched.error({ name: "TypeError", message: "not a function", stack: "TypeError: not a function\n    at x" });
ok("an error-shaped object is not flattened to {}", buf.all.at(-1).text.includes("not a function"));

// --- console's format specifiers, which the frontend leans on heavily ---
patched.warn("%c[DEPRECATED]%c Monkey-patching is deprecated.", "color: orange", "color: inherit");
ok("%c consumes its style argument", buf.all.at(-1).text === "[DEPRECATED] Monkey-patching is deprecated.");

patched.log("%s took %dms", "loading", 42.7);
ok("%s and %d substitute", buf.all.at(-1).text === "loading took 42ms");

patched.log("100% done");
ok("a bare percent is left alone", buf.all.at(-1).text === "100% done");

patched.log("%c one", "css", "extra");
ok("arguments past the specifiers are kept", buf.all.at(-1).text.endsWith("one extra"));

patched.log("%s and %s", "only one");
ok("a specifier with no argument is left as-is", buf.all.at(-1).text === "only one and %s");

const circular = { name: "loop" };
circular.self = circular;
patched.error(circular);
ok("a circular object does not throw", buf.all.at(-1).text.includes("<circular>"));

patched.log({ a: 1 }, undefined, null, 42, true, Symbol("s"), function named() {});
ok("odd argument types survive", buf.all.at(-1).text.includes("<function named>"));

patched.log("x".repeat(5000));
ok("a huge argument is clipped", buf.all.at(-1).text.length < 1200);

// --- the reason there are two rings ---
const before = buf.all.length;
for (let i = 0; i < 500; i += 1) patched.log(`spam ${i}`);
ok("the everything-ring is capped", buf.all.length === 400);
ok("eviction is counted", buf.dropped === before + 500 - 400);
ok(
  "spam cannot evict a problem",
  buf.problems.some((entry) => entry.text.includes("Error loading extension")),
);

// --- what console never sees ---
listeners.error?.({ error: new Error("uncaught!"), message: "x", filename: "f.js", lineno: 1 });
ok("an uncaught error is recorded and tagged", buf.all.at(-1).source === "uncaught");
listeners.unhandledrejection?.({ reason: new Error("rejected!") });
ok("an unhandled rejection is recorded and tagged", buf.all.at(-1).source === "unhandled-rejection");

ok("the blind window is measured", buf.blindMs === 1235);
ok("the start of the record is stamped", typeof buf.since === "string" && buf.since.includes("T"));

for (const [good, name] of results) console.log(`${good ? "ok  " : "FAIL"} ${name}`);
const failed = results.filter(([good]) => !good);
console.log(failed.length ? `\n${failed.length} of ${results.length} failed` : `\nall ${results.length} passed`);
process.exit(failed.length ? 1 : 0);
