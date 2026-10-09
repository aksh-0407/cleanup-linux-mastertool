// The dashboard's script: one file for every page, no libraries, no network
// beyond this server. Pages are made on the server (every name escaped
// there); this script asks the JSON API (/api/...) for changes and puts the
// server's HTML or plain text (always as textContent) in place.
"use strict";

(function () {
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const page = document.body.dataset.page;
  let init = {};
  const initEl = document.getElementById("init");
  if (initEl) {
    try { init = JSON.parse(initEl.textContent); } catch (e) { init = {}; }
  }

  class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }

  // GET without a body, POST (JSON, with the header the server requires) with one.
  async function api(path, body) {
    const opts = { credentials: "same-origin", headers: { "Accept": "application/json" } };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-Clean"] = "1";
      opts.body = JSON.stringify(body);
    }
    let r;
    try {
      r = await fetch(path, opts);
    } catch (e) {
      throw new ApiError(0, "The dashboard is not answering: is `clean ui` still running?");
    }
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok) {
      throw new ApiError(r.status, (data && data.error) || `${r.status} ${r.statusText}`);
    }
    return data;
  }

  const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

  // ------------------------------------------------------------ small things

  let toastTimer = null;
  function toast(text, bad) {
    const t = $("#toast");
    if (!t) return;
    t.textContent = text;
    t.classList.toggle("bad", !!bad);
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 8000 : 4000);
  }

  async function copyFrom(el) {
    const text = el.textContent;
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const range = document.createRange();
      range.selectNodeContents(el);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (e2) { ok = false; }
      if (!ok) { toast("Select the command and copy it."); return; }
    }
    toast("Copied.");
  }

  // A command line as a shell reads it (POSIX quoting).
  function quote(arg) {
    if (arg !== "" && /^[A-Za-z0-9_\-+=.,/:@%^]+$/.test(arg)) return arg;
    return "'" + arg.replace(/'/g, "'\\''") + "'";
  }
  const commandLine = (argv) => "clean " + argv.map(quote).join(" ");

  function plural(n, one, many) {
    return `${n.toLocaleString("en-US")} ${n === 1 ? one : (many || one + "s")}`;
  }

  // Python's selection.ext_of: "jpg" for "IMG_1.JPG", "" for "README" or ".bashrc".
  function extOf(name) {
    const i = name.lastIndexOf(".");
    return i > 0 ? name.slice(i + 1).toLowerCase() : "";
  }

  // ------------------------------------------------------------ the confirm dialog
  //
  // Every action that removes, restores or deletes goes: /api/confirm says
  // exactly what would happen (server HTML) and gives a one-time
  // confirmation; the button sends it to /api/do, which starts the job.

  const dlg = $("#confirm");
  const dlgGo = $("#dlg-go");
  const dlgWord = $("#dlg-word");
  let pending = null;           // {body, nonce, typed, ticket}

  function dlgError(text) {
    const e = $("#dlg-error");
    e.textContent = text || "";
    e.hidden = !text;
  }

  function wordOk() {
    return !pending || !pending.typed
      || dlgWord.value.trim().toLowerCase() === pending.typed;
  }

  async function openConfirm(body) {
    if (!dlg) return;
    pending = { body: body, nonce: null, typed: null, ticket: null };
    const method = $("#dlg-method");
    method.hidden = body.action !== "remove";
    for (const r of $$("input[name=method]", method)) r.checked = r.value === body.method;
    if (!dlg.open) dlg.showModal();
    await loadConfirm();
  }

  async function loadConfirm(errorText) {
    const ticket = {};
    pending.ticket = ticket;
    pending.nonce = null;
    $("#dlg-title").textContent = "Checking...";
    $("#dlg-html").textContent = "";
    $("#dlg-typed").hidden = true;
    $("#dlg-clibox").hidden = true;
    dlgGo.hidden = false;
    dlgGo.disabled = true;
    dlgGo.textContent = "OK";
    $("#dlg-cancel").textContent = "Cancel";
    dlgError(null);
    let c;
    try {
      c = await api("/api/confirm", pending.body);
    } catch (e) {
      if (pending.ticket !== ticket) return;
      $("#dlg-title").textContent = "Can't do this";
      dlgGo.hidden = true;
      $("#dlg-cancel").textContent = "Close";
      dlgError(e.message);
      return;
    }
    if (pending.ticket !== ticket) return;
    $("#dlg-title").textContent = c.title;
    $("#dlg-html").innerHTML = c.html || "";    // made on the server; every name escaped there
    if (c.refused && !c.html) {
      const p = document.createElement("p");
      p.textContent = c.refused;
      $("#dlg-html").append(p);
    }
    if (c.refused) {
      dlgGo.hidden = true;
      $("#dlg-cancel").textContent = "Close";
      $("#dlg-cancel").focus();
      return;
    }
    pending.nonce = c.nonce;
    pending.typed = c.typed || null;
    dlgGo.textContent = c.button;
    dlgGo.classList.toggle("danger", !!c.danger);
    $("#dlg-typed").hidden = !c.typed;
    dlgWord.value = "";
    $("#dlg-cli").textContent = c.cli || "";
    $("#dlg-clibox").hidden = !c.cli;
    dlgGo.disabled = !wordOk();
    if (errorText) dlgError(errorText);
    (c.typed ? dlgWord : dlgGo).focus();
  }

  async function confirmed() {
    if (!pending || !pending.nonce || !wordOk()) return;
    dlgGo.disabled = true;
    dlgError(null);
    try {
      const r = await api("/api/do", { nonce: pending.nonce, typed: dlgWord.value });
      openJob(r);
    } catch (e) {
      if (e.status === 403 && pending.typed && !wordOk()) {
        dlgError(e.message);            // the confirmation is still good: type it again
        dlgGo.disabled = !wordOk();
        return;
      }
      await loadConfirm(e.message);     // used up: show the error with a fresh one
    }
  }

  if (dlg) {
    $("#dlg-form").addEventListener("submit", (ev) => { ev.preventDefault(); confirmed(); });
    dlgWord.addEventListener("input", () => { dlgGo.disabled = !pending || !pending.nonce || !wordOk(); });
    for (const r of $$("#dlg-method input[name=method]")) {
      r.addEventListener("change", () => {
        if (!r.checked || !pending) return;
        pending.body = Object.assign({}, pending.body, { method: r.value });
        loadConfirm();
      });
    }
    dlg.addEventListener("close", () => { pending = null; });
  }

  // ------------------------------------------------------------ previews

  async function previewError(url) {
    // A media element can't say why it failed: ask for one byte to find out.
    try {
      const r = await fetch(url, { headers: { Range: "bytes=0-0" }, credentials: "same-origin" });
      if (r.ok) return null;
      const data = await r.json().catch(() => null);
      return (data && data.error) || `${r.status} ${r.statusText}`;
    } catch (e) {
      return "The dashboard is not answering.";
    }
  }

  function message(box, text) {
    const p = document.createElement("p");
    p.textContent = text;
    box.replaceChildren(p);
  }

  async function preview(btn) {
    const box = $("#pv-body");
    const name = btn.dataset.name || "";
    const kind = (init.preview || {})[extOf(name)];
    const url = `/preview/${encodeURIComponent(init.run)}/${encodeURIComponent(btn.dataset.file)}`;
    $("#pv-title").textContent = name;
    message(box, "Loading...");
    $("#preview").showModal();
    if (kind === "text") {
      try {
        const r = await fetch(url, { headers: { Range: "bytes=0-65535" }, credentials: "same-origin" });
        if (!r.ok) {
          const data = await r.json().catch(() => null);
          message(box, (data && data.error) || `${r.status} ${r.statusText}`);
          return;
        }
        const text = new TextDecoder("utf-8").decode(await r.arrayBuffer());
        const pre = document.createElement("pre");
        pre.textContent = text + (r.status === 206 ? "\n\n... (the first 64 KB)" : "");
        box.replaceChildren(pre);
      } catch (e) {
        message(box, "The dashboard is not answering.");
      }
      return;
    }
    const problem = await previewError(url);
    if (problem) { message(box, problem); return; }
    if (kind === "pdf") {
      const p = document.createElement("p");
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = "Open the PDF in a new tab";
      p.append(a);
      box.replaceChildren(p);
      return;
    }
    let el;
    if (kind === "image") {
      el = new Image();
      el.alt = name;
    } else if (kind === "audio" || kind === "video") {
      el = document.createElement(kind);
      el.controls = true;
      el.preload = "metadata";
    } else {
      message(box, "This kind of file has no preview.");
      return;
    }
    el.addEventListener("error", () => message(box, "The browser can't show this file."));
    el.src = url;
    box.replaceChildren(el);
  }

  const pv = $("#preview");
  if (pv) {
    pv.addEventListener("close", () => {
      for (const m of $$("audio, video", pv)) m.pause();
      $("#pv-body").replaceChildren();
    });
  }

  // ------------------------------------------------------------ results: choosing

  const R = { q: Object.assign({}, init.q || {}), result: init.result || null, queue: Promise.resolve() };

  function cleanQ(q) {
    const out = {};
    for (const [k, v] of Object.entries(q)) {
      if (v === undefined || v === null || v === "") continue;
      if (k === "page" && String(v) === "1") continue;
      if (k === "sort" && v === "space") continue;
      if (k === "size" && String(v) === "0") continue;
      if (k === "tab" && v === "families") continue;
      out[k] = String(v);
    }
    return out;
  }

  function renderFoot(f) {
    const text = $("#foot-text");
    if (!text || !f) return;
    text.textContent = f.text;
    const how = [f.how ? `(${f.how})` : ""];
    if (f.problem) how.push(f.problem);
    $("#foot-how").textContent = how.filter(Boolean).join(" ");
    $("#remove-btn").disabled = !f.items;
  }

  function renderPager(r) {
    const nav = $("#pager");
    if (!nav) return;
    nav.replaceChildren();
    if (R.q.card) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "small";
      b.dataset.act = "show-all";
      b.textContent = "Show all results";
      nav.append(b);
      return;
    }
    if (r.pages <= 1) return;
    const add = (n, label) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "small" + (n === r.page ? " on" : "");
      b.dataset.act = "page";
      b.dataset.page = String(n);
      b.textContent = label || String(n);
      if (n === r.page) b.setAttribute("aria-current", "page");
      nav.append(b);
    };
    if (r.page > 1) add(r.page - 1, "← Previous");
    const shown = new Set([1, r.pages, r.page - 2, r.page - 1, r.page, r.page + 1, r.page + 2]);
    let last = 0;
    for (let n = 1; n <= r.pages; n++) {
      if (!shown.has(n)) continue;
      if (n > last + 1) {
        const s = document.createElement("span");
        s.className = "muted";
        s.textContent = "…";
        nav.append(s);
      }
      add(n);
      last = n;
    }
    if (r.page < r.pages) add(r.page + 1, "Next →");
  }

  function renderResults(r, keepFocus) {
    R.result = r;
    R.q.tab = r.tab;
    const cards = $("#cards");
    const focused = keepFocus && document.activeElement && document.activeElement.dataset
      ? document.activeElement.dataset.ref : null;
    cards.innerHTML = r.html;           // made on the server; every name escaped there
    for (const a of $$("#tabs a[data-tab]")) {
      a.classList.toggle("on", a.dataset.tab === r.tab);
      const c = $(".count", a);
      if (c && r.counts) c.textContent = r.counts[a.dataset.tab].toLocaleString("en-US");
    }
    const fb = $("#folded-box");
    if (fb) fb.hidden = r.tab !== "groups" || !r.folded;
    const fn = $("#folded-n");
    if (fn) fn.textContent = (r.folded || 0).toLocaleString("en-US");
    const shown = $("#shown");
    if (shown) {
      shown.textContent = r.shown === r.total ? plural(r.total, "result")
        : `${r.shown.toLocaleString("en-US")} of ${plural(r.total, "result")}`;
    }
    renderFoot(r.footer);
    renderPager(r);
    if (r.cli) $("#cli").textContent = r.cli;
    if (r.message) toast(r.message);
    if (focused) {
      const again = $(`input[data-ref="${CSS.escape(focused)}"]`, cards);
      if (again) again.focus({ preventScroll: true });
    }
  }

  async function loadResults(scrollTop) {
    const cards = $("#cards");
    cards.classList.add("loading");
    const q = cleanQ(R.q);
    const qs = new URLSearchParams(q).toString();
    try {
      const r = await api(`/api/results/${encodeURIComponent(init.run)}${qs ? "?" + qs : ""}`);
      renderResults(r, false);
      history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
      if (scrollTop) $("#tabs").scrollIntoView({ block: "start" });
    } catch (e) {
      toast(e.message, true);
    } finally {
      cards.classList.remove("loading");
    }
  }

  // Changes go one after another, each answered with the cards as they are now.
  function choose(body, keepFocus) {
    R.queue = R.queue.then(async () => {
      body.q = cleanQ(R.q);
      try {
        const r = await api(`/api/selection/${encodeURIComponent(init.run)}`, body);
        renderResults(r, keepFocus);
      } catch (e) {
        toast(e.message, true);
        await loadResults(false);         // show the boxes as they really are
      }
    });
    return R.queue;
  }

  const RANK = { newest: ["newest", "first-root", "shortest"], oldest: ["oldest", "first-root", "shortest"] };

  function auto(how) {
    const menu = $("#auto");
    if (menu) menu.open = false;
    const f = (R.result && R.result.footer) || {};
    if (how === "suggested") {
      if (f.chosen && !window.confirm(`Forget your ${plural(f.chosen, "choice")} and go back to the suggestions?`)) return;
      return choose({ op: "reset" });
    }
    if (how === "newest" || how === "oldest") {
      return choose({ op: "rules", values: { base: "rules", rank: RANK[how], rank_folders: true } });
    }
    if (how === "one") return choose({ op: "rules", values: { base: "rules", per_group: "one" } });
    if (how === "merge") return choose({ op: "rules", values: { base: "rules", merge: true } });
    if (how === "exts") {
      const text = window.prompt("Remove only copies with these extensions (comma-separated, e.g. jpg,png). Leave empty for every extension.", "");
      if (text === null) return;
      const exts = text.split(/[\s,]+/).map((x) => x.replace(/^\./, "").toLowerCase()).filter(Boolean);
      return choose({ op: "rules", values: { base: "rules", exts: exts } });
    }
    if (how === "invert") return choose({ op: "invert" });
    if (how === "none") return choose({ op: "none" });
  }

  function setupResults() {
    renderFoot(R.result && R.result.footer);
    if (R.result) renderPager(R.result);
    if (R.result) {
      const shown = $("#shown");
      const r = R.result;
      shown.textContent = r.shown === r.total ? plural(r.total, "result")
        : `${r.shown.toLocaleString("en-US")} of ${plural(r.total, "result")}`;
      const fb = $("#folded-box");
      if (fb) fb.hidden = r.tab !== "groups" || !r.folded;
    }
    if (init.loc) {
      const loc = $("#filters select[name=loc]");
      if (loc) loc.value = init.loc;
    }
    const form = $("#filters");
    let timer = null;
    const changed = (el) => {
      const name = el.name;
      if (!name) return;
      R.q[name] = el.type === "checkbox" ? (el.checked ? el.value : "") : el.value;
      delete R.q.page;
      delete R.q.card;
      loadResults(false);
    };
    form.addEventListener("submit", (ev) => ev.preventDefault());
    form.addEventListener("change", (ev) => {
      if (ev.target.name === "q") return;
      changed(ev.target);
    });
    form.addEventListener("input", (ev) => {
      if (ev.target.name !== "q") return;
      clearTimeout(timer);
      timer = setTimeout(() => changed(ev.target), 300);
    });
    for (const a of $$("#tabs a[data-tab]")) {
      a.addEventListener("click", (ev) => {
        ev.preventDefault();
        R.q.tab = a.dataset.tab;
        delete R.q.page;
        delete R.q.card;
        loadResults(false);
      });
    }
    $("#cards").addEventListener("change", (ev) => {
      const box = ev.target;
      if (!box.matches("input[type=checkbox][data-ref]")) return;
      choose({ op: "set", ref: box.dataset.ref, value: box.checked ? "remove" : "keep" }, true);
    });
    document.addEventListener("click", (ev) => {
      const menu = $("#auto");
      if (menu && menu.open && !menu.contains(ev.target)) menu.open = false;
    });
  }

  // ------------------------------------------------------------ the scan wizard

  const W = { roots: [], at: null, hidden: false };

  function renderRoots() {
    const ul = $("#roots");
    ul.replaceChildren();
    W.roots.forEach((r, i) => {
      const li = document.createElement("li");
      const span = document.createElement("span");
      span.className = "path";
      span.textContent = r.shown;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "small";
      b.dataset.act = "remove-root";
      b.dataset.i = String(i);
      b.textContent = "Remove";
      li.append(span, b);
      ul.append(li);
    });
    updateScanCli();
  }

  function addRoot(path, shown) {
    if (!path) return;
    path = path.length > 1 ? path.replace(/\/+$/, "") : path;
    if (W.roots.some((r) => r.path === path)) { toast("That folder is in the list already."); return; }
    W.roots.push({ path: path, shown: shown || path });
    renderRoots();
  }

  async function browse(token, path) {
    const qs = new URLSearchParams();
    if (token) qs.set("t", token);
    else if (path) qs.set("path", path);
    if (W.hidden) qs.set("hidden", "1");
    let d;
    try {
      d = await api("/api/browse?" + qs.toString());
    } catch (e) {
      toast(e.message, true);
      return;
    }
    W.at = d;
    $("#here").textContent = d.shown;
    $("#up").disabled = !d.parent;
    const places = $("#places");
    places.replaceChildren();
    for (const p of d.places) {
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.act = "browse";
      b.dataset.token = p.token;
      b.textContent = p.label;
      places.append(b);
    }
    const ul = $("#dirs");
    ul.replaceChildren();
    if (d.error) {
      const li = document.createElement("li");
      li.className = "muted";
      li.textContent = "Can't list this folder: " + d.error;
      ul.append(li);
    } else if (!d.dirs.length) {
      const li = document.createElement("li");
      li.className = "muted";
      li.textContent = "No folders in here.";
      ul.append(li);
    }
    for (const x of d.dirs) {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.act = "browse";
      b.dataset.token = x.token;
      b.textContent = x.name + "/";
      li.append(b);
      ul.append(li);
    }
    if (d.more) {
      const li = document.createElement("li");
      li.className = "muted";
      li.textContent = `... and ${d.more.toLocaleString("en-US")} more: type the path below`;
      ul.append(li);
    }
  }

  function scanArgv() {
    const form = $("#scan");
    const argv = [form.dataset.kind];
    for (const el of $$("[data-kind]", form)) {
      const k = el.dataset.kind;
      if (k === "bool") {
        const def = el.dataset.default === "1";
        if (el.checked !== def) argv.push(el.checked ? el.dataset.on : el.dataset.off);
      } else if (k === "text") {
        const v = el.value.trim();
        if (v !== "" && v !== el.dataset.default) argv.push(`${el.dataset.flag}=${v}`);
      } else if (k === "lines") {
        for (const line of el.value.split("\n")) {
          const v = line.trim();
          if (v) argv.push(`${el.dataset.flag}=${v}`);
        }
      } else if (k === "set") {
        const picked = $$("input:checked", el).map((i) => i.value);
        const def = (el.dataset.default || "").split(",").filter(Boolean);
        const same = picked.length === def.length && picked.every((x) => def.includes(x));
        if (!same) argv.push(`${el.dataset.flag}=${picked.join(",")}`);
      }
    }
    argv.push("--");
    for (const r of W.roots) argv.push(r.path);
    return argv;
  }

  function updateScanCli() {
    let argv = scanArgv();
    // "--" (the end of the options) matters only before a folder named like an option.
    if (!W.roots.some((r) => r.path.startsWith("-"))) argv = argv.filter((a) => a !== "--");
    if (!W.roots.length) argv.push("FOLDER...");
    $("#scan-cli").textContent = commandLine(argv);
  }

  function setupScan() {
    for (const p of init.roots || []) addRoot(p, p);
    renderRoots();
    browse(null, null);
    const form = $("#scan");
    form.addEventListener("input", updateScanCli);
    form.addEventListener("change", updateScanCli);
    $("#show-hidden").addEventListener("change", (ev) => {
      W.hidden = ev.target.checked;
      if (W.at) browse(W.at.token, null);
    });
    $("#typed-path").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); typedPath(); }
    });
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const err = $("#scan-error");
      err.hidden = true;
      if (!W.roots.length) {
        err.textContent = "Add at least one folder to look in.";
        err.hidden = false;
        return;
      }
      $("#start").disabled = true;
      try {
        const r = await api("/api/jobs", { argv: scanArgv() });
        openJob(r);
      } catch (e) {
        err.textContent = e.message;
        err.hidden = false;
        $("#start").disabled = false;
      }
    });
  }

  function typedPath() {
    const input = $("#typed-path");
    const v = input.value.trim();
    if (!v) return;
    if (!v.startsWith("/")) { toast("Give a full path, starting with /", true); return; }
    addRoot(v, v);
    input.value = "";
  }

  // ------------------------------------------------------------ a job

  const STOPPED = { 130: "Cancelled: what was done so far is saved.", 2: "Refused: see the output." };

  function openJob(r) {
    // The job page goes on to the results once the job ends; it knows to
    // even when the job ends before the page has loaded.
    sessionStorageSet("started", String(r.id));
    location.href = r.url;
  }

  async function setupJob() {
    const out = $("#job-out");
    let since = 0;
    let chars = 0;
    // Started from here, or seen running: go on to its results when it ends.
    let watched = sessionStorageTake("started") === String(init.id);
    for (;;) {
      let s;
      try {
        s = await api(`/api/jobs/${encodeURIComponent(init.id)}?since=${since}`);
      } catch (e) {
        $("#job-head").textContent = e.message;
        if (e.status === 404 || e.status === 403) return;
        await sleep(2000);
        continue;
      }
      if (s.lines.length) {
        const atEnd = out.scrollTop + out.clientHeight >= out.scrollHeight - 40;
        const text = s.lines.join("\n") + "\n";
        out.append(document.createTextNode(text));
        chars += text.length;
        while (chars > 2000000 && out.firstChild) {     // keep the page light
          chars -= out.firstChild.textContent.length;
          out.firstChild.remove();
        }
        if (atEnd) out.scrollTop = out.scrollHeight;
      }
      since = s.next;
      $("#job-progress").textContent = s.progress || "";
      if (!s.running) { jobEnded(s, watched); return; }
      watched = true;
      $("#job-head").textContent = `Running... ${Math.round(s.elapsed)} s`;
      await sleep(500);
    }
  }

  function jobEnded(s, watched) {
    const box = $("#job-box");
    box.classList.remove("s-running");
    const ok = s.code === 0 && !s.cancelled;
    box.classList.add(ok ? "s-done" : (s.cancelled || s.code === 130) ? "s-interrupted" : s.code === 1 ? "s-interrupted" : "s-failed");
    let head = ok ? "Finished." : s.cancelled ? STOPPED[130] : STOPPED[s.code]
      || (s.code === 1 ? "Finished, with problems: see the output." : `Stopped with exit code ${s.code}: see the output.`);
    $("#job-head").textContent = head;
    $("#cancel").hidden = true;
    const links = $("#job-links");
    links.replaceChildren();
    const link = (href, text) => {
      const a = document.createElement("a");
      a.href = href;
      a.textContent = text;
      links.append(a);
    };
    const next = s.after || (s.run ? `/runs/${encodeURIComponent(s.run)}` : null);
    if (s.run) link(`/runs/${encodeURIComponent(s.run)}`, "Open the run");
    if (s.after && s.after !== `/runs/${s.run}`) link(s.after, "Go on");
    link("/history", "History");
    // Only for a job started from here or seen running: coming back to a
    // finished one (Back, History) shows its output and stays.
    if (watched && next && (ok || (s.code === 1 && !s.cancelled))) {
      $("#job-head").textContent = head + " Opening the results...";
      setTimeout(() => { location.href = next; }, ok ? 300 : 1500);
    }
  }

  // ------------------------------------------------------------ settings

  async function saveSetting(li, reset) {
    const key = li.dataset.key;
    const input = $("[data-input]", li);
    let body;
    if (reset) {
      body = { key: key, op: "reset" };
    } else {
      let value;
      if (li.dataset.kind === "bool") value = input.checked;
      else if (li.dataset.kind === "list") value = input.value.split("\n").map((x) => x.trim()).filter(Boolean);
      else value = input.value.trim();
      body = { key: key, op: "set", value: value };
    }
    for (const b of $$("button", li)) b.disabled = true;
    try {
      const r = await api("/api/settings", body);
      if (key === "ui.theme") {
        document.documentElement.dataset.theme = ["light", "dark"].includes(r.value) ? r.value : "auto";
      }
      $("#cli").textContent = r.cli;
      if (reset) {
        sessionStorageSet("toast", `${key} is back to its default.`);
        location.reload();
        return;
      }
      const badge = $(".s-name .badge", li);
      badge.textContent = r.source;
      badge.className = "badge " + (r.source.startsWith("environment") ? "src-env"
        : r.source === "settings file" ? "src-file" : "src-default");
      li.classList.remove("dirty");
      toast(r.message || `Saved: ${key} = ${r.value}`);
    } catch (e) {
      toast(e.message, true);
    } finally {
      for (const b of $$("button", li)) b.disabled = false;
    }
  }

  function sessionStorageSet(k, v) {
    try { sessionStorage.setItem(k, v); } catch (e) { /* not kept: fine */ }
  }

  function sessionStorageTake(k) {
    try {
      const v = sessionStorage.getItem(k);
      sessionStorage.removeItem(k);
      return v;
    } catch (e) {
      return null;
    }
  }

  // ------------------------------------------------------------ clicks

  async function post(path, body, done) {
    try {
      const r = await api(path, body);
      if (done) done(r);
    } catch (e) {
      toast(e.message, true);
    }
  }

  const ACTS = {
    "copy-cli": () => copyFrom($("#cli")),
    "close-dialog": () => dlg && dlg.close(),
    "close-preview": () => $("#preview").close(),
    "confirm": (el) => {
      const body = { action: el.dataset.action };
      if (el.dataset.run) body.run = el.dataset.run;
      if (el.dataset.token) body.token = el.dataset.token;
      if (el.dataset.drive) body.drive = el.dataset.drive;
      if (el.dataset.back) body.back = el.dataset.back;
      openConfirm(body);
    },
    "remove": () => openConfirm({ action: "remove", run: init.run, method: init.method === "permanent" ? "permanent" : "trash" }),
    "trash-empty": () => {
      const form = $("#trash-empty");
      const days = Number(form.elements.older_than.value);
      if (!form.elements.older_than.value.trim() || !(days >= 0)) { toast("Give a number of days (0 or more).", true); return; }
      const body = { action: "trash_empty", older_than: days };
      if (form.elements.drive.value) body.drive = form.elements.drive.value;
      openConfirm(body);
    },
    "preview": (el) => preview(el),
    "open": (el) => post("/api/open", { run: init.run, ref: el.dataset.ref }, (r) => toast(`Opened ${r.opened}`)),
    "open-place": (el) => post("/api/open", { place: el.dataset.place }, (r) => toast(`Opened ${r.opened}`)),
    "ignore": (el) => {
      R.queue = R.queue.then(() => post("/api/ignore", { run: init.run, ref: el.dataset.ref, q: cleanQ(R.q) }, (r) => renderResults(r, false)));
    },
    "unset": (el) => choose({ op: "unset", ref: el.dataset.ref }),
    "card-reset": (el) => choose({ op: "reset_card", card: el.dataset.card }),
    "card-keep": (el) => choose({ op: "keep_all", card: el.dataset.card }),
    "auto": (el) => auto(el.dataset.how),
    "goto": (el) => {
      R.q = { tab: R.q.tab, card: el.dataset.card };
      loadResults(true);
    },
    "show-all": () => {
      delete R.q.card;
      loadResults(true);
    },
    "page": (el) => {
      R.q.page = el.dataset.page;
      loadResults(true);
    },
    "browse": (el) => browse(el.dataset.token, null),
    "up": () => { if (W.at && W.at.parent) browse(W.at.parent, null); },
    "add-here": () => { if (W.at) addRoot(W.at.path, W.at.shown); },
    "add-typed": () => typedPath(),
    "remove-root": (el) => {
      W.roots.splice(Number(el.dataset.i), 1);
      renderRoots();
    },
    "cancel-job": (el) => {
      el.disabled = true;
      el.textContent = "Cancelling...";
      post(`/api/jobs/${encodeURIComponent(init.id)}/cancel`, {}, null);
    },
    "setting-save": (el) => saveSetting(el.closest("li.setting"), false),
    "setting-reset": (el) => saveSetting(el.closest("li.setting"), true),
  };

  document.addEventListener("click", (ev) => {
    const el = ev.target.closest("[data-act]");
    if (!el || el.disabled) return;
    const act = ACTS[el.dataset.act];
    if (!act) return;
    ev.preventDefault();
    act(el);
  });

  // ------------------------------------------------------------ start

  const said = sessionStorageTake("toast");
  if (said) toast(said);
  if (page === "results") setupResults();
  else if (page === "scan") setupScan();
  else if (page === "job") setupJob();
  else if (page === "settings") {
    // Save is the main button of a setting only once its value is changed
    document.addEventListener("input", (ev) => {
      const li = ev.target.closest && ev.target.closest("li.setting");
      if (li) li.classList.add("dirty");
    });
  }
})();
