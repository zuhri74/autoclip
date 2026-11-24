"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const fileUrl = p => p ? "/file?p=" + encodeURIComponent(p) : "";
const ytId = u => (String(u).match(/(?:v=|youtu\.be\/|\/shorts\/|\/live\/)([\w-]{11})/) || [])[1];

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2400);
}
async function copy(text, what = "Teks") {
  try { await navigator.clipboard.writeText(text); }
  catch { const ta = document.createElement("textarea"); ta.value = text; document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove(); }
  toast(what + " disalin ✓");
}
const fmtViews = v => v >= 1e6 ? (v / 1e6).toFixed(1) + " jt" : v >= 1e3 ? Math.round(v / 1e3) + " rb" : String(v || 0);
const fmtDur = s => { s = Math.round(s || 0); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), d = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(d).padStart(2, "0")}` : `${m}:${String(d).padStart(2, "0")}`; };
const fmtDate = t => new Date(t * 1000).toLocaleString("id-ID", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});

let META = {}, SET = {}, JOBSEEN = 0, LASTJOB = null;
const GENRE_ICO = {otomatis: "✨", podcast: "🎙️", gaming: "🎮", reaction: "😲", masak: "🍳", tutorial: "📚", tutorial_pc: "💻",
  interview: "🗣️", fitness: "💪", kendaraan: "🚗", unboxing: "📦", review: "⭐", musik: "🎵", sport: "⚽", komedi: "😂",
  presentasi: "📊", vlog: "📹", hiburan: "🎉", berita: "📰", action: "💥", ekstrem: "🛹", balapan: "🏁", anime: "🌸",
  horor: "👻", sinematik: "🎬"};
const MAIN_GENRES = ["otomatis", "gaming", "podcast", "reaction", "komedi", "vlog", "masak", "tutorial", "action", "sport"];
const FOCUS_SHORT = {otomatis: "✨ Otomatis", action: "💥 Aksi", lucu: "😂 Lucu", emosional: "😢 Emosional", informatif: "💡 Informatif"};

/* ============================================================ navigasi */
function go(page) {
  $$("#nav a").forEach(a => a.classList.toggle("on", a.dataset.page === page));
  $$(".page").forEach(p => p.classList.toggle("hide", p.id !== "page-" + page));
  if (page === "hasil") loadResults();
  if (page === "campaign") loadCampaigns();
  if (page === "pengaturan") loadFix();
  window.scrollTo(0, 0);
  history.replaceState(null, "", "#" + page);
}
$$("#nav a").forEach(a => a.onclick = () => go(a.dataset.page));
$("#jobpill").onclick = () => go(LASTJOB?.kind === "campaign" ? "campaign" : "buat");

/* ============================================================ isi pilihan */
function fillSelect(sel, items, val) {
  sel.innerHTML = items.map(it => typeof it === "string" ? `<option>${esc(it)}</option>`
    : `<option value="${esc(it.id)}">${esc(it.label)}</option>`).join("");
  if (val !== undefined) sel.value = val;
}
function renderGenres(all) {
  const list = META.genres.filter(g => all || MAIN_GENRES.includes(g.id) || g.id === SET.genre);
  $("#genres").innerHTML = list.map(g => `<div class="genre ${g.id === SET.genre ? "on" : ""}" data-id="${g.id}">
    <i>${GENRE_ICO[g.id] || "🎞️"}</i><span>${esc(g.id === "otomatis" ? "Otomatis" : g.label.split(" / ")[0])}</span></div>`).join("");
  $$(".genre").forEach(el => el.onclick = () => { SET.genre = el.dataset.id; $$(".genre").forEach(x => x.classList.toggle("on", x === el)); summary(); });
  $("#more-genres").textContent = all ? "Tampilkan lebih sedikit ▴" : "Tampilkan semua jenis ▾";
  $("#more-genres").dataset.all = all ? "1" : "";
}
$("#more-genres").onclick = () => renderGenres(!$("#more-genres").dataset.all);
function renderFocus() {
  $("#focus").innerHTML = META.focus.map(f => `<button data-id="${f.id}" class="${f.id === SET.focus ? "on" : ""}" title="${esc(f.label)}">${FOCUS_SHORT[f.id] || esc(f.label)}</button>`).join("");
  $$("#focus button").forEach(b => b.onclick = () => { SET.focus = b.dataset.id; $$("#focus button").forEach(x => x.classList.toggle("on", x === b)); summary(); });
}
function renderCampaignSelect() {
  fillSelect($("#campaign"), [{id: "", label: "Tanpa campaign"}, ...META.campaigns.map(c => ({id: c, label: c}))], SET.campaign || "");
  campMini();
}
async function campMini() {
  const id = $("#campaign").value, box = $("#camp-mini");
  SET.campaign = id;
  if (!id) { box.classList.add("hide"); summary(); return; }
  try {
    const c = await api("/api/campaign?id=" + encodeURIComponent(id));
    box.innerHTML = (c.logo_url ? `<img src="${fileUrl(c.logo_url)}">` : "🎯") +
      `<div><b>${esc(c.name || id)}</b><div class="muted small">${esc(c.summary || "")}</div></div>`;
    box.classList.remove("hide");
  } catch { box.classList.add("hide"); }
  summary();
}
$("#campaign").onchange = campMini;

function summary() {
  const n = +$("#n_clips").value || 5, d = +$("#duration").value;
  const g = META.genres?.find(x => x.id === SET.genre);
  $("#quick-summary").textContent = `${n} klip × ${d < 60 ? d + " detik" : (d / 60).toFixed(d % 60 ? 1 : 0) + " menit"} · ${g ? g.label.split(" (")[0] : ""}` +
    (SET.campaign ? " · campaign " + SET.campaign : "");
}
$("#duration").oninput = () => { const d = +$("#duration").value; $("#dur-lbl").textContent = d < 60 ? d + " detik" : d % 60 ? `${Math.floor(d / 60)} menit ${d % 60} detik` : d / 60 + " menit"; summary(); };
$$(".stepper button").forEach(b => b.onclick = () => { const i = $("#n_clips"); i.value = Math.min(30, Math.max(1, (+i.value || 1) + +b.dataset.d)); summary(); });
$("#n_clips").oninput = summary;
$("#sources").oninput = () => { const n = $("#sources").value.split("\n").filter(x => x.trim()).length; $("#src-count").textContent = n ? n + " video" : ""; };

function addSources(list) {
  const cur = $("#sources").value.split("\n").map(x => x.trim()).filter(Boolean);
  for (const s of list) if (!cur.includes(s)) cur.push(s);
  $("#sources").value = cur.join("\n");
  $("#sources").oninput();
}
$("#btn-pickvid").onclick = async () => {
  toast("Jendela pilih file dibuka…");
  const r = await api("/api/pick", {kind: "video"}).catch(e => toast(e.message));
  if (r?.files?.length) addSources(r.files);
};

/* ============================================================ pengaturan */
const S_KEYS = ["handle", "logo", "logo_pos", "cta", "voice", "brand", "fmt", "look", "text_color", "cam_pos", "language", "ai",
  "profile", "sub_model", "strict", "desc_langs", "out_dir", "cookies"];
const S_BOOL = ["intro", "subtitles", "jumpcut", "meme_sfx", "top_text", "compile", "ai_thumb"];
function fillSettings() {
  fillSelect($("#s-logo_pos"), META.logo_pos); fillSelect($("#s-voice"), META.voices);
  fillSelect($("#s-brand"), META.brand_modes.map(b => ({id: b, label: b === "mati" ? "Tidak" : b[0].toUpperCase() + b.slice(1)})));
  fillSelect($("#s-fmt"), META.formats); fillSelect($("#s-look"), META.looks); fillSelect($("#s-text_color"), META.text_colors);
  fillSelect($("#s-ai"), META.ai_modes); fillSelect($("#s-profile"), META.profiles);
  fillSelect($("#s-sub_model"), META.sub_models.map(m => ({id: m, label: m === "otomatis" ? "Otomatis (disarankan)" : m})));
  fillSelect($("#s-strict"), META.strict.map(s => ({id: s, label: {ketat: "Ketat – hanya momen terbaik", normal: "Normal", longgar: "Longgar – lebih banyak klip"}[s] || s})));
  fillSelect($("#s-desc_langs"), META.desc_langs);
  for (const k of S_KEYS) { const el = $("#s-" + k); if (el && SET[k] !== undefined && SET[k] !== null) el.value = SET[k]; }
  for (const k of S_BOOL) { const el = $("#s-" + k); if (el) el.checked = !!SET[k]; }
  $("#key-state").textContent = SET.api_key_set ? "(tersimpan ✓)" : "(belum diisi)";
  $("#gkey-state").textContent = SET.gemini_key_set ? "(tersimpan ✓)" : "(belum diisi)";
}
function collectSettings() {
  const s = {genre: SET.genre, focus: SET.focus, campaign: $("#campaign").value,
    n_clips: +$("#n_clips").value || 5, duration: +$("#duration").value || 60};
  for (const k of S_KEYS) { const el = $("#s-" + k); if (el) s[k] = el.value; }
  for (const k of S_BOOL) { const el = $("#s-" + k); if (el) s[k] = el.checked; }
  s.outro = s.intro;
  s.api_key = $("#s-api_key").value.trim();
  s.gemini_key = $("#s-gemini_key").value.trim();
  return s;
}
$("#btn-save").onclick = async () => {
  await api("/api/settings", collectSettings());
  if ($("#s-api_key").value.trim()) { SET.api_key_set = true; $("#s-api_key").value = ""; $("#key-state").textContent = "(tersimpan ✓)"; }
  if ($("#s-gemini_key").value.trim()) { SET.gemini_key_set = true; $("#s-gemini_key").value = ""; $("#gkey-state").textContent = "(tersimpan ✓)"; }
  toast("Pengaturan disimpan ✓");
};
$$("[data-pick]").forEach(b => b.onclick = async () => {
  const r = await api("/api/pick", {kind: b.dataset.pick, multi: false}).catch(e => toast(e.message));
  if (r?.files?.[0]) $("#" + b.dataset.to).value = r.files[0];
});
$$("#set-tabs button").forEach(b => b.onclick = () => {
  $$("#set-tabs button").forEach(x => x.classList.toggle("on", x === b));
  $$(".settings").forEach(c => c.classList.toggle("hide", c.id !== "set-" + b.dataset.t));
});
async function loadFix() { try { $("#fix-text").value = (await api("/api/fix")).text; } catch {} }
$("#btn-fix").onclick = async () => { await api("/api/fix", {text: $("#fix-text").value}); $("#fix-msg").textContent = "Tersimpan – dipakai di proses berikutnya."; toast("Koreksi kata disimpan ✓"); };

/* ============================================================ proses */
$("#btn-start").onclick = async () => {
  const sources = $("#sources").value.split("\n").map(x => x.trim()).filter(Boolean);
  if (!sources.length) { toast("Tempel link video atau pilih file dulu"); $("#sources").focus(); return; }
  try {
    await api("/api/start", {sources, settings: collectSettings()});
    JOBSEEN = 0; $("#log").textContent = ""; showRun(true); poll();
  } catch (e) { toast(e.message); }
};
$("#btn-cancel").onclick = () => api("/api/cancel", {}).then(() => toast("Membatalkan…"));
$("#toggle-log").onclick = () => { const l = $("#log"); l.classList.toggle("hide"); $("#toggle-log").textContent = l.classList.contains("hide") ? "Lihat detail proses ▾" : "Sembunyikan detail ▴"; };

function showRun(on) {
  $("#prog-idle").classList.toggle("hide", on); $("#prog-run").classList.toggle("hide", !on);
  if (on) $("#prog-done").classList.add("hide");
  $("#btn-start").disabled = on;
}
let pollT = null;
async function poll() {
  clearTimeout(pollT);
  let j;
  try { j = await api("/api/job?since=" + JOBSEEN); } catch { pollT = setTimeout(poll, 2000); return; }
  LASTJOB = j;
  if (j.logs.length) {
    const L = $("#log"); L.textContent += j.logs.join("\n") + "\n"; L.scrollTop = L.scrollHeight;
    JOBSEEN = j.log_count;
    const meaningful = j.logs.map(s => s.trim()).filter(s => s && !/^\d+(\.\d+)?%/.test(s));
    if (meaningful.length) $("#prog-last").textContent = meaningful.slice(-2).join("\n");
  }
  const pct = Math.round(j.progress * 100);
  $("#jobpill").classList.toggle("hide", !j.running);
  $("#jp-title").textContent = j.kind === "campaign" ? "Membaca brief…" : "Membuat klip…";
  $("#jp-pct").textContent = pct + "%";
  if (j.kind === "clip") {
    if (j.running) {
      showRun(true);
      $("#prog-bar").style.width = pct + "%"; $("#prog-pct").textContent = pct + "%";
      $("#prog-stage").textContent = (j.stage || "Menyiapkan…").replace(/Video 1\/1 · /, "");
      $("#prog-time").textContent = "⏱ " + fmtDur(j.elapsed);
    } else if (j.result || j.error) finishClip(j);
  } else if (j.kind === "campaign") {
    campJob(j);
  }
  if (j.running) pollT = setTimeout(poll, 1200);
}
let finishedShown = null;
async function finishClip(j) {
  $("#btn-start").disabled = false;
  const key = j.elapsed + (j.error || "") + JSON.stringify(j.result || {});
  if (finishedShown === key) return; finishedShown = key;
  $("#prog-run").classList.add("hide"); $("#prog-idle").classList.add("hide");
  const box = $("#prog-done"); box.classList.remove("hide");
  if (j.error) {
    box.innerHTML = `<div class="alert">${j.cancelled ? "Proses dibatalkan." : "Gagal: " + esc(j.error)}</div>
      <pre class="log">${esc($("#log").textContent.slice(-4000))}</pre>
      <button class="btn ghost" onclick="resetProg()">Tutup</button>`;
    return;
  }
  const folders = j.result?.folders || [];
  let clips = [];
  for (const f of folders) { try { const r = await api("/api/result?folder=" + encodeURIComponent(f)); clips.push(...r.clips.map(c => ({...c, folder: f}))); } catch {} }
  box.innerHTML = `<div class="done-head"><div class="ok">✓</div><div><h3>${clips.length} klip siap upload!</h3>
      <div class="muted small">Selesai dalam ${fmtDur(j.elapsed)}</div></div></div>
    <div class="mini-clips">${clips.slice(0, 6).map(c => `<div class="row gap" style="margin-bottom:8px">
      <img src="${fileUrl(c.poster || c.thumb)}" style="width:44px;height:78px;object-fit:cover;border-radius:6px;background:#222">
      <div class="grow"><b class="small">${esc(c.title)}</b><div><span class="score">★ ${c.score}</span> <span class="muted small">${fmtDur(c.duration)}</span></div></div></div>`).join("")}</div>
    <div class="row gap wrap" style="margin-top:12px">
      ${folders[0] ? `<button class="btn primary" id="open-res">Lihat &amp; salin judul</button>` : ""}
      ${folders[0] ? `<button class="btn ghost" id="open-fold">📂 Buka folder</button>` : ""}
      <button class="btn ghost" onclick="resetProg()">Buat lagi</button></div>`;
  if (folders[0]) {
    $("#open-res").onclick = () => { go("hasil"); openResult(folders[0]); };
    $("#open-fold").onclick = () => api("/api/open", {path: folders[0]});
  }
}
window.resetProg = () => { $("#prog-done").classList.add("hide"); $("#prog-idle").classList.remove("hide"); };

/* ============================================================ hasil */
async function loadResults() {
  $("#hasil-detail").classList.add("hide"); $("#hasil-list").classList.remove("hide");
  const list = await api("/api/results").catch(() => []);
  $("#hasil-list").innerHTML = list.length ? list.map(r => `<div class="rcard" data-f="${esc(r.folder)}">
      <div class="th" style="background-image:url('${fileUrl(r.thumb)}')"><span class="badge">${r.n} klip</span></div>
      <div class="bd"><b>${esc(r.name)}</b><div class="muted small">${fmtDate(r.time)}${r.campaign ? " · 🎯 " + esc(r.campaign) : ""}</div></div></div>`).join("")
    : `<div class="empty">Belum ada hasil. Buat klip pertamamu di menu <b>Buat Klip</b>.</div>`;
  $$("#hasil-list .rcard").forEach(c => c.onclick = () => openResult(c.dataset.f));
}
$("#btn-refresh-hasil").onclick = loadResults;
async function openResult(folder) {
  const r = await api("/api/result?folder=" + encodeURIComponent(folder)).catch(e => { toast(e.message); });
  if (!r) return;
  $("#hasil-list").classList.add("hide");
  const d = $("#hasil-detail"); d.classList.remove("hide");
  d.innerHTML = `<button class="link back" id="res-back">← Semua hasil</button>
    <div class="row between wrap gap" style="margin-bottom:16px"><div><h2 style="margin:0">${esc(r.name)}</h2>
      <div class="muted small">${esc(r.source)}</div></div>
      <button class="btn ghost" id="res-fold">📂 Buka folder</button></div>
    <div class="clips">${r.clips.map((c, i) => clipCard(c, i)).join("")}</div>`;
  $("#res-back").onclick = loadResults;
  $("#res-fold").onclick = () => api("/api/open", {path: r.folder});
  r.clips.forEach((c, i) => {
    const el = $(`#clip-${i}`);
    $$("[data-copy]", el).forEach(b => b.onclick = () => {
      const k = b.dataset.copy;
      const txt = k === "title" ? c.title : k === "desc" ? c.description : k === "tags" ? c.tags.join(" ") : (c.translations[k] ? c.translations[k].join ? c.translations[k].join("\n\n") : JSON.stringify(c.translations[k]) : "");
      copy(txt, k === "title" ? "Judul" : k === "desc" ? "Deskripsi" : k === "tags" ? "Hashtag" : "Teks");
    });
    $("[data-open]", el)?.addEventListener("click", () => api("/api/open", {path: c.file}));
  });
  window.scrollTo(0, 0);
}
function clipCard(c, i) {
  const ck = (c.campaign_check || []);
  const bad = ck.filter(x => x.startsWith("✗")).length;
  return `<div class="clip" id="clip-${i}">
    <video src="${fileUrl(c.file)}" poster="${fileUrl(c.cover || c.poster)}" controls preload="none"></video>
    <div class="meta">
      <div class="row gap wrap"><span class="score">★ ${c.score}</span><span class="tag">${esc(c.category)}</span>
        <span class="muted small">${fmtDur(c.duration)} · dari menit ${fmtDur(c.start)}</span></div>
      <h3 style="margin-top:8px">${esc(c.clip_title)}</h3>
      <div class="muted small">${esc(c.reason)}</div>
      <div class="copybox"><div class="hd">Judul <button class="btn ghost sm" data-copy="title">Salin</button></div><div class="tx">${esc(c.title)}</div></div>
      <div class="copybox"><div class="hd">Deskripsi <button class="btn ghost sm" data-copy="desc">Salin</button></div><div class="tx">${esc(c.description)}</div></div>
      <div class="row gap wrap"><button class="btn ghost sm" data-copy="tags">Salin hashtag</button>
        <a class="btn ghost sm" href="${fileUrl(c.file)}" download>⬇ Unduh video</a>
        ${c.thumb ? `<a class="btn ghost sm" href="${fileUrl(c.thumb)}" download>🖼 Thumbnail</a>` : ""}
        ${c.cover ? `<a class="btn ghost sm" href="${fileUrl(c.cover)}" download>🖼 Cover Shorts</a>` : ""}</div>
      ${ck.length ? `<details ${bad ? "open" : ""}><summary>Checklist campaign ${bad ? `(<span style="color:var(--bad)">${bad} belum sesuai</span>)` : "✓"}</summary>
        <div class="checklist">${ck.map(x => `<div class="${x.startsWith("✓") ? "ok" : x.startsWith("✗") ? "no" : ""}">${esc(x)}</div>`).join("")}</div></details>` : ""}
    </div></div>`;
}

/* ============================================================ campaign */
async function loadCampaigns() {
  $("#camp-detail").classList.add("hide"); $("#camp-list").classList.remove("hide");
  const list = await api("/api/campaigns").catch(() => []);
  META.campaigns = list.map(c => c.id); renderCampaignSelect();
  $("#camp-list").innerHTML = list.length ? list.map(c => `<div class="rcard" data-id="${esc(c.id)}">
      <div class="th" style="background:#3d4058 ${c.logo_url ? `url('${fileUrl(c.logo_url)}') center/60% no-repeat` : ""}">${c.logo_url ? "" : '<div style="display:grid;place-items:center;height:100%;font-size:40px">🎯</div>'}</div>
      <div class="bd"><b>${esc(c.name || c.id)}</b><div class="muted small">${esc(c.deadline ? "Deadline " + c.deadline : "")}</div>
      <div style="margin-top:6px">${(c.hashtags || []).slice(0, 3).map(h => `<span class="tag">${esc(h)}</span>`).join("")}</div></div></div>`).join("")
    : `<div class="empty">Belum ada campaign. Klik <b>Upload brief</b> lalu pilih PDF / foto brief dari brand.</div>`;
  $$("#camp-list .rcard").forEach(c => c.onclick = () => openCampaign(c.dataset.id));
}
$("#btn-brief").onclick = async () => {
  const r = await api("/api/pick", {kind: "brief"}).catch(e => toast(e.message));
  if (!r?.files?.length) return;
  try {
    await api("/api/campaign/analyze", {files: r.files});
    JOBSEEN = 0; $("#log").textContent = "";
    $("#camp-list").innerHTML = `<div class="card" style="grid-column:1/-1"><div class="row gap"><div class="spin" style="border-color:var(--line);border-top-color:var(--brand)"></div>
      <div><b>Membaca brief…</b><div class="muted small" id="camp-prog">Teks, aturan, logo &amp; video sumber</div></div></div></div>`;
    poll();
  } catch (e) { toast(e.message); }
};
let campDoneKey = null;
function campJob(j) {
  const p = $("#camp-prog");
  if (p && j.running) p.textContent = $("#prog-last").textContent || "Menganalisa…";
  if (!j.running && (j.result || j.error)) {
    const key = j.elapsed + (j.error || "");
    if (campDoneKey === key) return; campDoneKey = key;
    if (j.error) { toast("Gagal membaca brief: " + j.error); loadCampaigns(); return; }
    toast("Brief selesai dibaca ✓");
    loadCampaigns().then(() => openCampaign(j.result.campaign));
  }
}
async function openCampaign(id) {
  const c = await api("/api/campaign?id=" + encodeURIComponent(id)).catch(e => toast(e.message));
  if (!c) return;
  $("#camp-list").classList.add("hide");
  const d = $("#camp-detail"); d.classList.remove("hide");
  const li = a => (a || []).length ? `<ul class="list-ul">${a.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : '<span class="muted">-</span>';
  d.innerHTML = `<button class="link back" id="camp-back">← Semua campaign</button>
   <div class="card"><div class="camp-hero">${c.logo_url ? `<img src="${fileUrl(c.logo_url)}">` : '<div style="font-size:44px">🎯</div>'}
     <div class="grow"><h2 style="margin:0">${esc(c.name || id)}</h2><div class="muted">${esc(c.summary)}</div></div>
     <div class="row gap wrap"><button class="btn primary" id="camp-use">Pakai campaign ini</button><button class="btn ghost" id="camp-edit">✏️ Edit</button>
     <button class="btn ghost danger" id="camp-del">Hapus</button></div></div>
     <dl class="kv">
      <dt>Brand</dt><dd>${esc(c.brand || "-")}</dd><dt>Topik / game</dt><dd>${esc(c.topic || "-")}</dd>
      <dt>Platform</dt><dd>${esc((c.platforms || []).join(", ") || "-")}</dd><dt>Deadline</dt><dd>${esc(c.deadline || "-")}</dd>
      <dt>Hashtag wajib</dt><dd>${(c.hashtags || []).map(h => `<span class="tag">${esc(h)}</span>`).join("") || "-"}</dd>
      <dt>Tag akun</dt><dd>${esc((c.mentions || []).join(" ") || "-")}</dd>
      <dt>CTA</dt><dd>${esc(c.cta || "-")}</dd><dt>Tulisan di video</dt><dd>${esc(c.overlay_text || "-")}</dd>
      <dt>Momen yang diambil</dt><dd>${esc(c.content_brief || "-")}</dd>
      <dt>Larangan</dt><dd>${li(c.banned)}</dd><dt>Aturan lain</dt><dd>${li(c.rules)}</dd>
     </dl></div>
   <div class="card"><div class="row between wrap gap"><div><h3>Video sumber yang cocok</h3><div class="muted small">Link dari brief paling atas, lalu hasil pencarian YouTube sesuai topik campaign.</div></div>
     <div class="row gap"><button class="btn ghost" id="camp-search">🔎 Cari ulang</button><button class="btn primary" id="camp-addall">Pakai semua</button></div></div>
     <div class="vlist" id="camp-vids" style="margin-top:14px"></div></div>`;
  $("#camp-back").onclick = loadCampaigns;
  $("#camp-use").onclick = () => { SET.campaign = id; renderCampaignSelect(); go("buat"); toast("Campaign dipilih ✓"); };
  $("#camp-del").onclick = async () => { if (!confirm(`Hapus campaign "${id}"?`)) return; await api("/api/campaign/delete", {id}); loadCampaigns(); };
  $("#camp-edit").onclick = () => editCampaign(c);
  const show = vids => {
    $("#camp-vids").innerHTML = vids.length ? vids.map(videoItem).join("") : '<div class="empty">Belum ada. Klik Cari ulang.</div>';
    bindVideoItems($("#camp-vids"), () => { SET.campaign = id; renderCampaignSelect(); });
    $("#camp-addall").onclick = () => { addSources(vids.filter(v => !v.done).map(v => v.url)); SET.campaign = id; renderCampaignSelect(); toast("Video ditambahkan ke Buat Klip ✓"); };
  };
  show(c.video_suggestions || []);
  $("#camp-search").onclick = async () => {
    $("#camp-vids").innerHTML = '<div class="empty">Mencari video… (±30 detik)</div>';
    try { show((await api("/api/campaign/videos", {id})).videos); } catch (e) { toast(e.message); show([]); }
  };
}
function editCampaign(c) {
  const f = (k, label, v, area) => `<label class="field"><span>${label}</span>${area ? `<textarea data-k="${k}" rows="3">${esc(v)}</textarea>` : `<input data-k="${k}" value="${esc(v)}">`}</label>`;
  $("#mbody").innerHTML = `<h2 style="margin-top:0">Edit campaign</h2>
    ${f("brand", "Brand", c.brand)}${f("topic", "Topik / game", c.topic)}
    ${f("hashtags", "Hashtag wajib (pisah spasi)", (c.hashtags || []).join(" "))}${f("mentions", "Tag akun", (c.mentions || []).join(" "))}
    ${f("cta", "CTA", c.cta)}${f("overlay_text", "Tulisan di video", c.overlay_text)}${f("deadline", "Deadline", c.deadline)}
    ${f("content_brief", "Momen yang diambil", c.content_brief, 1)}${f("banned", "Larangan (satu per baris)", (c.banned || []).join("\n"), 1)}
    <label class="field"><span>Logo</span><div class="row gap"><input data-k="logo_file" value="${esc(c.logo_file || "")}"><button class="btn ghost" id="ce-logo">Pilih…</button></div></label>
    <div class="row gap"><button class="btn primary" id="ce-save">Simpan</button><button class="btn ghost" id="ce-cancel">Batal</button></div>`;
  $("#modal").classList.remove("hide");
  $("#ce-logo").onclick = async () => { const r = await api("/api/pick", {kind: "logo", multi: false}); if (r.files[0]) $('[data-k="logo_file"]').value = r.files[0]; };
  $("#ce-cancel").onclick = closeModal;
  $("#ce-save").onclick = async () => {
    const body = {id: c.id}; $$("#mbody [data-k]").forEach(el => body[el.dataset.k] = el.value);
    await api("/api/campaign/save", body).catch(e => toast(e.message));
    closeModal(); toast("Campaign disimpan ✓"); openCampaign(c.id);
  };
}
function closeModal() { $("#modal").classList.add("hide"); }
$("#mclose").onclick = closeModal;
$("#modal").onclick = e => { if (e.target.id === "modal") closeModal(); };

/* ============================================================ video list (viral & campaign) */
function videoItem(v) {
  const id = ytId(v.url);
  return `<div class="vitem ${v.done ? "done" : ""}"><div class="th" style="${id ? `background-image:url('https://i.ytimg.com/vi/${id}/mqdefault.jpg')` : ""}">
    ${v.sec ? `<span class="badge" style="position:absolute;right:6px;bottom:6px">${fmtDur(v.sec)}</span>` : ""}</div>
    <div class="bd"><div class="t">${esc(v.title || v.url)}</div>
    <div class="muted small">${esc(v.channel || "")}${v.views ? " · " + fmtViews(v.views) + " ditonton" : ""}${v.done ? " · ✓ sudah diunduh" : ""}</div>
    ${v.source ? `<div class="muted small">${esc(v.source)}</div>` : ""}
    <div class="act"><button class="btn primary sm" data-add="${esc(v.url)}">+ Pakai</button>
    <a class="btn ghost sm" href="${esc(v.url)}" target="_blank" rel="noopener">Tonton</a></div></div></div>`;
}
function bindVideoItems(root, extra) {
  $$("[data-add]", root).forEach(b => b.onclick = () => { addSources([b.dataset.add]); extra && extra(); b.textContent = "✓ Ditambahkan"; b.disabled = true; toast("Ditambahkan ke Buat Klip ✓"); });
}
$("#btn-viral").onclick = async () => {
  $("#viral-list").innerHTML = '<div class="empty">Mencari video yang sedang ramai… (±20 detik)</div>';
  try {
    const r = await api("/api/viral", {country: $("#v-country").value, cat: $("#v-cat").value, period: $("#v-period").value, n: 12});
    $("#viral-list").innerHTML = r.videos.length ? r.videos.map(videoItem).join("") : '<div class="empty">Tidak ada hasil. Coba kategori / periode lain.</div>';
    bindVideoItems($("#viral-list"));
  } catch (e) { $("#viral-list").innerHTML = `<div class="empty">Gagal: ${esc(e.message)}</div>`; }
};

/* ============================================================ mulai */
(async function init() {
  [META, SET] = await Promise.all([api("/api/meta"), api("/api/settings")]);
  SET.genre = SET.genre || "otomatis"; SET.focus = SET.focus || "otomatis";
  renderGenres(false); renderFocus(); renderCampaignSelect(); fillSettings();
  $("#n_clips").value = SET.n_clips || 5;
  $("#duration").value = Math.round(SET.duration || 60); $("#duration").oninput();
  fillSelect($("#v-country"), META.viral_countries); fillSelect($("#v-cat"), META.viral_cats);
  fillSelect($("#v-period"), META.viral_periods, "Minggu ini");
  const hw = META.hw || {};
  $("#hwinfo").innerHTML = `${esc(hw.cpu || "")}<br>${hw.ram ? hw.ram + " GB RAM" : ""}${hw.cuda ? " · GPU NVIDIA ✓" : ""}`;
  summary();
  const h = location.hash.slice(1);
  if (h && $("#page-" + h)) go(h);
  poll();
})();
