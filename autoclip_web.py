"""AutoClip Web - tampilan website untuk AutoClip (berjalan lokal di komputer ini).

Jalankan:  python autoclip_web.py      -> browser terbuka otomatis di http://127.0.0.1:8765
Semua pemrosesan memakai mesin yang sama dengan autoclip.py.
"""
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from dataclasses import asdict, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autoclip as A  # noqa: E402

HOST, PORT = "127.0.0.1", 8765
WEB_DIR = os.path.join(A.APP_DIR, "web")
SETTINGS_FILE = os.path.join(A.APP_DIR, "web_settings.json")
HASIL_DIR = os.path.join(A.APP_DIR, "hasil")
RECENT_FILE = os.path.join(A.APP_DIR, "_cache", "web_hasil.json")


def recent_dirs():
    try:
        with open(RECENT_FILE, encoding="utf-8") as f:
            return [d for d in json.load(f) if os.path.isdir(d)]
    except Exception:
        return []


def add_recent(dirs):
    cur = [d for d in recent_dirs() if d not in dirs]
    os.makedirs(os.path.dirname(RECENT_FILE), exist_ok=True)
    with open(RECENT_FILE, "w", encoding="utf-8") as f:
        json.dump((list(dirs) + cur)[:300], f, ensure_ascii=False)

# pengaturan yang boleh diubah dari web (kunci = nama field Options)
OPT_FIELDS = {f.name for f in fields(A.Options)} - {"keep_core", "out_dir"}


# ============================================================================ pengaturan

def import_old_config():
    """Ambil pengaturan dari aplikasi lama (autoclip_config.json berisi label tampilan) -> kunci Options."""
    old = A.load_config()
    if not old:
        return {}
    maps = dict(fmt=A.FORMATS, style=A.STYLES, profile=A.PROFILES, ai=A.AI_MODES, look=A.LOOKS, voice=A.VOICE_LABEL,
                desc_langs=A.DESC_LANGS, genre={k: g["label"] for k, g in A.GENRES.items()},
                focus={k: v[0] for k, v in A.FOCUS.items()}, text_color={k: v[0] for k, v in A.TEXT_COLORS.items()})
    d = {}
    for k, v in old.items():
        if k in maps:
            hit = next((key for key, lab in maps[k].items() if lab == v or key == v), None)
            if hit is not None:
                d[k] = hit
        elif k in OPT_FIELDS or k == "compile":
            d[k] = v
    if d.get("cookies") == "tanpa":
        d["cookies"] = ""
    if "intro" in d:
        d["outro"] = d["intro"]
    if d.get("campaign") and d["campaign"] not in A.campaign_list():
        d["campaign"] = ""
    return d


def load_settings():
    d = {k: v for k, v in asdict(A.Options()).items() if k in OPT_FIELDS}
    d.update(duration=60.0, n_clips=5, compile=False, out_dir="")
    if not os.path.exists(SETTINGS_FILE):
        d.update(import_old_config())
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            d.update(json.load(f))
    except Exception:
        pass
    return d


def save_settings(d):
    cur = load_settings()
    cur.update({k: v for k, v in d.items() if k in OPT_FIELDS or k in ("compile", "out_dir")})
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=1)
    return cur


def make_options(s):
    base = asdict(A.Options())
    for k in OPT_FIELDS:
        if k in s and s[k] is not None:
            t = type(base[k]) if base[k] is not None else str
            try:
                base[k] = t(s[k]) if t in (int, float, bool, str) else s[k]
            except (TypeError, ValueError):
                pass
    base["n_clips"] = max(1, int(base["n_clips"]))
    base["duration"] = max(10.0, float(base["duration"]))
    base["out_dir"] = (s.get("out_dir") or "").strip() or None
    if base["campaign"] in ("", "(tanpa campaign)"):
        base["campaign"] = ""
    return A.Options(**base)


# ============================================================================ pekerjaan latar belakang

class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset("")

    def reset(self, kind):
        self.kind, self.running, self.progress, self.stage = kind, False, 0.0, ""
        self.logs, self.result, self.error, self.t0, self.cancelled = [], None, "", time.time(), False

    def log(self, m):
        with self.lock:
            for ln in str(m).splitlines() or [""]:
                self.logs.append(ln)
            del self.logs[:-3000]

    def prog(self, p, st=""):
        self.progress, self.stage = max(0.0, min(1.0, float(p))), st or self.stage

    def start(self, kind, fn):
        if self.running:
            return False
        self.reset(kind)
        self.running = True
        A.CANCEL.clear()

        def run():
            try:
                self.result = fn()
                self.progress = 1.0
            except A.Cancelled:
                self.cancelled, self.error = True, "Dibatalkan"
                self.log("Dibatalkan oleh pengguna.")
            except Exception as ex:
                self.error = str(ex)[:500]
                self.log("GAGAL: " + self.error)
                self.log(traceback.format_exc()[-1500:])
            finally:
                self.running = False
        threading.Thread(target=run, daemon=True).start()
        return True

    def state(self, since=0):
        with self.lock:
            logs = self.logs[since:]
            n = len(self.logs)
        return dict(kind=self.kind, running=self.running, progress=self.progress, stage=self.stage, logs=logs,
                    log_count=n, result=self.result, error=self.error, elapsed=round(time.time() - self.t0),
                    cancelled=self.cancelled)


JOB = Job()


# ============================================================================ hasil

def _rel(p):
    return p.replace("\\", "/")


def clip_info(folder, c):
    def f(name):
        p = os.path.join(folder, name) if name else ""
        return _rel(p) if p and os.path.exists(p) else ""
    base = os.path.splitext(c.get("file", ""))[0]
    return dict(file=f(c.get("file")), thumb=f(c.get("thumbnail") or base + "_thumbnail.jpg"),
                cover=f(c.get("cover") or base + "_cover_shorts.jpg"), poster=f(base + ".jpg"),
                kit=f(base + "_youtube.txt"), title=c.get("yt_title") or c.get("title", ""),
                clip_title=c.get("title", ""), description=c.get("description", ""), score=c.get("score", 0),
                category=c.get("category", ""), reason=c.get("reason", ""), duration=c.get("duration", 0),
                start=c.get("start", 0), tags=c.get("hashtags", []), translations=c.get("translations", {}),
                campaign_check=c.get("campaign_check", []))


def read_result(folder):
    fp = os.path.join(folder, "laporan.json")
    try:
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    clips = [clip_info(folder, c) for c in d.get("clips", [])]
    return dict(folder=_rel(folder), name=os.path.basename(folder), source=d.get("source", ""),
                time=os.path.getmtime(fp), clips=clips, campaign=(d.get("options") or {}).get("campaign", ""))


def list_results(limit=60):
    roots = {HASIL_DIR}
    od = (load_settings().get("out_dir") or "").strip()
    if od:
        roots.add(od)
    out = [(os.path.getmtime(os.path.join(d, "laporan.json")), d) for d in recent_dirs()
           if os.path.isfile(os.path.join(d, "laporan.json"))]
    for r in roots:
        if not os.path.isdir(r):
            continue
        for d in os.listdir(r):
            p = os.path.join(r, d)
            if os.path.isfile(os.path.join(p, "laporan.json")):
                out.append((os.path.getmtime(os.path.join(p, "laporan.json")), p))
            elif os.path.isdir(p):  # banyak video: hasil/<judul>/video_01
                for d2 in os.listdir(p):
                    p2 = os.path.join(p, d2)
                    if os.path.isfile(os.path.join(p2, "laporan.json")):
                        out.append((os.path.getmtime(os.path.join(p2, "laporan.json")), p2))
    seen, uniq = set(), []
    for t, p in sorted(out, reverse=True):
        k = os.path.realpath(p).lower()
        if k not in seen:
            seen.add(k)
            uniq.append((t, p))
    res = []
    for _, p in uniq[:limit]:
        r = read_result(p)
        if r:
            res.append(dict(folder=r["folder"], name=r["name"], time=r["time"], n=len(r["clips"]),
                            thumb=next((c["thumb"] or c["poster"] for c in r["clips"] if c["thumb"] or c["poster"]), ""),
                            campaign=r["campaign"]))
    return res


def allowed_file(p):
    try:
        ap = os.path.realpath(p)
    except Exception:
        return False
    roots = [A.APP_DIR, (load_settings().get("out_dir") or "").strip()] + recent_dirs()
    return any(r and (ap + os.sep).lower().startswith(os.path.realpath(r).lower() + os.sep) for r in roots) \
        and os.path.isfile(ap)


# ============================================================================ campaign

CAMP_FIELDS = ["name", "brand", "topic", "content_brief", "hashtags", "mentions", "cta", "overlay_text", "banned",
               "rules", "platforms", "deadline", "min_sec", "max_sec", "aspect", "focus", "logo_file", "links",
               "desc_must", "title_must", "source_links", "video_suggestions", "language", "analyzed_by"]


def camp_public(nm):
    p = A.campaign_load(nm)
    if not p:
        return None
    d = {k: p.get(k) for k in CAMP_FIELDS if k in p}
    d["id"] = nm
    d["summary"] = A.campaign_summary(p)
    if p.get("logo_file") and os.path.exists(p["logo_file"]):
        d["logo_url"] = _rel(p["logo_file"])
    return d


# ============================================================================ dialog pilih file (di komputer ini)

_DLG = threading.Lock()
PICK_TYPES = {
    "video": [("Video", " ".join("*" + e for e in sorted(A.VIDEO_EXT))), ("Semua file", "*.*")],
    "logo": [("Gambar", "*.png *.jpg *.jpeg *.webp"), ("Semua file", "*.*")],
    "brief": [("Brief (PDF / foto)", "*.pdf *.png *.jpg *.jpeg *.webp *.txt"), ("Semua file", "*.*")],
}


def pick_files(kind, multi=True):
    with _DLG:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            if kind == "folder":
                r = filedialog.askdirectory(parent=root, title="Pilih folder")
                return [r] if r else []
            ft = PICK_TYPES.get(kind, [("Semua file", "*.*")])
            if multi:
                return list(filedialog.askopenfilenames(parent=root, filetypes=ft, title="Pilih file"))
            r = filedialog.askopenfilename(parent=root, filetypes=ft, title="Pilih file")
            return [r] if r else []
        finally:
            root.destroy()


# ============================================================================ metadata pilihan

def meta():
    hw = {}
    try:
        h = A.detect_hw()
        hw = dict(cpu=h.get("cpu"), ram=round(h.get("ram") or 0), cuda=bool(h.get("cuda")), threads=h.get("threads"))
    except Exception:
        pass
    return dict(
        genres=[dict(id=k, label=g["label"]) for k, g in A.GENRES.items()],
        focus=[dict(id=k, label=v[0]) for k, v in A.FOCUS.items()],
        formats=[dict(id=k, label=v) for k, v in A.FORMATS.items()],
        styles=[dict(id=k, label=v) for k, v in A.STYLES.items()],
        profiles=[dict(id=k, label=v) for k, v in A.PROFILES.items()],
        looks=[dict(id=k, label=v) for k, v in A.LOOKS.items()],
        ai_modes=[dict(id=k, label=v) for k, v in A.AI_MODES.items()],
        sub_models=A.SUB_MODELS, logo_pos=A.LOGO_POS,
        voices=[dict(id=k, label=v) for k, v in A.VOICE_LABEL.items()],
        text_colors=[dict(id=k, label=v[0]) for k, v in A.TEXT_COLORS.items()],
        desc_langs=[dict(id=k, label=v) for k, v in A.DESC_LANGS.items()],
        strict=list(A.STRICT), brand_modes=["mati", "awal & akhir", "awal", "akhir"],
        viral_countries=list(A.VIRAL_COUNTRIES), viral_cats=list(A.VIRAL_CATS), viral_periods=list(A.VIRAL_PERIOD),
        campaigns=A.campaign_list(), whisper=A.has_whisper(), hw=hw, fix_file=A.FIX_FILE,
    )


# ============================================================================ HTTP

class H(BaseHTTPRequestHandler):
    server_version = "AutoClipWeb/1.0"

    def log_message(self, *a):
        pass

    # ---- util
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _same_origin(self):
        o = self.headers.get("Origin")
        return not o or o in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}")

    def _file(self, path, ctype=None):
        size = os.path.getsize(path)
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    # ---- GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self._file(os.path.join(WEB_DIR, "index.html"), "text/html")
            if p.startswith("/static/"):
                fp = os.path.realpath(os.path.join(WEB_DIR, p[len("/static/"):]))
                if fp.startswith(os.path.realpath(WEB_DIR)) and os.path.isfile(fp):
                    return self._file(fp)
                return self._json({"error": "tidak ada"}, 404)
            if p == "/file":
                fp = q.get("p", "")
                if allowed_file(fp):
                    return self._file(fp)
                return self._json({"error": "file tidak diizinkan"}, 403)
            if p == "/api/meta":
                return self._json(meta())
            if p == "/api/settings":
                s = load_settings()
                s["api_key_set"] = bool(s.get("api_key"))
                s["api_key"] = ""
                s["gemini_key_set"] = bool(s.get("gemini_key"))
                s["gemini_key"] = ""
                return self._json(s)
            if p == "/api/job":
                return self._json(JOB.state(int(q.get("since", 0))))
            if p == "/api/results":
                return self._json(list_results())
            if p == "/api/result":
                fp = q.get("folder", "")
                r = read_result(fp) if os.path.isdir(fp) else None
                return self._json(r or {"error": "hasil tidak ditemukan"}, 200 if r else 404)
            if p == "/api/campaigns":
                return self._json([camp_public(n) for n in A.campaign_list()])
            if p == "/api/campaign":
                c = camp_public(q.get("id", ""))
                return self._json(c or {"error": "campaign tidak ditemukan"}, 200 if c else 404)
            if p == "/api/fix":
                A.fix_map()
                with open(A.FIX_FILE, encoding="utf-8") as f:
                    return self._json({"text": f.read()})
            return self._json({"error": "tidak ada"}, 404)
        except Exception as ex:
            return self._json({"error": str(ex)[:300]}, 500)

    # ---- POST
    def do_POST(self):
        if not self._same_origin():
            return self._json({"error": "ditolak"}, 403)
        p = urllib.parse.urlparse(self.path).path
        b = self._body()
        try:
            if p == "/api/pick":
                return self._json({"files": pick_files(b.get("kind", "video"), b.get("multi", True))})
            if p == "/api/settings":
                d = dict(b)
                if not d.get("api_key"):
                    d.pop("api_key", None)  # kosong = jangan hapus kunci tersimpan
                if not d.get("gemini_key"):
                    d.pop("gemini_key", None)
                if d.pop("clear_api_key", False):
                    d["api_key"] = ""
                save_settings(d)
                return self._json({"ok": True})
            if p == "/api/start":
                srcs = [x.strip().strip('"') for x in b.get("sources", []) if x and x.strip()]
                bad = [x for x in srcs if not (A.is_url(x) or os.path.isfile(x))]
                if not srcs:
                    return self._json({"error": "Tempel link video atau pilih file video dulu."}, 400)
                if bad:
                    return self._json({"error": "Tidak ditemukan / bukan link: " + ", ".join(bad[:3])}, 400)
                s = save_settings(b.get("settings", {}))
                opt = make_options(s)
                warn = ""
                if opt.ai == "claude" and not (opt.api_key or os.environ.get("ANTHROPIC_API_KEY")):
                    opt.ai, warn = "lokal", "API key Claude belum diisi -> memakai AI lokal."
                comp = bool(s.get("compile")) and len(srcs) > 1

                def work():
                    if warn:
                        JOB.log(warn)
                    results, cmp_ = A.process_many(srcs, opt, JOB.log, JOB.prog, comp)
                    add_recent([os.path.abspath(o) for o, _ in results] + ([os.path.dirname(cmp_)] if cmp_ else []))
                    return dict(folders=[_rel(o) for o, _ in results], compilation=_rel(cmp_) if cmp_ else "")
                ok = JOB.start("clip", work)
                return self._json({"ok": ok} if ok else {"error": "Masih ada proses berjalan."}, 200 if ok else 409)
            if p == "/api/cancel":
                A.CANCEL.set()
                JOB.stage = "Membatalkan..."
                return self._json({"ok": True})
            if p == "/api/campaign/analyze":
                files = [f for f in b.get("files", []) if os.path.isfile(f)]
                if not files:
                    return self._json({"error": "Pilih file brief (PDF / foto) dulu."}, 400)
                s = load_settings()
                key = s.get("api_key", "") if s.get("ai") == "claude" else ""

                def work():
                    JOB.prog(0.1, "Membaca brief campaign")
                    nm, _p = A.campaign_analyze(files, key, JOB.log)
                    return dict(campaign=nm)
                ok = JOB.start("campaign", work)
                return self._json({"ok": ok} if ok else {"error": "Masih ada proses berjalan."}, 200 if ok else 409)
            if p == "/api/campaign/save":
                c = A.campaign_load(b.get("id", ""))
                if not c:
                    return self._json({"error": "campaign tidak ditemukan"}, 404)
                for k in ("brand", "topic", "content_brief", "cta", "overlay_text", "deadline", "desc_must"):
                    if k in b:
                        c[k] = str(b[k])
                for k in ("hashtags", "mentions", "banned"):
                    if k in b:
                        v = b[k] if isinstance(b[k], list) else re.split(r"[\n,]+" if k == "banned" else r"[\s,]+", b[k])
                        c[k] = [x.strip() for x in v if x.strip()]
                if b.get("logo_file") and os.path.isfile(b["logo_file"]):
                    import shutil
                    dst = os.path.join(c["_dir"], "logo.png")
                    if os.path.abspath(b["logo_file"]) != os.path.abspath(dst):
                        shutil.copy2(b["logo_file"], dst)
                    c["logo_file"] = dst
                A.campaign_save(c)
                return self._json({"ok": True})
            if p == "/api/campaign/videos":
                c = A.campaign_load(b.get("id", ""))
                if not c:
                    return self._json({"error": "campaign tidak ditemukan"}, 404)
                vids = A.campaign_find_videos(c, 15, b.get("period", "Bulan ini"), lambda m: None)
                c["video_suggestions"] = vids
                A.campaign_save(c)
                return self._json({"videos": vids})
            if p == "/api/campaign/delete":
                import shutil
                c = A.campaign_load(b.get("id", ""))
                if c and os.path.realpath(c["_dir"]).startswith(os.path.realpath(A.CAMPAIGN_DIR)):
                    shutil.rmtree(c["_dir"], ignore_errors=True)
                return self._json({"ok": True})
            if p == "/api/viral":
                vids = A.viral_videos(b.get("country", "Indonesia"), b.get("cat", "Semua"),
                                      b.get("period", "Minggu ini"), int(b.get("n", 12)))
                have = A._downloaded_ids()
                for v in vids:
                    v["done"] = A._yt_id(v["url"]) in have
                return self._json({"videos": vids})
            if p == "/api/fix":
                with open(A.FIX_FILE, "w", encoding="utf-8") as f:
                    f.write(str(b.get("text", "")))
                return self._json({"ok": True})
            if p == "/api/open":
                fp = b.get("path", "")
                if fp and os.path.exists(fp) and (os.path.isdir(fp) or allowed_file(fp)):
                    os.startfile(fp) if os.name == "nt" else None
                    return self._json({"ok": True})
                return self._json({"error": "tidak ditemukan"}, 404)
            return self._json({"error": "tidak ada"}, 404)
        except Exception as ex:
            return self._json({"error": str(ex)[:300]}, 500)


def main():
    global PORT
    for port in range(PORT, PORT + 10):
        try:
            srv = ThreadingHTTPServer((HOST, port), H)
            PORT = port
            break
        except OSError:
            continue
    else:
        print("Port 8765-8774 terpakai semua.")
        return
    url = f"http://{HOST}:{PORT}/"
    print(f"AutoClip Web berjalan di {url}  (tutup jendela ini untuk berhenti)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
