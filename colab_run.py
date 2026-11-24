"""Jalankan AutoClip tanpa tampilan (untuk Google Colab / server).

Contoh:
  python colab_run.py --out /content/drive/MyDrive/AutoClip/hasil "https://youtu.be/xxxx"
Pengaturan dibaca dari web_settings.json (sama dengan AutoClip Studio), bisa ditimpa dengan --set key=nilai.
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from dataclasses import asdict, fields

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
import autoclip as A  # noqa: E402

OPT_FIELDS = {f.name for f in fields(A.Options)}


def local_path(p):
    """Path Windows (D:\\...) dari laptop -> cari file yang sama di folder aset/ di server."""
    if not p or os.path.exists(p):
        return p
    nm = re.split(r"[\\/]", p)[-1]
    for d in ("aset", "."):
        alt = os.path.join(APP_DIR, d, nm)
        if os.path.exists(alt):
            return alt
    return ""


def load_options(settings_file, overrides):
    s = {}
    if os.path.exists(settings_file):
        with open(settings_file, encoding="utf-8") as f:
            s = json.load(f)
    s.update(overrides)
    base = asdict(A.Options())
    for k in OPT_FIELDS:
        if k in s and s[k] is not None:
            t = type(base[k]) if base[k] is not None else str
            try:
                if t is bool and isinstance(s[k], str):
                    base[k] = s[k].strip().lower() in ("1", "true", "ya", "yes", "on")
                else:
                    base[k] = t(s[k]) if t in (int, float, bool, str) else s[k]
            except (TypeError, ValueError):
                pass
    opt = A.Options(**base)
    opt.logo = local_path(opt.logo)
    opt.brand_logo = local_path(opt.brand_logo)
    # di server tidak ada browser -> cookies harus berupa file cookies.txt
    ck = os.path.join(APP_DIR, "cookies.txt")
    opt.cookies = opt.cookies if os.path.isfile(opt.cookies or "") else (ck if os.path.exists(ck) else "")
    if opt.ai == "claude" and not (opt.api_key or os.environ.get("ANTHROPIC_API_KEY")):
        print("API key Claude kosong -> pakai AI lokal")
        opt.ai = "lokal"
    return opt, bool(s.get("compile"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="+", help="link video / file video")
    ap.add_argument("--out", required=True, help="folder hasil (mis. di Google Drive)")
    ap.add_argument("--settings", default=os.path.join(APP_DIR, "web_settings.json"))
    ap.add_argument("--set", action="append", default=[], metavar="KEY=NILAI")
    a = ap.parse_args()
    ov = {}
    for kv in a.set:
        if "=" in kv:
            k, v = kv.split("=", 1)
            if v.strip() != "":
                ov[k.strip()] = v.strip()
    opt, compile_ = load_options(a.settings, ov)
    run_dir = os.path.join(a.out, time.strftime("%Y-%m-%d_%H%M"))
    os.makedirs(run_dir, exist_ok=True)
    opt.out_dir = run_dir
    print(f"Komputer : {A.hw_summary() if hasattr(A, 'hw_summary') else ''}")
    print(f"Hasil ke : {run_dir}")
    print(f"Klip     : {opt.n_clips} x {opt.duration:.0f} dtk · genre {opt.genre} · fokus {opt.focus}"
          f" · thumbnail AI {'ya' if (opt.gemini_key or os.environ.get('GEMINI_API_KEY')) and opt.ai_thumb else 'tidak'}\n", flush=True)
    last = {"p": -1, "t": 0}

    def prog(p, stage=""):
        if int(p * 100) != last["p"] and time.time() - last["t"] > 3:
            last.update(p=int(p * 100), t=time.time())
            print(f"   [{100 * p:5.1f}%] {stage[:80]}", flush=True)

    res = {}

    def work():
        try:
            res["r"] = A.process_many(a.sources, opt, lambda m: print(m, flush=True), prog,
                                      compile_ and len(a.sources) > 1)
        except Exception as ex:
            res["err"] = ex

    t0 = time.time()
    th = threading.Thread(target=work, daemon=True)
    th.start()
    try:
        while th.is_alive():
            th.join(0.5)
    except KeyboardInterrupt:
        A.CANCEL.set()
        th.join(10)
        print("Dibatalkan.")
        return
    if "err" in res:
        print("\nGAGAL:", res["err"])
        sys.exit(1)
    results, comp = res.get("r") or ([], None)
    n = sum(len(c) for _, c in results)
    print(f"\nSELESAI: {n} klip dalam {(time.time() - t0) / 60:.1f} menit -> {run_dir}")
    for out, clips in results:
        for c in clips:
            print(f"  #{c['rank']} skor {c['score']}  {c['title']}")


if __name__ == "__main__":
    threading.stack_size(64 * 1024 * 1024)  # Whisper butuh stack besar
    main()
