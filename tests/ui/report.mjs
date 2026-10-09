// Drives a report page in headless Chrome (DevTools protocol over Node's own
// WebSocket; no packages) and prints what a person would see after each step,
// as JSON, for tests/test_report.py.
//
//   node report.mjs PAGE.html STEPS.json
//
// STEPS: [{"click": "css"}, {"set": "css", "value": "x"}, {"pick": "css",
//         "label": "option text"}, {"hash": "F1"}, {"reload": true} (the
//         current address) or {"reload": "#C1"}, {"size": [390, 844]}, ...].
// After the page loads and after every step:
//   {tab, shown, visible: [ids of the visible cards in the open tab, in order],
//    open: [ids of the cards inside an open <details>], toolbar}
// plus, at the end: errors (exceptions, console errors, CSP reports),
// requests (anything not file:// or data:), and overflow (page wider than
// the window, per step).
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const [page, stepsFile] = process.argv.slice(2);
const steps = JSON.parse(readFileSync(stepsFile, "utf8"));
const profile = mkdtempSync(join(tmpdir(), "clean-chrome-"));
const chrome = spawn(process.env.CHROME || "google-chrome", [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-background-networking", `--user-data-dir=${profile}`,
  "--remote-debugging-port=0", "about:blank"], { stdio: ["ignore", "ignore", "pipe"] });

async function finish(code) {
  // Chrome writes to its profile until it has exited: wait, then remove it.
  if (chrome.exitCode === null && chrome.signalCode === null) {
    const gone = new Promise(ok => chrome.once("exit", ok));
    chrome.kill();
    await Promise.race([gone, new Promise(ok => setTimeout(ok, 5000))]);
  }
  try {
    rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  } catch (e) { console.error(String(e)); }
  process.exit(code);
}
const timer = setTimeout(() => { console.error("timed out"); finish(3); }, 60000);

const wsUrl = await new Promise((ok, bad) => {
  let buf = "";
  chrome.stderr.on("data", d => {
    buf += d;
    const m = buf.match(/DevTools listening on (ws:\/\/\S+)/);
    if (m) ok(m[1]);
  });
  chrome.on("exit", () => bad(new Error("chrome exited: " + buf.slice(-500))));
}).catch(e => { console.error(String(e)); finish(3); });

const ws = new WebSocket(wsUrl);
await new Promise(ok => ws.addEventListener("open", ok));
let id = 0;
const waiting = new Map();
const events = [];
ws.addEventListener("message", e => {
  const msg = JSON.parse(e.data);
  if (msg.id && waiting.has(msg.id)) {
    const [ok, bad] = waiting.get(msg.id);
    waiting.delete(msg.id);
    msg.error ? bad(new Error(JSON.stringify(msg.error))) : ok(msg.result);
  } else {
    events.push(msg);
  }
});
const send = (method, params = {}, sessionId) => new Promise((ok, bad) => {
  const n = ++id;
  waiting.set(n, [ok, bad]);
  ws.send(JSON.stringify({ id: n, method, params, sessionId }));
});
const { targetId } = await send("Target.createTarget", { url: "about:blank" });
const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
const s = (m, p) => send(m, p, sessionId);
for (const domain of ["Page", "Runtime", "Log", "Network"]) await s(`${domain}.enable`);
await s("Emulation.setDeviceMetricsOverride",
        { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });

const evaluate = async expression => {
  const r = await s("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
  return r.result.value;
};
const settle = () => evaluate("new Promise(r => requestAnimationFrame(() => setTimeout(r, 0)))");

const STATE = `(() => {
  const vis = el => !el.closest("[hidden]");
  const tab = document.querySelector("nav.tabs a.on");
  const section = tab ? document.getElementById(tab.hash.slice(1)) : document;
  const cards = [...section.querySelectorAll(".card")];
  const bar = document.getElementById("toolbar");
  const shown = document.getElementById("shown");
  return {
    tab: tab ? tab.hash.slice(1) : null,
    shown: shown ? shown.textContent : null,
    toolbar: bar ? !bar.hidden : null,
    visible: cards.filter(c => vis(c) && (!c.closest("details") || c.closest("details").open))
                  .map(c => c.id),
    open: [...document.querySelectorAll("details[open] .card")].map(c => c.id),
    overflow: document.documentElement.scrollWidth > innerWidth,
  };
})()`;

const states = [];
const loaded = async () => {
  for (let i = 0; i < 400 && !events.some(e => e.method === "Page.loadEventFired"); i++)
    await new Promise(r => setTimeout(r, 25));
  await settle();
};
const navigate = async url => {
  // Through about:blank, so that only a new hash still loads the page anew.
  await s("Page.navigate", { url: "about:blank" });
  await loaded();
  events.length = 0;
  await s("Page.navigate", { url });
  await loaded();
};
const seen = [];
const collect = () => { seen.push(...events.splice(0)); };

await navigate(pathToFileURL(page).href);
collect();
states.push(await evaluate(STATE));
for (const step of steps) {
  if (step.click) {
    await evaluate(`document.querySelector(${JSON.stringify(step.click)}).click()`);
  } else if (step.set) {
    await evaluate(`(() => { const el = document.querySelector(${JSON.stringify(step.set)});
      el.value = ${JSON.stringify(step.value)};
      el.dispatchEvent(new Event(el.tagName === "INPUT" ? "input" : "change", {bubbles: true}));
    })()`);
  } else if (step.pick) {
    await evaluate(`(() => { const el = document.querySelector(${JSON.stringify(step.pick)});
      const o = [...el.options].find(o => o.textContent.trim() === ${JSON.stringify(step.label)});
      if (!o) throw new Error("no option " + ${JSON.stringify(step.label)});
      el.value = o.value;
      el.dispatchEvent(new Event("change", {bubbles: true}));
    })()`);
  } else if (step.hash) {
    await evaluate(`location.hash = ${JSON.stringify(step.hash)}`);
  } else if (step.reload) {
    if (step.reload === true) {
      collect();
      await s("Page.reload", { ignoreCache: true });
      await loaded();
    } else {
      collect();
      await navigate(pathToFileURL(page).href + step.reload);
    }
  } else if (step.size) {
    const [w, h] = step.size;
    await s("Emulation.setDeviceMetricsOverride",
            { width: w, height: h, deviceScaleFactor: 1, mobile: w < 600 });
  }
  await settle();
  collect();
  states.push(await evaluate(STATE));
}
collect();

const errors = [];
const requests = [];
for (const e of seen) {
  if (e.method === "Runtime.exceptionThrown") {
    errors.push(e.params.exceptionDetails.exception?.description || e.params.exceptionDetails.text);
  } else if (e.method === "Runtime.consoleAPICalled" && e.params.type === "error") {
    errors.push(e.params.args.map(a => a.value ?? a.description).join(" "));
  } else if (e.method === "Log.entryAdded" && ["error", "warning"].includes(e.params.entry.level)) {
    errors.push(e.params.entry.text);
  } else if (e.method === "Network.requestWillBeSent") {
    const u = e.params.request.url;
    if (!u.startsWith("file://") && !u.startsWith("data:")) requests.push(u);
  }
}
console.log(JSON.stringify({ states, errors, requests }));
clearTimeout(timer);
ws.close();
await finish(0);
