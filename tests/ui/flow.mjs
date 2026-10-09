// A person using the dashboard, in headless Chrome (DevTools protocol over
// Node's own WebSocket; no packages), for tests/test_web.py:
//
//   node flow.mjs CONFIG.json      CONFIG: {"url": launch link, "root": folder,
//                                            "shots": folder for screenshots or null}
//
// Signs in with the link, scans `root` for duplicates, unticks a copy,
// removes the rest to the Trash, opens the completed page, undoes it, finds
// empty folders, changes a setting, and deletes a run from the history.
// Prints JSON: {steps: {what was seen at each step}, errors (exceptions,
// console errors, CSP and other browser complaints), requests (every URL
// the page asked for), overflow (pages wider than the window)}.
//
// With "shots", every page is captured light and dark, on a desktop and a
// phone: NAME-light-desktop.png, NAME-dark-phone.png, ...
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const config = JSON.parse(readFileSync(process.argv[2], "utf8"));
const origin = new URL(config.url).origin;
const profile = mkdtempSync(join(tmpdir(), "clean-chrome-"));
const chrome = spawn(process.env.CHROME || "google-chrome", [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-background-networking", "--disable-sync",
  "--disable-component-update", "--hide-scrollbars", `--user-data-dir=${profile}`,
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
const timer = setTimeout(() => { console.error("timed out at: " + where); finish(3); }, 400000);
let where = "starting Chrome";

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
const seen = [];
let sessionId = null;
ws.addEventListener("message", e => {
  const msg = JSON.parse(e.data);
  if (msg.id && waiting.has(msg.id)) {
    const [ok, bad] = waiting.get(msg.id);
    waiting.delete(msg.id);
    msg.error ? bad(new Error(JSON.stringify(msg.error))) : ok(msg.result);
    return;
  }
  if (msg.method === "Page.javascriptDialogOpening") {    // (none expected: say yes)
    seen.push({ method: "dialog", params: msg.params });
    send("Page.handleJavaScriptDialog", { accept: true }, sessionId).catch(() => {});
  }
  seen.push(Object.assign(msg, { at: where }));
});
const send = (method, params = {}, sid) => new Promise((ok, bad) => {
  const n = ++id;
  waiting.set(n, [ok, bad]);
  ws.send(JSON.stringify({ id: n, method, params, sessionId: sid }));
});
const { targetId } = await send("Target.createTarget", { url: "about:blank" });
({ sessionId } = await send("Target.attachToTarget", { targetId, flatten: true }));
const s = (m, p) => send(m, p, sessionId);
for (const domain of ["Page", "Runtime", "Log", "Network"]) await s(`${domain}.enable`);

const SIZES = { desktop: [1280, 900], phone: [390, 844] };
const view = async (device, theme) => {
  const [width, height] = SIZES[device];
  await s("Emulation.setDeviceMetricsOverride",
          { width, height, deviceScaleFactor: 1, mobile: device === "phone" });
  await s("Emulation.setEmulatedMedia",
          { features: [{ name: "prefers-color-scheme", value: theme }] });
};
await view("desktop", "light");

const sleep = ms => new Promise(r => setTimeout(r, ms));
const evaluate = async expression => {
  const r = await s("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 500));
  return r.result.value;
};
// Polls `expression` until it is truthy; the page may be between two
// documents meanwhile.
const waitFor = async (expression, what, ms = 60000) => {
  const end = Date.now() + ms;
  let last = null;
  while (Date.now() < end) {
    try {
      const v = await evaluate(expression);
      if (v) return v;
    } catch (e) { last = e; }
    await sleep(100);
  }
  const at = await evaluate("location.href + ' ' + document.title").catch(() => "?");
  throw new Error(`waited in vain for ${what} (at ${at})${last ? ": " + last.message : ""}`);
};
const settle = () => evaluate("new Promise(r => requestAnimationFrame(() => " +
                              "requestAnimationFrame(() => setTimeout(r, 50))))");
const ready = page => waitFor(
  `document.readyState === "complete" && document.body && document.body.dataset.page === ${
    JSON.stringify(page)} && location.origin === ${JSON.stringify(origin)}`, `the ${page} page`);
const go = async (path, page) => {
  where = path;
  await s("Page.navigate", { url: origin + path });
  await ready(page);
  await settle();
};
const click = sel => evaluate(`(() => {
  const el = document.querySelector(${JSON.stringify(sel)});
  if (!el) throw new Error("nothing matches " + ${JSON.stringify(sel)});
  el.click();
})()`);
const text = sel => evaluate(`(() => {
  const el = document.querySelector(${JSON.stringify(sel)});
  return el ? el.textContent.trim() : null;
})()`);
// The page's heading as a reader sees its title: the h1 without its tag
// ("remove", "undo"), or a message page's headline.
const heading = () => evaluate(`(() => {
  const h = document.querySelector("h1");
  if (!h) {
    const m = document.querySelector(".callout .headline");
    return m ? m.textContent.trim() : null;
  }
  const c = h.cloneNode(true);
  c.querySelectorAll(".tag").forEach(t => t.remove());
  return c.textContent.trim();
})()`);

const overflow = [];
const shot = async (name, { full = true } = {}) => {
  for (const theme of ["light", "dark"]) {
    for (const device of ["desktop", "phone"]) {
      await view(device, theme);
      await settle();
      const wide = await evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth");
      if (wide) overflow.push(`${name}-${theme}-${device}`);
      if (!config.shots) continue;
      const params = { format: "png" };
      if (full) {
        const m = await s("Page.getLayoutMetrics");
        const [width] = SIZES[device];
        params.captureBeyondViewport = true;
        params.clip = { x: 0, y: 0, width, height: Math.min(m.cssContentSize.height, 8000),
                        scale: 1 };
      }
      const { data } = await s("Page.captureScreenshot", params);
      writeFileSync(join(config.shots, `${name}-${theme}-${device}.png`),
                    Buffer.from(data, "base64"));
    }
  }
  await view("desktop", "light");
  await settle();
};
if (config.shots) mkdirSync(config.shots, { recursive: true });

// The confirm dialog: wait for its answer, then press its button.
const confirmWith = async (opener, shotName) => {
  await click(opener);
  await waitFor(`document.getElementById("confirm").open && !document.getElementById("dlg-go")
                 .hidden && !document.getElementById("dlg-go").disabled`, "the confirmation");
  if (shotName) await shot(shotName, { full: false });
  const title = await text("#dlg-title");
  await click("#dlg-go");
  return title;
};
// After a job starts: its page, then wherever it goes on to.
const jobThen = async page => {
  await waitFor(`location.pathname.startsWith("/job/")`, "the job page");
  const job = await evaluate("location.pathname");
  where = job;
  await ready(page);
  await settle();
  return job;
};

const steps = {};
const notFound = origin + "/no-such-page";
try {
  // 1. the link from `clean ui` signs in and opens the home page
  where = "launch";
  await s("Page.navigate", { url: config.url });
  await ready("home");

  // 2. a scan for empty folders, then for duplicates, from the wizard
  await go("/scan?kind=empty", "scan");
  await evaluate(`document.getElementById("typed-path").value = ${JSON.stringify(config.root)}`);
  await click("[data-act=add-typed]");
  await waitFor(`document.querySelectorAll("#roots li").length === 1`, "the folder added");
  await shot("scan-empty");
  await click("#start");
  await jobThen("run");
  steps.empty = { run: decodeURIComponent((await evaluate("location.pathname")).split("/")[2]) };
  await shot("run-empty");

  await go("/scan", "scan");
  await waitFor(`document.querySelectorAll("#dirs li").length > 0`, "the folder browser");
  await evaluate(`document.getElementById("typed-path").value = ${JSON.stringify(config.root)}`);
  await click("[data-act=add-typed]");
  await waitFor(`document.querySelectorAll("#roots li").length === 1`, "the folder added");
  steps.scan = { cli: await text("#scan-cli") };
  await shot("scan");
  await click("#start");
  const scanJob = await jobThen("results");
  const run = decodeURIComponent((await evaluate("location.pathname")).split("/")[2]);
  await waitFor(`document.getElementById("foot-text").textContent`, "the footer");
  steps.results = {
    run,
    remove_enabled: await evaluate(`!document.getElementById("remove-btn").disabled`),
    foot: await text("#foot-text"),
    cards: await evaluate(`document.querySelectorAll("#cards .card").length`),
  };
  await shot("results");
  await go(`/runs/${encodeURIComponent(run)}?tab=groups`, "results");
  await shot("results-groups");
  await go(`/runs/${encodeURIComponent(run)}`, "results");
  await waitFor(`document.getElementById("foot-text").textContent`, "the footer");

  // 3. untick the first copy that would go: the total changes, and stays so
  const ref = await evaluate(`document.querySelector("#cards input[data-ref]:checked")
                              .dataset.ref`);
  const foot = await text("#foot-text");
  await click(`#cards input[data-ref="${ref}"]`);
  await waitFor(`document.getElementById("foot-text").textContent.trim() !== ${
    JSON.stringify(foot)}`, "the footer to change");
  const unticked = await text("#foot-text");
  await go(`/runs/${encodeURIComponent(run)}`, "results");
  await waitFor(`document.getElementById("foot-text").textContent`, "the footer");
  steps.unticked = {
    ref,
    foot: unticked,
    saved: await evaluate(`!document.querySelector('#cards input[data-ref="${ref}"]').checked`)
           && await text("#foot-text") === unticked,
    how: await text("#foot-how"),
  };

  // 4. remove the rest to the Trash: the completed page
  await click("#remove-btn");
  await waitFor(`!document.getElementById("dlg-go").disabled`, "the removal to be checked");
  await click('#dlg-method input[value="permanent"]');
  await waitFor(`!document.getElementById("dlg-typed").hidden`, "the word to type");
  await shot("confirm-permanent", { full: false });
  await click('#dlg-method input[value="trash"]');
  await waitFor(`document.getElementById("dlg-typed").hidden &&
                 !document.getElementById("dlg-go").disabled`, "the Trash confirmation");
  await evaluate(`document.getElementById("confirm").close()`);
  const removeTitle = await confirmWith("#remove-btn", "confirm-remove");
  await jobThen("completed");
  steps.completed = {
    confirm: removeTitle,
    path: await evaluate("location.pathname"),
    title: await heading(),
    buttons: await evaluate(`[...document.querySelectorAll("main p.acts > *")]
                             .map(b => b.textContent.trim())`),
  };
  await shot("completed");
  await go("/trash", "trash");
  steps.trash = { items: await evaluate(`document.querySelectorAll("[data-action=trash_restore]")
                                        .length`) };
  await shot("trash");
  await go(steps.completed.path, "completed");

  // 5. undo it: all back
  await confirmWith('[data-action="undo"]', "confirm-undo");
  await jobThen("run");
  steps.undone = { path: await evaluate("location.pathname"), title: await heading() };
  await shot("run-undo");
  await go(scanJob, "job");                 // a finished job, visited again, stays
  await sleep(800);
  steps.job = { path: await evaluate("location.pathname"), head: await text("#job-head") };
  await shot("job");

  // 6. a setting: saved, and still there after a reload
  await go("/settings", "settings");
  const li = `li.setting[data-key="dupes.min_size"]`;
  await evaluate(`(() => { const el = document.querySelector('${li} [data-input]');
    el.value = "2K"; el.dispatchEvent(new Event("input", {bubbles: true})); })()`);
  await click(`${li} [data-act=setting-save]`);
  await waitFor(`document.querySelector('${li} .badge').textContent === "settings file"`,
                "the setting saved");
  steps.setting = { toast: await text("#toast") };
  const junk = `li.setting[data-key="junk.names"]`;          // saved as shown: unchanged
  await click(`${junk} [data-act=setting-save]`);
  await waitFor(`document.querySelector('${junk} .badge').textContent === "settings file"`,
                "the list saved");
  await go("/settings", "settings");
  steps.setting.after_reload = await evaluate(
    `document.querySelector('${li} [data-input]').value`);
  await shot("settings");

  // 7. the history: delete the empty-folders run
  await go("/history", "history");
  await shot("history");
  const empty = steps.empty.run;
  await confirmWith(`[data-action="history_delete"][data-run="${empty}"]`, "confirm-history");
  await jobThen("history");
  steps.history = {
    path: await evaluate("location.pathname"),
    deleted: empty,
    listed: await evaluate(`!!document.querySelector('a[href="/runs/${empty}"]')`),
  };

  // 8. space: the page before any look; a look from the wizard (which starts with the home
  //    folder: it is swapped for the test's, and the command checked before it starts);
  //    the run's page and one folder on its own; the Space page after; pruning the hash
  //    cache from there, which comes back to it
  await go("/space", "space");
  steps.spaceBefore = { empty: await evaluate(`!!document.querySelector(".empty-state")`) };
  await shot("space-empty");
  await go("/scan?kind=space", "scan");
  const firstRoots = await evaluate(
    `[...document.querySelectorAll("#roots .path")].map(e => e.textContent)`);
  await click("#roots [data-act=remove-root]");
  await evaluate(`document.getElementById("typed-path").value = ${JSON.stringify(config.root)}`);
  await click("[data-act=add-typed]");
  await waitFor(`document.querySelectorAll("#roots li").length === 1`, "the folder added");
  const spaceCli = await text("#scan-cli");
  if (spaceCli !== `clean space ${config.root}`) throw new Error(`not starting: ${spaceCli}`);
  await shot("scan-space");
  await click("#start");
  await jobThen("run");
  steps.space = {
    first_roots: firstRoots, cli: spaceCli, title: await heading(),
    run: decodeURIComponent((await evaluate("location.pathname")).split("/")[2]),
    rows: await evaluate(`document.querySelectorAll(".tree .node-row").length`),
    bars: await evaluate(`document.querySelectorAll(".tree meter.bar").length`),
    sections: await evaluate(`[...document.querySelectorAll("main h2")].map(h => h.firstChild.textContent.trim())`),
  };
  await shot("run-space");
  await go(`/runs/${encodeURIComponent(steps.space.run)}?dir=1`, "run");
  steps.space.folder = { crumbs: await text("p.crumbs"),
                         top: await text(".tree > details > summary .nm") };
  await shot("run-space-folder");
  await go("/space", "space");
  steps.spaceAfter = {
    tree: await evaluate(`!!document.querySelector(".tree")`),
    buttons: await evaluate(`[...document.querySelectorAll("main button")].map(b => b.textContent.trim())`),
  };
  await shot("space");
  steps.spaceAfter.prune = await confirmWith("[data-action=cache_prune]", "confirm-cache-prune");
  await jobThen("space");
  steps.spaceAfter.back = await evaluate("location.pathname");

  // 9. the rest: home with its runs, and a page that isn't there
  await go("/", "home");
  await shot("home");
  where = "not found";
  await s("Page.navigate", { url: notFound });
  await ready("message");
  steps.missing = { title: await heading() };
  await shot("not-found");
} catch (e) {
  console.error(`at ${where}: ${e.stack || e}`);
  clearTimeout(timer);
  ws.close();
  await finish(1);
}

const errors = [];
const requests = [];
for (const e of seen) {
  const p = e.params || {};
  if (e.method === "Runtime.exceptionThrown") {
    errors.push(`${e.at}: ${p.exceptionDetails.exception?.description || p.exceptionDetails.text}`);
  } else if (e.method === "Runtime.consoleAPICalled" && p.type === "error") {
    errors.push(`${e.at}: ` + p.args.map(a => a.value ?? a.description).join(" "));
  } else if (e.method === "Log.entryAdded" && ["error", "warning"].includes(p.entry.level)) {
    // (the page asked for on purpose answers 404)
    if (!(p.entry.source === "network" && p.entry.url === notFound))
      errors.push(`${e.at}: ${p.entry.text} ${p.entry.url || ""}`);
  } else if (e.method === "dialog") {
    errors.push(`${e.at}: a JavaScript dialog: ${p.message}`);
  } else if (e.method === "Network.requestWillBeSent") {
    const u = p.request.url;
    if (!u.startsWith("data:") && u !== "about:blank") requests.push(u);
  }
}
console.log(JSON.stringify({ steps, errors, requests, overflow }));
clearTimeout(timer);
ws.close();
await finish(0);
