// Starts the real app window (app.pyw) with WebView2's DevTools port open and checks that the
// page gets its data from Python: library, covers, genre tree, Last.fm albums on demand, and
// that preferences (theme) survive a restart. The window shows on screen while it runs.
//   node pipeline/ui/tests/desktop_test.mjs        (Node 22+, for the built-in WebSocket)
import { spawn, execFileSync } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PIPELINE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PORT = 9334;
const results = [];
const check = (name, ok, extra = "") => { results.push(ok); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? "  (" + extra + ")" : ""}`); };

function launch() {
  return spawn(path.join(PIPELINE, "python", "pythonw.exe"), ["app.pyw", "--debug-port", String(PORT)], { cwd: PIPELINE, stdio: "ignore" });
}
async function stop(proc) {   // ask the window to close like a user would (so WebView2 saves its storage), then make sure
  try { execFileSync("taskkill", ["/PID", String(proc.pid), "/T"], { stdio: "ignore" }); } catch {}
  for (let i = 0; i < 20 && proc.exitCode === null; i++) await sleep(250);
  try { execFileSync("taskkill", ["/PID", String(proc.pid), "/T", "/F"], { stdio: "ignore" }); } catch {}
  for (let i = 0; i < 40; i++) {   // wait until WebView2 has let go of the debug port, or the next start can't open it
    try { await fetch(`http://127.0.0.1:${PORT}/json`); } catch { return; }
    await sleep(250);
  }
}

async function connect() {
  let page;
  for (let i = 0; i < 160 && !page; i++) {
    try { page = (await (await fetch(`http://127.0.0.1:${PORT}/json`)).json()).find(t => t.type === "page" && t.url.includes("?desktop")); } catch {}
    if (!page) await sleep(250);
  }
  if (!page) throw new Error("app window never showed up on the debug port");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener("open", r, { once: true }));
  let id = 0; const pending = new Map();
  ws.addEventListener("message", m => { const d = JSON.parse(m.data); if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); } });
  const send = (method, params = {}) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
  const js = async expr => {
    const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " " + (r.result.exceptionDetails.exception?.description || ""));
    return r.result.result.value;
  };
  const waitFor = async (expr, ms = 10000) => {   // tolerates errors while the page is still loading
    for (let t = 0; t < ms; t += 150) { try { if (await js(expr)) return true; } catch {} await sleep(150); }
    return false;
  };
  return { ws, js, waitFor };
}

let app = launch();
let userTheme = null;   // the theme you picked, restored when the test ends
try {
  let { ws, js, waitFor } = await connect();
  check("page runs in app mode with the Python bridge", await waitFor(`!!(window.pywebview && window.pywebview.api && window.pywebview.api.load)`));
  // your own theme choice (kept by Python in app-prefs.json, and by the page) is put back at the end
  await waitFor(`!!window.pywebview.api.get_settings`);
  userTheme = (await js(`window.pywebview.api.get_settings().then(r => r.theme)`)) || (await js(`localStorage.getItem('phx-theme')`)) || "dark";
  await js(`window.pywebview.api.set_theme('dark').then(() => { localStorage.removeItem('phx-theme'); location.reload(); })`);
  await sleep(500);
  check("dark theme shows", await waitFor(`document.readyState === 'complete' && document.documentElement.dataset.theme === 'dark'`));
  check("library loads from Python", await waitFor(`!document.querySelector('#view .empty') && document.querySelectorAll('#view .tile, #view .art').length > 0`, 60000),
    await js(`document.querySelector('#view')?.innerText.slice(0, 80)`));

  check("the bundled Inter font loads (no Google Fonts in the app)", await waitFor(`document.fonts.ready.then(() =>
    [...document.fonts].some(f => f.family.includes('InterApp') && f.status === 'loaded') && !document.querySelector('link[href*="fonts.googleapis"]'))`, 10000));
  await js(`location.hash = 'albums'`);
  check("Albums shows the library's albums", await waitFor(`document.querySelectorAll('#view .tile').length > 50`), (await js(`document.querySelectorAll('#view .tile').length`)) + " tiles");
  check("album covers come through", await js(`[...document.querySelectorAll('#view img')].filter(i => i.src.startsWith('data:image/jpeg')).length > 50`));

  await js(`location.hash = 'genres'`);
  check("genre tree loads from Python", await waitFor(`!!document.querySelector('.ochart .ocard[data-name]')`, 20000));
  const g = await js(`(() => { const b = [...document.querySelectorAll('.gpanel .gitem')].find(b => !b.disabled); if (!b) return null; b.click(); return b.firstChild.textContent; })()`);
  check(`clicking ${g} in the panel selects it`, !!g && await waitFor(`(document.querySelector('.gpanel h3')?.textContent || '').toLowerCase() === ${JSON.stringify((g || "").toLowerCase())}`));
  check("no stray 'null' text in the panel", await js(`!/\\bnull\\b/.test(document.querySelector('.gpanel').innerText)`));
  check("no Mockup tag in the app", await js(`!document.querySelector('.mock-tag')`));
  check("Classic Window button is there, wired to Python", await js(`!!document.getElementById('classicBtn') && typeof window.pywebview.api.open_classic === 'function'`));
  check("its albums from your library show",await js(`document.querySelectorAll('.gpanel .tile').length > 0`));
  check("popular albums arrive from Last.fm", await waitFor(`document.querySelectorAll('.gpanel .pop').length > 0 || /Couldn't reach|no top albums/.test(document.querySelector('.gpanel').innerText)`, 20000),
    (await js(`document.querySelectorAll('.gpanel .pop').length`)) + " albums");
  await js(`document.querySelector('.gpanel .pop img')?.scrollIntoView({ block: 'center' })`);   // covers load lazily, when seen
  check("Last.fm covers are real images", await waitFor(`[...document.querySelectorAll('.gpanel .pop img')].some(i => i.complete && i.naturalWidth > 0)`, 15000));

  // Get Music > Browse by Genre: a genre the library doesn't have asks Last.fm on click
  await js(`location.hash = 'get'`);
  await waitFor(`!!document.querySelector('.mode-switch')`);
  await js(`[...document.querySelectorAll('.mode-switch button')].find(b => b.textContent === 'Browse by Genre').click()`);
  check("Browse by Genre shows the tree", await waitFor(`!!document.querySelector('.ochart .ocard[data-name]')`));
  await js(`document.querySelector('.ochart .ocard[data-name]').click()`);
  check("clicking a genre fetches its popular albums", await waitFor(`document.querySelectorAll('.gpanel .pop').length > 0`, 20000),
    await js(`document.querySelector('.gpanel h3')?.textContent`));

  // ---- live search (real MusicBrainz/Last.fm lookups, read-only), artist profile, album page
  await js(`[...document.querySelectorAll('.mode-switch button')].find(b => b.textContent === 'Search').click()`);
  await waitFor(`!!document.getElementById('getQ')`);
  const type = async (text, tab) => js(`(() => {
    [...document.querySelectorAll('.seg button')].find(b => b.textContent === ${JSON.stringify(tab)})?.click();
    const i = document.getElementById('getQ'); i.value = ${JSON.stringify(text)}; i.dispatchEvent(new Event('input')); })()`);
  await type("mazzy star", "Artists");
  check("artist search shows MusicBrainz results", await waitFor(`[...document.querySelectorAll('#liveResults .res-main b')].some(b => b.textContent === 'Mazzy Star')`, 30000),
    await js(`document.getElementById('liveResults')?.innerText.slice(0, 120)`));
  await js(`[...document.querySelectorAll('#liveResults .res-main')].find(b => b.textContent === 'Mazzy Star').click()`);
  check("clicking an artist opens their profile", await waitFor(`document.querySelector('#profile h1')?.textContent === 'Mazzy Star'`, 60000),
    await js(`document.getElementById('view').innerText.slice(0, 160)`));
  check("the profile lists their albums", await js(`[...document.querySelectorAll('#profile .section-title')].some(h => h.textContent.startsWith('Albums')) && document.querySelectorAll('#profile .tile').length >= 3`));
  check("the profile has top songs from Last.fm", await js(`document.querySelectorAll('#profile .top-song').length > 0 || /Last\\.fm/.test(document.querySelector('#profile .note')?.textContent || '')`));
  await js(`document.querySelector('#profile .tile').click()`);
  check("an album on the profile opens its page with the track list", await waitFor(`document.querySelectorAll('#albumPage tbody tr').length >= 3`, 60000),
    await js(`document.getElementById('view').innerText.slice(0, 160)`));
  await js(`document.getElementById('albumArtist').click()`);
  check("the album's artist name leads back to the profile", await waitFor(`document.querySelector('#profile h1')?.textContent === 'Mazzy Star'`, 10000));
  await js(`location.hash = 'get'`);
  await waitFor(`!!document.getElementById('getQ')`);
  await type("fade into you", "Songs");
  check("song search shows results with clickable artists", await waitFor(`document.querySelectorAll('#liveResults .res.live small a[href^="#mbartist-"]').length > 0`, 30000));
  await js(`document.querySelector('#liveResults .res-main').click()`);
  check("a song opens its album with that song picked", await waitFor(`!!document.querySelector('#albumPage tr.focus')`, 60000),
    await js(`document.getElementById('view').innerText.slice(0, 160)`));
  // Download sends the picked song to Python. Faked here: a real download would change your library
  // (downloadrun.py is tested end to end in a sandbox copy of the app instead).
  // tick exactly one song that isn't in the library (the picked one may already be)
  const pickIndex = await js(`(() => { const boxes = [...document.querySelectorAll('#albumPage tbody input.chk')];
    boxes.forEach(c => { if (c.checked) c.click(); });
    const i = boxes.findIndex(c => !c.disabled); if (i >= 0) boxes[i].click(); return i; })()`);
  await js(`window.pywebview.api.start_download = job => { window.__job = job; return Promise.resolve({ running: false, seq: 0 }); }`);
  await js(`document.getElementById('albumDownload').click()`);
  check("Download asks Python for just the ticked song", pickIndex >= 0 && await waitFor(`!!window.__job`, 3000) &&
    await js(`window.__job.kind === 'album' && window.__job.picks.length === 1 && window.__job.picks[0] === ${pickIndex} && /^[0-9a-f-]{36}$/.test(window.__job.release_id)`),
    await js(`JSON.stringify(window.__job || null)`));
  // A second download while one runs joins the queue (faked status: one running, one waiting)
  await js(`history.back()`);
  await waitFor(`!!document.getElementById('albumDownload')`, 30000);
  await js(`(() => { const boxes = [...document.querySelectorAll('#albumPage tbody input.chk')];
    boxes.forEach(c => { if (c.checked) c.click(); }); boxes.find(c => !c.disabled)?.click(); })()`);
  await js(`(() => {
    const st = { running: true, seq: 5, label: 'Running Album · Someone', kind: 'album', release_id: null, result: null, summary: '',
      problems: [], now: 'Downloading', progress: 0.3, steps: [{ key: 'download', name: 'Download', state: 'active', done: 0, total: 2 }],
      songs: [], log: [], queue: [{ qid: 7, label: 'Queued Album · Someone', kind: 'album' }],
      finished: [{ seq: 4, label: 'Earlier Album · Someone', result: 'done', summary: '2 new songs' }] };
    const api = window.pywebview.api;
    window.__real = { start: api.start_download, status: api.download_status, unqueue: api.unqueue_download, stop: api.stop_download };
    window.__fakeSt = st;
    api.start_download = () => Promise.resolve({ ...st, queued: true });
    api.download_status = () => Promise.resolve(window.__fakeSt);
    api.unqueue_download = qid => { window.__unqueued = qid; window.__fakeSt = { ...st, queue: [] }; return Promise.resolve(true); };
    api.stop_download = everything => { window.__stopAll = everything; return Promise.resolve(true); };
  })()`);
  await js(`document.getElementById('albumDownload').click()`);
  check("a download while one runs says it was queued and stays on the page",
    await waitFor(`/Added to the queue/.test(document.getElementById('toast').textContent)`, 3000) &&
    await js(`!!document.getElementById('albumDownload')`), await js(`document.getElementById('toast').textContent`));
  await js(`location.hash = 'activity'`);
  check("Activity lists the queue and earlier downloads", await waitFor(`/Queued Album/.test(document.getElementById('runQueue')?.innerText || '')
    && /Earlier Album/.test(document.getElementById('runEarlier')?.innerText || '')`, 5000), await js(`document.getElementById('view').innerText.slice(0, 300)`));
  await js(`document.getElementById('stopAll').click()`);
  check("Stop All asks Python to stop the queue too", await waitFor(`window.__stopAll === true`, 3000));
  await js(`[...document.querySelectorAll('#runQueue button')].find(b => b.textContent === 'Remove').click()`);
  check("Remove takes a download out of the queue", await waitFor(`window.__unqueued === 7 && !document.getElementById('runQueue')`, 5000));
  await js(`(() => { const api = window.pywebview.api, r = window.__real;
    window.__fakeSt = { ...window.__fakeSt, running: false, result: 'done', summary: 'test' };   // lets the page's poll end
    setTimeout(() => { api.start_download = r.start; api.download_status = r.status; api.unqueue_download = r.unqueue; api.stop_download = r.stop; }, 2000);
  })()`);
  await new Promise(r => setTimeout(r, 2500));
  await js(`location.hash = 'artists'`);
  await waitFor(`document.querySelectorAll('#view .avatar').length > 0`);
  await js(`document.querySelector('#view .avatar').click()`);
  check("a library artist page links to their profile", await waitFor(`!!document.getElementById('profileBtn') || !!document.querySelector('#view .head .btn')`, 5000),
    await js(`document.getElementById('profileBtn') ? 'has profile' : 'no MusicBrainz id for this artist'`));

  // ---- saving genre changes. The count is the real one (fillgenres --preview changes nothing), but
  // save_genres and refile_status are swapped for fakes in the page: a real save would change your
  // genre settings and re-file your music files. genrerules.py's own save/re-file is tested on a scratch copy.
  await js(`location.hash = 'genres'`);
  await waitFor(`(document.querySelector('.gpanel h3')?.textContent || '') === ${JSON.stringify(g)}`);
  const bar = () => js(`document.querySelector('.editbar b')?.textContent || ''`);
  check("starts with no unsaved changes (saved rules loaded)", (await bar()) === "", await bar());
  await js(`document.getElementById('fileHere').click()`);
  check(`switching ${g} off shows 1 unsaved change`, await waitFor(`document.querySelector('.editbar b')?.textContent === '1 unsaved change'`, 3000), await bar());
  await js(`[...document.querySelectorAll('.editbar button')].find(b => b.textContent === 'Save…').click()`);
  check("Save… asks Python for exact numbers", await waitFor(`!!document.querySelector('.sheet') && !/Counting/.test(document.querySelector('.sheet').innerText)`, 30000),
    await js(`[...document.querySelectorAll('.sheet .choice small')].pop()?.textContent`));
  check("the count mentions songs without saved tags", await js(`/no saved Last\\.fm tags|would move|move no songs/.test(document.querySelector('.sheet').innerText)`));
  await js(`(() => {
    const api = window.pywebview.api; let n = 0; window.__saved = null;
    api.save_genres = (rules, refile) => { window.__saved = { rules, refile }; return Promise.resolve({ ...rules }); };
    api.refile_status = () => Promise.resolve(++n < 4 ? { running: true, done: n * 100, kept: 0, errors: [], code: null }
      : { running: false, done: 396, kept: 2, errors: [], code: 0 });
  })()`);
  await js(`document.querySelectorAll('.sheet input[name=applyTo]')[1].click(); [...document.querySelectorAll('.sheet button')].find(b => b.textContent === 'Save').click()`);
  check("Save sends the new rules with the genre switched off", await waitFor(`!!window.__saved && !window.__saved.rules.list.includes(${JSON.stringify(g.toLowerCase())}) && window.__saved.refile === true`, 3000),
    await js(`JSON.stringify(window.__saved)`));
  check("the sheet closes and the save bar goes away", await waitFor(`!document.querySelector('.sheet') && !document.querySelector('.editbar')`, 3000));
  check("re-file progress shows in the status capsule", await waitFor(`document.getElementById('lcdTitle').textContent === 'Re-filing genres'`, 3000));
  check("when it's done the library reloads and says so", await waitFor(`/Genres re-filed/.test(document.getElementById('toast').textContent) && document.getElementById('lcdTitle').textContent === 'Ready'`, 15000),
    await js(`document.getElementById('toast').textContent`));
  check("after the reload the chart shows the settings actually saved", await waitFor(`document.querySelector('.ochart .ocard[data-name="${g.toLowerCase()}"]')?.classList.contains('on')`, 15000));

  // ---- Preferences and My Playlists (read-only here: saving or editing them would change your files;
  // they're tested in a sandbox copy of the app)
  await js(`location.hash = 'prefs'`);
  const real = await js(`window.pywebview.api.get_settings()`);
  check("Preferences show the real settings", await waitFor(`!!document.getElementById('musicFolder')`, 10000) &&
    await js(`document.getElementById('musicFolder').textContent === ${JSON.stringify(real.folder)} &&
      [...document.querySelectorAll('[aria-label=format] button')].find(b => b.getAttribute('aria-pressed') === 'true')?.textContent.toLowerCase() === ${JSON.stringify(real.settings.format)}`),
    real.folder);
  check("bad settings are refused", (await js(`window.pywebview.api.save_settings({format: 'wav'}).then(() => 'saved', e => e.message)`)).includes("can't set"));
  await js(`location.hash = 'playlists'`);
  const nLists = await js(`window.pywebview.api.load('data').then(d => d.playlists.length)`);
  check("My Playlists shows each saved playlist", await waitFor(`document.querySelectorAll('#plist .pcard').length === ${nLists}`, 10000), `${nLists} playlists`);
  if (nLists) check("the first playlist gets checked for new songs", await waitFor(`!/Checking/.test(document.querySelector('#plist .pcard .chip').textContent)`, 90000),
    await js(`document.querySelector('#plist .pcard').innerText.split(String.fromCharCode(10)).join(' | ')`));

  // preferences survive a restart: the theme comes back from Python even if WebView2 lost the page's copy
  await js(`window.pywebview.api.set_theme('light').then(() => localStorage.removeItem('phx-theme'))`);
  ws.close(); await stop(app); await sleep(1000);
  app = launch();
  ({ ws, js, waitFor } = await connect());
  check("theme choice is remembered after a restart", await waitFor(`document.documentElement.dataset.theme === 'light'`),
    await js(`'stored=' + localStorage.getItem('phx-theme') + ' theme=' + document.documentElement.dataset.theme + ' ' + location.href`));
  await js(`window.pywebview.api.set_theme(${JSON.stringify(userTheme)}).then(() => localStorage.setItem('phx-theme', ${JSON.stringify(userTheme)}))`);
  check("second start is fast (covers cached)", await waitFor(`document.querySelectorAll('#view .tile, #view .art').length > 0`, 5000));
  ws.close();
} catch (e) {
  check("test ran to the end", false, e.message);
} finally {
  await stop(app);
}
const passed = results.filter(Boolean).length;
console.log(`\n${passed}/${results.length} passed`);
process.exit(passed === results.length ? 0 : 1);
