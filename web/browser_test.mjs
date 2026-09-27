// Real-browser smoke test against dist/, with ALL external hosts blocked —
// proves the vendored runtime needs no CDN / PyPI.
import { chromium } from "playwright";
import { spawn } from "node:child_process";
const port = 8765;
const server = spawn("python3", ["-m", "http.server", String(port), "-d", "dist"], { stdio: "ignore" });
await new Promise(r => setTimeout(r, 800));
const browser = await chromium.launch();
const page = await browser.newPage();
const blocked = [];
await page.route(/^https?:\/\/(?!localhost)/, route => { blocked.push(route.request().url()); route.abort(); });
const logs = [];
page.on("console", m => logs.push(m.text()));
page.on("pageerror", e => logs.push("PAGEERROR " + e.message));
const t0 = Date.now();
await page.goto(`http://localhost:${port}/index.html`);
try {
  await page.waitForSelector("#app:not(.hidden)", { timeout: 180000 });
  console.log(`booted in ${((Date.now()-t0)/1000).toFixed(1)}s`);
  await page.waitForFunction(() => document.querySelector("#summary")?.textContent.includes("Net"), null, { timeout: 120000 });
  console.log("summary:", (await page.textContent("#summary")).replace(/\s+/g, " ").slice(0, 100));
  console.log("versions:", await page.textContent("#versions"));
  await page.waitForFunction(() => document.querySelectorAll("#graph .card").length > 0, null, { timeout: 30000 });
  const cards = await page.$$eval("#graph .card", els => els.length);
  console.log("inspector graph cards:", cards);
  // Formulas is the landing tab, with values on by default; the Inspector is a click away
  const landing = await page.evaluate(() => ({ tab: document.querySelector(".tab.active").id, values: document.querySelector("#fx-values").checked, sel: document.querySelector("#fx-cards .card.fx.sel")?.dataset.key }));
  console.log("landing:", JSON.stringify(landing));
  if (landing.tab !== "tab-formulas" || !landing.values || landing.sel !== "Projection.result_cf") throw new Error("landing tab wrong");
  await page.click('.tabs button[data-tab="inspector"]');
  // y-axis mode control on the multi-line value chart (result_cf): shared -> independent -> multiples
  const ymodes = {};
  for (const m of ["independent", "multiples", "shared"]) {
    await page.click(`#chart .ymode button[data-mode=${m}]`);
    ymodes[m] = await page.$eval("#chart", el => ({ on: el.querySelector(".ymode button.on")?.dataset.mode, panels: el.querySelectorAll("svg rect[fill='#fafbfc']").length, pct: /%/.test(el.textContent), stored: localStorage.getItem("yMode") }));
  }
  console.log("y-mode:", JSON.stringify(ymodes));
  if (ymodes.multiples.panels < 2 || !ymodes.independent.pct || ymodes.shared.on !== "shared" || ymodes.multiples.stored !== "multiples") throw new Error("y-mode control broken");
  // dependency graph: result_cf reads 6 cells; the 6th (proj_len, a plain value) is a card with
  // its value. In a 920px-tall window the dense layout fits all six; at 720px the column
  // scrolls and says so (sticky ▾ hint) until scrolled to the end. Navigating to a value node
  // must put the value on the centre card (it used to be title-only).
  const depView = () => page.evaluate(() => {
    const col = document.querySelector("#graph .col"), cr = col.getBoundingClientRect();
    const cards = [...document.querySelectorAll("#graph .card:not(.cur)")].map(c => { const r = c.getBoundingClientRect(); return { t: c.querySelector(".t").textContent, big: c.querySelector(".big")?.textContent ?? null, inside: r.top >= cr.top - 1 && r.bottom <= cr.bottom + 1 }; });
    return { dense: document.querySelector("#graph").classList.contains("dense"), scroll: col.scrollHeight - col.clientHeight, hint: col.classList.contains("overflow") && getComputedStyle(col.querySelector(".scrollhint")).display !== "none", cards };
  });
  const settle = () => page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));   // let ResizeObserver deliver
  await page.setViewportSize({ width: 1280, height: 920 }); await settle();
  const depTall = await depView();
  console.log("dep view @920:", JSON.stringify(depTall));
  const projLen = depTall.cards.find(c => c.t === "proj_len");
  if (!depTall.dense || depTall.scroll > 0 || depTall.hint || !projLen || projLen.big !== "121" || !depTall.cards.every(c => c.inside)) throw new Error("dep view @920: value card clipped or missing");
  await page.setViewportSize({ width: 1280, height: 720 }); await settle();
  const depShort = await depView();
  await page.$eval("#graph .col", c => { c.scrollTop = c.scrollHeight; });
  await page.waitForFunction(() => !document.querySelector("#graph .col").classList.contains("overflow"), null, { timeout: 5000 });   // scroll events are async
  const depScrolled = await depView();
  console.log("dep view @720:", JSON.stringify({ scroll: depShort.scroll, hint: depShort.hint, projInside: depShort.cards.find(c => c.t === "proj_len").inside }), "-> scrolled:", JSON.stringify({ hint: depScrolled.hint, projInside: depScrolled.cards.find(c => c.t === "proj_len").inside }));
  if (depShort.scroll <= 0 || !depShort.hint || depScrolled.hint || !depScrolled.cards.find(c => c.t === "proj_len").inside) throw new Error("dep view @720: scroll hint broken");
  await page.click('#graph .card[title^="Projection.proj_len"]');
  await page.waitForFunction(() => document.querySelector("#graph .card.cur .t")?.textContent === "Projection.proj_len()", null, { timeout: 30000 });
  const curBig = await page.$eval("#graph .card.cur", el => el.querySelector(".big")?.textContent ?? null);
  console.log("centre card on a value node:", curBig);
  if (curBig !== "121") throw new Error("centre card of a value node is empty");
  await page.click("#back");                                                    // history back → result_cf
  await page.waitForFunction(() => document.querySelector("#graph .card.cur .t")?.textContent === "Projection.result_cf()", null, { timeout: 30000 });
  // Formulas tab: cards for every cell, selection follows the Inspector (result_cf), precedents violet
  await page.click('.tabs button[data-tab="formulas"]');
  const fx = await page.$eval("#fx-cards", el => ({ cards: el.querySelectorAll(".card.fx").length, sel: el.querySelector(".card.fx.sel")?.dataset.key, prec: el.querySelectorAll(".card.fx.prec").length, dep: el.querySelectorAll(".card.fx.dep").length }));
  await page.click('.card.fx[data-key="Projection.pols_if"]');
  const fx2 = await page.$eval("#fx-cards", el => ({ sel: el.querySelector(".card.fx.sel")?.dataset.key, prec: el.querySelectorAll(".card.fx.prec").length, dep: el.querySelectorAll(".card.fx.dep").length, both: el.querySelectorAll(".card.fx.prec.dep").length }));
  const fxSrc = await page.textContent("#fx-source");
  console.log("formulas tab:", JSON.stringify(fx), "-> pols_if:", JSON.stringify(fx2), "source:", fxSrc.slice(0, 18));
  if (fx.cards < 40 || fx.sel !== "Projection.result_cf" || fx.prec < 5 || fx2.sel !== "Projection.pols_if" || !fx2.prec || !fx2.dep || !fx2.both || !fxSrc.startsWith("def pols_if")) throw new Error("formulas tab broken");
  // Formulas selection history: result_cf (load) → proj_len (Inspector card) → result_cf (Inspector ◀) → pols_if (card click);
  // ◀ ▶ and Alt+←/→ (while this tab is open) replay it without touching the Inspector's own history
  const fxSel = () => page.$eval("#fx-cards", el => el.querySelector(".card.fx.sel")?.dataset.key);
  const fxNav = () => page.evaluate(() => ({ back: !document.querySelector("#fx-back").disabled, fwd: !document.querySelector("#fx-fwd").disabled, n: document.querySelectorAll("#fx-history option").length,
    top: document.querySelector("#fx-history option").textContent, pos: window.__playground.state.fxHistPos, insp: window.__playground.state.histPos, hdr: document.querySelector("#fx-header").textContent.slice(0, 22) }));
  const h0 = await fxNav();
  await page.click("#fx-back"); const s1 = await fxSel(); const h1 = await fxNav();
  await page.keyboard.press("Alt+ArrowLeft"); const s2 = await fxSel();
  await page.click("#fx-fwd"); const s3 = await fxSel();
  await page.keyboard.press("Alt+ArrowRight"); const s4 = await fxSel(); const h4 = await fxNav();
  console.log("formulas history:", JSON.stringify({ h0, s1, h1, s2, s3, s4, h4 }));
  if (!h0.back || h0.fwd || h0.n !== 4 || !/^▸ .*pols_if/.test(h0.top) || h0.pos !== 3) throw new Error("formulas history not recorded");
  if (s1 !== "Projection.result_cf" || !h1.fwd || !/result_cf/.test(h1.hdr) || s2 !== "Projection.proj_len" || s3 !== "Projection.result_cf" || s4 !== "Projection.pols_if" || h4.pos !== 3 || h4.n !== 4 || h4.insp !== h0.insp) throw new Error("formulas history navigation broken");
  // "Show values": sparklines + value at t on the cards; t slider drives caption, header and the Open button
  await page.uncheck("#fx-values"); await page.check("#fx-values");            // exercise the toggle (default is on)
  await page.waitForFunction(() => document.querySelectorAll("#fx-cards .card.fx .sp").length > 10, null, { timeout: 60000 });
  await page.$eval("#fx-trange", r => { r.value = 12; r.dispatchEvent(new Event("input")); });
  const fxv = await page.evaluate(() => ({ sparks: document.querySelectorAll("#fx-cards .card.fx .sp").length, t: document.querySelector("#fx-t").value, tmax: document.querySelector("#fx-trange").max,
    cap: document.querySelector('.card.fx[data-key="Projection.pols_if"] .cap')?.textContent, big: document.querySelector('.card.fx[data-key="Projection.policy_term"] .big')?.textContent,
    table: document.querySelector('.card.fx[data-key="Projection.mort_table"] .cap')?.textContent, hdr: document.querySelector("#fx-header").textContent, btn: document.querySelector("#fx-inspect").textContent, stored: localStorage.getItem("fxValues") }));
  console.log("show values:", JSON.stringify(fxv));
  if (fxv.sparks < 10 || fxv.t !== "12" || fxv.tmax !== "120" || !/t=12: 0\.8994/.test(fxv.cap) || fxv.big !== "10" || fxv.table !== "table" || !/= 0\.899407 at t=12/.test(fxv.hdr) || !/pols_if\(12\)/.test(fxv.btn) || fxv.stored !== "1") throw new Error("show values broken");
  await page.click("#fx-inspect");
  await page.waitForFunction(() => document.querySelector("#header")?.textContent.includes("pols_if(12)") && document.querySelector("#tab-inspector").classList.contains("active"), null, { timeout: 30000 });
  console.log("open in inspector:", (await page.textContent("#header")).slice(0, 40));
  // a slider drag later must refresh the values (stale -> refetch while the tab is visible); t must not move
  await page.click('.tabs button[data-tab="formulas"]');
  await page.uncheck("#fx-values");
  if (await page.$$eval("#fx-cards .card.fx .sp", els => els.length)) throw new Error("values should hide when toggled off");
  await page.click('.tabs button[data-tab="inspector"]');
  // slider edit -> recompute
  const before = await page.textContent("#summary");
  await page.$eval("#fields input[type=range]", el => { el.value = el.max; el.dispatchEvent(new Event("input")); });
  await page.waitForFunction(b => document.querySelector("#summary").textContent !== b && document.querySelector("#summary").textContent.includes("edited"), before, { timeout: 30000 });
  console.log("after slider edit:", (await page.textContent("#summary")).replace(/\s+/g, " ").slice(0, 90));
  // switch to a Past Library model
  await page.selectOption("#model", "simplelife");
  await page.waitForFunction(() => window.__playground.state.loaded?.name === "simplelife", null, { timeout: 120000 });
  await page.waitForFunction(() => /point 1/.test(document.querySelector("#summary")?.textContent) && !document.querySelector("#error:not(.hidden)"), null, { timeout: 60000 });
  console.log("simplelife:", (await page.textContent("#summary")).replace(/\s+/g, " ").slice(0, 80));
  await page.screenshot({ path: "../docs/screenshot-web.png", fullPage: false });

  // switch to a new reference-library model (krlib/delib, lifelib v0.17.1+):
  // these define result_cf but NO PV cells -> the results picker must offer
  // cashflows only and the charts must still render.
  for (const name of ["Term_KR_S", "RLV_DE_S"]) {
    await page.selectOption("#model", name);
    await page.waitForFunction(n => window.__playground.state.loaded?.name === n, name, { timeout: 120000 });
    await page.waitForFunction(() => /point/.test(document.querySelector("#summary")?.textContent) && !document.querySelector("#error:not(.hidden)"), null, { timeout: 60000 });
    await page.click('.tabs button[data-tab="cashflows"]');
    await page.waitForFunction(() => document.querySelectorAll("#cf-chart svg").length > 0, null, { timeout: 30000 });
    await page.click('.tabs button[data-tab="pv"]');
    const st = await page.evaluate(() => {
      const s = window.__playground.state;
      return { pv: !!s.loaded.has_result_pv, cf: !!s.loaded.has_result_cf,
               steps: s.lastCompute.result_cf.data.length, fields: document.querySelectorAll("#fields .field").length,
               cfLines: document.querySelectorAll("#cf-chart svg path, #cf-chart svg rect").length,
               pvEmpty: document.querySelector("#pv-chart").innerHTML === "",
               note: /no result_pv/.test(document.querySelector("#summary").textContent) };
    });
    if (st.pv || !st.cf) throw new Error(`${name}: expected cashflow-only model, got ${JSON.stringify(st)}`);
    if (!st.cfLines || !st.fields || st.steps < 2) throw new Error(`${name}: nothing rendered ${JSON.stringify(st)}`);
    // no PV cells -> PV chart stays empty and the summary says so (pre-existing contract)
    if (!st.pvEmpty || !st.note) throw new Error(`${name}: PV-less handling wrong ${JSON.stringify(st)}`);
    console.log(`${name}:`, JSON.stringify(st));
    await page.click('.tabs button[data-tab="inspector"]');
  }

  // ---- formula edit -> export model -> load the exported zip back ----
  page.on("dialog", d => d.accept());                       // "edits will be lost" confirms
  await page.selectOption("#model", "BasicTerm_S");
  await page.waitForFunction(() => window.__playground.state.loaded?.name === "BasicTerm_S", null, { timeout: 120000 });
  await page.waitForFunction(() => /Net/.test(document.querySelector("#summary")?.textContent), null, { timeout: 60000 });
  const netOf = async () => { const t = await page.textContent("#summary"); return (t.match(/Net Cashflow\s*([-\d.,]+)/) || [])[1]; };
  const baseNet = await netOf();
  await page.fill("#search", "premium_pp");
  await page.click("#results li[data-name=premium_pp]");
  await page.waitForFunction(() => document.querySelector("#source")?.textContent.startsWith("def premium_pp"), null, { timeout: 30000 });
  await page.click("#src-edit");
  await page.$eval("#source-edit", ta => { ta.value = ta.value.replace("return round(", "return 2 * round("); });
  await page.click("#src-apply");
  await page.waitForFunction(b => { const t = document.querySelector("#summary").textContent; return /Net/.test(t) && (t.match(/Net Cashflow\s*([-\d.,]+)/) || [])[1] !== b; }, baseNet, { timeout: 60000 });
  const editedNet = await netOf();
  const badge = await page.$eval("#edited-badge", el => getComputedStyle(el).display !== "none");
  const dot = await page.$("#results li[data-name=premium_pp] .edited-dot");
  console.log(`formula edit: Net ${baseNet} -> ${editedNet}, badge=${badge}, results dot=${!!dot}, export label="${await page.textContent("#export-model")}"`);
  if (editedNet === baseNet || !badge || !dot) throw new Error("formula edit did not take effect in the UI");

  // ---- Pyodide fatal error -> flash, restart the runtime, restore the session ----
  // (real fatal_error path via the worker's test hook; the edit, the point's field
  // edits and the inspector view must all survive)
  await page.$eval("#fields input[type=range]", el => { el.value = el.max; el.dispatchEvent(new Event("input")); });
  await page.waitForFunction(() => /edited/.test(document.querySelector("#summary").textContent), null, { timeout: 30000 });
  const preCrash = await page.evaluate(() => ({ net: (document.querySelector("#summary").textContent.match(/Net Cashflow\s*([-\d.,]+)/) || [])[1], field: document.querySelector("#fields input[type=number]").value }));
  await page.evaluate(() => window.__playground.simulateCrash());
  await page.waitForFunction(() => /crashed/.test(document.querySelector("#toast")?.textContent || "") && !document.querySelector("#toast").classList.contains("hidden"), null, { timeout: 15000 });
  const t1 = Date.now();
  await page.waitForFunction(() => /Python restarted/.test(document.querySelector("#toast")?.textContent || ""), null, { timeout: 180000 });
  await page.waitForFunction(() => /Point 1 computed/.test(document.querySelector("#status")?.textContent || "") && document.querySelector("#busy").textContent === "", null, { timeout: 60000 });
  const post = await page.evaluate(() => ({ toast: document.querySelector("#toast").textContent, net: (document.querySelector("#summary").textContent.match(/Net Cashflow\s*([-\d.,]+)/) || [])[1],
    field: document.querySelector("#fields input[type=number]").value, edits: window.__playground.state.formulaEdits.size, hdr: document.querySelector("#header").textContent.slice(0, 30),
    badge: getComputedStyle(document.querySelector("#edited-badge")).display !== "none", banner: document.querySelector("#error").textContent.slice(0, 60), crashes: window.__playground.state.crashes, restarting: window.__playground.state.restarting }));
  console.log(`crash recovery in ${((Date.now() - t1) / 1000).toFixed(1)}s:`, JSON.stringify(post), "pre:", JSON.stringify(preCrash));
  if (!/1 formula edit re-applied/.test(post.toast) || post.net !== preCrash.net || post.field !== preCrash.field || post.edits !== 1 || !post.badge || !/premium_pp/.test(post.hdr) || !/crashed and was restarted/.test(post.banner) || post.crashes !== 0 || post.restarting) throw new Error("crash recovery did not restore the session");
  // the runtime is usable again: another edit recomputes
  await page.click("#reset"); await page.waitForFunction(b => (document.querySelector("#summary").textContent.match(/Net Cashflow\s*([-\d.,]+)/) || [])[1] === b, editedNet, { timeout: 30000 });

  const [dl] = await Promise.all([page.waitForEvent("download", { timeout: 120000 }), page.click("#export-model")]);
  const zipPath = await dl.path(); const zipName = dl.suggestedFilename();
  const zipSize = (await import("node:fs")).statSync(zipPath).size;
  console.log(`exported ${zipName}: ${Math.round(zipSize / 1024)} KB`);
  if (!/BasicTerm_S-edited\.zip/.test(zipName) || zipSize < 10000) throw new Error("export looks wrong");
  // load it back as a user model: same edited Net, no pending edits, picker shows it
  await page.setInputFiles("#model-file", { name: "MyTerm.zip", mimeType: "application/zip", buffer: (await import("node:fs")).readFileSync(zipPath) });
  await page.waitForFunction(() => window.__playground.state.loaded?.name === "MyTerm", null, { timeout: 120000 });
  await page.waitForFunction(() => /Net/.test(document.querySelector("#summary")?.textContent) && !document.querySelector("#error:not(.hidden)"), null, { timeout: 60000 });
  const rtNet = await netOf();
  const picker = await page.$eval("#model", s => ({ value: s.value, group: s.selectedOptions[0]?.parentElement?.label, n: s.querySelectorAll('optgroup[label="Your models"] option').length }));
  console.log(`loaded exported zip: Net ${rtNet}, picker=${JSON.stringify(picker)}, export label="${await page.textContent("#export-model")}"`);
  if (rtNet !== editedNet || picker.value !== "MyTerm" || picker.group !== "Your models") throw new Error("round-trip mismatch");
  // the loaded copy carries the edit as its *original* formula: source shows it, no badge
  await page.fill("#search", "premium_pp"); await page.click("#results li[data-name=premium_pp]");
  await page.waitForFunction(() => document.querySelector("#source")?.textContent.includes("2 * round"), null, { timeout: 30000 });
  console.log("exported formula present in loaded model; badge:", await page.$eval("#edited-badge", el => getComputedStyle(el).display !== "none"));
  console.log("external requests blocked:", blocked.length, blocked.slice(0, 3));
  console.log("BROWSER TEST OK");
} catch (e) {
  console.log("FAIL:", e.message.split("\n")[0]);
  console.log("boot status:", await page.textContent("#boot-status").catch(() => "?"));
  console.log("status:", await page.textContent("#status").catch(() => "?"), "| banner:", (await page.textContent("#error").catch(() => "?")).slice(0, 300), "| toast:", await page.textContent("#toast").catch(() => "?"));
  console.log("logs:", logs.slice(-8));
  process.exitCode = 1;
}
await browser.close(); server.kill();
