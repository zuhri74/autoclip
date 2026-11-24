#!/usr/bin/env python3
"""
AutoClip - klip video otomatis, dipilih & dinilai AI, diedit kreatif.

Alur (mirip autoclipper):
  1. Tambah video: file atau link (YouTube, Instagram, Facebook, TikTok, ...).
  2. Analisis cepat: transkrip seluruh video (model cepat) + analisis gambar memakai GPU
     (Intel Quick Sync / NVIDIA / DirectX) - berjalan BERSAMAAN.
  3. AI memilih momen terbaik, dipotong di awal & akhir kalimat, lalu memberi
     SKOR VIRAL 0-100 + judul + alasan + kategori + hashtag.
       - Claude AI (bila ada API key) membaca transkrip lengkap, atau
       - AI lokal (gratis, offline) menilai hook, energi, reaksi, kata emosional, tempo, aksi.
  4. Subtitle akurat: hanya klip terpilih yang ditranskrip ulang dengan model lebih akurat.
  5. Edit kreatif: montase di ketukan musik / klip cerita dengan subtitle animasi,
     stiker, zoom, judul hook, CTA, layout 9:16 otomatis.

Profil perangkat menyesuaikan kecepatan dengan laptop (otomatis terdeteksi).

  python autoclip.py                         -> aplikasi (GUI)
  python autoclip.py <file|link> -n 5        -> baris perintah
"""
import argparse
import json
import math
import os
import platform
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, asdict

import numpy as np

HOP = 0.5     # resolusi skor audio (detik)
FINE = 0.25   # resolusi analisis gambar (detik) - diatur ulang sesuai profil
APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("HF_HOME", os.path.join(APP_DIR, "_hf"))  # model Whisper disimpan di folder aplikasi
CONFIG = os.path.join(APP_DIR, "autoclip_config.json")
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".ts"}
NOWIN = 0x08000000 if os.name == "nt" else 0
EMOJI_FONT = next((p_ for p_ in (r"C:\Windows\Fonts\seguiemj.ttf", os.path.join(APP_DIR, "fonts", "seguiemj.ttf"),
                                  os.path.join(APP_DIR, "fonts", "NotoColorEmoji.ttf"),
                                  "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf") if os.path.exists(p_)),
                  r"C:\Windows\Fonts\seguiemj.ttf")
OUT_FPS = 30
CLAUDE_MODEL = "claude-opus-5-5"
CANCEL = threading.Event()


class Cancelled(Exception):
    pass


FORMATS = {
    "vertical_face": "9:16 - otomatis (ikuti wajah / sinematik)",
    "vertical_blur": "9:16 - sinematik (video utuh + latar blur)",
    "square": "1:1 - kotak (Feed)",
    "original": "Rasio asli",
}
STYLES = {"otomatis": "Otomatis", "montase": "Montase musik (potong di beat)", "cerita": "Cerita (orang bicara)"}
STRICT = {"ketat": 0.85, "normal": 0.70, "longgar": 0.50}
PROFILES = {
    "otomatis": "Otomatis (sesuai laptop & panjang video)",
    "ringan": "Laptop biasa / tanpa GPU - paling cepat",
    "seimbang": "Seimbang - CPU kuat",
    "nvidia": "GPU NVIDIA - cepat & akurat",
    "maksimal": "Kualitas maksimal - paling lambat",
}
# Preset genre: gaya edit, durasi, tampilan, bobot penilaian AI, warna aksen subtitle (ASS &HBBGGRR), arahan Claude
W_DEFAULT = dict(energy=0.22, peak=0.16, kw=0.2, hook=0.16, pace=0.1, motion=0.1, action=0.0)
ACTION_WORDS = set("""tembak tembakin tembakan nembak dor dar dor-dor mati matiin mampus bunuh dibunuh kill killed
kills musuh perang serang serangan serbu nyerang lawan hajar hajarr pukul gebuk gelut berantem tarung duel kabur lari
kejar dikejar kejaran polisi polisinya wanted bintang ledak meledak ledakan bom granat rpg roket senjata pistol shotgun
sniper senapan peluru amunisi darah headshot boss bos misi heist rampok merampok garong tabrak nabrak crash kebut ngebut
awas bahaya tolong cepet cepat aduh anjir anjay waduh mampus shoot shot shooting enemy enemies fight fighting attack
chase run explosion explode dead die died war battle combat gun guns bullets reload ambush help""".split())
FOCUS = {
    "otomatis": ("Otomatis (sesuai jenis video)", None, ""),
    "action": ("Action / pertempuran / kejar-kejaran",
               dict(energy=0.03, peak=0.04, kw=0.0, hook=0.02, pace=0.0, motion=0.0, action=0.91),
               "FOKUS PENGGUNA: pilih HANYA momen aksi - pertempuran, tembak-tembakan, ledakan, perkelahian, "
               "kejar-kejaran, kabur dari polisi, bos fight, kecelakaan/crash. Mulai sesaat sebelum aksi dimulai, "
               "akhiri setelah aksi selesai/menang/kalah. Abaikan obrolan santai tanpa aksi."),
    "lucu": ("Lucu / ngakak", dict(energy=0.12, peak=0.34, kw=0.3, hook=0.12, pace=0.06, motion=0.03, action=0.03),
             "FOKUS PENGGUNA: pilih momen paling lucu - tawa keras, kejadian konyol, fail, celetukan kocak."),
    "emosional": ("Emosional / dramatis", dict(energy=0.18, peak=0.2, kw=0.3, hook=0.18, pace=0.06, motion=0.04, action=0.04),
                  "FOKUS PENGGUNA: pilih momen paling emosional/dramatis - kaget, marah, sedih, plot twist, teriakan."),
    "informatif": ("Informatif / tips", dict(energy=0.06, peak=0.04, kw=0.2, hook=0.3, pace=0.36, motion=0.02, action=0.02),
                   "FOKUS PENGGUNA: pilih penjelasan, tips, dan fakta paling berguna yang utuh."),
}


def _g(label, style, look, dur, frame, accent, w, brief, kw=""):
    return dict(label=label, style=style, look=look, dur=dur, frame=frame, accent=accent, w=w or W_DEFAULT,
                brief=brief, kw=kw)


# frame (cara kamera/layar):
#   speaker      = 9:16 ikuti orang yang sedang bicara (pindah kamera antar pembicara)
#   face         = close-up wajah/ekspresi
#   split_game   = game dominan di atas, wajah kecil di bawah
#   split_react  = wajah besar, video yang ditonton kecil di atas
#   split_screen = layar komputer dominan, wajah kecil
#   object       = ikuti objek/tangan/produk yang bergerak (+ wajah saat bicara)
#   action       = ikuti aksi (bola/pemain), abaikan penonton & papan skor
#   body         = seluruh badan terlihat
#   present      = slide/materi di atas, presenter di bawah
#   vlog         = wajah saat bicara, lingkungan saat menunjukkan sesuatu
GENRES = {
    "otomatis": _g("Otomatis (AI menganalisa videonya)", None, None, None, "auto", "&H0000E6FF", None, ""),
    "podcast": _g("Podcast", "cerita", "bersih", 60, "speaker", "&H0040FF7A",
                  dict(energy=0.08, peak=0.08, kw=0.24, hook=0.3, pace=0.25, motion=0.05),
                  "Podcast: utamakan insight kuat, opini berani, cerita personal, jawaban mengejutkan, kutipan yang "
                  "layak dibagikan. Klip harus utuh satu pemikiran.",
                  "podcast episode ngobrol obrolan bincang tamu narasumber"),
    "gaming": _g("Gaming / streamer", "cerita", "bersih", 45, "split_game", "&H0000E6FF",
                 dict(energy=0.22, peak=0.22, kw=0.2, hook=0.14, pace=0.08, motion=0.14),
                 "Gaming/streamer: utamakan reaksi heboh streamer, momen lucu, gagal, menang, kejadian tak terduga "
                 "di game, dan celetukan yang bisa dikutip.",
                 "game gaming gameplay gta minecraft roblox valorant mobile_legends free_fire pubg mabar misi boss "
                 "level stream live"),
    "reaction": _g("Reaction", "cerita", "bersih", 40, "split_react", "&H0000E6FF",
                   dict(energy=0.2, peak=0.28, kw=0.2, hook=0.12, pace=0.08, motion=0.12),
                   "Reaction: utamakan ekspresi dan komentar paling heboh/lucu saat menonton.",
                   "reaction react reaksi bereaksi nonton menonton"),
    "masak": _g("Masak / kuliner", "cerita", "bersih", 50, "object", "&H0000B4FF",
                dict(energy=0.1, peak=0.08, kw=0.22, hook=0.25, pace=0.15, motion=0.2),
                "Masak: utamakan langkah memasak yang menarik, hasil akhir yang menggugah selera, dan tips rahasia.",
                "masak resep bumbu goreng rebus tumis aduk bahan dapur garam bawang minyak panci wajan enak"),
    "tutorial": _g("Tutorial / edukasi", "cerita", "bersih", 60, "object", "&H00FFE500",
                   dict(energy=0.06, peak=0.04, kw=0.24, hook=0.3, pace=0.3, motion=0.06),
                   "Tutorial: utamakan tips praktis, fakta mengejutkan, penjelasan singkat yang lengkap.",
                   "tutorial langkah tips belajar step trik praktik"),
    "tutorial_pc": _g("Tutorial komputer / layar", "cerita", "bersih", 60, "split_screen", "&H00FFE500",
                      dict(energy=0.05, peak=0.03, kw=0.22, hook=0.3, pace=0.3, motion=0.1),
                      "Tutorial komputer: utamakan trik/shortcut yang bikin 'wow', langkah singkat dengan hasil jelas.",
                      "klik install aplikasi software excel word coding komputer laptop windows menu folder file "
                      "setting browser website download"),
    "interview": _g("Interview / wawancara", "cerita", "bersih", 60, "speaker", "&H0040FF7A",
                    dict(energy=0.08, peak=0.08, kw=0.24, hook=0.3, pace=0.25, motion=0.05),
                    "Wawancara: utamakan jawaban paling mengejutkan, jujur, atau kontroversial beserta pertanyaannya.",
                    "wawancara interview pertanyaan narasumber jawab"),
    "fitness": _g("Olahraga / fitness", "cerita", "bersih", 40, "body", "&H00008CFF",
                  dict(energy=0.2, peak=0.15, kw=0.15, hook=0.15, pace=0.1, motion=0.25),
                  "Fitness: utamakan gerakan yang jelas dicontohkan, tips teknik, dan transformasi/hasil.",
                  "workout gym latihan otot squat push_up set repetisi kardio diet badan"),
    "kendaraan": _g("Review kendaraan", "cerita", "bersih", 50, "object", "&H00008CFF",
                    dict(energy=0.2, peak=0.15, kw=0.2, hook=0.2, pace=0.1, motion=0.15),
                    "Review kendaraan: utamakan fitur unik, suara mesin, harga, dan kesan berkendara.",
                    "mobil motor mesin cc velg knalpot kabin bagasi test_drive tenaga torsi transmisi irit"),
    "unboxing": _g("Unboxing", "cerita", "bersih", 45, "object", "&H0000E6FF",
                   dict(energy=0.12, peak=0.18, kw=0.22, hook=0.25, pace=0.1, motion=0.13),
                   "Unboxing: utamakan momen produk pertama kali terlihat dan reaksi pembuka.",
                   "unboxing unbox kardus paket isi kotak dus segel"),
    "review": _g("Review produk", "cerita", "bersih", 50, "object", "&H0000E6FF",
                 dict(energy=0.08, peak=0.08, kw=0.25, hook=0.3, pace=0.22, motion=0.07),
                 "Review produk: utamakan kelebihan/kekurangan paling penting, harga, dan kesimpulan jujur.",
                 "review harga spesifikasi fitur kelebihan kekurangan kualitas worth beli murah mahal"),
    "musik": _g("Musik / live performance", "cerita", "ramai", 30, "speaker", "&H00C84DFF",
                dict(energy=0.35, peak=0.3, kw=0.0, hook=0.0, pace=0.0, motion=0.35),
                "Live musik: pakai bagian lagu paling bertenaga (reff/solo).",
                "lagu nyanyi konser panggung band musik live cover akustik"),
    "sport": _g("Pertandingan olahraga", "cerita", "ramai", 30, "action", "&H00008CFF",
                dict(energy=0.3, peak=0.28, kw=0.1, hook=0.06, pace=0.04, motion=0.22),
                "Pertandingan: utamakan gol/poin, aksi skill, momen dramatis, teriakan komentator.",
                "gol goal pertandingan liga skor tendangan wasit pemain babak bola penalti"),
    "komedi": _g("Komedi", "cerita", "bersih", 30, "face", "&H0000E6FF",
                 dict(energy=0.15, peak=0.3, kw=0.25, hook=0.15, pace=0.1, motion=0.05),
                 "Komedi: cari setup + punchline utuh dan tawa paling keras; akhiri tepat setelah punchline.",
                 "wkwk haha lucu ngakak prank komedi lawak"),
    "presentasi": _g("Presentasi / webinar", "cerita", "bersih", 60, "present", "&H00FFE500",
                     dict(energy=0.08, peak=0.05, kw=0.22, hook=0.3, pace=0.3, motion=0.05),
                     "Presentasi: utamakan poin kunci, data/angka mengejutkan, dan kesimpulan.",
                     "slide materi presentasi webinar data grafik peserta"),
    "vlog": _g("Vlog", "cerita", "bersih", 45, "vlog", "&H0000E6FF",
               dict(energy=0.18, peak=0.15, kw=0.2, hook=0.2, pace=0.12, motion=0.15),
               "Vlog: utamakan momen seru, tempat menarik, dan cerita/celetukan spontan.",
               "vlog jalan_jalan hari trip liburan kota pagi sore makan_siang"),
    "hiburan": _g("Hiburan / entertainment", "cerita", "bersih", 40, "auto", "&H0000E6FF",
                  dict(energy=0.2, peak=0.2, kw=0.22, hook=0.16, pace=0.1, motion=0.12),
                  "Hiburan: utamakan momen lucu, drama, kejutan, reaksi spontan.", "seru drama kejutan"),
    "berita": _g("Berita / informasi", "cerita", "bersih", 45, "speaker", "&H003B3BFF",
                 dict(energy=0.1, peak=0.08, kw=0.2, hook=0.3, pace=0.27, motion=0.05),
                 "Berita: utamakan fakta utama, pernyataan penting, angka mengejutkan; jaga konteks akurat.",
                 "berita presiden pemerintah polisi dilaporkan menteri warga kasus"),
    "action": _g("Action / film laga / fight", "cerita", "ramai", 30, "action", "&H003C3CFF",
                 dict(energy=0.3, peak=0.3, kw=0.06, hook=0.05, pace=0.02, motion=0.17, action=0.25),
                 "Action: utamakan perkelahian, ledakan, kejar-kejaran, stunt; mulai sesaat sebelum aksi, potong tepat setelah puncaknya.",
                 "action laga fight berantem baku_hantam tembak tembakan ledakan meledak kejar kejaran duel tarung pukul hajar gelut"),
    "ekstrem": _g("Olahraga ekstrem / stunt / parkour", "cerita", "ramai", 30, "action", "&H00008CFF",
                  dict(energy=0.28, peak=0.3, kw=0.06, hook=0.06, pace=0.02, motion=0.28),
                  "Ekstrem: utamakan trik paling berbahaya, lompatan, jatuh/fail, dan reaksi penonton.",
                  "parkour stunt skateboard bmx motocross freestyle ekstrem extreme salto backflip trik"),
    "balapan": _g("Balapan / drift / MotoGP / F1", "cerita", "ramai", 30, "action", "&H00008CFF",
                  dict(energy=0.3, peak=0.3, kw=0.08, hook=0.05, pace=0.02, motion=0.25),
                  "Balapan: utamakan salip-menyalip, crash, drift, finish, teriakan komentator.",
                  "balapan balap race racing drift motogp f1 overtake menyalip tikungan sirkuit crash finish lap"),
    "anime": _g("Anime / animasi", "cerita", "ramai", 30, "action", "&H00F75AA8",
                dict(energy=0.25, peak=0.25, kw=0.12, hook=0.12, pace=0.06, motion=0.2),
                "Anime: utamakan adegan pertarungan, jurus, momen epik atau emosional, dan dialog ikonik.",
                "anime episode jurus kekuatan power villain nakama sensei"),
    "horor": _g("Horor / misteri", "cerita", "bersih", 40, "auto", "&H003C3CFF",
                dict(energy=0.2, peak=0.32, kw=0.18, hook=0.18, pace=0.05, motion=0.07),
                "Horor: utamakan jumpscare, teriakan, penampakan, dan cerita paling bikin merinding.",
                "horor horror hantu seram serem misteri penampakan kuntilanak pocong angker merinding mistis"),
    "sinematik": _g("Video musik / trailer / sinematik", "montase", "ramai", 30, "auto", "&H00C84DFF", None,
                    "Sinematik: pakai bagian lagu paling bertenaga.", "trailer official mv cinematic"),
}
FRAME_LABEL = {"speaker": "ikuti pembicara", "face": "close-up ekspresi", "split_game": "game dominan + wajah kecil",
               "split_react": "wajah besar + video kecil", "split_screen": "layar dominan + wajah kecil",
               "object": "ikuti objek/tangan", "action": "ikuti aksi", "body": "badan penuh",
               "present": "slide + presenter", "vlog": "wajah saat bicara, lingkungan saat tidak",
               "cinematic": "sinematik", "square": "kotak", "original": "asli"}
LOGO_POS = ["kanan atas", "kiri atas", "kanan bawah", "kiri bawah"]
TEXT_COLORS = {  # warna ASS (&HBBGGRR)
    "otomatis": ("Otomatis (sesuai jenis video)", None),
    "hijau": ("Hijau neon", "&H0014FF39"),
    "merah": ("Merah", "&H003C3CFF"),
    "oranye": ("Oranye", "&H00008CFF"),
    "biru": ("Biru muda", "&H00FFD000"),
    "pink": ("Pink", "&H00B040FF"),
    "ungu": ("Ungu", "&H00F75AA8"),
    "putih": ("Putih (kata aktif membesar)", "&H00FFFFFF"),
    "kuning": ("Kuning", "&H0000E6FF"),
}
YELLOWS = {"&H0000E6FF", "&H0000B4FF", "&H0000F0FF", "&H0000E1FF"}


def accent_color(opt):
    c = TEXT_COLORS.get(getattr(opt, "text_color", "otomatis"), (0, None))[1]
    if c:
        return c
    a = GENRES.get(opt.genre, GENRES["otomatis"])["accent"]
    return "&H0014FF39" if a in YELLOWS else a  # tidak lagi kuning


LOOKS = {"bersih": "Bersih ala streamer (tanpa stiker)", "ramai": "Ramai (stiker, zoom, power word)"}
AI_MODES = {"lokal": "AI lokal (gratis, offline)", "claude": "Claude AI (perlu API key)"}
SUB_MODELS = ["otomatis", "base", "small", "medium", "large-v3-turbo", "large-v3"]
SPEED_CPU = {"tiny": 30, "base": 19, "small": 5, "medium": 2, "large-v3-turbo": 2.6, "large-v3": 1.0}
GRADE = "eq=contrast=1.08:saturation=1.28:gamma=0.97,unsharp=5:5:0.5"

KEYWORDS = {
    "😂": "haha hahaha wkwk wkwkwk lucu ketawa ngakak lol funny laugh kocak",
    "🔥": "keren mantap mantul gokil gacor gila hebat amazing awesome epic terbaik best fire dahsyat jago",
    "🤯": "wow anjay anjir anjrit buset bjir njir waduh wadaw parah astaga serius insane crazy",
    "😱": "kaget takut bahaya seram scary danger horor ngeri mengerikan",
    "💰": "uang duit rupiah juta miliar money dollar kaya rich bisnis cuan gaji profit untung",
    "❤️": "cinta sayang love romantis",
    "🤫": "rahasia secret bocoran",
    "⚠️": "penting perhatian awas warning important hati-hati",
    "🤔": "kenapa mengapa bagaimana gimana why",
    "😢": "sedih nangis sad cry kecewa",
    "🙏": "terima makasih thanks thank tolong please",
    "💡": "ide tips trik idea tip solusi hack",
    "🚀": "cepat sukses success grow viral meledak",
    "🍔": "makan makanan food enak lapar kuliner",
    "😎": "santai cool gaya",
}
KW = {w: e for e, ws in KEYWORDS.items() for w in ws.split()}
ADDRESS = {"kalian", "kamu", "lo", "lu", "elu", "guys", "gue", "gua", "bro", "sis", "you", "cuy"}
HYPE = ["KEREN!", "GILA!", "MANTAP!", "EPIC!", "WOW!", "SADIS!"]
FALLBACK_TITLES = ["MOMEN PALING SERU!", "WAJIB NONTON SAMPAI HABIS!", "INI BAGIAN TERBAIKNYA!"]


@dataclass
class Options:
    n_clips: int = 5
    duration: float = 45.0
    fmt: str = "vertical_face"
    style: str = "otomatis"
    subtitles: bool = True
    profile: str = "otomatis"
    sub_model: str = "otomatis"
    language: str = "id"
    ai: str = "lokal"
    api_key: str = ""
    out_dir: str | None = None
    strict: str = "normal"
    creative: bool = True
    look: str = "bersih"         # bersih (ala referensi) / ramai (stiker & efek)
    genre: str = "otomatis"      # lihat GENRES
    frame: str = "auto"          # diisi otomatis dari genre
    jumpcut: bool = True
    sfx: bool = True
    title: str = ""
    handle: str = ""
    logo: str = ""               # file logo (PNG transparan) untuk watermark
    logo_pos: str = "kanan atas"
    top_text: bool = False       # teks judul/hook di atas video (default mati)
    intro: bool = True           # kartu pembuka berisi judul + suara AI
    outro: bool = True           # kartu penutup berisi ajakan + suara AI
    voice: str = "pria"          # pria / wanita / mati
    text_color: str = "otomatis"  # warna kata aktif subtitle (lihat TEXT_COLORS)
    meme_sfx: bool = True         # efek suara meme (boom, boing, airhorn, dll)
    focus: str = "otomatis"       # fokus momen: otomatis / action / lucu / emosional / informatif
    cam_pos: str = "bawah"        # posisi facecam streamer di video edit: "bawah" / "atas"
    cam_box: str = ""             # kotak facecam manual "x,y,lebar,tinggi" dalam % (kosong = deteksi otomatis)
    brand: str = "mati"           # logo besar + tulisan: "awal & akhir" / "awal" / "akhir" / "mati"
    brand_logo: str = ""          # kosong = pakai logo watermark
    brand_text: str = ""          # tulisan di bawah logo (kosong = teks watermark, mis. @akun)
    desc_langs: str = "en"        # bahasa tambahan judul+deskripsi, dipisah koma ("" = tidak ada)
    campaign: str = ""            # nama campaign (folder campaign/) atau path -> aturan brief campaign clipper
    campaign_text: str = ""       # tulisan wajib campaign di video (diisi otomatis dari brief)
    campaign_pos: str = "atas"    # posisi tulisan campaign: atas / bawah
    keep_core: bool = False      # simpan klip inti (untuk kompilasi banyak video)
    cta: str = "FOLLOW UNTUK PART SELANJUTNYA!"
    cookies: str = ""
    gemini_key: str = ""          # API key Google Gemini -> thumbnail & cover dibuat AI (kosong = desain lokal)
    ai_thumb: bool = True         # pakai AI untuk thumbnail bila key Gemini ada


# ============================================================================ perangkat

_HW = None


def detect_hw():
    global _HW
    if _HW:
        return _HW
    threads = os.cpu_count() or 4
    ram, cpu = 0.0, platform.processor() or "CPU"
    try:
        if os.name == "nt":
            import ctypes
            import winreg

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + \
                           [(n, ctypes.c_ulonglong) for n in ("t", "a", "tp", "ap", "tv", "av", "ae")]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            ram = m.t / 2 ** 30
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            cpu = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        else:
            ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2 ** 30
    except Exception:
        pass
    cuda = 0
    try:
        import ctranslate2
        cuda = ctranslate2.get_cuda_device_count()
    except Exception:
        pass
    _HW = dict(cpu=cpu, threads=threads, ram=ram, cuda=cuda, intel="intel" in cpu.lower())
    return _HW


def hw_summary():
    h = detect_hw()
    gpu = "GPU NVIDIA" if h["cuda"] else ("GPU Intel (Quick Sync)" if h["intel"] else "tanpa GPU NVIDIA")
    return f"{h['cpu']} · {h['threads']} thread · RAM {h['ram']:.0f} GB · {gpu}"


def resolve_profile(opt, dur):
    h = detect_hw()
    name = opt.profile
    if name == "otomatis":
        name = "nvidia" if h["cuda"] else ("ringan" if dur > 40 * 60 or h["threads"] < 8 else "seimbang")
    cuda = bool(h["cuda"]) and name in ("nvidia", "maksimal")
    cfg = {
        "ringan": dict(scan="base", sub="small", fps=2),
        "seimbang": dict(scan="small", sub="small", fps=4),
        "nvidia": dict(scan="large-v3-turbo", sub="large-v3-turbo", fps=4),
        "maksimal": dict(scan="large-v3-turbo" if cuda else "small", sub="large-v3", fps=4),
    }[name]
    cfg.update(name=name, device="cuda" if cuda else "cpu", ct="float16" if cuda else "int8",
               batch=4 if h["ram"] and h["ram"] < 8 else (16 if cuda else 8))
    if opt.sub_model != "otomatis":
        cfg["sub"] = opt.sub_model
    return cfg


def model_speed(name, cfg):
    return SPEED_CPU.get(name, 3) * (10 if cfg["device"] == "cuda" else 1) * max(0.4, detect_hw()["threads"] / 16)


# ============================================================================ ffmpeg

_FF = None


def ffmpeg_exe():
    global _FF
    if _FF:
        return _FF
    _FF = shutil.which("ffmpeg")
    if not _FF:
        try:
            import imageio_ffmpeg
            _FF = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            raise RuntimeError("ffmpeg tidak ditemukan. Jalankan: pip install imageio-ffmpeg")
    return _FF


def run(cmd, cwd=None):
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NOWIN)
    while True:
        try:
            out, err = p.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if CANCEL.is_set():
                p.kill()
                p.communicate()
                raise Cancelled()
    if p.returncode != 0:
        raise RuntimeError("ffmpeg gagal:\n" + err.decode("utf-8", "replace")[-1500:])
    return out


def check_cancel():
    if CANCEL.is_set():
        raise Cancelled()


def probe(path):
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path], capture_output=True, creationflags=NOWIN)
    txt = r.stderr.decode("utf-8", "replace")
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", txt)
    if not m:
        raise RuntimeError("Tidak bisa membaca video: " + path)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    vm = re.search(r"Stream #.*Video:.*?, (\d{2,5})x(\d{2,5})", txt)
    w, h = (int(vm[1]), int(vm[2])) if vm else (1920, 1080)
    rot = re.search(r"rotate\s*:\s*(-?\d+)|rotation of (-?[\d.]+)", txt)
    if rot and abs(round(float(rot[1] or rot[2]))) % 180 == 90:
        w, h = h, w
    cm = re.search(r"Stream #.*Video: (\w+)", txt)
    return dict(duration=dur, width=w, height=h, codec=cm[1] if cm else "",
                has_audio=bool(re.search(r"Stream #.*Audio:", txt)), dec=[])


def decode_plans(info, fps):
    """Cara decode untuk analisis, dari tercepat (GPU) ke paling aman (CPU)."""
    c, W, H = info["codec"], 64, 36
    plans = []
    if c in ("h264", "hevc", "av1", "vp9"):
        plans.append(("Intel Quick Sync", ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-c:v", f"{c}_qsv"],
                      f"vpp_qsv=w={W}:h={H},hwdownload,format=nv12,fps={fps}", W * H * 3 // 2, ["-c:v", f"{c}_qsv"]))
    if detect_hw()["cuda"]:
        plans.append(("NVIDIA", ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"],
                      f"scale_cuda={W}:{H},hwdownload,format=nv12,fps={fps}", W * H * 3 // 2, ["-hwaccel", "cuda"]))
    if os.name == "nt":
        plans.append(("DirectX", ["-hwaccel", "d3d11va"], f"fps={fps},scale={W}:{H},format=gray", W * H,
                      ["-hwaccel", "d3d11va"]))
    plans.append(("CPU", [], f"fps={fps},scale={W}:{H},format=gray", W * H, []))
    return plans


def pick_decoder(path, info, fps):
    for plan in decode_plans(info, fps):
        name, dec, vf, fb, _ = plan
        r = subprocess.run([ffmpeg_exe(), "-v", "error", *dec, "-t", "2", "-i", path, "-an", "-vf", vf,
                            "-f", "rawvideo", "pipe:1"], capture_output=True, creationflags=NOWIN)
        if r.returncode == 0 and len(r.stdout) >= fb:
            return plan
    return decode_plans(info, fps)[-1]


# ============================================================================ unduh dari link

VIRAL_COUNTRIES = {  # nama: (gl, hl, kata negara, bahasa judul untuk filter)
    "Indonesia": ("ID", "id", "indonesia", "id"),
    "Amerika": ("US", "en", "", "en"),
    "Inggris": ("GB", "en", "uk", "en"),
    "Malaysia": ("MY", "ms", "malaysia", None),
    "Filipina": ("PH", "en", "philippines", None),
    "India": ("IN", "hi", "india", None),
    "Korea": ("KR", "ko", "", "ko"),
    "Jepang": ("JP", "ja", "", "ja"),
    "Brasil": ("BR", "pt", "brasil", None),
}
VIRAL_CATS = {  # kategori: (kata kunci ID, kata kunci EN)
    "Semua": ("viral", "viral"),
    "Podcast": ("podcast", "podcast"),
    "Gaming": ("gameplay", "gameplay"),
    "Komedi": ("lucu komedi", "funny comedy"),
    "Reaction": ("reaction", "reaction"),
    "Interview": ("wawancara", "interview"),
    "Olahraga": ("highlights sepak bola", "sports highlights"),
    "Masak": ("masak resep", "cooking recipe"),
    "Vlog": ("vlog", "vlog"),
    "Review": ("review", "review"),
    "Action / film laga": ("film action laga full movie", "hollywood action movie fight scene"),
    "Trailer film": ("trailer film", "official trailer"),
    "Balapan": ("balapan motogp drift", "racing highlights"),
    "Ekstrem / stunt": ("parkour stunt ekstrem", "extreme sports stunts"),
    "Anime": ("anime", "anime"),
    "Horor / misteri": ("horor misteri", "horror story"),
    "Prank": ("prank", "prank"),
    "Musik live": ("live konser", "live performance"),
    "Teknologi": ("teknologi gadget", "tech review"),
    "Hewan": ("hewan lucu", "animals"),
    "Berita": ("berita terkini", "news"),
}
VIRAL_MIN = {"Trailer film": 1, "Musik live": 2, "Hewan": 2, "Berita": 2}  # menit minimal per kategori
VIRAL_PERIOD = {"Hari ini": 2, "Minggu ini": 3, "Bulan ini": 4}
_STOP = {"id": set("yang dan di ke dari ini itu aku kamu kita gue lo nggak gak tidak apa ada sama buat jadi bikin "
                   "banget udah sudah lagi kok mau bisa pakai cara".split()),
         "en": set("the and of to a in is you my your how what this that with for i we it on".split())}


def _lang_ok(title, lang):
    if not lang:
        return True
    if lang in ("ja", "ko"):
        return bool(re.search(r"[぀-ヿ一-鿿]" if lang == "ja" else r"[가-힯]", title))
    ws = set(re.findall(r"[a-z]+", title.lower()))
    foreign = {"india", "telugu", "tamil", "hindi", "malayalam", "kannada", "punjabi", "bhojpuri", "marathi", "bangla",
               "urdu", "dubbed", "nxt", "bollywood", "tollywood", "kollywood", "pinoy", "tagalog"}
    if lang in ("en", "id") and ws & foreign:
        return False
    if lang == "en" and ws & {"indonesia", "indo"}:
        return False
    hit = {k: len(ws & v) for k, v in _STOP.items()}
    other = max(v for k, v in hit.items() if k != lang)
    if lang == "id":  # judul Indonesia hampir selalu punya kata sambung Indonesia
        return hit["id"] > other or bool(ws & {"indonesia", "indo", "wkwk", "gw", "gue", "banget"})
    return hit.get(lang, 0) > other or (hit.get(lang, 0) == other == 0)


def _yt_search(query, gl, hl, sp):
    import urllib.request
    body = {"context": {"client": {"clientName": "WEB", "clientVersion": "2.20251101.00.00", "hl": hl, "gl": gl}},
            "query": query, "params": sp}
    req = urllib.request.Request("https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r_:
        d = json.loads(r_.read().decode("utf-8"))

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "videoRenderer":
                    yield v
                else:
                    yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)
    out = []
    for v in walk(d):
        if not v.get("videoId") or "lengthText" not in v:  # lewati siaran langsung
            continue
        txt = lambda k: v.get(k, {}).get("simpleText") or "".join(x.get("text", "") for x in v.get(k, {}).get("runs", []))
        views = int(re.sub(r"\D", "", txt("viewCountText")) or 0)
        parts = [int(x) for x in re.split(r"[:.]", txt("lengthText")) if x.isdigit()]
        sec = sum(x * 60 ** i for i, x in enumerate(reversed(parts)))
        out.append(dict(url=f"https://www.youtube.com/watch?v={v['videoId']}", title=txt("title"),
                        channel=txt("ownerText"), views=views, sec=sec, published=txt("publishedTimeText")))
    return out


def viral_videos(country="Indonesia", cat="Semua", period="Minggu ini", n=3, min_min=None):
    """n video YouTube paling banyak ditonton (periode tertentu) dari suatu negara, cocok untuk dipotong."""
    gl, hl, cw, lang = VIRAL_COUNTRIES[country]
    if min_min is None:
        min_min = VIRAL_MIN.get(cat, 5)
    kid, ken = VIRAL_CATS.get(cat, VIRAL_CATS["Semua"])
    kw = kid if lang == "id" else ken
    if lang in ("ja", "ko"):
        kw = {"ja": {"Semua": "話題", "Podcast": "対談", "Gaming": "ゲーム実況", "Komedi": "お笑い", "Masak": "料理"},
              "ko": {"Semua": "화제", "Podcast": "팟캐스트", "Gaming": "게임", "Komedi": "예능", "Masak": "요리"}}[lang].get(cat, ken)
    q = f"{kw} {cw}".strip()
    pcode = VIRAL_PERIOD.get(period, 3)
    import base64
    sp = base64.b64encode(bytes([0x08, 0x03, 0x12, 0x04, 0x08, pcode, 0x10, 0x01])).decode()  # urut penonton, video
    res, seen = [], set()
    tries = [q, f"{q} full" if lang != "id" else f"{q} terbaru"]
    if lang == "id":
        tries += [f"{ken} indonesia", kid.split()[0]]
    for qq in tries:
        for v in _yt_search(qq, gl, hl, sp):
            if v["url"] in seen or v["sec"] < min_min * 60:
                continue
            seen.add(v["url"])
            if _lang_ok(v["title"], lang):
                res.append(v)
        if len(res) >= n:
            break
    res.sort(key=lambda v: -v["views"])
    return res[:n]


def fmt_views(v):
    return f"{v / 1e6:.1f} jt" if v >= 1e6 else (f"{v / 1e3:.0f} rb" if v >= 1e3 else str(v))


def is_url(s):
    return bool(re.match(r"^https?://", s.strip(), re.I))


CHANNELS_FILE = os.path.join(APP_DIR, "_cache", "channels.json")
CAM_PROFILES = os.path.join(APP_DIR, "facecam_profil.json")


def _json_load(fp):
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _json_update(fp, upd):
    d = _json_load(fp)
    d.update(upd)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def channel_of(path):
    """Nama channel/streamer sebuah video (dari catatan unduhan; video lama dicari lewat ID YouTube di namanya)."""
    base = os.path.basename(path or "")
    d = _json_load(CHANNELS_FILE)
    if base in d:
        return d[base]
    m = re.search(r"\[([\w-]{11})\]", base)
    ch = ""
    if m:
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL(dict(quiet=True, no_warnings=True, skip_download=True)) as y:
                inf = y.extract_info(f"https://www.youtube.com/watch?v={m.group(1)}", download=False, process=False)
            ch = inf.get("channel") or inf.get("uploader") or ""
        except Exception:
            ch = ""
    _json_update(CHANNELS_FILE, {base: ch})
    return ch


def download(url, folder, cookies="", log=print, progress=lambda p: None):
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp belum terpasang. Jalankan: pip install -U \"yt-dlp[default]\" deno")
    os.makedirs(folder, exist_ok=True)
    ffdir = os.path.join(APP_DIR, "_ffmpeg")
    ffname = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if not shutil.which("ffmpeg") and not os.path.exists(os.path.join(ffdir, ffname)):
        os.makedirs(ffdir, exist_ok=True)
        shutil.copy(ffmpeg_exe(), os.path.join(ffdir, ffname))
    last = {"t": 0}

    def hook(d):
        if CANCEL.is_set():
            raise Cancelled()
        if d["status"] == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes", 0)
            if tot:
                progress(min(1.0, got / tot))
            if time.time() - last["t"] > 4:
                last["t"] = time.time()
                spd = (d.get("speed") or 0) / 2 ** 20
                pct = f"{100 * got / tot:.0f}%" if tot else f"{got / 2 ** 20:.0f} MB"
                log(f"      mengunduh {pct} ({got / 2 ** 20:.0f}/{tot / 2 ** 20:.0f} MB, {spd:.1f} MB/s)")

    opts = dict(
        outtmpl=os.path.join(folder, "%(title).70s [%(id)s].%(ext)s"),
        format="bv*[height<=1080]+ba/b[height<=1080]/b",
        format_sort=["res:1080", "vcodec:h264", "acodec:m4a"],  # H.264 paling cepat diproses di semua laptop
        merge_output_format="mp4", noplaylist=True, quiet=True, no_warnings=True, noprogress=True,
        windowsfilenames=True, progress_hooks=[hook], retries=5,
    )
    if not shutil.which("ffmpeg"):
        opts["ffmpeg_location"] = ffdir
    if cookies and os.path.isfile(cookies):  # file cookies.txt (mis. untuk Google Colab)
        opts["cookiefile"] = cookies
    elif cookies:
        opts["cookiesfrombrowser"] = (cookies,)
    deno = shutil.which("deno")
    if not deno:
        try:
            import deno as _deno
            deno = _deno.find_deno_bin()
        except Exception:
            deno = None
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}
    attempts = [
        {},
        {"extractor_args": {"youtube": {"player_client": ["tv", "web_safari"]}}},
        {"format": "b[height<=1080]/b", "extractor_args": {"youtube": {"player_client": ["android_vr", "web_embedded"]}}},
    ]
    log(f"Mengunduh dari link: {url}")
    err = None
    for k, extra in enumerate(attempts):
        try:
            with yt_dlp.YoutubeDL({**opts, **extra}) as y:
                info = y.extract_info(url, download=True)
                if info.get("entries"):
                    info = info["entries"][0]
                rd = info.get("requested_downloads") or []
                path = rd[0].get("filepath") if rd else y.prepare_filename(info)
            if path and os.path.exists(path):
                log(f"      selesai: {os.path.basename(path)}")
                ch_ = info.get("channel") or info.get("uploader") or ""
                if ch_:
                    _json_update(CHANNELS_FILE, {os.path.basename(path): ch_})
                return path, info.get("title") or ""
        except Cancelled:
            raise
        except Exception as ex:
            if CANCEL.is_set():
                raise Cancelled()
            err = re.sub(r"\x1b\[[0-9;]*m", "", str(ex)).replace("ERROR: ", "")
            for f in os.listdir(folder):
                if f.endswith((".part", ".ytdl")):
                    try:
                        os.remove(os.path.join(folder, f))
                    except OSError:
                        pass
            if k + 1 < len(attempts):
                log(f"      gagal ({err[:120]}) -> mencoba cara lain ({k + 2}/{len(attempts)})...")
    hint = ""
    if "403" in (err or "") or "Sign in" in (err or "") or "login" in (err or "").lower():
        hint = ("\n\nSaran: 1) pip install -U \"yt-dlp[default]\" deno  2) pilih Cookies = browser tempat "
                "Anda login  3) video privat/berbatas usia memang tidak bisa diunduh tanpa login.")
    raise RuntimeError(f"Unduhan gagal: {err}{hint}")


# ============================================================================ analisis audio & gambar

def audio_db(path, n, has_audio):
    if not has_audio:
        return np.full(n, -60.0)
    sr = 8000
    raw = run([ffmpeg_exe(), "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "pipe:1"])
    a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    hop = int(sr * HOP)
    m = min(n, len(a) // hop)
    rms = np.full(n, 1e-3)
    if m:
        rms[:m] = np.sqrt((a[:m * hop].reshape(m, hop) ** 2).mean(1))
    return 20 * np.log10(rms + 1e-6)


def frame_stats(path, n, info, plan, progress):
    """Statistik gambar tiap FINE detik (decode memakai GPU bila ada)."""
    name, dec, vf, fb, _ = plan
    Y = 64 * 36
    dur = info["duration"]
    # beberapa decoder paralel di potongan waktu berbeda (GPU: 2, CPU: sesuai jumlah core)
    K = (2 if name != "CPU" else max(1, min(4, detect_hw()["threads"] // 4))) if dur > 120 else 1
    seg = dur / K
    expected = max(1, dur / FINE)
    counts = [0] * K
    results = [None] * K
    procs = []

    def reader(i):
        cmd = [ffmpeg_exe(), "-v", "error", *dec, "-ss", f"{i * seg:.3f}", "-t", f"{seg:.3f}", "-i", path,
               "-an", "-vf", vf, "-f", "rawvideo", "pipe:1"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NOWIN)
        procs.append(p)
        parts, pending = [], b""
        while True:
            chunk = p.stdout.read(fb * 240)
            if not chunk or CANCEL.is_set():
                break
            pending += chunk
            k = len(pending) // fb
            if k:
                parts.append(np.frombuffer(pending[:k * fb], np.uint8).reshape(k, fb)[:, :Y].copy())
                pending = pending[k * fb:]
                counts[i] += k
        p.wait()
        f_ = np.concatenate(parts) if parts else np.zeros((0, Y), np.uint8)
        want = int(round(seg / FINE)) if i < K - 1 else len(f_)
        if len(f_) < want and len(f_):  # samakan panjang supaya waktu tetap selaras
            f_ = np.concatenate([f_, np.repeat(f_[-1:], want - len(f_), 0)])
        results[i] = f_[:want]

    ths = [threading.Thread(target=reader, args=(i,), daemon=True) for i in range(K)]
    for t in ths:
        t.start()
    while any(t.is_alive() for t in ths):
        if CANCEL.is_set():
            for p in procs:
                p.kill()
            raise Cancelled()
        progress(min(1.0, sum(counts) / expected))
        time.sleep(0.3)
    f = np.concatenate([r for r in results if r is not None and len(r)]) if any(
        r is not None and len(r) for r in results) else np.zeros((0, Y), np.uint8)
    k = len(f)
    diff = np.zeros(k)
    for i in range(1, k, 4000):
        j = min(k, i + 4000)
        diff[i:j] = np.abs(f[i:j].astype(np.int16) - f[i - 1:j - 1]).mean(1)
    bright = f.mean(1) if k else np.zeros(0)
    contrast = f.std(1) if k else np.zeros(0)
    per = max(1, int(round(HOP / FINE)))
    mot = np.zeros(n)
    m = min(n, k // per)
    if m:
        mot[:m] = diff[:m * per].reshape(m, per).max(1)
    progress(1.0)
    return dict(diff=diff, bright=bright, contrast=contrast, motion=mot)


def scene_cuts(diff):
    if len(diff) < 3:
        return []
    thr = max(16.0, float(np.median(diff)) * 3 + 8)
    cuts = []
    for i in np.where(diff > thr)[0]:
        t = i * FINE
        if not cuts or t - cuts[-1] > 0.5:
            cuts.append(t)
    return cuts


# ============================================================================ transkripsi (Whisper)

_MODELS = {}


def whisper_model(name, cfg, log):
    key = (name, cfg["device"], cfg["ct"])
    if key not in _MODELS:
        from faster_whisper import WhisperModel
        log(f"      memuat model Whisper '{name}' ({'GPU NVIDIA' if cfg['device'] == 'cuda' else 'CPU'})...")
        try:
            _MODELS[key] = WhisperModel(name, device=cfg["device"], compute_type=cfg["ct"])
        except Exception as ex:
            if cfg["device"] != "cuda":
                raise
            log(f"      GPU gagal ({ex}) -> pakai CPU")
            cfg.update(device="cpu", ct="int8", batch=8)
            return whisper_model(name, cfg, log)
    return _MODELS[key]


def extract_wav(path, start, dur, out):
    run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{max(0, start):.3f}", "-t", f"{dur:.3f}", "-i", path,
         "-vn", "-ac", "1", "-ar", "16000", out])


FIX_FILE = os.path.join(APP_DIR, "koreksi_kata.txt")
FIX_DEFAULT = """# Koreksi kata subtitle, judul & deskripsi. Satu per baris:  salah = benar
# Huruf besar/kecil diikuti otomatis. Baris diawali # diabaikan.
luat = laut
loat = laut
lusia = Lucia
jeson = Jason
"""
_FIX = {"mtime": None, "map": {}}


def fix_map():
    if not os.path.exists(FIX_FILE):
        try:
            with open(FIX_FILE, "w", encoding="utf-8") as f:
                f.write(FIX_DEFAULT)
        except OSError:
            pass
    try:
        mt = os.path.getmtime(FIX_FILE)
    except OSError:
        return {}
    if mt != _FIX["mtime"]:
        m = {}
        for ln in open(FIX_FILE, encoding="utf-8", errors="ignore"):
            if "=" in ln and not ln.strip().startswith("#"):
                a, b = ln.split("=", 1)
                if a.strip() and b.strip():
                    m[a.strip().lower()] = b.strip()
        _FIX.update(mtime=mt, map=m)
    return _FIX["map"]


def _case_like(src, dst):
    if src.isupper() and len(src) > 1:
        return dst.upper()
    if src[:1].isupper():
        return dst[:1].upper() + dst[1:]
    return dst


def fix_text(t):
    """Ganti kata salah dengar Whisper sesuai koreksi_kata.txt (utuh per kata, bisa frasa)."""
    m = fix_map()
    if not t or not m:
        return t
    for a, b in sorted(m.items(), key=lambda kv: -len(kv[0])):
        t = re.sub(rf"(?<![\w]){re.escape(a)}(?![\w])", lambda mm: _case_like(mm.group(0), b), t, flags=re.I)
    return t


def fix_words(words):
    m = fix_map()
    if not m:
        return words
    return [(s, e, fix_text(w)) for s, e, w in words]


def transcribe(path, start, dur, model_name, cfg, language, log, progress, chunk=900, prompt=""):
    """Transkrip [start, start+dur] per potongan 15 menit (hemat RAM, ada progres & bisa dibatalkan)."""
    from faster_whisper import BatchedInferencePipeline
    pipe = BatchedInferencePipeline(model=whisper_model(model_name, cfg, log))
    tmp = tempfile.mkdtemp(prefix="autoclip_")
    words, nsp, probs = [], [], []
    lang = None if language == "auto" else language
    try:
        c0 = 0.0
        while c0 < dur - 0.5:
            check_cancel()
            L = min(chunk, dur - c0)
            wav = os.path.join(tmp, "a.wav")
            extract_wav(path, start + c0, L, wav)
            segs, info = pipe.transcribe(wav, batch_size=cfg["batch"], language=lang, word_timestamps=True,
                                         vad_filter=True,
                                         initial_prompt=(prompt or None) if "large" in model_name else None)
            if lang is None and info.language_probability > 0.5:
                lang = info.language
                log(f"      bahasa terdeteksi: {lang} ({info.language_probability:.0%})")
            for s in segs:
                check_cancel()
                nsp.append(s.no_speech_prob or 0.0)
                for w in s.words or []:
                    probs.append(w.probability)
                    if w.word.strip() and w.probability >= 0.4:
                        words.append((start + c0 + w.start, start + c0 + w.end, w.word.strip()))
                progress(min(1.0, (c0 + s.end) / dur))
            c0 += L
            progress(min(1.0, c0 / dur))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    words = fix_words(dedupe_words(words))
    stats = dict(no_speech=float(np.mean(nsp)) if nsp else 1.0, word_prob=float(np.mean(probs)) if probs else 0.0,
                 words=len(words), lang=lang or (language if language != "auto" else None))
    return words, stats


def dedupe_words(words):
    """Buang pengulangan khas halusinasi Whisper (frasa sama berulang >2x berturut-turut)."""
    toks = [clean(w) for _, _, w in words]
    keep = [True] * len(words)
    i = 0
    while i < len(words):
        found = False
        for nlen in range(1, 9):
            if i + 3 * nlen > len(words):
                break
            pat = toks[i:i + nlen]
            reps = 1
            while toks[i + reps * nlen:i + (reps + 1) * nlen] == pat:
                reps += 1
            if reps >= 3:
                for k in range(i + nlen, i + reps * nlen):
                    keep[k] = False
                i += reps * nlen
                found = True
                break
        if not found:
            i += 1
    out = []
    for w in (w for w, k in zip(words, keep) if k):
        if out and w[2].startswith("-") and w[0] - out[-1][1] < 0.6:
            out[-1] = (out[-1][0], w[1], out[-1][2] + w[2])
        else:
            out.append(w)
    return out


def build_sentences(words):
    sents, cur = [], []

    def flush():
        if cur:
            sents.append(dict(start=cur[0][0], end=cur[-1][1], text=" ".join(w for _, _, w in cur), words=list(cur)))
            cur.clear()
    for s, e, w in words:
        if cur and s - cur[-1][1] > 0.8:
            flush()
        cur.append((s, e, w))
        if re.search(r"[.!?]$", w) or len(cur) >= 28:
            flush()
    flush()
    return sents


def clean(w):
    return re.sub(r"[^\w\-]", "", w.lower())


def kw_emoji(word):
    w = clean(word)
    if w in KW:
        return KW[w]
    for k, e in KW.items():
        if len(k) >= 4 and w.startswith(k) and len(w) <= len(k) + 3:
            return e
    return None


def norm(x):
    if len(x) == 0:
        return x
    lo, hi = np.percentile(x, 5), np.percentile(x, 95)
    if hi - lo < 1e-9:
        return np.zeros_like(x, dtype=float)
    return np.clip((x - lo) / (hi - lo), 0, 1)


def smooth(x, k):
    k = max(1, min(k, len(x)))
    return np.convolve(x, np.ones(k) / k, "same")


def clean_title(t):
    t = re.sub(r"#\S+|\[[^\]]*\]|\([^)]*\)|https?://\S+", " ", t or "")
    t = re.sub(r"[^\w\s!?.,'&-]", " ", t)
    t = re.sub(r"\.{2,}", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -|.,")
    ws = t.split()
    return " ".join(ws[:8]).upper() if ws else ""


def hms(t, sep="-"):
    return f"{int(t // 3600):02d}{sep}{int(t % 3600 // 60):02d}{sep}{int(t % 60):02d}"


# ============================================================================ penilaian AI

CLIP_REPO = "Xenova/clip-vit-base-patch32"
ACTION_POS = {  # adegan yang menarik penonton (bobot)
    "a video game screenshot of a gunfight with muzzle flashes and bullets flying": 1.0,
    "a video game screenshot of shooting a gun at enemies": 1.0,
    "a video game screenshot of a huge explosion with fire and smoke": 1.0,
    "a video game screenshot of a car crashing into another car": 1.0,
    "a video game screenshot of a car flipping over in an accident": 1.0,
    "a video game screenshot of a police chase with sirens and crashing cars": 0.9,
    "a video game screenshot of a helicopter shooting with a machine gun": 0.9,
    "a video game screenshot of a burning car wreck": 0.9,
    "a video game screenshot of a brutal fist fight": 0.8,
    "a video game screenshot of a character getting shot and blood": 0.9,
}
ACTION_NEG = [
    "a video game screenshot of a character running on the street",
    "a video game screenshot of a character sprinting away",
    "a video game screenshot of a character jogging",
    "a video game screenshot of a person walking calmly",
    "a video game screenshot of a character sneaking crouched in the dark",
    "a video game screenshot of a character climbing a wall or fence",
    "a video game screenshot of looking through binoculars",
    "a video game screenshot of driving a car calmly on a road",
    "a video game screenshot of a parked car in a garage",
    "a video game pause menu, map or settings screen",
    "a video game cutscene of people talking",
    "a video game screenshot inside a house or shop",
    "a video game screenshot of swimming underwater",
    "a video game character customization or clothing screen",
    "a loading screen with text",
    "a person standing still in a game",
    "a video game screenshot of a city skyline at night",
    "a video game screenshot of driving a boat on water",
    "a video game screenshot of flying a helicopter calmly in the sky",
    "a video game screenshot of a character riding a bicycle or motorcycle calmly",
    "a video game weapon selection wheel menu",
    "a close-up of hands holding an object in a game",
    "a video game screenshot of driving a pickup truck off-road",
    "a video game screenshot of a car on a highway at night",
    "a video game screenshot of a character using a power tool or nail gun",
    "a video game screenshot of a character doing chores or repairing something",
    "a close-up of photos, cards and money in a box",
    "a close-up of a character's back or shoulder",
]
_CLIP = {}


CLIP_BIG = "Xenova/clip-vit-large-patch14"  # pemeriksa teliti (~430 MB) untuk kandidat terbaik


def clip_models(repo=CLIP_REPO):
    """Model CLIP ONNX terkuantisasi (sekali unduh, disimpan di folder aplikasi)."""
    if repo not in _CLIP:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        def get(fn):
            try:  # sudah ada di laptop -> tanpa internet
                return hf_hub_download(repo, fn, local_files_only=True)
            except Exception:
                return hf_hub_download(repo, fn)
        vp, tp, tk = get("onnx/vision_model_quantized.onnx"), get("onnx/text_model_quantized.onnx"), get("tokenizer.json")
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(2, (os.cpu_count() or 4) - 2)
        tok = Tokenizer.from_file(tk)
        tok.enable_padding(length=77, pad_id=49407)
        tok.enable_truncation(77)
        _CLIP[repo] = dict(v=ort.InferenceSession(vp, so, providers=["CPUExecutionProvider"]),
                           t=ort.InferenceSession(tp, so, providers=["CPUExecutionProvider"]), tok=tok)
    return _CLIP[repo]


def clip_text(labels, repo=CLIP_REPO):
    m = clip_models(repo)
    key = repo + "\n" + "\n".join(labels)
    if key not in _CLIP:
        ids = np.array([e.ids for e in m["tok"].encode_batch(labels)], np.int64)
        te = m["t"].run(None, {"input_ids": ids})[0]
        _CLIP[key] = te / np.linalg.norm(te, axis=1, keepdims=True)
    return _CLIP[key]


def vision_embeds(path, info, cam_rect=None, step=2.0, log=print, progress=lambda p: None):
    """'Sidik gambar' AI penglihatan (512 angka) tiap `step` detik, disimpan di cache -> penilaian bisa diubah tanpa memindai ulang."""
    import hashlib
    st_ = os.stat(path)
    key = hashlib.sha1(f"{os.path.basename(path)}|{st_.st_size}|{int(st_.st_mtime)}|emb1|{step}".encode()).hexdigest()[:16]
    cf = os.path.join(APP_DIR, "_cache", key + "_emb.npy")
    if os.path.exists(cf):
        return np.load(cf).astype(np.float32)
    m = clip_models()
    D = info["duration"]
    W_, H_ = 320, int(320 * info["height"] / info["width"]) // 2 * 2
    log(f"      AI penglihatan memeriksa gambar video (1 tiap {step:.0f} dtk, ~{int(D / step)} gambar, sekali saja)...")
    proc = subprocess.Popen([ffmpeg_exe(), "-v", "error", *info["dec"], "-i", path, "-an",
                             "-vf", f"fps=1/{step},scale={W_}:{H_}", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
                            stdout=subprocess.PIPE, creationflags=NOWIN)
    mean = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
    std = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
    S_ = max(W_, H_)
    embs, batch = [], []
    from PIL import Image

    def flush():
        ie = m["v"].run(None, {"pixel_values": np.stack(batch)})[0]
        embs.append((ie / np.linalg.norm(ie, axis=1, keepdims=True)).astype(np.float16))
        batch.clear()
    fsz = W_ * H_ * 3
    got = 0
    try:
        while True:
            if CANCEL.is_set():
                proc.kill()
                raise Cancelled()
            buf = proc.stdout.read(fsz)
            if len(buf) < fsz:
                break
            fr = np.frombuffer(buf, np.uint8).reshape(H_, W_, 3).copy()
            if cam_rect:  # tutup facecam supaya wajah streamer tidak ikut dinilai
                x0, y0 = int(cam_rect[0] * W_), int(cam_rect[1] * H_)
                fr[y0:y0 + int(cam_rect[3] * H_) + 1, x0:x0 + int(cam_rect[2] * W_) + 1] = 128
            sq = np.full((S_, S_, 3), 128, np.uint8)  # letterbox: seluruh gambar dinilai, bukan hanya tengah
            sq[(S_ - H_) // 2:(S_ - H_) // 2 + H_, (S_ - W_) // 2:(S_ - W_) // 2 + W_] = fr
            im = np.asarray(Image.fromarray(sq).resize((224, 224), Image.BICUBIC), np.float32) / 255
            batch.append(((im - mean) / std).transpose(2, 0, 1))
            got += 1
            if len(batch) >= 32:
                flush()
                progress(min(1.0, got * step / max(1, D)))
        if batch:
            flush()
    finally:
        proc.wait()
    E = np.concatenate(embs) if embs else np.zeros((0, 512), np.float16)
    np.save(cf, E)
    return E.astype(np.float32)


def action_probs(E, repo=CLIP_REPO):
    pos = list(ACTION_POS)
    te = clip_text(pos + ACTION_NEG, repo)
    sim = 100 * E @ te.T
    pr = np.exp(sim - sim.max(1, keepdims=True))
    pr /= pr.sum(1, keepdims=True)
    return (pr[:, :len(pos)] * np.array([ACTION_POS[k] for k in pos], np.float32)).sum(1)


def clip_frames(repo, frames):
    """frames: daftar gambar RGB uint8 (sudah ditutup facecam) -> embedding ternormalisasi."""
    from PIL import Image
    m = clip_models(repo)
    mean = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
    std = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
    X = []
    for fr in frames:
        H_, W_ = fr.shape[:2]
        S_ = max(W_, H_)
        sq = np.full((S_, S_, 3), 128, np.uint8)
        sq[(S_ - H_) // 2:(S_ - H_) // 2 + H_, (S_ - W_) // 2:(S_ - W_) // 2 + W_] = fr
        im = np.asarray(Image.fromarray(sq).resize((224, 224), Image.BICUBIC), np.float32) / 255
        X.append(((im - mean) / std).transpose(2, 0, 1))
    out = []
    for i in range(0, len(X), 16):
        ie = m["v"].run(None, {"pixel_values": np.stack(X[i:i + 16])})[0]
        out.append(ie / np.linalg.norm(ie, axis=1, keepdims=True))
    return np.concatenate(out) if out else np.zeros((0, 768), np.float32)


def verify_action(path, info, cam_rect, windows, every=2.5, log=print):
    """Tahap 2: periksa ulang kandidat dengan model besar (lebih teliti). Return skor 0-1 per jendela."""
    W_, H_ = 448, int(448 * info["height"] / info["width"]) // 2 * 2
    res = []
    for s, e in windows:
        check_cancel()
        raw = run([ffmpeg_exe(), "-v", "error", "-ss", f"{s:.2f}", "-t", f"{e - s:.2f}", *info["dec"], "-i", path, "-an",
                   "-vf", f"fps=1/{every},scale={W_}:{H_}", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"])
        sz = W_ * H_ * 3
        frs = [np.frombuffer(raw, np.uint8)[i * sz:(i + 1) * sz].reshape(H_, W_, 3).copy() for i in range(len(raw) // sz)]
        for fr in frs:
            if cam_rect:
                x0, y0 = int(cam_rect[0] * W_), int(cam_rect[1] * H_)
                fr[y0:y0 + int(cam_rect[3] * H_) + 1, x0:x0 + int(cam_rect[2] * W_) + 1] = 128
        if not frs:
            res.append(0.0)
            continue
        sc = np.sort(action_probs(clip_frames(CLIP_BIG, frs), CLIP_BIG))[::-1]
        res.append(float(0.5 * sc[:3].mean() + 0.3 * sc[:max(1, len(sc) // 2)].mean() + 0.2 * sc.mean()))
    return res


def action_scores(E, step, n):
    """Skor 'adegan seru' 0-1 per HOP: tembak-tembakan, ledakan, kecelakaan, kejar-kejaran (bukan sekadar berlari)."""
    if not len(E):
        return np.zeros(n)
    sc = action_probs(E)
    return np.interp(np.arange(n) * HOP, np.arange(len(sc)) * step + step / 2, sc)


def vision_action(path, info, cam_rect=None, step=2.0, log=print, progress=lambda p: None):
    n = int(info["duration"] / HOP) + 1
    return action_scores(vision_embeds(path, info, cam_rect, step, log, progress), step, n)


def game_impacts(path, info, words, n):
    """Lonjakan suara tajam SAAT streamer tidak bicara = suara game (tembakan, ledakan, tabrakan). Per HOP."""
    import hashlib
    st_ = os.stat(path)
    key = hashlib.sha1(f"{os.path.basename(path)}|{st_.st_size}|{int(st_.st_mtime)}|imp1".encode()).hexdigest()[:16]
    cf = os.path.join(APP_DIR, "_cache", key + "_imp.npy")
    if os.path.exists(cf):
        tr = np.load(cf)
    else:
        sr = 8000
        raw = run([ffmpeg_exe(), "-v", "error", "-i", path, "-vn", "-ac", "1", "-af", "highpass=f=1500", "-ar", str(sr),
                   "-f", "s16le", "pipe:1"])
        x = np.frombuffer(raw, np.int16).astype(np.float32)
        fr = sr // 50
        k = len(x) // fr
        db_ = 20 * np.log10(np.sqrt((x[:k * fr].reshape(k, fr) ** 2).mean(1)) + 1)
        base = np.array([np.median(db_[max(0, i - 12):i + 13]) for i in range(0, k)]) if k < 2000 else None
        if base is None:
            from numpy.lib.stride_tricks import sliding_window_view
            pad = np.pad(db_, 12, mode="edge")
            base = np.median(sliding_window_view(pad, 25), axis=1)
        tr = ((db_ - base > 12) & (db_ > np.percentile(db_, 85))).astype(np.uint8)
        np.save(cf, tr)
    speech = np.zeros(len(tr), bool)
    for ws, we, _ in words:
        speech[max(0, int((ws - 0.15) * 50)):int((we + 0.15) * 50)] = True
    g = (tr.astype(bool) & ~speech)
    per = g[:len(g) // 25 * 25].reshape(-1, 25).sum(1).astype(float)
    per = np.pad(per, (0, max(0, n - len(per))))[:n]
    return norm(smooth(per, 10))


def action_blend(vis, imp, motion):
    """Gabung skor aksi: gambar (utama, dinilai relatif: hanya ~10% momen teratas dianggap aksi), suara game, gerak."""
    v = smooth(vis, 4)
    lo, hi = np.percentile(v, 60), np.percentile(v, 97)
    vr = np.clip((v - lo) / max(1e-6, hi - lo), 0, 1)
    return np.clip(0.9 * vr + 0.1 * imp * vr, 0, 1)  # gambar = penentu utama; suara game hanya penguat


def build_features(n, db, fs):
    loud = norm(db)
    rise = db - smooth(db, 20)
    peak = (rise > 6).astype(float)
    motion = norm(fs["motion"])
    # aksi: rentetan lonjakan suara pendek (tembakan, ledakan, benturan) + layar bergerak cepat
    burst = np.maximum(0, np.diff(db, prepend=db[:1])) > 4
    impact = norm(smooth(burst.astype(float), 8))
    action = norm(0.55 * impact + 0.45 * smooth(motion, 6))
    return dict(energy=0.6 * loud + 0.4 * norm(np.maximum(0, rise)), peak=peak, motion=motion, action=action)


def hook_score(text):
    ws = text.split()
    h = 0.0
    if "?" in text:
        h += 0.4
    if any(kw_emoji(w) for w in ws[:8]):
        h += 0.3
    if re.search(r"\d", text):
        h += 0.15
    if any(clean(w) in ADDRESS for w in ws[:8]):
        h += 0.15
    if 3 <= len(ws) <= 12:
        h += 0.15
    return min(1.0, h)


REASON_TXT = {
    "energy": "Suasana heboh: energi & suara tinggi",
    "peak": "{k} momen reaksi keras/tawa",
    "kw": "Kata-kata emosional: {w}",
    "hook": "Hook kuat di kalimat pertama{q}",
    "pace": "Tempo bicara cepat & padat",
    "motion": "Banyak aksi di layar",
    "action": "Adegan aksi: tembakan/ledakan & gerak cepat",
}
CATEGORY = {"energy": "Heboh", "peak": "Reaksi", "kw": "Emosional", "hook": "Hook kuat", "pace": "Informatif",
            "motion": "Aksi", "action": "Aksi/Pertempuran"}


def score_span(sents, i, j, feats, n, w8=None):
    """Skor lokal untuk rentang kalimat i..j: (raw, detail)."""
    s, e = sents[i]["start"], sents[j]["end"]
    a, b = int(s / HOP), max(int(s / HOP) + 1, min(n, int(math.ceil(e / HOP))))
    words = [w for k in range(i, j + 1) for w in sents[k]["words"]]
    L = max(1.0, e - s)
    kws = [w for _, _, w in words if kw_emoji(w)]
    spoken = sum(we - ws for ws, we, _ in words)
    f = dict(
        energy=float(feats["energy"][a:b].mean()),
        peak=min(1.0, float(feats["peak"][a:b].sum()) / 3),
        kw=min(1.0, len(kws) / L * 60 / 8),
        hook=hook_score(sents[i]["text"]),
        pace=min(1.0, len(words) / L / 3.0),
        motion=float(feats["motion"][a:b].mean()),
        action=min(1.0, 0.55 * float(feats["action"][a:b].mean()) + 0.35 * float(np.percentile(feats["action"][a:b], 90))
                   + 0.1 * min(1.0, sum(1 for _, _, w in words if clean(w) in ACTION_WORDS) / L * 60 / 6)),
    )
    end_ok = 1.0 if re.search(r"[.!?]$", sents[j]["text"]) else 0.5
    silence = max(0.0, 1 - spoken / L - 0.25)
    w8 = w8 or W_DEFAULT
    raw = sum(w8.get(k, 0.0) * v for k, v in f.items()) + 0.06 * end_ok - 0.2 * silence * (0.4 if w8.get("action", 0) > 0.2 else 1)
    return raw, f, kws, s, e


ACTION_MIN = 0.55  # tahap 1 (model cepat): ambang kandidat
ACTION_MIN2 = 0.5  # tahap 2 (model besar): ambang adegan aksi sungguhan


def action_select(feats, sents, n, target, count, dur, verify=None, log=print):
    """Mode Action: ambil jendela dengan skor gambar aksi tertinggi, lalu rapikan ke batas kalimat."""
    act = feats["action"]
    L = max(4, int(target / HOP))
    cs = np.concatenate([[0], np.cumsum(act)])
    win = (cs[L:] - cs[:-L]) / L  # rata-rata skor aksi tiap jendela
    peak = np.array([act[i:i + L].max() for i in range(0, len(win))]) if len(win) < 60000 else win
    sc = 0.6 * win + 0.4 * peak
    starts = [x["start"] for x in sents]
    ends = [x["end"] for x in sents]
    raw = feats.get("vision_raw", act)
    picks, used = [], []
    best = float(sc.max()) if len(sc) else 1
    want = count * 5 if verify else count  # tahap 1: banyak kandidat
    for i in np.argsort(-sc):
        if sc[i] < 0.45 * best or len(picks) >= want:
            break
        s, e = i * HOP, i * HOP + target
        if any(not (e + 4 < a or s > b + 4) for a, b in used):
            continue
        # rapikan: mulai di awal kalimat terdekat (maks 2 dtk lebih awal), akhiri di akhir kalimat (maks 3 dtk lebih lama)
        s2 = max([t for t in starts if s - 2.0 <= t <= s + 0.5] or [s])
        e2 = min([t for t in ends if e - 0.5 <= t <= e + 3.0] or [e])
        s2, e2 = max(0.0, min(s2, s)), min(dur, max(e2, e - 0.5))
        used.append((s2, e2))
        a_, b_ = int(s2 / HOP), max(int(s2 / HOP) + 1, int(e2 / HOP))
        pk = float(act[a_:b_].max())
        if float(raw[a_:b_].max()) < ACTION_MIN:
            continue  # bukan aksi sungguhan -> jangan dipaksakan
        picks.append(dict(start=round(s2, 2), end=round(e2, 2), score=int(np.clip(round(100 * sc[i] / best), 1, 99)),
                          title="", category="Aksi/Pertempuran",
                          reason=f"Adegan aksi paling seru (tembak-tembakan/ledakan/kecelakaan), puncak {pk:.0%}",
                          hashtags=[]))
    if verify and picks:
        log(f"      memeriksa ulang {len(picks)} kandidat dengan AI penglihatan besar (lebih teliti)...")
        vs = verify([(p_["start"], p_["end"]) for p_ in picks])
        for p_, v_ in zip(picks, vs):
            p_["v2"] = v_
        good = sorted([p_ for p_ in picks if p_["v2"] >= ACTION_MIN2], key=lambda p_: -p_["v2"])
        dropped = len(picks) - len(good)
        gap = max(240.0, target * 6)  # variasi: jangan semua dari satu misi yang sama
        keep = []
        for p_ in good:
            if all(abs(p_["start"] - q["start"]) >= gap for q in keep):
                keep.append(p_)
        for p_ in good:  # kalau kurang, isi dengan sisa terbaik
            if len(keep) >= count:
                break
            if p_ not in keep:
                keep.append(p_)
        keep = sorted(keep[:count], key=lambda p_: -p_["v2"])
        top = keep[0]["v2"] if keep else 1
        for p_ in keep:
            p_["score"] = int(np.clip(round(100 * (0.35 + 0.65 * p_["v2"] / top)), 1, 99))
            p_["reason"] = f"Adegan aksi terverifikasi ({p_['v2']:.0%}): tembak-tembakan/ledakan/kecelakaan"
        log(f"      {dropped} kandidat dibuang (bukan aksi sungguhan: berlari, menu, alat, dll)")
        return keep
    return picks


def local_ai_select(sents, feats, n, target, count, strict, log, w8=None):
    mn, mx = max(8.0, target * 0.6), target * 1.35
    cands = []
    for i in range(len(sents)):
        for j in range(i, len(sents)):
            L = sents[j]["end"] - sents[i]["start"]
            if L > mx:
                break
            if L >= mn:
                raw, f, kws, s, e = score_span(sents, i, j, feats, n, w8)
                cands.append((raw, i, j, f, kws, s, e))
    if not cands:
        return []
    raws = np.array([c[0] for c in cands])
    best = raws.max()
    pr = raws.argsort().argsort() / max(1, len(raws) - 1)
    picks = []
    for idx in np.argsort(-raws):
        raw, i, j, f, kws, s, e = cands[idx]
        if raw < STRICT.get(strict, 0.7) * best:
            break
        if any(not (e + 3 < p["start"] or s > p["end"] + 3) for p in picks):
            continue
        score = int(np.clip(round(100 * (0.45 * pr[idx] + 0.55 * raw / best)), 1, 99))
        top = sorted(f.items(), key=lambda kv: -kv[1] * (1.6 if (w8 or {}).get(kv[0], 0) >= 0.3 else 1))[:3]
        reasons = []
        for k, v in top:
            if v < 0.35:
                continue
            uniq = list(dict.fromkeys(clean(w) for w in kws))[:3]
            reasons.append(REASON_TXT[k].format(k=int(round(v * 3)), w=", ".join(uniq) or "-",
                                                q=" (pertanyaan)" if "?" in sents[i]["text"] else ""))
        cat = CATEGORY[top[0][0]]
        if any(KW.get(clean(w)) == "😂" for w in kws):
            cat = "Lucu"
        if (w8 or {}).get("action", 0) >= 0.5 and f.get("action", 0) >= 0.5:
            cat = "Aksi/Pertempuran"
        picks.append(dict(start=s, end=e, score=score, title="", reason="; ".join(reasons) or "Momen paling hidup",
                          category=cat, hashtags=[]))
        if len(picks) >= count:
            break
    return picks


CLIP_SCHEMA = {
    "type": "object",
    "properties": {"clips": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "start_id": {"type": "integer"}, "end_id": {"type": "integer"}, "score": {"type": "integer"},
            "title": {"type": "string"}, "reason": {"type": "string"}, "category": {"type": "string"},
            "hook": {"type": "string"}, "yt_title": {"type": "string"}, "description": {"type": "string"},
            "thumb_text": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["start_id", "end_id", "score", "title", "reason", "category", "hashtags", "hook", "yt_title",
                     "description", "thumb_text"],
        "additionalProperties": False,
    }}},
    "required": ["clips"],
    "additionalProperties": False,
}


def claude_ai_select(sents, feats, n, target, count, video_title, api_key, log, brief=""):
    """Claude membaca transkrip berstempel waktu dan memilih momen terkuat."""
    import anthropic
    mn, mx = max(8.0, target * 0.6), target * 1.35
    lines = []
    for k, s in enumerate(sents):
        a, b = int(s["start"] / HOP), max(int(s["start"] / HOP) + 1, int(math.ceil(s["end"] / HOP)))
        mark = (" (!)" if feats["peak"][a:b].sum() > 0 else "") + (" [AKSI]" if feats.get("action") is not None
                                                                  and feats["action"][a:b].mean() > 0.6 else "")
        lines.append(f"S{k} [{hms(s['start'], ':')}]{mark} {s['text']}")
    transcript = "\n".join(lines)
    log(f"      Claude membaca {len(sents)} kalimat (~{len(transcript) // 3500}k token, "
        f"estimasi biaya ~${len(transcript) / 3.5 / 1e6 * 4 + 0.05:.2f})...")
    system = ("Kamu editor video short-form (TikTok/Reels/Shorts) berpengalaman untuk penonton Indonesia. "
              "Tugasmu memilih momen dari video panjang yang paling berpotensi viral sebagai klip berdiri sendiri.")
    prompt = (
        f"Judul video: {video_title or '-'}\n\n"
        f"Transkrip berikut terdiri dari kalimat bernomor (S0, S1, ...) dengan waktu mulai. Tanda (!) berarti "
        f"ada lonjakan suara (tawa, teriakan, reaksi) di kalimat itu; [AKSI] berarti banyak tembakan/ledakan & gerakan cepat di layar.\n\n<transkrip>\n{transcript}\n</transkrip>\n\n"
        + (f"Jenis konten: {brief}\n\n" if brief else "") +
        f"Pilih sampai {count} klip terbaik. Aturan:\n"
        f"- Satu klip = rentang kalimat start_id..end_id, durasi {mn:.0f}-{mx:.0f} detik.\n"
        f"- Klip harus bisa dipahami tanpa konteks, dibuka dengan hook kuat di 3 detik pertama, dan selesai di akhir "
        f"pikiran/punchline.\n- Klip tidak boleh tumpang tindih. Lebih baik sedikit klip bagus daripada banyak klip "
        f"biasa: jangan memasukkan klip dengan skor di bawah 60.\n"
        f"- score: 0-100, seberapa besar peluang viral.\n- title: judul hook bahasa Indonesia, maks 8 kata, huruf "
        f"kapital, tanpa emoji.\n- reason: 1 kalimat bahasa Indonesia, kenapa klip ini akan berhasil.\n"
        f"- category: satu kata (Lucu, Heboh, Informatif, Emosional, Drama, Tips, Reaksi, Aksi, Cerita).\n"
        f"- hashtags: 3-5 hashtag relevan, diawali #.\n"
        f"- hook: teks pembuka di layar, kalimat biasa (bukan kapital semua), maks 8 kata, bikin penasaran, "
        f"contoh gaya: '120 ribu orang nonton, tapi...'.\n"
        f"- yt_title: judul YouTube menarik maks 70 karakter (bahasa Indonesia, boleh 1 emoji yang relevan).\n"
        f"- description: deskripsi YouTube 3-5 kalimat bahasa Indonesia: ringkasan momen yang bikin penasaran, "
        f"ajakan subscribe/komentar, lalu hashtag di baris terakhir.\n"
        f"- thumb_text: 2-4 kata KAPITAL untuk thumbnail, sangat menarik perhatian, tanpa emoji.")
    client = anthropic.Anthropic(api_key=api_key or None)
    resp = client.beta.messages.create(
        model=CLAUDE_MODEL, max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": CLIP_SCHEMA}},
        system=system, messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude menolak permintaan ini")
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    picks = []
    for c in sorted(data["clips"], key=lambda c: -c["score"]):
        i, j = max(0, c["start_id"]), min(len(sents) - 1, c["end_id"])
        if j < i:
            continue
        while j > i and sents[j]["end"] - sents[i]["start"] > mx * 1.15:
            j -= 1
        s, e = sents[i]["start"], sents[j]["end"]
        if e - s < mn * 0.6 or any(not (e + 2 < p["start"] or s > p["end"] + 2) for p in picks):
            continue
        picks.append(dict(start=s, end=e, score=int(np.clip(c["score"], 1, 99)), title=clean_title(c["title"]),
                          reason=c["reason"], category=c["category"],
                          hashtags=[h if h.startswith("#") else "#" + h for h in c["hashtags"]][:5],
                          hook=c["hook"], yt_title=c["yt_title"], description=c["description"],
                          thumb_text=clean_title(c["thumb_text"])))
        if len(picks) >= count:
            break
    log(f"      Claude memilih {len(picks)} klip (token masuk {resp.usage.input_tokens}, keluar {resp.usage.output_tokens})")
    return picks


def local_hook(segs):
    """Kalimat pembuka yang bikin penasaran dari transkrip klip (kalimat biasa, maks 8 kata)."""
    best, bs = None, -9
    for x in segs[:6]:
        ws = x["text"].split()
        if len(ws) < 3:
            continue
        sc = 1.5 * ("?" in x["text"]) + sum(1 for w in ws if kw_emoji(w)) + 0.5 * bool(re.search(r"\d", x["text"]))
        sc -= abs(len(ws) - 7) * 0.1
        if sc > bs:
            best, bs = strip_filler(fix_text(x["text"])), sc
    if not best:
        return ""
    ws = best.split()
    t = " ".join(ws[:8]).rstrip(",.;:")
    return sentence_case(t + ("..." if len(ws) > 8 else ""))


TAG_STOP = set("""aku kamu dia kita gue gua lu dan atau yang ini itu ada akan sudah udah baru lagi juga bisa jadi
mau sama saja aja nih tuh dong deh kok ketika saat sampai banget bikin kenapa gimana apa siapa main the and with for""".split())


def default_hashtags(category, video_title):
    tags = ["#fyp", "#viral"]
    if category:
        tags.append("#" + clean(category))
    for w in (video_title or "").split():
        w2 = clean(w).replace("-", "")
        if len(w2) >= 3 and not w2.isdigit() and w2 not in TAG_STOP and len(tags) < 6:
            tags.append("#" + w2)
    return list(dict.fromkeys(tags))


# ============================================================================ deteksi ketukan (beat)

def onset_envelope(path, start, dur):
    sr, hop, nfft = 11025, 256, 1024
    raw = run([ffmpeg_exe(), "-v", "error", "-ss", f"{max(0, start):.3f}", "-t", f"{dur:.3f}", "-i", path,
               "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "pipe:1"])
    x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    nfr = (len(x) - nfft) // hop + 1
    if nfr < 50:
        return np.zeros(1), sr / hop
    idx = np.arange(nfft)[None, :] + hop * np.arange(nfr)[:, None]
    S = np.log1p(30 * np.abs(np.fft.rfft(x[idx] * np.hanning(nfft), axis=1))[:, :400])
    env = np.concatenate([[0], np.maximum(0, np.diff(S, axis=0)).sum(1)])
    fr = sr / hop
    env = np.maximum(0, env - smooth(env, int(fr * 0.4)))
    return env / (env.max() + 1e-9), fr


def beat_grid(env, fr):
    n = len(env)
    e = env - env.mean()
    F = np.fft.rfft(e, 2 * n)
    ac = np.fft.irfft(F * np.conj(F))[:n]
    bpms = np.arange(70, 181)
    vals = np.interp(60 * fr / bpms, np.arange(n), ac)
    prior = np.exp(-0.5 * (np.log2(bpms / 115) / 0.7) ** 2)
    bpm = float(bpms[int(np.argmax(vals * prior))])
    P = 60 / bpm
    best, bo = -1, 0.0
    for o in np.arange(0, P, 1 / fr):
        ix = np.round((o + np.arange(int((n / fr - o) / P) + 1) * P) * fr).astype(int)
        sc = env[ix[ix < n]].sum()
        if sc > best:
            best, bo = sc, o
    beats = bo + np.arange(int((n / fr - bo) / P) + 1) * P
    strength = np.array([env[max(0, int(b * fr) - 2):int(b * fr) + 3].max(initial=0) for b in beats])
    down = int(np.argmax([strength[o::4].sum() for o in range(4)])) if len(beats) >= 8 else 0
    return bpm, P, beats, strength, down


# ============================================================================ analisa jenis konten

def visual_profile(path, info, dur, samples=24):
    """Ciri visual video: wajah (besar/kecil/facecam/berapa orang), layar komputer, warna."""
    try:
        import cv2
        cv2.ocl.setUseOpenCL(False)
        casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    except ImportError:
        return dict(face_ratio=0, face_w=0, multi=0, facecam=None, screen=0, sat=0.3)
    boxes_all, n_face, n_multi, ws, screen, sats, tried = [], 0, 0, [], 0, [], 0
    for t in np.linspace(dur * 0.04, dur * 0.96, samples):
        check_cancel()
        try:
            im = grab_frame(path, t, info["dec"], width=640)
        except Exception:
            continue
        tried += 1
        g = np.asarray(im.convert("L"))
        faces = [f for f in casc.detectMultiScale(g, 1.1, 6, minSize=(20, 20))]
        faces = [(x / g.shape[1], y / g.shape[0], w / g.shape[1], h / g.shape[0]) for x, y, w, h in faces]
        boxes_all.append(faces)
        if faces:
            n_face += 1
            big = max(faces, key=lambda f: f[2] * f[3])
            ws.append(big[2])
            if sum(1 for f in faces if f[2] > 0.04) >= 2:
                n_multi += 1
        hsv = np.asarray(im.convert("HSV"), np.float32)
        sat = hsv[..., 1].mean() / 255
        sats.append(sat)
        gf = g.astype(np.float32)
        edges = (np.abs(np.diff(gf, axis=1)) > 40).mean()
        flat = (np.abs(np.diff(gf, axis=1)) < 2).mean()
        if sat < 0.22 and edges > 0.035 and flat > 0.55:
            screen += 1
    tried = max(1, tried)
    cam = consistent_small_face(boxes_all)
    return dict(face_ratio=n_face / tried, face_w=float(np.median(ws)) if ws else 0.0, multi=n_multi / tried,
                facecam=cam, screen=screen / tried, sat=float(np.mean(sats)) if sats else 0.3)


def detect_genre(title, words, music, wpm, cuts_pm, vis):
    """Tebak jenis konten dari judul, kata yang diucapkan, dan ciri visual. Return (key, alasan)."""
    text = " " + (title or "").lower() + " " + " ".join(clean(w) for _, _, w in words[:8000]) + " "
    tl = " " + (title or "").lower() + " "
    score, why = {}, {}
    for key, g in GENRES.items():
        if key in ("otomatis",) or not g["kw"]:
            continue
        sc, hit = 0.0, []
        for k in g["kw"].split():
            k2 = k.replace("_", " ")
            c = text.count(" " + k2 + " ") + text.count(" " + k2)
            if c:
                sc += min(3.0, math.log1p(c))
                hit.append(k2)
            if (" " + k2) in tl:
                sc += 3
        score[key], why[key] = sc, hit[:3]
    fr, fw, mul, cam, scr = vis["face_ratio"], vis["face_w"], vis["multi"], vis["facecam"], vis["screen"]
    vis_why = {}

    def add(k, v, r):
        score[k] = score.get(k, 0) + v
        vis_why.setdefault(k, []).append(r)
    if cam:
        add("gaming", 3, "facecam kecil di pojok")
        add("reaction", 1.5, "facecam")
        if scr > 0.4:
            add("tutorial_pc", 3, "layar komputer + facecam")
            add("presentasi", 1.5, "layar/slide")
    elif scr > 0.5:
        add("tutorial_pc", 3, "layar komputer")
        add("presentasi", 2, "layar/slide")
    if fr > 0.45 and fw > 0.08 and not cam and scr < 0.2 and wpm > 60:
        add("podcast", 2.5, "orang berbicara ke kamera/mik (multikamera)")
    if fr > 0.55 and fw > 0.08 and not cam:
        add("podcast", 2 + (1.5 if wpm > 100 else 0), "wajah besar & banyak bicara")
        add("vlog", 1, "wajah")
        add("komedi", 0.5, "wajah")
    if mul > 0.35:
        add("interview", 1.5, "beberapa orang bergantian")
        add("podcast", 1.5, "beberapa orang")
    if music:
        add("musik" if fr > 0.3 else "sinematik", 4, "musik terdeteksi")
    if cuts_pm > 12 and fr < 0.3 and wpm < 60:
        add("sinematik", 3, f"{cuts_pm:.0f} potongan adegan/menit")
    if 6 < cuts_pm and fr < 0.4 and not cam and wpm < 90 and not music:
        add("action", 2, f"adegan cepat ({cuts_pm:.0f} potongan/menit) & sedikit dialog")
    if fr < 0.25 and not cam and wpm < 80 and cuts_pm < 12:
        add("sport", 1, "banyak aksi tanpa wajah")
    best = max(score, key=score.get) if score else "hiburan"
    if score.get(best, 0) < 2:
        best = "podcast" if fr > 0.5 else "hiburan"
    reasons = vis_why.get(best, []) + ([f"kata: {', '.join(why.get(best, []))}"] if why.get(best) else [])
    return best, "; ".join(reasons) or "ciri umum"


def claude_classify(title, words, vis, api_key):
    import anthropic
    keys = [k for k in GENRES if k != "otomatis"]
    sample = " ".join(w for _, _, w in words[:1500])
    prompt = (f"Judul video: {title}\nCiri visual: wajah terlihat di {vis['face_ratio']:.0%} frame, lebar wajah "
              f"{vis['face_w']:.0%} layar, facecam kecil: {'ya' if vis['facecam'] else 'tidak'}, beberapa orang: "
              f"{vis['multi']:.0%}, tampilan layar komputer: {vis['screen']:.0%}.\nCuplikan transkrip:\n{sample}\n\n"
              f"Tentukan jenis konten video ini. Pilihan: " + ", ".join(f"{k} ({GENRES[k]['label']})" for k in keys))
    schema = {"type": "object", "properties": {"genre": {"type": "string", "enum": keys}, "alasan": {"type": "string"}},
              "required": ["genre", "alasan"], "additionalProperties": False}
    resp = anthropic.Anthropic(api_key=api_key or None).beta.messages.create(
        model=CLAUDE_MODEL, max_tokens=2000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}])
    if resp.stop_reason == "refusal":
        raise RuntimeError("ditolak")
    d = json.loads(next(b.text for b in resp.content if b.type == "text"))
    return d["genre"], d["alasan"]


# ============================================================================ kamera pintar (reframe dinamis)

_YUNET = {}
YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"


def face_detector(width, height):
    """Detektor wajah YuNet (OpenCV DNN): kenal wajah miring/menyamping + titik sudut mulut. None bila tak tersedia."""
    import cv2
    model = os.path.join(APP_DIR, "_models", "face_detection_yunet_2023mar.onnx")
    if not os.path.exists(model):
        try:
            import urllib.request
            os.makedirs(os.path.dirname(model), exist_ok=True)
            urllib.request.urlretrieve(YUNET_URL, model + ".part")
            os.replace(model + ".part", model)
        except Exception:
            return None
    key = (width, height)
    if key not in _YUNET:
        try:
            _YUNET[key] = cv2.FaceDetectorYN.create(model, "", (width, height), 0.6, 0.3, 50)
        except Exception:
            _YUNET[key] = None
    return _YUNET[key]


def sample_track(path, info, s, e, fps=8, width=480):
    """Sampel klip 8x/detik: posisi wajah, gerak mulut tiap wajah, peta gerakan."""
    import cv2
    cv2.ocl.setUseOpenCL(False)
    sh = int(width * info["height"] / info["width"]) // 2 * 2
    raw = run([ffmpeg_exe(), "-v", "error", "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", *info["dec"], "-i", path,
               "-an", "-vf", f"fps={fps},scale={width}:{sh},format=gray", "-f", "rawvideo", "pipe:1"])
    k = len(raw) // (width * sh)
    fr = np.frombuffer(raw, np.uint8)[:k * width * sh].reshape(k, sh, width)
    yn = face_detector(width, sh)
    casc = None if yn else cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces, mouth, maps = [], [], []
    prev = None
    for i in range(k):
        f = fr[i]
        boxes, mregs = [], []
        if yn is not None:
            _, det = yn.detect(cv2.cvtColor(f, cv2.COLOR_GRAY2BGR))
            for r in (det if det is not None else []):
                x, y, bw, bh = r[:4]
                if bw / width < 0.025:
                    continue
                boxes.append((max(0.0, x / width), max(0.0, y / sh), bw / width, bh / sh))
                mx0, mx1 = min(r[10], r[12]) - bw * 0.12, max(r[10], r[12]) + bw * 0.12
                my = (r[11] + r[13]) / 2
                mregs.append((int(max(0, mx0)), int(min(width, mx1)), int(max(0, my - bh * 0.14)), int(min(sh, my + bh * 0.2))))
        else:
            for x, y, bw, bh in casc.detectMultiScale(f, 1.1, 5, minSize=(16, 16)):
                if bw / width < 0.03:
                    continue
                boxes.append((x / width, y / sh, bw / width, bh / sh))
                mregs.append((x, x + bw, int(y + bh * 0.55), int(min(sh, y + bh * 1.05))))
        d = None if prev is None else np.abs(f.astype(np.int16) - prev).astype(np.float32)
        mm = []
        for x0, x1, y0, y1 in mregs:
            mm.append(float(d[y0:y1, x0:x1].mean()) if d is not None and y1 > y0 and x1 > x0 else 0.0)
        faces.append(boxes)
        mouth.append(mm)
        if d is None:
            maps.append(np.zeros((sh // 8, width // 8), np.float32))
        else:
            d[d < 12] = 0
            maps.append(d[:sh // 8 * 8, :width // 8 * 8].reshape(sh // 8, 8, width // 8, 8).mean((1, 3)))
        prev = f.astype(np.int16)
    return dict(fps=fps, k=k, faces=faces, mouth=mouth, maps=maps, frames=fr[::max(1, k // 48)])


def consistent_small_face(box_lists, min_ratio=0.3):
    """Cari wajah kecil (<16% lebar) yang muncul di posisi yang sama di banyak frame = facecam."""
    def cornerish(b):  # facecam selalu kecil & di tepi/pojok layar, bukan orang di tengah
        cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
        return b[2] < 0.16 and (cx < 0.3 or cx > 0.7 or cy > 0.62 or cy < 0.3)
    small = [(i, b) for i, fl in enumerate(box_lists) for b in fl if cornerish(b)]
    if not small:
        return None
    pts = np.array([(b[0] + b[2] / 2, b[1] + b[3] / 2) for _, b in small])
    best, bidx = None, []
    for c in pts:
        idx = [j for j, q in enumerate(pts) if abs(q[0] - c[0]) < 0.05 and abs(q[1] - c[1]) < 0.06]
        frames = {small[j][0] for j in idx}
        if best is None or len(frames) > best:
            best, bidx = len(frames), idx
    if best is None or best < min_ratio * max(1, len(box_lists)):
        return None
    return [float(v) for v in np.median(np.array([small[j][1] for j in bidx]), 0)]


def find_facecam(tr):
    cam = consistent_small_face(tr["faces"])
    if not cam:
        return None
    # video biasa (bukan streamer): ada wajah lain seukuran/lebih besar di luar pojok itu -> bukan facecam
    cx, cy = cam[0] + cam[2] / 2, cam[1] + cam[3] / 2
    others = sum(1 for fl in tr["faces"] if any(
        b[2] >= cam[2] * 0.5 and (abs(b[0] + b[2] / 2 - cx) > 0.2 or abs(b[1] + b[3] / 2 - cy) > 0.25) for b in fl))
    bigger = sum(1 for fl in tr["faces"] if any(
        b[2] > cam[2] * 1.15 and (abs(b[0] + b[2] / 2 - cx) > 0.04 or abs(b[1] + b[3] / 2 - cy) > 0.06) for b in fl))
    return None if others > 0.15 * max(1, tr["k"]) or bigger > 0.2 * max(1, tr["k"]) else cam


def video_facecam(path, info=None):
    """Deteksi facecam di 4 bagian video (sekali per video). Return (rect4, jenis, jumlah_cocok) atau (None, "", 0)."""
    info = info or probe(path)
    D = info["duration"]
    found = []
    for f_ in (0.15, 0.4, 0.65, 0.85):
        t0 = max(0.0, min(D * f_, D - 25))
        tr = sample_track(path, info, t0, min(D, t0 + 20), fps=4)
        cam = find_facecam(tr)
        if cam:
            r_ = detect_cam_rect(tr["frames"], cam)
            if r_:
                found.append(r_)
    if not found:
        return None, "", 0
    kinds = [r_[4] for r_ in found]
    kind_ = "kotak" if "kotak" in kinds else max(set(kinds), key=kinds.count)
    sel = [r_[:4] for r_ in found if r_[4] == kind_]
    return tuple(float(v) for v in np.median(np.array(sel), 0)), kind_, len(found)


def _iou(a, b):
    ax1, ay1, bx1, by1 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw, ih = max(0, min(ax1, bx1) - max(a[0], b[0])), max(0, min(ay1, by1) - max(a[1], b[1]))
    inter = iw * ih
    return inter / max(1e-6, a[2] * a[3] + b[2] * b[3] - inter)


def facecam_for(path, info=None, log=print):
    """Facecam untuk video ini: deteksi + profil streamer (diingat per channel).
    Return (rect4, sumber) atau (None, "")."""
    ch = channel_of(path)
    prof = _json_load(CAM_PROFILES).get(ch) if ch else None
    rect, kind_, nf = video_facecam(path, info)
    ok = rect and (nf >= 2 or kind_ == "kotak")
    if prof and prof.get("manual"):  # koreksi manual pengguna untuk streamer ini selalu dipakai
        return parse_cam_box(prof["box"]), f"profil manual {ch}"
    if ok:
        if ch:
            old = parse_cam_box(prof["box"]) if prof else None
            if not old or _iou(old, rect) < 0.5:
                _json_update(CAM_PROFILES, {ch: dict(box=",".join(f"{v * 100:.2f}" for v in rect), kind=kind_,
                                                     manual=False, updated=time.strftime("%Y-%m-%d"))})
                log(f"      profil facecam streamer '{ch}' {'diperbarui' if old else 'disimpan'}")
        nama = {"kotak": "kotak berbingkai", "potongan": "potongan/greenscreen", "mengambang": "bulat/mengambang"}
        return rect, f"{nama.get(kind_, kind_)}, terlihat di {nf}/4 bagian video"
    if prof:  # deteksi ragu (mis. banyak cutscene) -> pakai yang pernah berhasil di video streamer ini
        return parse_cam_box(prof["box"]), f"profil streamer {ch}"
    return None, ""


def save_manual_profile(path, box):
    ch = channel_of(path)
    if ch and parse_cam_box(box):
        _json_update(CAM_PROFILES, {ch: dict(box=box, kind="manual", manual=True, updated=time.strftime("%Y-%m-%d"))})
    return ch


def preview_facecam(path, cam_box=""):
    """Deteksi kotak facecam di beberapa bagian video, simpan gambar bertanda. Return (gambar, pesan, angka%)."""
    from PIL import ImageDraw
    info = probe(path)
    D = info["duration"]
    manual = parse_cam_box(cam_box)
    if manual:
        rect_, why_ = None, ""
        ch_ = save_manual_profile(path, cam_box)
    else:
        rect_, why_ = facecam_for(path, info)
        ch_ = channel_of(path)
    rect = manual or rect_
    img = grab_frame(path, D * 0.4, info["dec"], 1280).convert("RGB")
    d = ImageDraw.Draw(img)
    os.makedirs(os.path.join(APP_DIR, "_cache"), exist_ok=True)
    out = os.path.join(APP_DIR, "_cache", slug(os.path.splitext(os.path.basename(path))[0])[:60] + "_facecam.jpg")
    if rect:
        W_, H_ = img.size
        x0, y0 = rect[0] * W_, rect[1] * H_
        d.rectangle([x0, y0, x0 + rect[2] * W_, y0 + rect[3] * H_], outline=(57, 255, 20), width=6)
        txt = f"{rect[0] * 100:.1f}, {rect[1] * 100:.1f}, {rect[2] * 100:.1f}, {rect[3] * 100:.1f}"
        img.save(out, quality=90)
        src_ = (f"manual{', diingat untuk streamer ' + ch_ if ch_ else ''}" if manual else why_)
        return out, f"Kotak facecam ({src_}): {txt}  -> gambar: {out}", txt
    img.save(out, quality=90)
    return out, "Kotak facecam tidak terdeteksi - isi kotak manual (x, y, lebar, tinggi dalam %).", ""


def parse_cam_box(txt):
    """'x,y,lebar,tinggi' dalam persen -> (x,y,w,h) relatif 0-1, atau None."""
    try:
        v = [float(x) for x in re.split(r"[,;\s]+", (txt or "").strip()) if x]
        if len(v) == 4 and v[2] > 0 and v[3] > 0:
            v = [x / 100 for x in v]
            return (max(0, v[0]), max(0, v[1]), min(v[2], 1 - v[0]), min(v[3], 1 - v[1]))
    except ValueError:
        pass
    return None


def detect_cam_rect(frames, face, debug=False):
    """Cari area facecam streamer. Return (x, y, w, h, jenis) relatif; jenis "kotak" (overlay berbingkai)
    atau "potongan" (greenscreen / tanpa bingkai, menempel ke tepi layar). None bila gagal."""
    import cv2
    if frames is None or len(frames) < 4 or not face:
        return None
    E = np.mean([cv2.Canny(cv2.GaussianBlur(f, (3, 3), 0), 40, 120) > 0 for f in frames], 0)
    h, w = E.shape
    fx, fy, fw, fh = face
    X0, X1, Y0, Y1 = fx * w, (fx + fw) * w, fy * h, (fy + fh) * h
    FW, FH = max(4.0, fw * w), max(4.0, fh * h)
    cx, cy = (X0 + X1) / 2, (Y0 + Y1) / 2

    def pick(score, lo, hi, inside_low):
        """Garis tepi = garis diam yang kuat ATAU perubahan kepadatan detail yang tajam (dalam vs luar)."""
        lo, hi = int(max(1, lo)), int(min(len(score) - 2, hi))
        if hi <= lo:
            return None
        sc = np.convolve(score, [0.25, 0.5, 0.25], "same")
        n_ = len(sc)
        cs = np.concatenate([[0], np.cumsum(sc)])
        m = lambda a_, b_: (cs[min(n_, b_)] - cs[max(0, a_)]) / max(1, min(n_, b_) - max(0, a_))
        best, bi = 0.0, None
        med = float(np.median(sc[lo:hi]))
        for i in range(lo, hi):
            inside = m(i + 2, i + 10) if inside_low else m(i - 10, i - 1)
            outside = m(i - 10, i - 1) if inside_low else m(i + 2, i + 10)
            step = inside - outside  # sisi dalam facecam biasanya lebih "ramai" (wajah, kamar)
            line = sc[i] - med
            val = max(line, 0) + max(step, 0) * 0.8
            ok = (sc[i] >= 0.38) or (sc[i] >= 0.25 and line >= 0.08 and step >= 0.08)
            if ok and val > best:
                best, bi = val, i
        return bi

    # ---- 1) overlay berbingkai: cari garis lurus diam di keempat sisi
    band = E[int(max(0, Y0 - 0.5 * FH)):int(min(h, Y1 + 0.8 * FH)), :].mean(0)
    L = pick(band, X0 - max(4.5 * FW, 0.25 * w), X0 - 0.3 * FW, True)
    R = pick(band, X1 + 0.3 * FW, X1 + max(4.5 * FW, 0.25 * w), False)
    L_ = L if L is not None else (0 if X0 < max(4.5 * FW, 0.25 * w) else None)
    R_ = R if R is not None else (w - 1 if w - X1 < max(4.5 * FW, 0.25 * w) else None)
    box = None
    if L_ is not None and R_ is not None and R_ - L_ > 6:
        cols = E[:, int(L_) + 2:int(R_) - 1].mean(1)
        T = pick(cols, Y0 - max(3.2 * FH, 0.3 * h), Y0 - 0.2 * FH, True)
        B = pick(cols, Y1 + 0.3 * FH, Y1 + max(3.6 * FH, 0.3 * h), False)
        T_ = T if T is not None else (0 if Y0 < max(3.2 * FH, 0.3 * h) else None)
        B_ = B if B is not None else (h - 1 if h - Y1 < max(3.6 * FH, 0.3 * h) else None)
        lines = sum(v is not None for v in (L, R, T, B))
        if debug:
            print("garis L R T B:", L, R, T, B)
        if T_ is not None and B_ is not None and lines >= 3:
            asp = (R_ - L_) / max(1, B_ - T_)
            if 0.6 < asp < 2.8 and (R_ - L_) < 0.6 * w and (B_ - T_) < 0.7 * h and R_ - L_ > 1.8 * FW:
                pad = 2
                box = ((L_ + pad) / w, (T_ + pad) / h, (R_ - L_ - 2 * pad) / w, (B_ - T_ - 2 * pad) / h, "kotak")
    if box:
        return box
    # ---- 2) facecam potongan (greenscreen): orang menempel ke tepi bawah/atas, ambil kepala + badan
    if fw > 0.1:  # wajah besar tanpa bingkai = kemungkinan besar video biasa, bukan facecam
        return None
    if 0.25 * h < cy < 0.6 * h:  # facecam bulat/tanpa bingkai yang "mengambang" di sisi layar
        bw = min(w * 0.45, 3.8 * FW)
        bh = min(h * 0.6, bw / 1.25)
        L_ = min(max(cx - bw / 2, 0.0), w - bw)
        T_ = min(max(cy - bh * 0.45, 0.0), h - bh)
        return (L_ / w, T_ / h, bw / w, bh / h, "mengambang")
    bw = min(w * 0.5, 5.0 * FW)
    if cy > 0.5 * h:  # di bawah -> menempel ke tepi bawah
        T_, B_ = max(0.0, Y0 - 0.8 * FH), float(h)
    else:
        T_, B_ = 0.0, min(float(h), Y1 + 2.6 * FH)
    bh = B_ - T_
    bw = min(bw, bh * 2.2)
    L_ = min(max(cx - bw / 2, 0.0), w - bw)
    if cx > 0.75 * w:  # pojok kanan -> menempel ke tepi kanan
        L_ = w - bw
    elif cx < 0.25 * w:
        L_ = 0.0
    return (L_ / w, T_ / h, bw / w, bh / h, "potongan")


def motion_centroids(tr, exclude=None, top_cut=0.12, bottom_cut=0.92):
    """Pusat gerakan per sampel (abaikan papan skor/UI di tepi & area facecam)."""
    out = []
    for m in tr["maps"]:
        h, w = m.shape
        mm = m.copy()
        mm[:int(h * top_cut)] = 0
        mm[int(h * bottom_cut):] = 0
        if exclude:
            ex, ey, ew, eh = exclude
            mm[max(0, int((ey - eh * 0.6) * h)):int((ey + eh * 2.2) * h), max(0, int((ex - ew * 0.8) * w)):int((ex + ew * 1.8) * w)] = 0
        tot = mm.sum()
        if tot < mm.size * 0.35:
            out.append(None)
            continue
        out.append((float((mm.sum(0) * np.arange(w)).sum() / tot / w), float((mm.sum(1) * np.arange(h)).sum() / tot / h)))
    return out


def face_clusters(tr):
    cs = sorted(b[0] + b[2] / 2 for fl in tr["faces"] for b in fl)
    if not cs:
        return []
    groups = [[cs[0]]]
    for c in cs[1:]:
        if c - groups[-1][-1] > 0.07:
            groups.append([c])
        else:
            groups[-1].append(c)
    return [float(np.median(g)) for g in groups if len(g) >= max(3, 0.08 * tr["k"])]


def speaker_path(tr, gc, times, speak_iv):
    """Pembicara aktif = gerak mulut relatif terbesar. Boleh pindah setelah 0.7 dtk, hanya saat ada ucapan."""
    k, J, fps = tr["k"], len(gc), tr["fps"]
    act = np.zeros((k, J))
    pos = np.full((k, J, 3), np.nan)
    for i, (fl, mm) in enumerate(zip(tr["faces"], tr["mouth"])):
        for b, m in zip(fl, mm):
            cx = b[0] + b[2] / 2
            j = int(np.argmin([abs(cx - c) for c in gc]))
            if abs(cx - gc[j]) < 0.09 and m >= act[i, j]:
                act[i, j] = m
                pos[i, j] = (cx, b[1] + b[3] / 2, b[3])
    for j in range(J):  # normalisasi per orang (ukuran wajah & cahaya berbeda)
        nz = act[:, j][act[:, j] > 0]
        act[:, j] /= (np.median(nz) if len(nz) else 1.0) + 1e-6
    pres = (~np.isnan(pos[:, :, 0])).sum(0)
    sm = np.stack([smooth(act[:, j], max(2, int(fps * 0.5))) for j in range(J)], 1)
    cur, hold, sel, switches = int(np.argmax(pres)), 0, [], set()
    for i in range(k):
        talking = not speak_iv or any(a - 0.2 <= times[i] <= b + 0.2 for a, b in speak_iv)
        best = int(np.argmax(sm[i]))
        if talking and best != cur and sm[i, best] > sm[i, cur] * 1.25 + 0.15 and hold >= fps * 0.7:
            cur, hold = best, 0
            switches.add(i)
        hold += 1
        sel.append(cur)
    pts, last = [], {}
    for i in range(k):
        j = sel[i]
        p_ = pos[i, j]
        if not np.isnan(p_[0]):
            last[j] = tuple(p_)
        pts.append(last.get(j) or (gc[j], 0.4, float(np.nanmedian(pos[:, j, 2])) if pres[j] else 0.2))
    return pts, switches


def smooth_path(vals, alpha, dead, jumps=()):
    out, cur, tgt = [], None, None
    for i, v in enumerate(vals):
        if cur is None or i in jumps:
            cur = tgt = v
        else:
            if abs(v - tgt) > dead:
                tgt = v
            cur += alpha * (tgt - cur)
        out.append(cur)
    return out


def follow_path(vals, fps, switches=(), follow_tc=0.22, switch_tc=0.11, dead=0.012, cuts=()):
    """Kamera mengikuti target dengan cepat & mulus: pindah pembicara ~0.35 dtk (ease-out), tanpa goyang."""
    a_f = 1 - math.exp(-1 / (fps * follow_tc))
    a_s = 1 - math.exp(-1 / (fps * switch_tc))
    out, cur, tgt, fast = [], None, None, 0
    for i, v in enumerate(vals):
        if cur is None or i in cuts:
            cur = tgt = v
            fast = 0
        else:
            if i in switches:
                tgt, fast = v, int(fps * 0.45)
            elif abs(v - tgt) > dead:
                tgt = v
            cur += (a_s if fast > 0 else a_f) * (tgt - cur)
            fast -= 1
        out.append(cur)
    return out


def shot_hold(vals, cuts, k, center=0.5, bias=0.45, dead=0.07, min_len=0):
    """Kamera diam per adegan (tripod): satu posisi per shot, condong ke tengah layar.
    Untuk gameplay orang ketiga karakter selalu di tengah -> tidak goyang ikut gerakan layar."""
    bounds = [0]
    for c in sorted(c for c in cuts if 0 < c < k):
        if c - bounds[-1] >= min_len and k - c >= min_len:
            bounds.append(c)
    bounds.append(k)
    out = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg = [v for v in vals[a:b] if v is not None]
        m = float(np.median(seg)) if len(seg) >= max(2, (b - a) * 0.3) else center
        tgt = center + bias * (m - center)
        if abs(tgt - center) < dead:
            tgt = center
        out += [tgt] * (b - a)
    return out


def simplify_kf(kf, eps=1.5):
    """Kurangi titik kunci (Ramer-Douglas-Peucker) supaya ekspresi ffmpeg ringan tanpa mengubah gerakan."""
    if len(kf) < 3:
        return kf
    pts = np.array([(t, v) for t, v, _ in kf], float)
    keep = np.zeros(len(kf), bool)
    keep[0] = keep[-1] = True
    for i, (_, _, j) in enumerate(kf):
        if j:
            keep[i] = True
            if i > 0:
                keep[i - 1] = True
    idx = [i for i in range(len(kf)) if keep[i]]
    stack = list(zip(idx[:-1], idx[1:]))
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ta, va = pts[a]
        tb, vb = pts[b]
        seg = pts[a + 1:b]
        interp = va + (vb - va) * (seg[:, 0] - ta) / max(tb - ta, 1e-6)
        d = np.abs(seg[:, 1] - interp)
        m = int(np.argmax(d))
        if d[m] > eps:
            keep[a + 1 + m] = True
            stack += [(a, a + 1 + m), (a + 1 + m, b)]
    return [kf[i] for i in range(len(kf)) if keep[i]]


def piecewise_expr(kf):
    """Ekspresi ffmpeg linear antar titik kunci, tanpa nesting."""
    kf = simplify_kf(kf)
    if not kf:
        return "0"
    terms = [f"{kf[0][1]:.1f}"]
    for (ta, va, _), (tb, vb, jb) in zip(kf, kf[1:]):
        dv = vb - va
        if abs(dv) < 0.6:
            continue
        if jb or tb - ta < 1e-3:
            terms.append(f"{dv:+.1f}*gte(t,{tb:.3f})")
        else:
            terms.append(f"{dv:+.1f}*clip((t-{ta:.3f})/{tb - ta:.3f},0,1)")
    return "".join(terms)


def smart_reframe(path, info, s, e, keep, mapf, mode, speak_iv, src, dst, log, cam_pos="bawah", cam_box=""):
    """Kamera pintar 9:16 sesuai jenis konten. Return (graph, (W,H), mode_dipakai, kotak_wajah, posisi_subtitle_y)."""
    w, h = info["width"], info["height"]
    try:
        tr = sample_track(path, info, s, e)
    except Exception as ex:
        log(f"      kamera pintar gagal ({ex}) -> sinematik")
        g, wh = layout_graph("cinematic", info, src, dst)
        return g, wh, "cinematic", None, int(1920 * 0.79)
    if tr["k"] < 2:
        g, wh = layout_graph("cinematic", info, src, dst)
        return g, wh, "cinematic", None, int(1920 * 0.79)
    fps = tr["fps"]
    cam = find_facecam(tr)
    manual = parse_cam_box(cam_box)
    rect = None
    if manual:
        rect = (*manual, "manual")
        if not cam:  # wajah tidak terdeteksi: pakai kotak manual sebagai patokan
            cam = (manual[0] + manual[2] * 0.3, manual[1] + manual[3] * 0.2, manual[2] * 0.4, manual[3] * 0.5)
        if mode == "auto":
            mode = "split_game"
    elif cam:
        try:
            rect = detect_cam_rect(tr.get("frames"), cam)
        except Exception:
            rect = None
    gc = face_clusters(tr)
    face_frames = sum(1 for f in tr["faces"] if f) / tr["k"]
    if mode == "auto":
        mode = "split_game" if cam else ("speaker" if face_frames > 0.3 else "object")
    if mode in ("split_game", "split_react", "split_screen", "present") and not cam:
        mode = {"split_game": "action", "split_react": "speaker", "split_screen": "object", "present": "object"}[mode]
    if mode in ("speaker", "face", "vlog", "body") and (face_frames < 0.2 or not gc):
        mode = "object" if mode != "body" else "action"
    if mode in ("object", "action") and gc and face_frames > 0.5 and not (cam and mode == "action"):
        mode = "speaker"  # ada orang yang bicara -> fokus ke orangnya, bukan ke tengah
    times = [i / fps for i in range(tr["k"])]
    mmean = np.array([m.mean() for m in tr["maps"]])
    thr = max(8.0, float(np.median(mmean)) * 5 + 4)
    cuts = {i for i in range(1, tr["k"]) if mmean[i] > thr}  # pergantian shot -> kamera ikut "cut"

    def to_out(vals):
        return [(mapf(t_), v, i in cuts) for i, (t_, v) in enumerate(zip(times, vals)) if any(a <= t_ <= b for a, b in keep)]

    def fill(seq, default):
        out, last = [], None
        for v in seq:
            last = v if v is not None else last
            out.append(last)
        first = next((v for v in out if v is not None), default)
        return [v if v is not None else first for v in out]

    # ----------------------------------------------------- layar dibagi (wajah dari facecam)
    if mode in ("split_game", "split_react", "split_screen", "present"):
        top_h = {"split_game": 1320, "split_react": 880, "split_screen": 1400, "present": 700}[mode]
        bot_h = 1920 - top_h
        fx, fy, fw, fh = cam
        a_top = 1080 / top_h
        if mode == "present":
            ch_t, cw_t = h, w
        else:
            ch_t = h * (0.9 if mode == "split_screen" else (0.85 if mode == "split_react" else 1.0))
            cw_t = min(w, ch_t * a_top)
            ch_t = cw_t / a_top
        mc = motion_centroids(tr, exclude=cam)
        if mode == "split_game":  # game: bingkai diam per adegan, karakter di tengah (tidak ikut gerak tangan/kamera)
            xs = shot_hold([None if c is None else c[0] for c in mc], cuts, tr["k"], min_len=int(fps * 2))
            ys = [0.5] * len(xs)
            if rect:  # geser menjauh dari kotak facecam bila bingkai menyentuhnya
                rl, rr = rect[0], rect[0] + rect[2]
                half = cw_t / w / 2
                for i, x_ in enumerate(xs):
                    if rr < 0.5 and x_ - half < rr:
                        xs[i] = min(max(x_, rr + half), 0.5 + half)
                    elif rl > 0.5 and x_ + half > rl:
                        xs[i] = max(min(x_, rl - half), 0.5 - half)
        else:
            xs = fill([None if c is None else c[0] for c in mc], fx + fw / 2)
            ys = fill([None if c is None else c[1] for c in mc], 0.5)
            tc = 0.5 if mode == "split_screen" else 0.35
            xs = follow_path(xs, fps, follow_tc=tc, dead=0.04, cuts=cuts)
            ys = follow_path(ys, fps, follow_tc=tc, dead=0.04, cuts=cuts)
        xpx = [min(max(x * w - cw_t / 2, 0), w - cw_t) for x in xs]
        ypx = [min(max(y * h - ch_t / 2, 0), h - ch_t) for y in ys] if mode == "split_screen" else [(h - ch_t) / 2] * len(xs)
        if mode == "present":
            xpx, ypx = [0] * len(xs), [0] * len(xs)
        ex_, ey_ = piecewise_expr(to_out(xpx)), piecewise_expr(to_out(ypx))
        a_bot = 1080 / bot_h
        share = {"split_game": 0.27, "split_react": 0.4, "split_screen": 0.26, "present": 0.32}[mode]
        cam_w = min(w, fw * w / share)
        cam_h = min(h, cam_w / a_bot)
        cam_w = cam_h * a_bot
        x = int(min(max((fx + fw / 2) * w - cam_w / 2, 0), w - cam_w))
        y = int(min(max((fy + fh / 2) * h - cam_h * 0.62, 0), h - cam_h))
        if rect:  # potong tepat di kotak facecam yang terdeteksi
            rx, ry, rw_, rh_ = rect[0] * w, rect[1] * h, rect[2] * w, rect[3] * h
            jenis = rect[4] if len(rect) > 4 else "manual"
            if rw_ / rh_ > a_bot:  # kotak lebih lebar dari panel -> pangkas kiri/kanan, ikut wajah
                cam_h, cam_w = rh_, rh_ * a_bot
                x = int(min(max((fx + fw / 2) * w - cam_w / 2, rx), rx + rw_ - cam_w))
                y = int(ry)
            else:  # kotak lebih tinggi -> pangkas atas/bawah, sisakan ruang kepala
                cam_w, cam_h = rw_, rw_ / a_bot
                x = int(rx)
                y = int(min(max((fy + fh / 2) * h - cam_h * 0.55, ry), ry + rh_ - cam_h))
            log(f"      facecam {jenis}: x={rect[0]:.0%} y={rect[1]:.0%} "
                f"lebar={rect[2]:.0%} tinggi={rect[3]:.0%} -> ditaruh di {cam_pos}")
        else:
            log(f"      kotak facecam tidak jelas -> zoom ke wajah streamer, ditaruh di {cam_pos}")
        top = f"[ta]crop={int(cw_t) // 2 * 2}:{int(ch_t) // 2 * 2}:x='{ex_}':y='{ey_}',scale=1080:{top_h},setsar=1,{GRADE}[t1]"
        bot = f"[tb]crop={int(cam_w) // 2 * 2}:{int(cam_h) // 2 * 2}:{x}:{y},scale=1080:{bot_h}:flags=lanczos,setsar=1,{GRADE}[b1]"
        hide = ""
        if rect:  # samarkan facecam di panel game supaya wajah tidak muncul dua kali
            bx, by = int(rect[0] * w) // 2 * 2, int(rect[1] * h) // 2 * 2
            bw2, bh2 = max(8, int(rect[2] * w) // 2 * 2), max(8, int(rect[3] * h) // 2 * 2)
            hide = (f"[ta]split[ta0][tz];[tz]crop={bw2}:{bh2}:{bx}:{by},boxblur=luma_radius=min(w\,h)/6:luma_power=3[tzb];"
                    f"[ta0][tzb]overlay={bx}:{by}[tah];")
            top = top.replace("[ta]crop", "[tah]crop", 1)
        if cam_pos == "atas" and mode != "present":
            g = f"{src}split[ta][tb];{hide}{top};{bot};[b1][t1]vstack{dst}"
            return g, (1080, 1920), mode, cam, bot_h
        g = f"{src}split[ta][tb];{hide}{top};{bot};[t1][b1]vstack{dst}"
        return g, (1080, 1920), mode, cam, top_h

    # ----------------------------------------------------- satu bingkai 9:16 yang mengikuti orang/aksi
    pts, switches = None, set()
    if mode in ("speaker", "face", "vlog", "body"):
        if mode in ("speaker", "vlog") and len(gc) >= 2:
            pts, switches = speaker_path(tr, gc, times, speak_iv)
        else:
            main = max(gc, key=lambda c: sum(1 for fl in tr["faces"] for b in fl if abs(b[0] + b[2] / 2 - c) < 0.09))
            seq = []
            for fl in tr["faces"]:
                b = sorted([b for b in fl if abs(b[0] + b[2] / 2 - main) < 0.15], key=lambda z: -z[2])
                seq.append(None if not b else (b[0][0] + b[0][2] / 2, b[0][1] + b[0][3] / 2, b[0][3]))
            pts = fill(seq, (main, 0.4, 0.2))
    mc = motion_centroids(tr, top_cut=0.15 if mode == "action" else 0.05)
    if pts is None or mode == "vlog":
        mx = fill([None if c is None else c[0] for c in mc], gc[0] if gc else 0.5)
        my = fill([None if c is None else c[1] for c in mc], 0.5)
        if pts is None:  # object/action
            fpts = None
            if mode == "object" and gc:
                fpts = fill([(max(b, key=lambda z: z[2])[0] + max(b, key=lambda z: z[2])[2] / 2) if b else None
                             for b in tr["faces"]], gc[0])
            cx = [0.65 * a + 0.35 * fpts[i] if fpts else a for i, a in enumerate(mx)]
            pts = [(cx[i], my[i], 0.0) for i in range(len(cx))]
        else:  # vlog: wajah saat bicara, lingkungan saat diam
            pts = [p_ if any(a - 0.3 <= t_ <= b + 0.3 for a, b in speak_iv) else (mx[i], my[i], 0.0)
                   for i, (t_, p_) in enumerate(zip(times, pts))]
    fh_med = float(np.median([p_[2] for p_ in pts if p_[2] > 0])) if any(p_[2] > 0 for p_ in pts) else 0.0
    zoom = {"face": 5.4, "speaker": 7.2, "vlog": 7.6}.get(mode)
    ch = h if (not zoom or fh_med <= 0) else min(h, max(0.66 * h, fh_med * h * zoom))
    cw = min(w, ch * 9 / 16)
    ch = cw * 16 / 9
    tc, dz = {"action": (0.3, 0.03), "object": (0.35, 0.04), "body": (0.3, 0.03)}.get(mode, (0.22, 0.012))
    if mode == "action":  # aksi/gameplay: diam per adegan, bukan mengejar gerakan (penyebab goyang)
        xs = shot_hold([None if c is None else c[0] for c in mc], cuts, tr["k"], bias=0.6, dead=0.05, min_len=int(fps * 2))
        ys = [0.5] * len(xs)
    else:
        xs = follow_path([p_[0] for p_ in pts], fps, switches, follow_tc=tc, dead=dz, cuts=cuts)
        ys = follow_path([p_[1] for p_ in pts], fps, switches, follow_tc=tc, dead=dz, cuts=cuts)
    xpx = [min(max(x * w - cw / 2, 0), w - cw) for x in xs]
    if ch < h - 2 and mode in ("speaker", "face", "vlog"):
        ypx = [min(max(y * h - ch * 0.36, 0), h - ch) for y in ys]
    else:
        ypx = [(h - ch) / 2] * len(xs)
    ex_, ey_ = piecewise_expr(to_out(xpx)), piecewise_expr(to_out(ypx))
    g = f"{src}crop={int(cw) // 2 * 2}:{int(ch) // 2 * 2}:x='{ex_}':y='{ey_}',scale=1080:1920,setsar=1,{GRADE}{dst}"
    if switches:
        log(f"      kamera pindah antar {len(gc)} orang mengikuti yang bicara: {len(switches)}x")
    return g, (1080, 1920), mode, None, int(1920 * 0.72)


# ============================================================================ layout & kreasi visual

def face_center_x(path, start, end, samples=12):
    """(posisi horizontal wajah 0-1, rasio frame berwajah, kotak wajah median [x,y,w,h] relatif)."""
    try:
        import cv2
        cv2.ocl.setUseOpenCL(False)  # cegah crash OpenCL saat program ditutup
    except ImportError:
        return 0.5, 0.0, None
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(path)
    boxes, tried = [], 0
    for t in np.linspace(start, end, samples + 2)[1:-1]:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, fr = cap.read()
        if not ok:
            continue
        tried += 1
        sc = 640 / fr.shape[1]
        g = cv2.cvtColor(cv2.resize(fr, None, fx=sc, fy=sc), cv2.COLOR_BGR2GRAY)
        faces = casc.detectMultiScale(g, 1.1, 6, minSize=(22, 22))
        if len(faces):
            x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
            boxes.append((x / g.shape[1], y / g.shape[0], fw / g.shape[1], fh / g.shape[0]))
    cap.release()
    if not boxes:
        return 0.5, 0.0, None
    box = [float(v) for v in np.median(np.array(boxes), axis=0)]
    return box[0] + box[2] / 2, len(boxes) / tried, box


def layout_graph(kind, info, src, dst, cx=0.5, box=None):
    w, h = info["width"], info["height"]
    if kind == "gaming" and box:
        # 50/50: game di atas, wajah streamer di-zoom besar di bawah (tanpa garis pemisah)
        fx, fy, fw, fh = box
        ar = 1080 / 960
        cam_w = min(w, max(fw * 3.3, 0.14) * w)
        cam_h = min(h, cam_w / ar)
        cam_w = cam_h * ar
        ccx, ccy = (fx + fw / 2) * w, (fy + fh * 0.95) * h
        x = int(min(max(ccx - cam_w / 2, 0), w - cam_w))
        y = int(min(max(ccy - cam_h / 2, 0), h - cam_h))
        gw = int(min(w, h * ar)) // 2 * 2
        gx = int(min(max(w / 2 - gw / 2, 0), w - gw))
        return (f"{src}split[ga][gb];[ga]crop={gw}:{h // 2 * 2}:{gx}:0,scale=1080:960,setsar=1,{GRADE}[gtop];"
                f"[gb]crop={int(cam_w) // 2 * 2}:{int(cam_h) // 2 * 2}:{x}:{y},scale=1080:960,setsar=1,{GRADE}[gcam];"
                f"[gtop][gcam]vstack{dst}"), (1080, 1920)
    if kind == "face" and w * 16 > h * 9:
        cw = int(h * 9 / 16) // 2 * 2
        x = int(min(max(cx * w - cw / 2, 0), w - cw))
        return f"{src}crop={cw}:{h // 2 * 2}:{x}:0,scale=1080:1920,setsar=1,{GRADE}{dst}", (1080, 1920)
    if kind in ("face", "cinematic"):
        if w * 16 <= h * 9 * 1.05:
            return (f"{src}scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,{GRADE}{dst}",
                    (1080, 1920))
        ar = min(w / h, 1.15)
        fh = int(1080 / ar) // 2 * 2
        return (f"{src}split[la][lb];"
                f"[la]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=28:3,"
                f"eq=brightness=-0.10:saturation=1.4[lbg];"
                f"[lb]crop=w='min(iw,ih*{ar:.4f})':h=ih:x='(iw-ow)*{cx:.3f}':y=0,scale=1080:{fh},{GRADE},"
                f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.9:t=6[lfg];"
                f"[lbg][lfg]overlay=0:(H-h)/2-40,setsar=1{dst}"), (1080, 1920)
    if kind == "square":
        side = min(w, h) // 2 * 2
        x = int(min(max(cx * w - side / 2, 0), w - side))
        return f"{src}crop={side}:{side}:{x}:{(h - side) // 2},scale=1080:1080,setsar=1,{GRADE}{dst}", (1080, 1080)
    ow, oh = w // 2 * 2, h // 2 * 2
    return f"{src}scale={ow}:{oh},setsar=1,{GRADE}{dst}", (ow, oh)


def sticker_png(emoji, size, folder):
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    os.makedirs(folder, exist_ok=True)
    p = os.path.join(folder, "st_" + "-".join(f"{ord(c):x}" for c in emoji) + f"_{size}.png")
    if os.path.exists(p):
        return p
    font = ImageFont.truetype(EMOJI_FONT, 109)
    im = Image.new("RGBA", (260, 260), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((40, 40), emoji, font=font, embedded_color=True)
    im = im.crop(im.getbbox())
    inner = int(size * 0.8)
    im = im.resize((inner, int(inner * im.height / im.width)), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ox, oy = (size - im.width) // 2, (size - im.height) // 2
    alpha = Image.new("L", (size, size), 0)
    alpha.paste(im.split()[3], (ox, oy))
    border = alpha.filter(ImageFilter.MaxFilter((size // 18) * 2 + 1))
    shadow = border.filter(ImageFilter.GaussianBlur(size // 25)).point(lambda v: v * 0.5)
    canvas.paste((0, 0, 0, 255), (size // 30, size // 25), shadow)
    canvas.paste((255, 255, 255, 255), (0, 0), border)
    canvas.alpha_composite(im, (ox, oy))
    canvas.save(p)
    return p


_FONT_DIRS = [r"C:\Windows\Fonts", os.path.join(APP_DIR, "fonts"), os.path.expanduser("~/.fonts"),
              "/usr/share/fonts/truetype/msttcorefonts", "/Library/Fonts"]
_FONT_ALIAS = {"impact.ttf": ["Impact.ttf"], "ariblk.ttf": ["Arial_Black.ttf"], "arialbd.ttf": ["Arial_Bold.ttf"],
               "arial.ttf": ["Arial.ttf"]}
# font gratis (SIL OFL) di folder fonts/ sebagai pengganti font Windows
_FREE_FONT = {"impact.ttf": "Anton-Regular.ttf", "ariblk.ttf": "ArchivoBlack-Regular.ttf",
              "arialbd.ttf": "ArchivoBlack-Regular.ttf", "arial.ttf": "ArchivoBlack-Regular.ttf",
              "seguisb.ttf": "ArchivoBlack-Regular.ttf", "segoeuib.ttf": "ArchivoBlack-Regular.ttf"}


def _has_font(name):
    """True bila font asli (bukan pengganti) tersedia."""
    return any(os.path.exists(os.path.join(d, n_)) for d in _FONT_DIRS for n_ in [name] + _FONT_ALIAS.get(name, []))


def ass_font(kind):
    """Nama font untuk subtitle (ASS): font Windows bila ada, selain itu font gratis di folder fonts/."""
    if kind == "impact":
        return "Impact" if _has_font("impact.ttf") or not os.path.exists(os.path.join(APP_DIR, "fonts", "Anton-Regular.ttf"))             else "Anton"
    win = {"black": ("ariblk.ttf", "Arial Black"), "arial": ("arial.ttf", "Arial")}[kind]
    return win[1] if _has_font(win[0]) or not os.path.exists(os.path.join(APP_DIR, "fonts", "ArchivoBlack-Regular.ttf"))         else "Archivo Black"


def subs_filter(name):
    """Filter ffmpeg untuk membakar subtitle; sertakan folder fonts/ bila ada."""
    fd = os.path.join(APP_DIR, "fonts")
    if os.path.isdir(fd):
        fd = fd.replace("\\", "/").replace(":", "\:").replace("'", "")
        return f"subtitles={name}.ass:fontsdir='{fd}'"
    return f"subtitles={name}.ass"


def font_path(*names):
    for nm in names:
        for d in _FONT_DIRS:
            for n_ in [nm] + _FONT_ALIAS.get(nm.lower(), []):
                fp = os.path.join(d, n_)
                if os.path.exists(fp):
                    return fp
    for nm in names:  # font gratis bawaan (folder fonts/)
        fp = os.path.join(APP_DIR, "fonts", _FREE_FONT.get(nm.lower(), "-"))
        if os.path.exists(fp):
            return fp
    for nm in names:  # Linux: cari juga font pengganti tebal
        for fp in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
            if os.path.exists(fp):
                return fp
    return None


def wrap_text(draw, text, font, max_w):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if cur and draw.textlength(t, font=font) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


def hook_png(text, W, folder, name):
    """Kotak putih sudut membulat berisi teks hitam (gaya hook Shorts)."""
    from PIL import Image, ImageDraw, ImageFont
    os.makedirs(folder, exist_ok=True)
    fp = font_path("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf")
    size = int(W * 0.052)
    font = ImageFont.truetype(fp, size) if fp else ImageFont.load_default()
    d0 = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    lines = wrap_text(d0, text, font, W * 0.74)[:3]
    lh = int(size * 1.22)
    tw = int(max(d0.textlength(l, font=font) for l in lines))
    padx, pady = int(size * 0.55), int(size * 0.35)
    im = Image.new("RGBA", (tw + 2 * padx, lh * len(lines) + 2 * pady), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius=int(size * 0.45), fill=(255, 255, 255, 255))
    for i, l in enumerate(lines):
        lw = d.textlength(l, font=font)
        d.text(((im.width - lw) / 2, pady + i * lh), l, font=font, fill=(15, 15, 15, 255))
    out = os.path.join(folder, f"hook_{name}.png")
    im.save(out)
    return out, im.width, im.height


def sentence_case(t):
    t = re.sub(r"\s+", " ", (t or "").strip())
    if not t:
        return t
    if t.isupper():
        t = t.lower()
    return t[0].upper() + t[1:]


def pop_wav(folder):
    p = os.path.join(folder, "pop.wav")
    if not os.path.exists(p):
        os.makedirs(folder, exist_ok=True)
        run([ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i",
             "aevalsrc=0.7*sin(2*PI*(1400-5200*t)*t)*exp(-28*t):s=48000:d=0.16", p])
    return p


MEME_SFX = {  # nama: (sumber lavfi, filter, durasi, volume)
    "boom": ("aevalsrc=0.95*tanh(2.6*sin(2*PI*(44*t+8.5*(1-exp(-8*t)))))*exp(-3.0*t)*min(1\\,t*300)", "aecho=0.8:0.45:70|140:0.3|0.18,lowpass=f=900", 1.3, 1.0),
    "boing": ("aevalsrc=0.75*sin(2*PI*(170*t+150*t*t)+5*sin(2*PI*11*t))*exp(-4.2*t)*min(1\\,t*300)", None, 0.7, 0.75),
    "airhorn": ("aevalsrc=0.22*(tanh(5*sin(2*PI*466*t))+tanh(5*sin(2*PI*587*t))+tanh(5*sin(2*PI*699*t)))*lt(mod(t\\,0.3)\\,0.24)*min(1\\,t*90)", "lowpass=f=3200", 0.9, 0.5),
    "ding": ("aevalsrc=0.45*(sin(2*PI*1568*t)+0.45*sin(2*PI*2349*t)+0.2*sin(2*PI*3136*t))*exp(-3.2*t)*min(1\\,t*500)", None, 1.1, 0.6),
    "dramatis": ("aevalsrc=0.55*tanh(1.8*sin(2*PI*if(lt(t\\,0.28)\\,147\\,if(lt(t\\,0.56)\\,139\\,110))*t))*exp(-2.2*mod(t\\,0.28)*lt(t\\,0.56))*if(gte(t\\,0.56)\\,exp(-1.6*(t-0.56))\\,1)", "aecho=0.8:0.4:90:0.3,lowpass=f=1800", 1.6, 0.8),
    "sedih": ("aevalsrc=0.4*tanh(3*sin(2*PI*if(lt(t\\,0.3)\\,294\\,if(lt(t\\,0.6)\\,277\\,if(lt(t\\,0.9)\\,262\\,247)))*t+2.5*gte(t\\,0.9)*sin(2*PI*6*t)))*if(gte(t\\,0.9)\\,exp(-1.4*(t-0.9))\\,0.85+0.15*cos(2*PI*mod(t\\,0.3)/0.3))", "lowpass=f=2200", 1.9, 0.6),
    "whoosh": ("anoisesrc=c=pink:a=0.7", "highpass=f=350,lowpass=f=5000,afade=t=in:d=0.28:curve=exp,afade=t=out:st=0.28:d=0.2", 0.5, 0.55),
    "pop": (None, None, 0.16, 0.8),
}
SFX_FOR = {"😂": "boing", "🔥": "airhorn", "🤯": "boom", "😱": "dramatis", "⚠️": "dramatis", "💰": "ding",
           "💡": "ding", "😢": "sedih", "🤫": "whoosh"}


def meme_sound(kind):
    """File efek suara. Bisa diganti sendiri: taruh boom*.mp3 / boing*.wav dst di folder sfx_meme."""
    user = os.path.join(APP_DIR, "sfx_meme")
    if os.path.isdir(user):
        c = [f for f in os.listdir(user) if f.lower().startswith(kind) and
             f.lower().endswith((".wav", ".mp3", ".ogg", ".m4a", ".flac"))]
        if c:
            return os.path.join(user, random.choice(c))
    folder = os.path.join(APP_DIR, "_sfx")
    if kind == "pop":
        return pop_wav(folder)
    p = os.path.join(folder, kind + ".wav")
    if not os.path.exists(p):
        os.makedirs(folder, exist_ok=True)
        src, af, d, _ = MEME_SFX[kind]
        srcf = (src + f":s=48000:d={d}") if src.startswith("aevalsrc") else f"{src}:d={d}:r=48000"
        cmd = [ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", srcf]
        cmd += ["-af", (af + "," if af else "") + "alimiter=limit=0.9"]
        run(cmd + ["-ac", "1", "-ar", "48000", p + ".tmp.wav"])
        r_ = subprocess.run([ffmpeg_exe(), "-i", p + ".tmp.wav", "-af", "volumedetect", "-f", "null", "-"],
                            capture_output=True, text=True, creationflags=NOWIN).stderr
        m_ = re.search(r"max_volume: (-?[\d.]+) dB", r_)
        gain = -1.5 - float(m_.group(1)) if m_ else 0.0
        run([ffmpeg_exe(), "-y", "-v", "error", "-i", p + ".tmp.wav", "-af", f"volume={gain:.1f}dB", p])
        os.remove(p + ".tmp.wav")
    return p


def meme_events(words, peaks, D, stickers=()):
    """Pilih momen efek suara meme: kata kunci (lucu/kaget/keren), puncak reaksi, pembukaan."""
    cand = [(0.05, "whoosh", 9)]
    for ws, we, w in words:
        k = SFX_FOR.get(kw_emoji(w))
        if k and 0.6 < we < D - 1.2:
            cand.append((we + 0.02, k, 3))
    cand += [(t, "boom", 2) for t in peaks]
    cand += [(t, "pop", 0) for t, _ in stickers]
    out = []
    lim = max(2, int(D / 7))
    for t, k, pr in sorted(cand, key=lambda c: (-c[2], c[0])):
        if len([1 for _, k2 in out if k2 not in ("pop", "whoosh")]) >= lim and k not in ("pop", "whoosh"):
            continue
        if all(abs(t - t2) > (1.0 if {"pop", "whoosh"} & {k, k2} else 3.2) for t2, k2 in out):
            out.append((t, k))
    return sorted(out)


def get_sticker(emoji, size, tmp):
    """Stiker emoji animasi berkualitas tinggi dari Google Noto Animated Emoji (disimpan di _stiker).
    Return (path, animasi?). Cadangan: render emoji lokal."""
    code = "_".join(f"{ord(c):x}" for c in emoji if ord(c) not in (0xFE0F, 0x200D))
    folder = os.path.join(APP_DIR, "_stiker")
    os.makedirs(folder, exist_ok=True)
    gif = os.path.join(folder, f"{code}.gif")
    if not os.path.exists(gif):
        try:
            import urllib.request
            req = urllib.request.Request(f"https://fonts.gstatic.com/s/e/notoemoji/latest/{code}/512.gif",
                                         headers={"User-Agent": "AutoClip"})
            with urllib.request.urlopen(req, timeout=20) as r_, open(gif + ".part", "wb") as f:
                f.write(r_.read())
            os.replace(gif + ".part", gif)
        except Exception:
            pass
    if os.path.exists(gif) and os.path.getsize(gif) > 1000:
        return gif, True
    if os.path.exists(EMOJI_FONT):
        return sticker_png(emoji, size, tmp), False
    return None, False


def sticker_graph(stickers, W, H, D, cur, tmp, first_input):
    inputs, g = [], []
    u = min(W, H)
    size = int(u * 0.24)
    R = int(size * 1.42) // 2 * 2
    spots = ([(0.22, 0.27), (0.78, 0.27), (0.20, 0.66), (0.80, 0.66)] if H > W
             else [(0.12, 0.30), (0.88, 0.30), (0.12, 0.62), (0.88, 0.62)])
    k = -1
    for t0, emo in stickers:
        src_, animated = get_sticker(emo, size, tmp)
        if not src_:
            continue
        k += 1
        if animated:
            inputs += ["-ignore_loop", "0", "-t", f"{D:.3f}", "-i", src_]
            pre = f"format=rgba,scale={R}:{R},setpts=PTS-STARTPTS"
        else:
            inputs += ["-loop", "1", "-framerate", str(OUT_FPS), "-t", f"{D:.3f}", "-i", src_]
            pre = f"format=rgba,rotate='0.2*sin(9*t)':c=none:ow={R}:oh={R}"
        t1 = min(D, t0 + 1.9)
        fx, fy = spots[k % len(spots)]
        x, y = int(fx * W - R / 2), int(fy * H - R / 2)
        g.append(f"[{first_input + k}:v]{pre},"
                 f"fade=t=in:st={t0:.2f}:d=0.12:alpha=1,fade=t=out:st={t1 - 0.2:.2f}:d=0.2:alpha=1[sk{k}]")
        g.append(f"{cur}[sk{k}]overlay=x={x}:y='{y}-max(0,1-(t-{t0:.2f})/0.18)*{int(H * 0.08)}'"
                 f":enable='between(t,{t0:.2f},{t1:.2f})':eof_action=pass[ov{k}]")
        cur = f"[ov{k}]"
    return inputs, g, cur


# ============================================================================ teks (ASS)

def add_logo(g, inputs, opt, W, H, D, cur):
    """Tempel logo (PNG) sebagai watermark di pojok pilihan. Return label baru."""
    if not opt.logo or not os.path.exists(opt.logo):
        return cur
    idx = inputs.count("-i")
    inputs += ["-loop", "1", "-framerate", str(OUT_FPS), "-t", f"{D:.3f}", "-i", os.path.abspath(opt.logo)]
    lw = int(min(W, H) * 0.11) // 2 * 2
    m = int(min(W, H) * 0.035)
    pos = {"kanan atas": (f"W-w-{m}", f"{m}"), "kiri atas": (f"{m}", f"{m}"),
           "kanan bawah": (f"W-w-{m}", f"H-h-{m}"), "kiri bawah": (f"{m}", f"H-h-{m}")}.get(opt.logo_pos, (f"W-w-{m}", f"{m}"))
    g.append(f"[{idx}:v]format=rgba,scale={lw}:-1,colorchannelmixer=aa=0.85[lg]")
    g.append(f"{cur}[lg]overlay=x={pos[0]}:y={pos[1]}:eof_action=pass[wm]")
    return "[wm]"


def ass_time(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def esc(x):
    return x.replace("\\", "").replace("{", "(").replace("}", ")")


def wrap2(text, maxc):
    if len(text) <= maxc or " " not in text:
        return text
    ws = text.split()
    best = min(range(1, len(ws)), key=lambda i: abs(len(" ".join(ws[:i])) - len(text) / 2))
    return " ".join(ws[:best]) + "\\N" + " ".join(ws[best:])


def ass_styles(W, H, cap_mv=None):
    u = min(W, H)
    FB, FI, FA = ass_font("black"), ass_font("impact"), ass_font("arial")
    vert = H > W
    if cap_mv is None:
        cap_mv = int(H * (0.17 if vert else 0.1))
    return [
        f"Style: Cap,{FB},{int(u * 0.072)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,0,0,0,0,100,100,0,0,1,"
        f"{max(3, int(u * 0.009))},{max(2, int(u * 0.004))},2,{int(W * .06)},{int(W * .06)},{cap_mv},1",
        f"Style: Title,{FB},{int(u * 0.05)},&H00141414,&H00141414,&H00FFFFFF,&H00000000,0,0,0,0,100,100,0,0,3,"
        f"{int(u * 0.016)},0,8,{int(W * .08)},{int(W * .08)},{int(H * (0.10 if vert else 0.05))},1",
        f"Style: Kin,{FB},{int(u * 0.095)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,1,"
        f"{int(u * 0.013)},{int(u * 0.008)},5,{int(W * .05)},{int(W * .05)},0,1",
        f"Style: Pop,{FB},{int(u * 0.09)},&H0000F0FF,&H0000F0FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,"
        f"{int(u * 0.012)},{int(u * 0.006)},5,0,0,0,1",
        f"Style: CTA,{FB},{int(u * 0.042)},&H00FFFFFF,&H00FFFFFF,&H002A2AE8,&H00000000,0,0,0,0,100,100,0,0,3,"
        f"{int(u * 0.018)},0,5,{int(W * .08)},{int(W * .08)},0,1",
        f"Style: CapB,{FI},{int(u * 0.105)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,"
        f"{max(4, int(u * 0.008))},{max(2, int(u * 0.004))},5,{int(W * .05)},{int(W * .05)},0,1",
        f"Style: Handle,{FA},{int(u * 0.022)},&H60FFFFFF,&H60FFFFFF,&H90000000,&H00000000,-1,0,0,0,100,100,0,0,1,"
        f"1,0,2,0,0,{int(u * 0.03)},1",
    ]


def write_ass(path, W, H, events, cap_mv=None):
    header = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\n"
              "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
              "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
              "MarginL, MarginR, MarginV, Encoding\n" + "\n".join(ass_styles(W, H, cap_mv)) +
              "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events) + "\n")


def ev(layer, a, b, style, text):
    return f"Dialogue: {layer},{ass_time(a)},{ass_time(b)},{style},,0,0,0,,{text}"


POPIN = "\\fscx40\\fscy40\\t(0,160,\\fscx108\\fscy108)\\t(160,260,\\fscx100\\fscy100)"


def common_events(W, H, D, title, opt, title_from=0.05):
    vert = H > W
    out = []
    has_cta = opt.creative and opt.cta and D > 8 and not opt.outro
    if opt.creative and title and opt.top_text:
        out.append(ev(2, title_from, D - 2.6 if has_cta else D, "Title",
                      "{" + POPIN + "}" + wrap2(esc(title.upper()), 22 if vert else 40)))
    if has_cta:
        out.append(ev(3, D - 2.6, D, "CTA", f"{{\\pos({W // 2},{int(H * (0.16 if vert else 0.2))})\\fscx30\\fscy30"
                      f"\\t(0,180,\\fscx110\\fscy110)\\t(180,280,\\fscx100\\fscy100)\\t(600,2600,\\frz-3)}}"
                      + wrap2(esc(opt.cta.upper()), 22 if vert else 40)))
    if opt.handle:
        out.append(ev(0, 0, D, "Handle", esc(opt.handle)))
    return out


def pop_event(t0, word, W, H, col="&H0014FF39"):
    rz = random.choice([-7, -5, 5, 7])
    return ev(4, t0, t0 + 0.9, "Pop", f"{{\\pos({W // 2},{int(H * 0.40)})\\c{col}&\\frz{rz}\\fscx20\\fscy20"
              f"\\t(0,130,\\fscx120\\fscy120)\\t(130,220,\\fscx100\\fscy100)\\fad(0,150)}}" + esc(word.upper()))


def kinetic_chunks(title, maxn=5):
    out, cur = [], ""
    for w in title.upper().split():
        cur = (cur + " " + w).strip()
        if len(cur) > 3:
            out.append(cur)
            cur = ""
    if cur:
        if out:
            out[-1] += " " + cur
        else:
            out.append(cur)
    return out[:maxn]


# ============================================================================ render: gaya CERITA

def keep_segments(s, e, segs, min_gap=0.45, pad=0.12):
    d = e - s
    gaps = []
    words = sorted((max(0.0, ws - s), min(d, we - s)) for sg in segs for ws, we, _ in sg["words"] if we > s and ws < e)
    if words:
        prev = 0.0
        for ws, we in words:
            if ws - prev > min_gap:
                gaps.append((prev + pad, ws - pad))
            prev = max(prev, we)
        if d - prev > min_gap + 0.3:
            gaps.append((prev + pad + 0.25, d))
    keep, cur = [], 0.0
    for a, b in gaps:
        if b - a < 0.15:
            continue
        if a - cur > 0.1:
            keep.append((round(cur, 3), round(a, 3)))
        cur = b
    if d - cur > 0.1:
        keep.append((round(cur, 3), round(d, 3)))
    if not keep or sum(b - a for a, b in keep) < 0.4 * d:
        return [(0.0, round(d, 3))]
    return keep


def make_mapper(keep):
    def f(t):
        acc = 0.0
        for a, b in keep:
            if t < a:
                return acc
            if t <= b:
                return acc + t - a
            acc += b - a
        return acc
    return f


FILLER_START = set("""aduh eh ehh nah oke ok okey jadi ya yah wah wow guys gais bro anjir anjay hmm hm em emm dan tapi
lho loh terus trus udah sih tuh kan ayo yuk oh ah uh so okay well like yeah hey""".split())


def strip_filler(text):
    ws = text.split()
    while len(ws) > 2 and clean(ws[0]) in FILLER_START:
        ws = ws[1:]
    t = " ".join(ws)
    return t[:1].upper() + t[1:] if t else t


def content_words(text):
    return [clean(w) for w in text.split() if len(clean(w)) > 2 and clean(w) not in FILLER_START
            and clean(w) not in TAG_STOP and clean(w) not in CAMP_STOP]


def auto_title(sentences, fallback=""):
    """Judul dari kalimat yang paling 'berisi': ada kata emosi/aksi, nyambung dengan judul video, bukan cuma 'aduh gimana nih'."""
    best, bs = None, -9
    ctx = set(content_words(fallback or ""))
    sentences = [strip_filler(fix_text(t)) for t in sentences]
    for text in sentences:
        ws = text.split()
        cw = content_words(text)
        if len(ws) < 3 or len(cw) < 2:
            continue
        sc = (sum(2 for w in ws if kw_emoji(w)) + 1.5 * ("?" in text) + ("!" in text) - abs(len(ws) - 7) * 0.15
              + 0.5 * len(cw) + 1.2 * sum(1 for w in set(cw) if w in ctx))
        if sc > bs:
            best, bs = text, sc
    if not best or (bs < 1.5 and fallback):
        return fallback or random.choice(FALLBACK_TITLES)
    ws = best.split()
    return (" ".join(ws[:8]).rstrip(",.;:") + ("..." if len(ws) > 8 else "")).upper()


def encode(inputs, g, maps, name, out_dir, D):
    out = name + ".mp4"
    os.makedirs(os.path.join(out_dir, "_bahan"), exist_ok=True)
    gfile = os.path.join("_bahan", name + "_graph.txt")
    with open(os.path.join(out_dir, gfile), "w", encoding="utf-8") as f:
        f.write(";\n".join(g))
    if "-c:a" in maps:
        maps = maps + ["-ac", "2"]
    run([ffmpeg_exe(), "-y", "-v", "error", *inputs, "-filter_complex_script", gfile, *maps,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", out], cwd=out_dir)
    run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{min(2.5, D / 2):.2f}", "-i", out,
         "-frames:v", "1", "-vf", "scale=360:-2", name + ".jpg"], cwd=out_dir)


def render_story(path, info, s, e, opt, segs, db, out_dir, name, title, log, hook=""):
    keep = keep_segments(s, e, segs) if opt.jumpcut else [(0.0, round(e - s, 3))]
    mapf = make_mapper(keep)
    D = round(sum(b - a for a, b in keep), 3)
    clean_look = opt.look == "bersih"
    words = []
    for sg in segs:
        for ws, we, w in sg["words"]:
            if we > s and ws < e:
                a, b = mapf(max(0, ws - s)), mapf(min(e - s, we - s))
                words.append((a, max(b, a + 0.05), w))
    tmp = os.path.join(out_dir, "_bahan")
    kind, cx, box, cap_y_fix = {"square": "square", "original": "original", "vertical_blur": "cinematic"}.get(
        opt.fmt, "face"), 0.5, None, None
    smart = opt.fmt == "vertical_face" and info["width"] > info["height"] * 1.05
    if opt.fmt == "square":
        cx, ratio, box = face_center_x(path, s, e)
    inputs = ["-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", *info["dec"], "-i", os.path.abspath(path)]
    sel = "+".join(f"between(t,{a},{b})" for a, b in keep)
    cut = len(keep) > 1 or keep[0][0] > 0.05
    q = "'"
    g = [f"[0:v]{f'select={q}{sel}{q},setpts=N/FRAME_RATE/TB,' if cut else ''}fps={OUT_FPS}[sel]"]
    if smart:
        speak_iv = [(ws - s, we - s) for sg in segs for ws, we, _ in sg["words"] if we > s and ws < e]
        fg, (W, H), kind, box, cap_y_fix = smart_reframe(path, info, s, e, keep, mapf, opt.frame or "auto", speak_iv,
                                                          "[sel]", "[fmt]", log, opt.cam_pos, opt.cam_box)
    else:
        fg, (W, H) = layout_graph(kind, info, "[sel]", "[fmt]", cx, box)
    g.append(fg)
    cur = "[fmt]"
    stickers, pops, zooms = [], [], []
    if opt.creative and not clean_look:
        last = -9.0
        for ws, we, w in words:
            emo = kw_emoji(w)
            if emo and 0.6 < ws < D - 2 and ws - last >= 3.0:
                stickers.append((ws, emo))
                pops.append((ws, re.sub(r"[^\w\-]", "", w) + ("?" if emo == "🤔" else "!")))
                last = ws
        i0 = int(s / HOP)
        seg = db[i0:i0 + int((e - s) / HOP) + 1]
        peaks = []
        if len(seg) > 4:
            rise = seg - smooth(seg, 12)
            for i in np.argsort(-rise)[:6]:
                t_rel = i * HOP
                if rise[i] < 5 or not any(a <= t_rel <= b for a, b in keep):
                    continue
                t = mapf(t_rel)
                if 0.6 < t < D - 1.5 and all(abs(t - p) > 3 for p in peaks):
                    peaks.append(t)
        for t in peaks[:2]:  # momen reaksi keras/tawa
            if all(abs(t - st) > 3 for st, _ in stickers):
                stickers.append((t, random.choice(["🔥", "😂", "🤯", "😱"])))
        if not stickers and D > 4:
            stickers.append((min(1.5, D / 3), "🔥"))
        stickers = sorted(stickers)[:max(2, int(D / 5))]
        for t in sorted([st for st, _ in stickers] + peaks):
            if all(abs(t - z) > 2.5 for z in zooms):
                zooms.append(t)
        if zooms:
            z = "+".join(f"between(it,{t:.2f},{t + 0.9:.2f})" for t in zooms)
            g.append(f"{cur}zoompan=z='if({z},1.13,1)':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={W}x{H}:fps={OUT_FPS}[zm]")
            cur = "[zm]"
        si, sg_, cur = sticker_graph(stickers, W, H, D, cur, tmp, 1)
        inputs += si
        g += sg_

    # hook (kotak putih) di awal + ajakan di akhir - gaya bersih
    overlays = []
    if opt.creative and clean_look:
        htxt = sentence_case(hook or title) if opt.top_text else ""
        if htxt:
            overlays.append((htxt, 0.05, min(3.6, D - 0.5), int(H * 0.075)))
        if opt.cta and D > 8 and not opt.outro:
            overlays.append((sentence_case(opt.cta), D - 2.6, D, int(H * 0.075)))
        for k, (txt, a, b, y) in enumerate(overlays):
            png, pw, ph = hook_png(txt, W, tmp, f"{name}_{k}")
            idx = inputs.count("-i")
            inputs += ["-loop", "1", "-framerate", str(OUT_FPS), "-t", f"{D:.3f}", "-i", png]
            g.append(f"[{idx}:v]format=rgba,fade=t=in:st={a:.2f}:d=0.15:alpha=1,"
                     f"fade=t=out:st={max(a, b - 0.25):.2f}:d=0.25:alpha=1[hk{k}]")
            g.append(f"{cur}[hk{k}]overlay=x=(W-w)/2:y={y}:enable='between(t,{a:.2f},{b:.2f})':eof_action=pass[ho{k}]")
            cur = f"[ho{k}]"

    if clean_look:
        events = [ev(0, 0, D, "Handle", esc(opt.handle))] if opt.handle else []
    else:
        events = common_events(W, H, D, title if opt.creative else "", opt) + [pop_event(t, w, W, H, accent_color(opt)) for t, w in pops]
    if opt.subtitles:
        maxw, maxt = (3, 1.2) if clean_look else (3, 1.4)
        chunks, cw = [], []
        for wd in words:
            cw.append(wd)
            if len(cw) >= maxw or (cw[-1][1] - cw[0][0]) > maxt or re.search(r"[.,!?;:]$", wd[2]):
                chunks.append(cw)
                cw = []
        if cw:
            chunks.append(cw)
        cap_y = cap_y_fix or {"gaming": 960, "face": int(H * 0.70), "cinematic": int(H * 0.79)}.get(kind, int(H * 0.82))
        for ci, ch in enumerate(chunks):
            nxt = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else 1e9
            for k, (ws, we, _) in enumerate(ch):
                end = ch[k + 1][0] if k + 1 < len(ch) else min(we + 0.1, nxt)
                parts = []
                for j, (_, _, w) in enumerate(ch):
                    w2 = esc(w.upper())
                    if j == k:
                        big = "\\fscx112\\fscy112" if opt.text_color == "putih" else ""
                        parts.append("{\\c" + accent_color(opt) + "&" + big + "}" + w2
                                     + "{\\c&HFFFFFF&\\fscx100\\fscy100}" if clean_look else
                                     "{\\c" + accent_color(opt) + "&\\fscx118\\fscy118\\t(0,110,\\fscx100\\fscy100)}" + w2 + "{\\r}")
                    elif kw_emoji(w) and not clean_look:
                        parts.append("{\\c&H55FF55&}" + w2 + "{\\r}")
                    else:
                        parts.append(w2)
                if clean_look:
                    events.append(ev(1, ws, max(end, ws + 0.05), "CapB", f"{{\\pos({W // 2},{cap_y})}}" + " ".join(parts)))
                else:
                    events.append(ev(1, ws, max(end, ws + 0.05), "Cap", " ".join(parts)))
    post = []
    if events:
        write_ass(os.path.join(out_dir, name + ".ass"), W, H, events, (H - cap_y_fix - 60) if cap_y_fix else None)
        post.append(subs_filter(name))
    fd = min(0.2, D / 4)
    post.append(f"fade=t=in:st=0:d={fd},fade=t=out:st={D - fd:.2f}:d={fd}")
    g.append(f"{cur}{','.join(post)}[v0]")
    lg = add_logo(g, inputs, opt, W, H, D, campaign_text_overlay(g, inputs, opt, W, H, D, "[v0]"))
    g.append(f"{lg}null[v]")
    maps = ["-map", "[v]"]
    if info["has_audio"]:
        g.append(f"[0:a]{f'aselect={q}{sel}{q},asetpts=N/SR/TB,' if cut else ''}"
                 f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,afade=t=in:st=0:d={fd},"
                 f"afade=t=out:st={D - fd:.2f}:d={fd}[a0]")
        aout = "[a0]"
        sfx_ev = []
        if opt.sfx and opt.meme_sfx:
            i0 = int(s / HOP)
            seg_ = db[i0:i0 + int((e - s) / HOP) + 1]
            pk = []
            if len(seg_) > 4:
                rise = seg_ - smooth(seg_, 12)
                for i in np.argsort(-rise)[:6]:
                    t_rel = i * HOP
                    if rise[i] < 6 or not any(a <= t_rel <= b for a, b in keep):
                        continue
                    t = mapf(t_rel)
                    if 1.0 < t < D - 1.5 and all(abs(t - x) > 4 for x in pk):
                        pk.append(t)
            sfx_ev = meme_events(words, pk[:2], D, stickers)
        elif opt.sfx:
            sfx_ev = [(t0, "pop") for t0, _ in stickers]
        if sfx_ev:
            labs = []
            for i, (t0, k) in enumerate(sfx_ev):
                idx = inputs.count("-i")
                inputs += ["-i", meme_sound(k)]
                vol = MEME_SFX[k][3]
                g.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,adelay=delays={int(t0 * 1000)}:all=1,"
                         f"volume={vol}[pd{i}]")
                labs.append(f"[pd{i}]")
            g.append("[a0]" + "".join(labs) + f"amix=inputs={len(labs) + 1}:duration=first:normalize=0,"
                     f"alimiter=limit=0.95[a]")
            aout = "[a]"
            log(f"      efek suara: " + ", ".join(f"{k}@{t:.1f}s" for t, k in sfx_ev))
        maps += ["-map", aout, "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    encode(inputs, g, maps, name, out_dir, D)
    lay = FRAME_LABEL.get(kind, kind)
    log(f"      edit: layout {lay}, gaya {opt.look}, jeda dibuang {e - s - D:.1f} dtk"
        + (f", {len(stickers)} stiker, {len(zooms)} zoom" if not clean_look else ""))
    return D, kind, box


# ============================================================================ pembuka & penutup + suara AI

VOICES = {"pria": ("id-ID-ArdiNeural", "en-US-GuyNeural"), "wanita": ("id-ID-GadisNeural", "en-US-JennyNeural")}
VOICE_LABEL = {"pria": "Pria (Ardi)", "wanita": "Wanita (Gadis)", "mati": "Tanpa suara"}


def tts(text, voice, lang, out_wav, log):
    """Suara AI: Microsoft Edge Neural (online, natural). Cadangan: suara Windows (offline). Return durasi (detik)."""
    if voice == "mati" or not text.strip():
        return 0.0
    mp3 = out_wav[:-4] + ".mp3"
    try:
        import asyncio
        import edge_tts
        vname = VOICES.get(voice, VOICES["pria"])[1 if lang == "en" else 0]

        async def go():
            await edge_tts.Communicate(text, vname, rate="+6%").save(mp3)
        asyncio.run(go())
        run([ffmpeg_exe(), "-y", "-v", "error", "-i", mp3, "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
             "-ar", "48000", "-ac", "2", out_wav])
    except Exception as ex:
        if os.name != "nt":
            log(f"      suara AI gagal ({ex})")
            return 0.0
        try:  # cadangan offline: suara Windows
            ps = ("Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  f"$s.SetOutputToWaveFile('{out_wav}.tmp.wav'); $s.Speak([Console]::In.ReadToEnd()); $s.Dispose()")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], input=text.encode("utf-8"),
                           capture_output=True, creationflags=NOWIN, timeout=60)
            run([ffmpeg_exe(), "-y", "-v", "error", "-i", out_wav + ".tmp.wav", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                 "-ar", "48000", "-ac", "2", out_wav])
        except Exception as ex2:
            log(f"      suara AI gagal ({ex2})")
            return 0.0
    try:
        import wave
        with wave.open(out_wav) as wf:
            return wf.getnframes() / wf.getframerate()
    except Exception:
        return 0.0


def card_png(bg, text, sub, logo, out):
    """Kartu judul 9:16: latar blur gelap dari video + teks besar + nama seri + logo."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
    W, H = 1080, 1920
    im = bg.convert("RGB")
    sc = max(W / im.width, H / im.height)
    im = im.resize((int(im.width * sc) + 1, int(im.height * sc) + 1))
    im = im.crop(((im.width - W) // 2, (im.height - H) // 2, (im.width - W) // 2 + W, (im.height - H) // 2 + H))
    im = ImageEnhance.Brightness(im.filter(ImageFilter.GaussianBlur(22))).enhance(0.62).convert("RGBA")
    draw_big_text(im, text.upper(), (90, 760, 900, 380), align="center", rotate=-2, max_size=104)
    d = ImageDraw.Draw(im)
    if sub:
        fp = font_path("impact.ttf", "ariblk.ttf", "arialbd.ttf")
        f = ImageFont.truetype(fp, 56) if fp else ImageFont.load_default()
        tw = d.textlength(sub.upper(), font=f)
        d.rounded_rectangle([(W - tw) / 2 - 28, 520, (W + tw) / 2 + 28, 600], radius=16, fill=(230, 30, 30, 255))
        d.text(((W - tw) / 2, 528), sub.upper(), font=f, fill=(255, 255, 255, 255))
    if logo and os.path.exists(logo):
        try:
            lg = Image.open(logo).convert("RGBA")
            lw = 220
            lg = lg.resize((lw, int(lw * lg.height / lg.width)))
            im.alpha_composite(lg, ((W - lw) // 2, 1400))
        except Exception:
            pass
    im.convert("RGB").save(out, quality=95)


BRAND_POS = ["mati", "awal & akhir", "awal", "akhir"]


def brand_png(bg, logo, text, out, accent="&H0014FF39"):
    """Kartu logo 9:16: logo besar di tengah + tulisan di bawahnya, latar video gelap ter-blur."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
    W, H = 1080, 1920
    im = bg.convert("RGB")
    sc = max(W / im.width, H / im.height)
    im = im.resize((int(im.width * sc) + 1, int(im.height * sc) + 1))
    im = im.crop(((im.width - W) // 2, (im.height - H) // 2, (im.width - W) // 2 + W, (im.height - H) // 2 + H))
    im = ImageEnhance.Brightness(im.filter(ImageFilter.GaussianBlur(30))).enhance(0.32).convert("RGBA")
    # vinyet halus agar logo menonjol
    vg = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vg).ellipse([-200, 300, W + 200, H - 300], fill=110)
    vg = vg.filter(ImageFilter.GaussianBlur(160))
    im = Image.composite(Image.new("RGBA", (W, H), (255, 255, 255, 255)), im,
                         vg.point(lambda v: int(v * 0.12)))
    d = ImageDraw.Draw(im)
    lg_bottom = 960
    if logo and os.path.exists(logo):
        try:
            lg = Image.open(logo).convert("RGBA")
            box = 600
            r_ = min(box / lg.width, box / lg.height)
            lg = lg.resize((max(1, int(lg.width * r_)), max(1, int(lg.height * r_))), Image.LANCZOS)
            y0 = 860 - lg.height // 2 - (60 if text else 0)
            sh = Image.new("RGBA", lg.size, (0, 0, 0, 0))
            sh.putalpha(lg.split()[3].point(lambda a: int(a * 0.55)))
            im.alpha_composite(sh.filter(ImageFilter.GaussianBlur(14)), ((W - lg.width) // 2 + 6, y0 + 12))
            im.alpha_composite(lg, ((W - lg.width) // 2, y0))
            lg_bottom = y0 + lg.height
        except Exception:
            pass
    if text:
        b, g_, r2 = int(accent[4:6], 16), int(accent[6:8], 16), int(accent[8:10], 16)
        fp = font_path("impact.ttf", "ariblk.ttf", "arialbd.ttf")
        size = 64 if len(text) <= 16 else 54
        f = ImageFont.truetype(fp, size) if fp else ImageFont.load_default()
        lines = wrap_text(d, text.upper(), f, W - 140)[:3]
        y = lg_bottom + 70
        for l in lines:
            tw = d.textlength(l, font=f)
            d.text(((W - tw) / 2, y), l, font=f, fill=(255, 255, 255, 255), stroke_width=7, stroke_fill=(0, 0, 0, 255))
            y += int(size * 1.15)
        d.rounded_rectangle([W / 2 - 110, y + 18, W / 2 + 110, y + 32], radius=7, fill=(r2, g_, b, 255))
    im.convert("RGB").save(out, quality=95)


def render_brand(png, sfx_wav, dur, out, with_audio):
    """Logo muncul dengan efek 'pop' (membesar lalu pelan), lalu fade."""
    fd = 0.22
    vf = (f"[0:v]scale=1080:1920,zoompan=z='if(lt(on,9),0.86+0.2*on/9,1.06-0.0012*(on-9))':"
          f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=1080x1920:fps={OUT_FPS},"
          f"fade=t=in:st=0:d={fd},fade=t=out:st={dur - fd:.2f}:d={fd},format=yuv420p[v]")
    inputs = ["-loop", "1", "-framerate", str(OUT_FPS), "-t", f"{dur:.3f}", "-i", png]
    maps = ["-map", "[v]"]
    if with_audio:
        if sfx_wav:
            inputs += ["-i", sfx_wav]
            vf += f";[1:a]aresample=48000,aformat=channel_layouts=stereo,volume=0.7,apad,atrim=0:{dur:.3f}[a]"
        else:
            inputs += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            vf += ";[1:a]anull[a]"
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
    run([ffmpeg_exe(), "-y", "-v", "error", *inputs, "-filter_complex", vf, *maps, "-t", f"{dur:.3f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-r", str(OUT_FPS), out])


def render_card(png, wav, dur, out, with_audio):
    fd = 0.25
    vf = (f"[0:v]scale=1080:1920,zoompan=z='min(1+0.0011*on,1.08)':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1"
          f":s=1080x1920:fps={OUT_FPS},fade=t=in:st=0:d={fd},fade=t=out:st={dur - fd:.2f}:d={fd},format=yuv420p[v]")
    inputs = ["-loop", "1", "-framerate", str(OUT_FPS), "-t", f"{dur:.3f}", "-i", png]
    maps = ["-map", "[v]"]
    if with_audio:
        if wav:
            inputs += ["-i", wav]
            af = f"[1:a]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=0:{dur:.3f}[a]"
        else:
            inputs += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            af = "[1:a]anull[a]"
        vf += ";" + af
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
    run([ffmpeg_exe(), "-y", "-v", "error", *inputs, "-filter_complex", vf, *maps, "-t", f"{dur:.3f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-r", str(OUT_FPS), out])


def add_intro_outro(out_dir, name, title, opt, has_audio, lang, log):
    """Pembuka (judul + suara AI) dan penutup (ajakan + suara AI) di sekitar klip. Return tambahan durasi."""
    blogo = (opt.brand_logo or opt.logo or "").strip()
    btext = (opt.brand_text or opt.handle or "").strip()
    brand = opt.brand if opt.brand in BRAND_POS and (os.path.exists(blogo) or btext) else "mati"
    if not (opt.intro or opt.outro or brand != "mati"):
        return 0.0
    tmp = os.path.join(out_dir, "_bahan", name + "_io")
    os.makedirs(tmp, exist_ok=True)
    main = os.path.join(out_dir, name + ".mp4")
    D = probe(main)["duration"]
    parts, added = [], 0.0
    try:
        bpng = None
        if brand != "mati":
            bpng = os.path.join(tmp, "brand.png")
            brand_png(grab_frame(main, min(1.0, D / 3), [], 1080), blogo, btext, bpng, accent_color(opt))
            sfx_ = meme_sound("whoosh") if opt.sfx else None
            if brand in ("awal & akhir", "awal"):
                render_brand(bpng, sfx_, 1.8, os.path.join(tmp, "brand_a.mp4"), has_audio)
                parts.append("brand_a.mp4")
                added += 1.8
        if opt.intro and title:
            wav = os.path.join(tmp, "intro.wav")
            vd = tts(sentence_case(title), opt.voice, lang, wav, log) if has_audio else 0.0
            dur = min(5.0, max(2.0, vd + 0.5))
            png = os.path.join(tmp, "intro.png")
            card_png(grab_frame(main, min(1.0, D / 3), [], 1080), title, opt.title.strip(), opt.logo, png)
            render_card(png, wav if vd else None, dur, os.path.join(tmp, "intro.mp4"), has_audio)
            parts.append("intro.mp4")
            added += dur
        parts.append(None)
        if opt.outro and opt.cta.strip():
            wav = os.path.join(tmp, "outro.wav")
            vd = tts(sentence_case(opt.cta), opt.voice, lang, wav, log) if has_audio else 0.0
            dur = min(5.0, max(2.4, vd + 0.6))
            png = os.path.join(tmp, "outro.png")
            card_png(grab_frame(main, max(0.0, D - 1.0), [], 1080), opt.cta, opt.title.strip(), opt.logo, png)
            render_card(png, wav if vd else None, dur, os.path.join(tmp, "outro.mp4"), has_audio)
            parts.append("outro.mp4")
            added += dur
        if bpng and brand in ("awal & akhir", "akhir"):
            sfx_ = meme_sound("ding") if opt.sfx else None
            render_brand(bpng, sfx_, 2.2, os.path.join(tmp, "brand_z.mp4"), has_audio)
            parts.append("brand_z.mp4")
            added += 2.2
        if added == 0:
            return 0.0
        lst = os.path.join(tmp, "list.txt")
        with open(lst, "w", encoding="utf-8") as f:
            for p_ in parts:
                f.write(f"file '{os.path.abspath(main) if p_ is None else os.path.join(tmp, p_)}'\n".replace("\\", "/"))
        final = os.path.join(tmp, "final.mp4")
        try:
            run([ffmpeg_exe(), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                 "-movflags", "+faststart", final])
        except Exception:  # parameter beda -> gabung ulang (encode)
            run([ffmpeg_exe(), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c:v", "libx264",
                 "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                 "-movflags", "+faststart", final])
        if opt.keep_core:
            os.makedirs(os.path.join(out_dir, "_inti"), exist_ok=True)
            shutil.copy(main, os.path.join(out_dir, "_inti", name + ".mp4"))
        os.replace(final, main)
        log(f"      pembuka & penutup ditambahkan (+{added:.1f} dtk"
            + (f", logo di {brand}" if brand != "mati" else "")
            + f"{', suara AI ' + VOICE_LABEL.get(opt.voice, '') if opt.voice != 'mati' else ''})")
        return added
    except Cancelled:
        raise
    except Exception as ex:
        log(f"      pembuka/penutup gagal: {str(ex)[:200]}")
        return 0.0


# ============================================================================ paket publikasi YouTube

THUMB_EMO = {}


def grab_frame(path, t, dec, width=1280):
    from PIL import Image
    import io
    raw = run([ffmpeg_exe(), "-v", "error", "-ss", f"{max(0, t):.2f}", *dec, "-i", path, "-frames:v", "1",
               "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "png", "pipe:1"])
    return Image.open(io.BytesIO(raw)).convert("RGB")


def frame_quality(img):
    g = np.asarray(img.convert("L").resize((320, 180)), np.float32)
    lap = np.abs(4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]).mean()
    hsv = np.asarray(img.convert("HSV").resize((160, 90)), np.float32)
    sat, val = hsv[..., 1].mean() / 255, hsv[..., 2].mean() / 255
    return lap / 20 + sat + (1 - abs(val - 0.55) * 1.6)


def thumb_text_from(title, words_text):
    ws = [w for w in re.sub(r"[^\w\s!?$.,%-]", " ", title or "").split()]
    if not ws:
        return "WAJIB NONTON!"
    idx = next((i for i, w in enumerate(ws) if kw_emoji(w)), None)
    pick = ws[max(0, idx - 1):idx + 2] if idx is not None else ws[:3]
    t = " ".join(pick).strip(" ,.").upper()
    return t if t.endswith(("!", "?")) else t + "!"


def draw_big_text(img, text, box, align="left", rotate=-3, max_size=None):
    """Teks besar kuning/putih dengan outline hitam tebal + bayangan."""
    from PIL import Image, ImageDraw, ImageFont
    x0, y0, mw, mh = box
    fp = font_path("impact.ttf", "ariblk.ttf", "arialbd.ttf")
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    size, lines, font = int(mh / 1.6), [text], None
    if max_size:
        size = min(size, max_size)
    while size > 30:
        font = ImageFont.truetype(fp, size) if fp else ImageFont.load_default()
        lines = wrap_text(d, text, font, mw)
        if len(lines) <= 3 and len(lines) * size * 1.05 <= mh and all(d.textlength(l, font=font) <= mw for l in lines):
            break
        size -= 6
    lh = int(size * 1.02)
    y = y0 + (mh - lh * len(lines)) / 2
    sw = max(4, size // 11)
    for i, l in enumerate(lines):
        lw = d.textlength(l, font=font)
        x = x0 if align == "left" else x0 + (mw - lw) / 2
        col = (255, 228, 0, 255) if i % 2 == 0 else (255, 255, 255, 255)
        d.text((x + sw * 0.8, y + sw * 0.9), l, font=font, fill=(0, 0, 0, 200), stroke_width=sw, stroke_fill=(0, 0, 0, 200))
        d.text((x, y), l, font=font, fill=col, stroke_width=sw, stroke_fill=(0, 0, 0, 255))
        y += lh
    if rotate:
        layer = layer.rotate(rotate, resample=Image.BICUBIC, center=(x0 + mw / 2, y0 + mh / 2))
    img.alpha_composite(layer)


def make_thumbnail_16x9(path, info, s, e, kind, box, text, badge, out, logo=""):
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    best, bq, bt = None, -9, s
    for t in np.linspace(s + 1, max(s + 1.1, e - 1), 8):
        try:
            im = grab_frame(path, t, info["dec"])
        except Exception:
            continue
        qv = frame_quality(im)
        if qv > bq:
            best, bq, bt = im, qv, t
    if best is None:
        return None
    W, H = 1280, 720
    im = best.resize((W, int(W * best.height / best.width)))
    if im.height < H:
        im = im.resize((int(H * im.width / im.height), H))
    im = im.crop(((im.width - W) // 2, (im.height - H) // 2, (im.width - W) // 2 + W, (im.height - H) // 2 + H))
    im = ImageEnhance.Color(im).enhance(1.35)
    im = ImageEnhance.Contrast(im).enhance(1.15)
    im = ImageEnhance.Sharpness(im).enhance(1.4).convert("RGBA")
    # bayangan gelap di kiri untuk area teks
    grad = Image.new("L", (W, 1))
    grad.putdata([int(max(0, 215 * (1 - x / (W * 0.72)))) for x in range(W)])
    im.alpha_composite(Image.merge("RGBA", [Image.new("L", (W, H), 0)] * 3 + [grad.resize((W, H))]))
    text_w = W * 0.62
    if kind in ("gaming", "split_game", "split_react", "split_screen", "present") and box:  # wajah reaksi di kanan
        fx, fy, fw, fh = box
        sw_, sh_ = best.width, best.height
        cw = fw * 2.4 * sw_
        ch = cw * 1.15
        cx, cy = (fx + fw / 2) * sw_, (fy + fh * 0.8) * sh_
        crop = best.crop((int(max(0, cx - cw / 2)), int(max(0, cy - ch / 2)),
                          int(min(sw_, cx + cw / 2)), int(min(sh_, cy + ch / 2))))
        th = int(H * 0.86)
        crop = ImageEnhance.Color(crop.resize((int(th * crop.width / crop.height), th))).enhance(1.3)
        frame = Image.new("RGBA", (crop.width + 16, crop.height + 16), (255, 255, 255, 255))
        frame.paste(crop, (8, 8))
        frame = frame.rotate(3, expand=True, resample=Image.BICUBIC)
        im.alpha_composite(frame.filter(ImageFilter.SMOOTH), (W - frame.width - 24, (H - frame.height) // 2))
        text_w = W - frame.width - 90
    draw_big_text(im, text, (48, 200, text_w, 340), max_size=112)
    d = ImageDraw.Draw(im)
    if badge:
        from PIL import ImageFont
        fp = font_path("impact.ttf", "ariblk.ttf", "arialbd.ttf")
        f = ImageFont.truetype(fp, 52) if fp else ImageFont.load_default()
        bw = d.textlength(badge.upper(), font=f) + 44
        d.rounded_rectangle([40, H - 110, 40 + bw, H - 40], radius=14, fill=(230, 30, 30, 255))
        d.text((62, H - 106), badge.upper(), font=f, fill=(255, 255, 255, 255))
    if logo and os.path.exists(logo):
        try:
            from PIL import Image as _I
            lg = _I.open(logo).convert("RGBA")
            lw = int(W * 0.13)
            lg = lg.resize((lw, int(lw * lg.height / lg.width)))
            im.alpha_composite(lg, (W - lw - 28, 24))
        except Exception:
            pass
    im.convert("RGB").save(out, quality=92)
    return bt


GEMINI_IMG_MODELS = ["gemini-nano-banana-2.1", "gemini-3.1-flash-image", "gemini-2.5-flash-image", "gemini-3-pro-image"]
_GEM_OK = {}   # cara yang berhasil (model, api) / "mati" bila key ditolak


def _find_b64_image(j):
    """Cari data gambar base64 di respons Gemini (generateContent / interactions)."""
    if isinstance(j, dict):
        for k in ("inlineData", "inline_data"):
            v = j.get(k)
            if isinstance(v, dict) and str(v.get("mimeType", v.get("mime_type", "image"))).startswith("image") \
                    and v.get("data"):
                return v["data"]
        if j.get("type") == "image" and isinstance(j.get("data"), str) and len(j["data"]) > 1000:
            return j["data"]
        for v in j.values():
            r_ = _find_b64_image(v)
            if r_:
                return r_
    elif isinstance(j, list):
        for v in j:
            r_ = _find_b64_image(v)
            if r_:
                return r_
    return None


def gemini_image(key, prompt, imgs, aspect, size, out, log=print):
    """Buat gambar dengan Google Gemini (Nano Banana) dari prompt + gambar acuan. Return True bila berhasil."""
    import base64
    import io
    import urllib.error
    import urllib.request
    from PIL import Image
    if not key or _GEM_OK.get(key) == "mati":
        return False
    datas = []
    for im in imgs:
        b = io.BytesIO()
        im.convert("RGB").save(b, "JPEG", quality=90)
        datas.append(base64.b64encode(b.getvalue()).decode())

    def body(api, m, with_ratio=True):
        if api == "gc":
            gc = {"responseModalities": ["IMAGE", "TEXT"]}
            if with_ratio:
                gc["imageConfig"] = {"aspectRatio": aspect}
            return (f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent", {},
                    {"contents": [{"role": "user", "parts": [{"text": prompt}] +
                                   [{"inline_data": {"mime_type": "image/jpeg", "data": d}} for d in datas]}],
                     "generationConfig": gc})
        return ("https://generativelanguage.googleapis.com/v1beta/interactions", {"Api-Revision": "2026-05-20"},
                {"model": m, "input": [{"type": "text", "text": prompt}] +
                 [{"type": "image", "mime_type": "image/jpeg", "data": d} for d in datas],
                 "response_format": {"type": "image", "aspect_ratio": aspect}})

    ways = [_GEM_OK[key]] if key in _GEM_OK else [(api, m) for m in GEMINI_IMG_MODELS for api in ("gc", "ia")]
    last = ""
    for api, m in ways:
        for with_ratio in (True, False) if api == "gc" else (True,):
            url, hdr, bd = body(api, m, with_ratio)
            req = urllib.request.Request(url, data=json.dumps(bd).encode(), headers={
                "Content-Type": "application/json", "x-goog-api-key": key, **hdr})
            try:
                with urllib.request.urlopen(req, timeout=150) as r_:
                    j = json.loads(r_.read().decode("utf-8"))
            except urllib.error.HTTPError as ex:
                try:
                    msg = json.loads(ex.read().decode("utf-8", "ignore")).get("error", {}).get("message", "")
                except Exception:
                    msg = ""
                last = f"{ex.code} {msg[:160]}"
                if ex.code == 401 or "API key not valid" in msg or "API_KEY_INVALID" in msg:
                    _GEM_OK[key] = "mati"
                    log(f"      thumbnail AI: API key Gemini ditolak ({last})")
                    return False
                if ex.code == 429:
                    log(f"      thumbnail AI: kuota Gemini habis / dibatasi ({msg[:120]})")
                    return False
                if ex.code == 400 and with_ratio and api == "gc":
                    continue  # coba tanpa pengaturan rasio
                break
            except Exception as ex:
                last = str(ex)[:160]
                break
            data = _find_b64_image(j)
            if not data:
                last = "tidak ada gambar di respons"
                break
            im = Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")
            W, H = size
            sc = max(W / im.width, H / im.height)
            im = im.resize((max(W, round(im.width * sc)), max(H, round(im.height * sc))), Image.LANCZOS)
            l_, t_ = (im.width - W) // 2, (im.height - H) // 2
            im.crop((l_, t_, l_ + W, t_ + H)).save(out, quality=93)
            _GEM_OK[key] = (api, m)
            return True
    log(f"      thumbnail AI gagal ({last}) -> pakai desain lokal")
    return False


def ai_thumb_prompt(text, title, genre, vertical=False):
    kind = "cover YouTube Shorts / TikTok vertikal 9:16" if vertical else "thumbnail YouTube 16:9"
    return (
        f"Buat {kind} yang sangat menarik dan bikin penasaran (gaya kreator YouTube top, click-through tinggi) "
        f"memakai gambar acuan dari video ini. Topik video: \"{title}\". Jenis konten: {genre or 'umum'}.\n"
        "- Pertahankan orang, wajah, dan karakter/adegan dari gambar acuan agar tetap mirip aslinya; "
        "perbesar wajah/ekspresi paling menarik sebagai fokus utama.\n"
        "- Warna cerah dan kontras, pencahayaan dramatis, latar dipertajam, komposisi rapi dan bersih.\n"
        f"- Tambahkan SATU teks judul singkat persis: \"{text}\" (ejaan persis seperti ini, huruf kapital tebal, "
        "outline gelap, ukuran sedang - maksimal sekitar 20% luas gambar, tidak menutupi wajah).\n"
        "- Boleh tambah elemen grafis sederhana (panah, lingkaran, emoji reaksi) bila cocok.\n"
        "- Jangan ada teks lain, logo, watermark, atau tulisan acak.")


def make_ai_thumbs(opt, path, info, clip, out_dir, tfile, cfile, text, video_title, log=print):
    """Ganti thumbnail & cover lokal dengan versi AI Gemini (versi lokal disimpan sebagai cadangan)."""
    key = (getattr(opt, "gemini_key", "") or os.environ.get("GEMINI_API_KEY", "")).strip() if opt else ""
    if not key or not getattr(opt, "ai_thumb", True):
        return
    title = clip.get("title") or video_title or ""
    genre = clip.get("genre", "")
    tp = os.path.join(out_dir, tfile)
    refs = []
    for t in np.linspace(clip["start"] + 1, max(clip["start"] + 1.1, clip["end"] - 1), 6):
        try:
            im = grab_frame(path, t, info["dec"])
            refs.append((frame_quality(im), im))
        except Exception:
            pass
    refs = [im for _, im in sorted(refs, key=lambda x: -x[0])[:2]]
    if not refs:
        return
    log("      membuat thumbnail dengan AI Gemini...")
    for fn, size, vert in ((tfile, (1280, 720), False), (cfile, (1080, 1920), True)):
        fp = os.path.join(out_dir, fn)
        tmp = fp + ".ai.jpg"
        if gemini_image(key, ai_thumb_prompt(text, title, genre, vert), refs, "9:16" if vert else "16:9",
                        size, tmp, log):
            if os.path.exists(fp):
                os.replace(fp, fp[:-4] + "_lokal.jpg")
            os.replace(tmp, fp)
        else:
            break
    if os.path.exists(tp[:-4] + "_lokal.jpg"):
        log("      thumbnail AI selesai ✓")


def make_cover_9x16(clip_mp4, D, text, out):
    from PIL import ImageEnhance
    im = grab_frame(clip_mp4, min(D * 0.45, max(0.5, D - 1)), [], width=1080)
    im = ImageEnhance.Color(im).enhance(1.25).convert("RGBA")
    W, H = im.size
    draw_big_text(im, text, (80, int(H * 0.34), W - 160, int(H * 0.16)), align="center", rotate=-2,
                  max_size=int(W * 0.095))
    im.convert("RGB").save(out, quality=92)


LANG_NAMES = {"en": "English", "ms": "Bahasa Melayu", "es": "Español", "pt": "Português", "ar": "العربية",
              "hi": "हिन्दी", "ja": "日本語", "ko": "한국어", "zh-CN": "中文", "fr": "Français", "de": "Deutsch",
              "jv": "Basa Jawa", "su": "Basa Sunda", "th": "ไทย", "vi": "Tiếng Việt", "tl": "Filipino"}
DESC_LANGS = {
    "": "Tidak ada",
    "en": "English",
    "en,ms": "English + Melayu",
    "en,es,pt": "English + Spanyol + Portugis",
    "en,ms,ar,hi": "English + Melayu + Arab + India",
    "en,ms,es,pt,ar,hi,ja,ko": "8 bahasa (EN MS ES PT AR HI JA KO)",
}


def _gtx(text, lang, src="id"):
    """Google Translate gratis (teks dikirim di body POST, bukan di URL)."""
    import urllib.parse
    import urllib.request
    out = []
    for para in text.split("\n"):
        if not para.strip() or para.strip().startswith("#"):
            out.append(para)
            continue
        req = urllib.request.Request(
            f"https://translate.googleapis.com/translate_a/single?client=gtx&sl={src}&tl={lang}&dt=t",
            data=urllib.parse.urlencode({"q": para}).encode(), headers={"User-Agent": "Mozilla/5.0"})
        for att in range(5):  # dibatasi Google (429) -> tunggu sebentar lalu coba lagi
            try:
                with urllib.request.urlopen(req, timeout=20) as r_:
                    j = json.loads(r_.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as ex:
                if ex.code != 429 or att == 4:
                    raise
                time.sleep(3 * 2 ** att)
        time.sleep(0.3)
        out.append("".join(seg[0] for seg in j[0] if seg and seg[0]))
    return "\n".join(out)


def translate_kit(title, desc, langs, api_key="", log=print, keep=()):
    """Terjemahkan judul + deskripsi ke beberapa bahasa. Return {kode: (judul, deskripsi)}.
    Pakai Claude bila API key ada (lebih natural), selain itu Google Translate."""
    langs = [l for l in langs if l and l != "id"]
    if not langs:
        return {}
    res = {}
    if api_key:
        try:
            import anthropic
            schema = {"type": "object", "properties": {"items": {"type": "array", "items": {
                "type": "object", "properties": {"lang": {"type": "string", "enum": langs}, "title": {"type": "string"},
                                                 "description": {"type": "string"}},
                "required": ["lang", "title", "description"], "additionalProperties": False}}},
                "required": ["items"], "additionalProperties": False}
            prompt = (f"Terjemahkan judul dan deskripsi YouTube Shorts berikut ke bahasa: {', '.join(langs)}. "
                      "Pertahankan gaya santai & menarik khas kreator di bahasa tujuan, jangan kaku. Judul maks 90 karakter. "
                      "Hashtag (#...) dan nama orang/game/merek jangan diterjemahkan.\n\n"
                      f"JUDUL:\n{title}\n\nDESKRIPSI:\n{desc}")
            resp = anthropic.Anthropic(api_key=api_key).beta.messages.create(
                model=CLAUDE_MODEL, max_tokens=8000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": prompt}])
            if resp.stop_reason != "refusal":
                for it in json.loads(next(b.text for b in resp.content if b.type == "text"))["items"]:
                    res[it["lang"]] = (it["title"].strip()[:100], it["description"].strip())
        except Exception as ex:
            log(f"      terjemahan Claude gagal ({str(ex)[:120]}) -> Google Translate")
    for l in langs:
        if l in res:
            continue
        try:
            keep_ = sorted({k for k in keep if k and len(k) > 1}, key=len, reverse=True)

            def prot(x):
                for i, k in enumerate(keep_):
                    x = x.replace(k, f"[{i}]")
                return x

            def unprot(x):
                for i, k in enumerate(keep_):
                    x = re.sub(rf"\[\s*{i}\s*\]", lambda _m: k, x)
                return x
            res[l] = (unprot(_gtx(prot(title), l)).strip()[:100], unprot(_gtx(prot(desc), l)).strip())
        except Exception as ex:
            log(f"      terjemahan {l} gagal: {str(ex)[:120]}")
    return res


def make_publish_kit(path, info, clip, out_dir, name, video_title, kind=None, box=None, badge="", log=print, logo="",
                     langs=(), api_key="", campaign=None, opt=None):
    """Judul YouTube (<=100), deskripsi, tag, thumbnail 16:9 & cover Shorts 9:16."""
    hook = sentence_case(clip.get("hook") or clip["title"])
    # judul YouTube = judul klip (dipilih dari kalimat paling berisi), bukan sekadar kalimat pembuka
    yt_title = fix_text(clip.get("yt_title") or strip_filler(sentence_case(clip["title"] or hook)))
    if video_title and len(yt_title) + len(video_title) + 3 <= 92 and video_title.lower() not in yt_title.lower():
        yt_title = f"{yt_title} | {video_title.title() if video_title.isupper() else video_title}"
    if len(yt_title) + 8 <= 100:
        yt_title += " #shorts"
    yt_title = yt_title[:100]
    tags = clip.get("hashtags") or []
    teaser = {
        "Reaksi": "Reaksinya nggak ketolong, tonton sampai habis!",
        "Lucu": "Dijamin ngakak, tonton sampai akhir!",
        "Heboh": "Momen paling heboh di sesi ini!",
        "Emosional": "Bagian ini bikin semua orang terdiam.",
        "Hook kuat": "Kalian pasti penasaran sama jawabannya.",
        "Informatif": "Penjelasan singkat yang wajib kalian tahu.",
        "Aksi": "Aksi paling seru di video ini!",
        "Montase": "Cuplikan paling keren, wajib ditonton!",
    }.get(clip.get("category", ""), "Momen terbaik yang sayang dilewatkan!")
    teaser = {
        "podcast": "Obrolan yang bikin mikir, dengerin sampai habis!",
        "interview": "Jawabannya di luar dugaan, simak sampai akhir!",
        "gaming": "Momen paling seru di sesi ini, tonton sampai habis!",
        "komedi": "Dijamin ngakak, tonton sampai akhir!",
        "tutorial": "Tips singkat yang wajib kalian coba!",
        "tutorial_pc": "Trik singkat yang bikin kerja makin cepat!",
        "masak": "Resepnya gampang, hasilnya bikin ngiler!",
        "review": "Sebelum beli, tonton ini dulu!",
        "unboxing": "Isinya bikin kaget, lihat sampai habis!",
        "kendaraan": "Detail yang jarang dibahas, simak sampai akhir!",
        "sport": "Momen paling panas di pertandingan ini!",
        "fitness": "Coba gerakan ini, rasakan bedanya!",
        "berita": "Fakta penting yang perlu kalian tahu.",
        "presentasi": "Poin kunci yang wajib kalian catat.",
        "vlog": "Momen paling seru hari ini!",
    }.get(clip.get("genre", ""), teaser)
    desc = clip.get("description") or (
        f"{hook}\n\n{teaser}\n"
        f"Cuplikan dari \"{(video_title.title() if (video_title or '').isupper() else (video_title or 'video lengkap'))}\" (menit {hms(clip['start'], ':')}).\n\n"
        f"Subscribe & nyalakan notifikasi biar nggak ketinggalan part selanjutnya!\n"
        f"Tulis pendapat kalian di kolom komentar.")
    if tags and not any(t in desc for t in tags):
        desc += "\n\n" + " ".join(tags)
    thumb_text = (clip.get("thumb_text") or thumb_text_from(clip["title"], clip.get("text", ""))).upper()
    tfile, cfile = name + "_thumbnail.jpg", name + "_cover_shorts.jpg"
    try:
        make_thumbnail_16x9(path, info, clip["start"], clip["end"], kind, box, thumb_text, badge,
                            os.path.join(out_dir, tfile), logo=logo)
        make_cover_9x16(os.path.join(out_dir, clip["file"]), clip["duration"], thumb_text, os.path.join(out_dir, cfile))
    except Exception as ex:
        log(f"      thumbnail gagal: {ex}")
    try:
        make_ai_thumbs(opt, path, info, clip, out_dir, tfile, cfile, thumb_text, video_title, log)
    except Cancelled:
        raise
    except Exception as ex:
        log(f"      thumbnail AI gagal: {str(ex)[:160]}")
    plain_tags = [t.lstrip("#") for t in tags] + [w for w in (clean(x) for x in (video_title or "").split())
                                                   if len(w) > 2 and w not in CAMP_STOP and w not in TAG_STOP][:5]
    trans = {}
    if langs:
        base_title = yt_title.replace(" #shorts", "")
        vt = video_title or ""
        trans = translate_kit(base_title, desc, langs, api_key, log, keep=(vt, vt.title(), badge))
        for l, (t_, d_) in list(trans.items()):
            if "#shorts" in yt_title and "#shorts" not in t_ and len(t_) + 8 <= 100:
                t_ += " #shorts"
            if tags and not any(t in d_ for t in tags):
                d_ += "\n\n" + " ".join(tags)
            trans[l] = (t_[:100], d_)
    camp_ck = []
    if campaign:
        cl_ = (campaign.get("language") or "id").split("-")[0].lower()
        yt_title, desc, ctags, ck2, orig_ = campaign_kit(campaign, yt_title, desc, tags, api_key, log, trans.pop(cl_, None))
        plain_tags = ctags + [t for t in plain_tags if t not in ctags]
        need_ = " ".join(_uniq(campaign.get("disclosure", "").split() + campaign.get("hashtags", [])
                               + campaign.get("mentions", [])))
        for l, (t_, d_) in list(trans.items()):
            trans[l] = (t_, d_ + ("\n\n" + need_ if need_ else ""))
        if orig_:
            trans = {"id": (orig_[0], orig_[1]), **trans}
        camp_ck = (campaign_check(campaign, clip, opt) if opt else []) + ck2
        log(f"      judul & deskripsi disesuaikan aturan campaign '{campaign.get('name')}'")
    other = "\n\n".join(f"=== {LANG_NAMES.get(l, 'Bahasa Indonesia' if l == 'id' else l)} ({l}) ===\nJudul: {t_}\n\n{d_}" for l, (t_, d_) in trans.items())
    with open(os.path.join(out_dir, name + "_youtube.txt"), "w", encoding="utf-8") as f:
        f.write(f"=== JUDUL ({len(yt_title)}/100) ===\n{yt_title}\n\n=== DESKRIPSI ===\n{desc}\n\n"
                + (f"=== BAHASA LAIN (YouTube Studio > Subtitle > Tambah bahasa > Judul & deskripsi) ===\n{other}\n\n"
                   if other else "") +
                f"=== TAG ===\n{', '.join(dict.fromkeys(plain_tags))}\n\n=== THUMBNAIL ===\n"
                f"{tfile}  (16:9 - untuk video biasa, 'Upload file')\n{cfile}  (9:16 - cover Shorts)\n\n" +
                (f"=== CAMPAIGN: {campaign.get('name')} ===\n" + "\n".join(camp_ck) + "\n\n" if campaign else "") +
                f"=== PENILAIAN AI ===\nSkor viral: {clip['score']}/100 ({clip.get('category', '')})\n"
                f"Alasan: {clip.get('reason', '')}\n\n=== TRANSKRIP ===\n{clip.get('text', '')}\n")
    log(f"      paket YouTube: judul, deskripsi, tag, thumbnail"
        + (f", terjemahan {'/'.join(trans)}" if trans else "") + f" -> {name}_youtube.txt")
    return dict(yt_title=yt_title, description=desc, thumb_text=thumb_text, thumbnail=tfile, cover=cfile,
                youtube_txt=name + "_youtube.txt", translations=other,
                tags=", ".join(dict.fromkeys(plain_tags)), campaign_check=camp_ck)


# ============================================================================ CAMPAIGN CLIPPER
# Brief campaign (PDF / foto) -> aturan video: durasi, format, fokus momen, logo, teks di video,
# judul & deskripsi wajib (hashtag, mention, link, disclosure), larangan & checklist.

CAMPAIGN_DIR = os.path.join(APP_DIR, "campaign")
CAMPAIGN_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".txt", ".md"}
ASPECT_FMT = {"9:16": "vertical_face", "1:1": "square", "4:5": "square", "16:9": "original"}
CAMP_PLATFORMS = [("tiktok", "TikTok"), ("shorts", "YouTube Shorts"), ("youtube", "YouTube Shorts"),
                  ("reels", "Instagram Reels"), ("instagram", "Instagram Reels"), ("facebook", "Facebook Reels"),
                  ("twitter", "X / Twitter"), ("snapchat", "Snapchat"), ("kwai", "Kwai")]
CAMP_FOCUS_WORDS = {
    "action": "fps action fight fighting shooting shoot kill kills battle combat gunfight gunfights menegangkan "
              "tembak tembakan pertempuran aksi perang clutch explosion crash",
    "lucu": "funny lucu ngakak meme comedy komedi prank lawak kocak hilarious laugh jokes absurd konyol",
    "emosional": "emotional emosional inspiring inspiratif motivasi motivation sedih touching dramatic drama",
    "informatif": "tips tutorial edukasi educational education fakta facts explain insight podcast learn belajar",
}


def campaign_blank(name=""):
    return dict(name=name, brand="", platforms=[], min_sec=0, max_sec=0, aspect="", focus="otomatis",
                content_brief="", hashtags=[], mentions=[], links=[], source_links=[], title_must=[],
                title_style="", desc_must="", overlay_text="", overlay_pos="atas", logo_required=False,
                logo_pos="", logo_file="", watermark_text="", cta="", disclosure="", language="id",
                subtitles=True, no_intro=False, brand_intro=False, banned=[], rules=[], payout="", deadline="",
                files=[], analyzed_by="", topic="", logo_links=[])


_OCR = {}


def ocr_image(fp):
    """Baca teks dari gambar (offline, RapidOCR). Return teks per baris."""
    try:
        from rapidocr_onnxruntime import RapidOCR
        from PIL import Image
    except ImportError:
        return ""
    if "e" not in _OCR:
        _OCR["e"] = RapidOCR()
    im = Image.open(fp).convert("RGB")
    if max(im.size) > 2200:
        im.thumbnail((2200, 2200))
    res, _ = _OCR["e"](np.asarray(im)[:, :, ::-1].copy())
    if not res:
        return ""
    items = []
    for box, txt, conf in res:
        if float(conf) < 0.5:
            continue
        ys = [p[1] for p in box]
        items.append(((min(ys) + max(ys)) / 2, min(p[0] for p in box), max(ys) - min(ys), txt))
    items.sort()
    lines, cur, ly, lh = [], [], None, 0
    for y, x, h, t in items:
        if ly is not None and abs(y - ly) > 0.6 * max(lh, h, 8):
            lines.append(" ".join(w for _, w in sorted(cur)))
            cur = []
        cur.append((x, t))
        ly, lh = y, h
    if cur:
        lines.append(" ".join(w for _, w in sorted(cur)))
    return "\n".join(lines)


BULLET = "•●▪○◦‣∙·-*–✓✔☑✅❌⛔🚫"
SEC_HEAD = re.compile(r"^(jangan|dilarang|larangan|tidak boleh|don'?ts?|do not|boleh|do'?s|anjuran|wajib|"
                      r"yang boleh|yang tidak boleh)\s*:?$", re.I)


def _pdf_sections(pg):
    """Tabel/kolom BOLEH & JANGAN di PDF: kumpulkan butir di bawah tiap judul kolom (berdasarkan posisi)."""
    lines = []
    for b in pg.get_text("dict").get("blocks", []):
        for l in b.get("lines", []):
            t = "".join(sp["text"] for sp in l.get("spans", [])).replace("\u200b", "").strip()
            if t:
                lines.append((l["bbox"][0], l["bbox"][1], t, l["bbox"][2]))
    heads = [h for h in lines if SEC_HEAD.match(h[2])]
    out = []
    for hx, hy, ht, hx1 in heads:
        row = sorted([h for h in heads if abs(h[1] - hy) < 40], key=lambda h: h[0])
        i = row.index((hx, hy, ht, hx1))
        cen = [(h[0] + h[3]) / 2 for h in row]  # judul kolom biasanya di tengah kolom -> batas = titik tengah
        left = 0 if i == 0 else (cen[i - 1] + cen[i]) / 2
        right = (cen[i] + cen[i + 1]) / 2 if i + 1 < len(row) else pg.rect.width
        below = sorted([(y, x, t) for x, y, t, _x1 in lines if y > hy + 2 and left <= x < right], key=lambda z: z[0])
        items, last_y = [], hy
        for y, x, t in below:
            if y - last_y > 45 or SEC_HEAD.match(t) or re.match(r"^[A-Z]\.\s", t):
                break
            last_y = y
            if t[0] in BULLET or re.match(r"^\d{1,2}[.)]", t) or not items:
                items.append(t.lstrip(BULLET + " ").strip())
            else:
                items[-1] += " " + t
        ban = bool(re.match(r"(jangan|dilarang|larangan|tidak boleh|don|do not|yang tidak)", ht, re.I))
        out += [("Jangan: " if ban else "Boleh: ") + it for it in items if len(it.split()) >= 3]
    return out


def campaign_read(files, folder, log=print):
    """Teks (PDF + OCR foto) dan gambar kandidat logo dari file campaign."""
    os.makedirs(folder, exist_ok=True)
    texts, imgs = [], []
    for fp in files:
        ext = os.path.splitext(fp)[1].lower()
        nm = os.path.basename(fp)
        try:
            if ext == ".pdf":
                import fitz
                doc = fitz.open(fp)
                for pi, pg in enumerate(doc):
                    t = pg.get_text("text").strip()
                    if len(t) < 40:  # halaman hasil scan / desain gambar -> OCR
                        png = os.path.join(folder, f"_hal_{len(texts)}.png")
                        pg.get_pixmap(dpi=150).save(png)
                        t = (t + "\n" + ocr_image(png)).strip()
                    extra = []
                    for lk in pg.get_links():  # link tersembunyi (tombol/teks berlink) tidak muncul di teks biasa
                        if lk.get("uri"):
                            lab = re.sub(r"\s+", " ", pg.get_textbox(lk["from"]) or "").strip(" :") or "link"
                            extra.append(f"LINK: {lab} | {lk['uri']}")
                    extra += _pdf_sections(pg)
                    texts.append(f"[{nm} hal. {pi + 1}]\n{t}" + ("\n\n" + "\n".join(extra) if extra else ""))
                    for img in pg.get_images(full=True):
                        try:
                            pm = fitz.Pixmap(doc, img[0])
                            if img[1]:
                                pm = fitz.Pixmap(pm, fitz.Pixmap(doc, img[1]))
                            if pm.n - pm.alpha >= 4:
                                pm = fitz.Pixmap(fitz.csRGB, pm)
                            if 40 <= min(pm.width, pm.height) and max(pm.width, pm.height) <= 4000:
                                out = os.path.join(folder, f"_gambar_{len(imgs) + 1}.png")
                                pm.save(out)
                                imgs.append(out)
                        except Exception:
                            pass
                log(f"      {nm}: {len(doc)} halaman dibaca")
            elif ext in (".txt", ".md"):
                with open(fp, encoding="utf-8", errors="replace") as f:
                    texts.append(f"[{nm}]\n{f.read()}")
            else:
                t = ocr_image(fp)
                texts.append(f"[{nm}]\n{t}")
                imgs.append(fp)
                log(f"      {nm}: teks foto dibaca ({len(t.split())} kata)")
        except Exception as ex:
            log(f"      {nm} gagal dibaca: {str(ex)[:120]}")
    return "\n\n".join(texts), imgs


def pick_logo(cands):
    """Pilih gambar yang paling mirip logo (transparan, kecil, bukan foto). '' bila tidak ada."""
    from PIL import Image
    best, bs = "", 0.0
    for fp in cands:
        try:
            im = Image.open(fp)
            w, h = im.size
            sc, transp = 0.0, False
            if im.mode in ("RGBA", "LA", "P"):
                a = np.asarray(im.convert("RGBA"))[:, :, 3]
                tr = float((a < 20).mean())
                if 0.08 < tr < 0.95:
                    sc += 3
                    transp = True
            if not transp and max(w, h) > 800:  # gambar besar tanpa transparansi = foto/halaman brief, bukan logo
                continue
            if 0.2 <= w / max(1, h) <= 5:
                sc += 0.5
            if max(w, h) <= 1200:
                sc += 0.5
            small = np.asarray(im.convert("RGB").resize((64, 64)))
            ncol = len(np.unique((small // 32).reshape(-1, 3), axis=0))
            if ncol <= 24:
                sc += 1.5  # logo = sedikit warna; foto = banyak warna
            elif ncol > 60:
                sc -= 1.5
            if sc > bs:
                best, bs = fp, sc
        except Exception:
            pass
    return best if bs >= 2 else ""


def _uniq(xs):
    out, seen = [], set()
    for x in xs:
        k = str(x).strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(str(x).strip())
    return out


_UNIT = r"(detik|dtk|secs?|seconds?|s|menit|mnt|mins?|minutes?)\b"


def _sec(num, unit):
    return float(num) * (60 if unit.lower().startswith(("men", "mnt", "min")) else 1)


def _merge_lines(text):
    """Gabungkan kalimat yang terpotong ke beberapa baris (khas PDF)."""
    out = []
    for raw in text.replace("\u200b", "").splitlines():
        t = raw.strip()
        if not t:
            out.append("")
            continue
        new = (t[0] in BULLET or re.match(r"^(\d{1,2}[.)]|[A-Z]\.\s|LINK:|Jangan:|Boleh:|\[)", t)
               or (t.isupper() and len(t) < 40))
        if out and out[-1] and not new and len(out[-1]) > 25 and not out[-1].startswith(("LINK:", "[")) and (
                t[0].islower() or not out[-1].rstrip().endswith((".", ":", "!", "?", '"', "”"))):
            out[-1] = out[-1].rstrip() + " " + t
        else:
            out.append(t)
    return "\n".join(out)


KV_KEYS = {"games": "topic", "game": "topic", "judul game": "topic", "topik": "topic", "topic": "topic",
           "produk": "topic", "product": "topic", "brand": "brand", "klien": "brand", "client": "brand",
           "deadline": "deadline", "batas waktu": "deadline", "periode": "deadline"}


def campaign_parse(text, name=""):
    """Analisa lokal (tanpa internet): cari aturan campaign dari teks brief."""
    p = campaign_blank(name)
    raw_text = text.replace("\u200b", "")
    text = _merge_lines(raw_text)
    low = text.lower()
    # ---- durasi
    lo = hi = 0.0
    m_ = re.search(r"(\d{1,3})\s*(?:-|–|—|to|sampai|s/d|hingga|sd)\s*(\d{1,3})\s*" + _UNIT, low)
    if m_:
        lo, hi = _sec(m_.group(1), m_.group(3)), _sec(m_.group(2), m_.group(3))
    for m_ in re.finditer(r"(?:min(?:imal|imum)?\.?|at least|paling sedikit|minimal|>=?)\s*:?\s*(\d{1,3})\s*" + _UNIT, low):
        lo = max(lo, _sec(m_.group(1), m_.group(2)))
    for m_ in re.finditer(r"(?:max(?:imal|imum)?\.?|maks(?:imal|imum)?\.?|no longer than|under|kurang dari|"
                          r"tidak lebih dari|up to|<=?)\s*:?\s*(\d{1,3})\s*" + _UNIT, low):
        hi = _sec(m_.group(1), m_.group(2)) if not hi else min(hi, _sec(m_.group(1), m_.group(2)))
    p["min_sec"] = int(lo) if 3 <= lo <= 600 else 0
    p["max_sec"] = int(hi) if 5 <= hi <= 900 and hi >= p["min_sec"] else 0
    # ---- format
    m_ = re.search(r"\b(9\s*:\s*16|16\s*:\s*9|1\s*:\s*1|4\s*:\s*5)\b", low)
    if m_:
        p["aspect"] = re.sub(r"\s", "", m_.group(1))
    elif re.search(r"vertical|vertikal|portrait|potret|tegak", low):
        p["aspect"] = "9:16"
    elif re.search(r"landscape|horizontal|mendatar", low):
        p["aspect"] = "16:9"
    elif re.search(r"\bsquare\b|persegi", low):
        p["aspect"] = "1:1"
    low_nourl = re.sub(r"(?:https?://|www\.)\S+", " ", low)
    p["platforms"] = _uniq(v for k, v in CAMP_PLATFORMS if k in low_nourl)
    # ---- hashtag, mention, link
    p["hashtags"] = _uniq("#" + h for h in re.findall(r"(?<![\w&/])#([A-Za-z0-9_À-ɏ]{2,40})", text)
                          if not h.isdigit())
    p["mentions"] = _uniq("@" + h.rstrip(".") for h in re.findall(r"(?<![\w./])@([A-Za-z0-9_.]{2,30})", text))
    urls = _uniq(u.rstrip(".,;:)]}>\"'|") for u in re.findall(r"(?:https?://|www\.)[^\s<>\"'|]+", text))
    logo_l = set()
    for ln_ in text.splitlines():
        if re.search(r"logo|aset|asset|watermark", ln_, re.I):
            logo_l |= {u for u in urls if u in ln_}
    vid = re.compile(r"youtu\.?be|twitch\.tv|kick\.com|drive\.google|dropbox|mega\.nz|wetransfer|vimeo|"
                     r"tiktok\.com/.+/video|instagram\.com/reel/|facebook\.com/.+/videos|fb\.watch", re.I)
    p["logo_links"] = [u for u in urls if u in logo_l]
    p["source_links"] = [u for u in urls if vid.search(u) and u not in logo_l]
    p["links"] = [u for u in urls if not vid.search(u) and u not in logo_l and "instagram.com/p/" not in u]
    disc = [t for t in ("#ad", "#ads", "#sponsored", "#iklan", "#partner", "#promosi", "#paidpartnership")
            if t in low]
    if disc:
        p["disclosure"] = " ".join(disc)
        p["hashtags"] = [h for h in p["hashtags"] if h.lower() not in disc]
    elif re.search(r"paid partnership|ad disclosure|disclose|sponsored|bersponsor|label iklan", low):
        p["disclosure"] = "#ad"
    # ---- per baris
    quote = re.compile(r"[\"“”‘’]([^\"“”‘’\n]{2,80})[\"“”‘’]")
    banned_re = re.compile(r"\b(jangan|dilarang|tidak boleh|do not|don'?t|never|avoid|prohibited|not allowed|"
                           r"forbidden|must not|no)\b", re.I)
    rule_re = re.compile(r"\b(wajib|harus|must|required|require|need to|needs to|should|pastikan|make sure|"
                         r"please|tolong|include|sertakan|gunakan|use|tambahkan|add|boleh|cantumkan|angkat)\b", re.I)
    pos_map = [("kanan atas", "kanan atas"), ("top right", "kanan atas"), ("kiri atas", "kiri atas"),
               ("top left", "kiri atas"), ("kanan bawah", "kanan bawah"), ("bottom right", "kanan bawah"),
               ("kiri bawah", "kiri bawah"), ("bottom left", "kiri bawah")]
    brief, rules, banned, pay, dl = [], [], [], [], []
    for raw in text.splitlines():
        ln = raw.strip(" \t•·-*–>▪●○◦✓✔☑✅❌⛔🚫").strip()
        if len(ln) < 3 or ln.startswith("[") and ln.endswith("]") or ln.startswith("LINK:"):
            continue
        ll = ln.lower()
        q = quote.findall(ln)
        is_ban = (bool(banned_re.search(ll)) and not re.search(r"\bno\.\s*\d", ll)
                  and not (ln.endswith(":") and len(ln) < 25) and len(ln.split()) >= 3 and not ll.startswith("boleh:"))
        if is_ban:
            banned.append(re.sub(r"^jangan:\s*", "", ln, flags=re.I))
            if re.search(r"intro|outro|end ?card", ll):
                p["no_intro"] = True
            continue
        if re.search(r"text overlay|on-?screen text|teks (di|pada) video|tulisan (di|pada) video|teks layar|"
                     r"text on (the )?screen|overlay|caption (di|on) video|teks di layar", ll):
            if q:
                p["overlay_text"] = q[0]
            else:
                m_ = re.search(r"(?:overlay|text|teks|tulisan)[^:]*:\s*(.{3,60})$", ln, re.I)
                if m_:
                    p["overlay_text"] = m_.group(1).strip(" \"'")
            if re.search(r"bawah|bottom", ll):
                p["overlay_pos"] = "bawah"
        if re.search(r"\b(title|judul)\b", ll) and q:
            p["title_must"] += q
        if re.search(r"caption|deskripsi|description|keterangan", ll) and q:
            p["desc_must"] = "\n".join(_uniq(p["desc_must"].splitlines() + q))
        if re.search(r"logo|watermark", ll):
            p["logo_required"] = True
            for k, v in pos_map:
                if k in ll:
                    p["logo_pos"] = v
            if re.search(r"(awal|akhir|start|end|intro|outro|pembuka|penutup)", ll):
                p["brand_intro"] = True
        m_ = re.search(r"\b(?:code|kode|promo)\s*[:\"“]?\s*([A-Z0-9]{3,20})\b", ln)
        if m_ and not p["cta"]:
            p["cta"] = f"Pakai kode {m_.group(1)}"
        if re.search(r"link (in|di) bio|link di profil|download|unduh|daftar|sign ?up|register|join|gabung|"
                     r"kunjungi|visit|follow", ll) and not p["cta"]:
            p["cta"] = q[0] if q else (ln if len(ln) <= 50 else "")
        if re.search(r"\$\s?\d|\bcpm\b|per ?1\.?000|per ?1k|\brp\.?\s?\d|\bidr\b|bayaran|payout|reward|komisi", ll):
            pay.append(ln)
        if re.search(r"deadline|batas waktu|berakhir|ends? on|until|sampai tanggal|periode", ll):
            dl.append(ln)
        if re.search(r"\b(clip|klip|moment|momen|konten|content|highlight|fokus|focus|tema|theme|topik|topic)\b", ll):
            brief.append(ln)
        if rule_re.search(ll) and len(ln.split()) >= 4 and not ln.isupper():
            rules.append(re.sub(r"^boleh:\s*", "", ln, flags=re.I))
        m_ = re.match(r"(?:brand|klien|client|campaign|nama campaign|campaign name|produk|product)\s*[:\-]\s*(.{2,50})$",
                      ln, re.I)
        if m_ and not p["brand"]:
            p["brand"] = m_.group(1).strip()
    # butir dari tabel BOLEH/JANGAN menggantikan potongan kalimat yang sama
    full_ = [b.lower() for b in banned + rules if len(b) > 40]
    rules = [r for r in rules if not any(r.lower() != f and r.lower() in f for f in full_)]
    banned = [b for b in banned if not any(b.lower() != f and b.lower() in f for f in full_)]
    p["banned"], p["rules"] = _uniq(banned)[:15], _uniq(rules)[:20]
    p["payout"], p["deadline"] = " | ".join(_uniq(pay)[:3]), " | ".join(_uniq(dl)[:2])
    p["content_brief"] = " ".join(_uniq(brief)[:4])[:500]
    p["title_must"] = _uniq(p["title_must"])[:4]
    # tabel "Label" lalu "Nilai" di baris berikutnya (mis. Games -> GTA VI, Deadline -> 30 November 2026)
    rl = [l.strip() for l in raw_text.splitlines() if l.strip()]
    kv = {}
    for i, l in enumerate(rl[:-1]):
        k = KV_KEYS.get(l.lower().strip(" :"))
        nx = rl[i + 1]
        if k and k not in kv and len(nx) < 45 and nx.lower().strip(" :") not in KV_KEYS:
            kv[k] = nx.strip()
    flat = re.sub(r"\s+", " ", text)
    m_ = re.search(r"\bcampaign\s+([A-Z0-9][\w .:'-]{1,30}?)\s+[xX×]\s+([A-Z][\w]+(?: [A-Z][\w]+)?)", flat)
    if m_:
        kv.setdefault("topic", m_.group(1).strip())
        kv.setdefault("brand_x", m_.group(2).strip())
    m_ = re.search(r"\blogo\s+(?:dari\s+|of\s+)?([A-Z][\w]+(?: [A-Z][\w]+)?)", flat)
    if m_ and m_.group(1).lower() not in ("watermark", "brand", "campaign", "di", "harus", "wajib"):
        kv.setdefault("brand_logo", m_.group(1).strip())
    p["topic"] = kv.get("topic", "")
    if not p["brand"]:
        p["brand"] = kv.get("brand") or kv.get("brand_logo") or kv.get("brand_x") or ""
    if not p["brand"]:
        first = next((l.strip() for l in text.splitlines() if 2 < len(l.strip()) < 50 and not l.startswith("[")), "")
        p["brand"] = first
    if kv.get("deadline"):
        p["deadline"] = kv["deadline"]
    # CTA dalam tanda kutip (boleh terpotong ke beberapa baris)
    m_ = re.search(r"\bCTA\b[^\"“:]{0,40}:\s*[\"“](.{5,300}?)[\"”]", flat)
    if m_:
        p["cta"] = m_.group(1).strip()
        if re.search(r"cta[^.]{0,100}(caption|deskripsi|description)", flat, re.I):
            p["desc_must"] = "\n".join(_uniq(p["desc_must"].splitlines() + [p["cta"]]))
    if p["topic"]:
        p["content_brief"] = (f"Topik: {p['topic']}. " + p["content_brief"])[:500]
    title_ = next((l.strip() for l in text.splitlines() if 4 < len(l.strip()) < 70 and not l.startswith("[")), "")
    if p["topic"] and (kv.get("brand_x") or p["brand"]):
        p["name"] = f"{p['topic']} x {kv.get('brand_x') or p['brand']}"
    elif title_ and re.search(r"campaign|clipping|clipper|kampanye", title_, re.I):
        p["name"] = title_
    cnt = {k: sum(len(re.findall(rf"\b{re.escape(w)}\b", low)) for w in ws.split()) for k, ws in CAMP_FOCUS_WORDS.items()}
    k_ = max(cnt, key=cnt.get)
    second = sorted(cnt.values())[-2]
    p["focus"] = k_ if cnt[k_] >= 2 and cnt[k_] >= 1.5 * second else "otomatis"  # campuran -> otomatis
    en = sum(low.count(w) for w in (" the ", " and ", " must ", " your ", " you ", " with ", " clips "))
    idn = sum(low.count(w) for w in (" yang ", " dan ", " untuk ", " wajib ", " kamu ", " dengan ", " klip "))
    p["language"] = "en" if en > idn * 1.3 else "id"
    p["analyzed_by"] = "AI lokal (offline)"
    return p


CAMPAIGN_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "name": {"type": "string", "description": "nama campaign singkat"},
    "brand": {"type": "string", "description": "nama brand/klien/kreator pemilik campaign"},
    "platforms": {"type": "array", "items": {"type": "string"}},
    "min_sec": {"type": "integer", "description": "durasi minimal video (detik), 0 bila tidak disebut"},
    "max_sec": {"type": "integer", "description": "durasi maksimal video (detik), 0 bila tidak disebut"},
    "aspect": {"type": "string", "enum": ["", "9:16", "1:1", "4:5", "16:9"]},
    "focus": {"type": "string", "enum": list(FOCUS)},
    "topic": {"type": "string", "description": "topik/game/produk utama yang harus jadi isi klip, mis. GTA VI"},
    "content_brief": {"type": "string", "description": "momen/konten apa yang harus diambil dari video sumber"},
    "hashtags": {"type": "array", "items": {"type": "string"}, "description": "hashtag WAJIB, diawali #"},
    "mentions": {"type": "array", "items": {"type": "string"}, "description": "akun yang wajib di-tag, diawali @"},
    "links": {"type": "array", "items": {"type": "string"}, "description": "link yang wajib ada di deskripsi"},
    "source_links": {"type": "array", "items": {"type": "string"}, "description": "link video sumber untuk di-klip"},
    "title_must": {"type": "array", "items": {"type": "string"}, "description": "kata/frasa wajib di judul"},
    "title_style": {"type": "string", "description": "aturan gaya judul/caption"},
    "desc_must": {"type": "string", "description": "teks persis yang wajib ada di deskripsi/caption"},
    "overlay_text": {"type": "string", "description": "tulisan yang wajib tampil DI VIDEO, '' bila tidak ada"},
    "overlay_pos": {"type": "string", "enum": ["atas", "bawah"]},
    "logo_required": {"type": "boolean"},
    "logo_pos": {"type": "string", "enum": [""] + LOGO_POS},
    "watermark_text": {"type": "string", "description": "teks watermark wajib di video, mis. @akun"},
    "cta": {"type": "string", "description": "ajakan akhir (mis. link di bio, pakai kode X)"},
    "disclosure": {"type": "string", "description": "label iklan wajib, mis. #ad"},
    "language": {"type": "string", "description": "kode bahasa judul/deskripsi yang diminta, mis. id / en"},
    "subtitles": {"type": "boolean"},
    "no_intro": {"type": "boolean", "description": "true bila intro/outro/kartu pembuka dilarang"},
    "brand_intro": {"type": "boolean", "description": "true bila logo/brand wajib di awal atau akhir video"},
    "banned": {"type": "array", "items": {"type": "string"}, "description": "larangan"},
    "rules": {"type": "array", "items": {"type": "string"}, "description": "semua syarat penting lain, ringkas"},
    "payout": {"type": "string"},
    "deadline": {"type": "string"},
}, "required": ["name", "brand", "topic", "platforms", "min_sec", "max_sec", "aspect", "focus", "content_brief", "hashtags",
                "mentions", "links", "source_links", "title_must", "title_style", "desc_must", "overlay_text",
                "overlay_pos", "logo_required", "logo_pos", "watermark_text", "cta", "disclosure", "language",
                "subtitles", "no_intro", "brand_intro", "banned", "rules", "payout", "deadline"]}


def campaign_claude(files, text, api_key, log=print):
    """Analisa brief dengan Claude (membaca PDF & foto langsung, termasuk desain/gambar)."""
    import anthropic
    import base64
    import io
    from PIL import Image
    content = []
    for fp in files:
        ext = os.path.splitext(fp)[1].lower()
        if ext == ".pdf" and os.path.getsize(fp) < 30e6:
            with open(fp, "rb") as f:
                content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                               "data": base64.standard_b64encode(f.read()).decode()}})
        elif ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            im = Image.open(fp).convert("RGB")
            im.thumbnail((1568, 1568))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}})
    txt_extra = "\n\n".join(open(fp, encoding="utf-8", errors="replace").read() for fp in files
                            if fp.lower().endswith((".txt", ".md")))
    content.append({"type": "text", "text": (
        "Ini brief campaign clipping (kreator dibayar untuk membuat klip pendek dari video/stream). "
        "Baca semua halaman & gambar, lalu isi aturan pembuatan videonya. Ambil apa adanya dari brief: "
        "jangan mengarang hashtag/link/akun yang tidak tertulis. 'brand' = pemilik campaign/logo yang wajib "
        "dipasang (bukan kata 'Clippers'), 'topic' = isi konten (mis. nama game). Link tersembunyi di PDF ada di "
        "baris 'LINK:' pada teks hasil baca. Durasi dalam detik. 'focus' = jenis momen yang "
        "diminta (action = tembak-tembakan/aksi game, lucu, emosional, informatif, otomatis bila umum). "
        "'rules' dan 'banned' ditulis singkat dalam Bahasa Indonesia. 'language' = bahasa judul/caption yang "
        "diminta brief (bila tidak disebut: bahasa yang dipakai brief)."
        + (f"\n\nTeks tambahan:\n{txt_extra}" if txt_extra else "")
        + (f"\n\nTeks hasil baca otomatis (bantu bila gambar buram):\n{text[:12000]}" if text.strip() else ""))})
    resp = anthropic.Anthropic(api_key=api_key or None).beta.messages.create(
        model=CLAUDE_MODEL, max_tokens=16000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": CAMPAIGN_SCHEMA}},
        messages=[{"role": "user", "content": content}])
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude menolak menganalisa brief ini")
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


LOGO_SKIP_DOMAINS = re.compile(r"(^|\.)(youtube|youtu|tiktok|instagram|facebook|fb|twitter|x|twitch|kick|discord|"
                               r"google|drive|dropbox|mega|wetransfer|vimeo|linktr|bit|t|wa|whatsapp|telegram|"
                               r"gmail|whop|reddit|snapchat|threads)\.[a-z.]+$", re.I)
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


def _http(url, timeout=15, maxb=8_000_000):
    import urllib.request
    import urllib.error
    ua = _UA if "wiki" not in url else "AutoClip/1.0 (desktop clip editor; logo lookup)"  # aturan API Wikimedia
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Language": "en,id;q=0.8"})
    for att in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r_:
                data = r_.read(maxb + 1)
                ct, final = r_.headers.get("Content-Type", ""), r_.geturl()
            break
        except urllib.error.HTTPError as ex:
            if ex.code not in (429, 500, 502, 503, 504) or att == 2:
                raise
            time.sleep(1.5 * (att + 1))
        except (urllib.error.URLError, TimeoutError):
            if att == 2:
                raise
            time.sleep(1.0)
    if len(data) > maxb:
        raise ValueError("file terlalu besar")
    return data, ct, final


def _img_from_bytes(data, ct="", url=""):
    """Bytes gambar (PNG/JPG/WEBP/ICO/SVG) -> PIL RGBA, atau None."""
    import io
    from PIL import Image
    head = data[:3000].lower()
    if b"<svg" in head or "svg" in ct or url.lower().split("?")[0].endswith(".svg"):
        import fitz
        doc = fitz.open(stream=data, filetype="svg")
        pg = doc[0]
        z = 900 / max(pg.rect.width, pg.rect.height, 1)
        pix = pg.get_pixmap(matrix=fitz.Matrix(z, z), alpha=True)
        im = Image.frombytes("RGBA" if pix.alpha else "RGB", (pix.width, pix.height), pix.samples).convert("RGBA")
    else:
        if b"<html" in head or b"<!doctype" in head:
            return None
        im = Image.open(io.BytesIO(data))
        if getattr(im, "format", "") == "ICO":
            im.size = max(im.info.get("sizes", [im.size]))
        im.load()
        im = im.convert("RGBA")
    return im if min(im.size) >= 32 else None


def clean_logo(im):
    """Latar polos (putih/warna rata) dibuat transparan + tepi kosong dipotong."""
    import cv2
    from PIL import Image
    a = np.asarray(im)
    if float((a[:, :, 3] < 20).mean()) < 0.03:  # belum transparan
        rgb = np.ascontiguousarray(a[:, :, :3])
        h, w = rgb.shape[:2]
        corners = np.array([rgb[0, 0], rgb[0, -1], rgb[-1, 0], rgb[-1, -1]], np.int32)
        if np.abs(corners - corners[0]).max() < 24:  # 4 sudut berwarna sama -> latar polos
            mask = np.zeros((h + 2, w + 2), np.uint8)
            for (y, x) in ((0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)):
                if mask[y + 1, x + 1] == 0:
                    cv2.floodFill(rgb.copy(), mask, (x, y), 0, (14, 14, 14), (14, 14, 14),
                                  4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8))
            bg = mask[1:-1, 1:-1] > 0
            if 0.05 < bg.mean() < 0.9:
                a = a.copy()
                a[:, :, 3] = np.where(bg, 0, a[:, :, 3])
                a[:, :, 3] = cv2.GaussianBlur(a[:, :, 3], (3, 3), 0)
    im = Image.fromarray(a, "RGBA")
    bb = im.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox()
    if bb:
        pad = int(0.04 * max(bb[2] - bb[0], bb[3] - bb[1]))
        im = im.crop((max(0, bb[0] - pad), max(0, bb[1] - pad), min(im.width, bb[2] + pad), min(im.height, bb[3] + pad)))
    if max(im.size) > 1000:
        im.thumbnail((1000, 1000))
    return im


def _logo_score(im):
    a = np.asarray(im)
    tr = float((a[:, :, 3] < 20).mean())
    sc = 2.0 if 0.05 < tr < 0.95 else 0.0
    w, h = im.size
    sc += 0.5 if 0.2 <= w / h <= 6 else -1.5
    small = np.asarray(im.convert("RGB").resize((64, 64)))
    nc = len(np.unique((small // 32).reshape(-1, 3), axis=0))
    sc += 1.5 if nc <= 24 else (-1.5 if nc > 70 else 0)
    sc += -1.0 if min(w, h) < 90 else 0.0
    op = a[:, :, 3] > 128
    if op.any():  # logo hitam/gelap hampir tak terlihat di atas video game yang gelap
        luma = (0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2])[op]
        if float(np.percentile(luma, 90)) < 70:
            sc -= 3.0
    return sc


def campaign_domains(p, text=""):
    """Domain situs brand dari link / teks brief (bukan situs video/sosmed)."""
    src = " ".join(p.get("links", []) + [p.get("desc_must", ""), p.get("cta", ""), text or ""])
    doms = []
    for u in re.findall(r"(?<![@\w.])(?:https?://)?(?:www\.)?((?:[a-z0-9-]+\.)+(?:com|gg|id|co|io|net|org|app|tv|xyz|me|gg|"
                        r"games|store|shop|ai|asia|my|sg|us|uk|de|jp|kr|in|ph|vn|th))\b", src.lower()):
        d = u.strip(".")
        if d and not LOGO_SKIP_DOMAINS.search(d) and d not in doms and not re.match(r"^\d", d):
            doms.append(d)
    return doms[:3]


def logo_search(brand, domains=(), folder=None, log=print, limit=8):
    """Cari logo di internet: situs brand (logo/svg/ikon), Wikipedia & Wikimedia Commons.
    Return daftar dict(path, source, score) terurut dari yang paling cocok."""
    import urllib.parse
    from urllib.parse import urljoin
    folder = folder or os.path.join(APP_DIR, "_cache", "logo_web")
    os.makedirs(folder, exist_ok=True)
    found, seen = [], set()

    def add(url, source, bonus, data=None, ct=""):
        if len(found) >= 24 or url in seen:
            return
        seen.add(url)
        try:
            if data is None:
                data, ct, url_ = _http(url)
            im = _img_from_bytes(data, ct, url)
            if im is None:
                return
            im = clean_logo(im)
            if min(im.size) < 24:
                return
            a_ = np.asarray(im)
            nc_ = len(np.unique((np.asarray(im.convert("RGB").resize((64, 64))) // 32).reshape(-1, 3), axis=0))
            if float((a_[:, :, 3] < 20).mean()) < 0.03 and nc_ > 60:  # foto (gedung, produk, orang) -> bukan logo
                return
            fp = os.path.join(folder, f"web_logo_{len(found) + 1}.png")
            im.save(fp)
            found.append(dict(path=fp, source=source, url=url, score=round(_logo_score(im) + bonus, 2),
                              size=f"{im.width}x{im.height}"))
        except Exception:
            pass
    bl = brand.lower().strip()
    # 0) Wikidata: logo resmi perusahaan/brand (properti P154)
    if bl:
        try:
            log(f"      mencari logo resmi '{brand}' di Wikidata...")
            q = urllib.parse.urlencode(dict(action="wbsearchentities", search=brand, language="en", type="item",
                                            limit=6, format="json"))
            ids = [e["id"] for e in json.loads(_http(f"https://www.wikidata.org/w/api.php?{q}")[0]).get("search", [])]
            if ids:
                q = urllib.parse.urlencode(dict(action="wbgetentities", ids="|".join(ids), props="claims|labels",
                                                languages="en|id", format="json"))
                ents = json.loads(_http(f"https://www.wikidata.org/w/api.php?{q}")[0]).get("entities", {})
                n_ = 0
                for id_ in ids:
                    e_ = ents.get(id_, {})
                    for c_ in e_.get("claims", {}).get("P154", [])[:1]:
                        fn = c_.get("mainsnak", {}).get("datavalue", {}).get("value")
                        lab = next((v["value"] for v in e_.get("labels", {}).values()), id_)
                        if fn and n_ < 3:
                            n_ += 1
                            exact = lab.lower() == bl or lab.lower().startswith(bl + " ")
                            add("https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(fn)
                                + "?width=800", f"Wikidata (logo resmi): {lab}", 3.5 if exact else 1.0)
        except Exception:
            pass
    # 1) situs resmi brand (bila tidak ada di brief: tebak namabrand.com)
    guess = []
    if not domains and bl:
        sl = re.sub(r"[^a-z0-9]", "", bl)
        guess = [f"{sl}.com"] if 2 < len(sl) < 30 else []
    for dom in list(domains) + guess:
        try:
            html, ct, base = _http(f"https://{dom}/", maxb=3_000_000)
            html = html.decode("utf-8", "replace")
        except Exception as ex:
            log(f"      situs {dom} tidak bisa dibuka ({str(ex)[:60]})")
            if dom in guess:
                continue
            html, base = "", f"https://{dom}/"
        if dom in guess and bl.split()[0] not in html[:20000].lower():
            continue  # tebakan situs salah (bukan milik brand ini)
        log(f"      mencari logo di situs {dom}...")
        for m_ in re.finditer(r"<svg\b[^>]*>.*?</svg>", html, re.S | re.I):  # logo SVG langsung di halaman
            tag = m_.group(0)
            if re.search(r"logo|brand", tag[:400], re.I) and len(tag) < 200_000:
                svg = tag if "xmlns" in tag[:300] else tag.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
                add(f"{base}#svg{m_.start()}", f"{dom} (logo SVG di halaman)", 2.5, svg.encode(), "image/svg+xml")
        for m_ in re.finditer(r"<img\b[^>]*>", html, re.I):
            tag = m_.group(0)
            if re.search(r"logo", tag, re.I):
                sm = re.search(r"""\b(?:src|data-src)\s*=\s*["']([^"']+)["']""", tag, re.I)
                if sm and not sm.group(1).startswith("data:"):
                    slug_ = re.sub(r"[^a-z0-9]", "", bl)
                    named = bool(slug_) and slug_ in re.sub(r"[^a-z0-9]", "", tag.lower())
                    add(urljoin(base, sm.group(1)), f"{dom} (gambar logo)", 2.5 + (1.5 if named else 0))
        for m_ in re.finditer(r"<link\b[^>]*>", html, re.I):
            tag = m_.group(0)
            if re.search(r"""rel\s*=\s*["'][^"']*(apple-touch-icon|icon|mask-icon)""", tag, re.I):
                hm = re.search(r"""href\s*=\s*["']([^"']+)["']""", tag, re.I)
                if hm and not hm.group(1).startswith("data:"):
                    add(urljoin(base, hm.group(1)), f"{dom} (ikon situs)", 0.5)
        for m_ in re.finditer(r"""<meta\b[^>]*(?:og:logo|og:image)[^>]*>""", html, re.I):
            cm = re.search(r"""content\s*=\s*["']([^"']+)["']""", m_.group(0), re.I)
            if cm:
                add(urljoin(base, cm.group(1)), f"{dom} (gambar utama situs)", -0.5)
        add(f"https://www.google.com/s2/favicons?domain={dom}&sz=256", f"{dom} (ikon situs)", 0.0)
    # 2) Wikipedia (gambar halaman perusahaan = logo) & Wikimedia Commons
    if brand.strip():
        log(f"      mencari logo '{brand}' di Wikipedia & Wikimedia Commons...")
        for wiki in ("en", "id"):
            try:
                q = urllib.parse.urlencode(dict(action="query", generator="search", gsrsearch=brand, gsrlimit=4,
                                                prop="pageimages", piprop="thumbnail|name", pithumbsize=700,
                                                format="json"))
                j = json.loads(_http(f"https://{wiki}.wikipedia.org/w/api.php?{q}")[0])
                for pg in sorted(j.get("query", {}).get("pages", {}).values(), key=lambda x: x.get("index", 9)):
                    th = (pg.get("thumbnail") or {}).get("source")
                    title = pg.get("title", "")
                    tl = title.lower()
                    if th and (bl in tl or tl in bl):
                        is_logo = "logo" in (pg.get("pageimage") or "").lower()
                        exact = tl == bl or tl.startswith(bl + " (") or tl in (bl + " inc.", bl + " ltd", bl + " corp")
                        add(th, f"Wikipedia: {title}", (1.5 if exact else 0.0) + (1.5 if is_logo else 0))
            except Exception:
                pass
        try:
            q = urllib.parse.urlencode(dict(action="query", generator="search", gsrnamespace=6,
                                            gsrsearch=f"{brand} logo", gsrlimit=6, prop="imageinfo",
                                            iiprop="url", iiurlwidth=700, format="json"))
            j = json.loads(_http(f"https://commons.wikimedia.org/w/api.php?{q}")[0])
            for pg in sorted(j.get("query", {}).get("pages", {}).values(), key=lambda x: x.get("index", 9)):
                title = pg.get("title", "")
                ii = (pg.get("imageinfo") or [{}])[0]
                words = [w for w in re.findall(r"\w+", bl) if len(w) > 2]
                if ii.get("thumburl") and "logo" in title.lower() and all(w in title.lower() for w in words[:2]):
                    add(ii["thumburl"], f"Wikimedia Commons: {title[5:]}", 1.0)
        except Exception:
            pass
    found.sort(key=lambda d: -d["score"])
    log(f"      {len(found)} kandidat logo ditemukan")
    return found[:limit]


def logo_from_links(links, folder, log=print):
    """Logo dari link aset di brief: folder/file Google Drive publik atau link gambar langsung."""
    out = []
    os.makedirs(folder, exist_ok=True)
    for u in links:
        files = []
        try:
            m_ = re.search(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([\w-]+)", u)
            if m_:
                h = _http(f"https://drive.google.com/embeddedfolderview?id={m_.group(1)}")[0].decode("utf-8", "replace")
                ids = re.findall(r'id="entry-([\w-]+)"', h)
                names = re.findall(r'flip-entry-title">([^<]+)<', h)
                files = list(zip(ids, names + [""] * len(ids)))
            else:
                m_ = re.search(r"(?:/file/d/|[?&]id=)([\w-]{20,})", u)
                if m_:
                    files = [(m_.group(1), "")]
            for fid, fname in files[:8]:
                if fname and not re.search(r"\.(png|jpe?g|webp|svg)$", fname, re.I):
                    continue
                data, ct, _ = _http(f"https://drive.google.com/uc?export=download&id={fid}")
                im = _img_from_bytes(data, ct, fname)
                if im is None:
                    continue
                im = clean_logo(im)
                fp = os.path.join(folder, f"brief_logo_{len(out) + 1}.png")
                im.save(fp)
                light = bool(re.search(r"putih|white|light|terang", fname, re.I))
                out.append(dict(path=fp, source=f"Brief: {fname or 'aset logo'}", url=u,
                                score=round(9.0 + (0.5 if light else 0) + 0.1 * _logo_score(im), 2),
                                size=f"{im.width}x{im.height}"))
            if not files and re.search(r"\.(png|jpe?g|webp|svg)(\?|$)", u, re.I):
                data, ct, _ = _http(u)
                im = _img_from_bytes(data, ct, u)
                if im is not None:
                    fp = os.path.join(folder, f"brief_logo_{len(out) + 1}.png")
                    clean_logo(im).save(fp)
                    out.append(dict(path=fp, source="Brief: link logo", url=u, score=9.0, size=""))
        except Exception as ex:
            log(f"      link logo di brief tidak bisa diambil ({str(ex)[:80]})")
    if out:
        log(f"      {len(out)} logo diambil dari link aset di brief")
    return sorted(out, key=lambda d: -d["score"])


def campaign_fetch_logo(p, text="", log=print):
    """Ambil logo campaign dari internet & pasang sebagai logo.png. Return path atau ''."""
    brand = (p.get("brand") or "").strip()
    doms = campaign_domains(p, text)
    res = logo_from_links(p.get("logo_links", []), os.path.join(p.get("_dir") or CAMPAIGN_DIR, "_logo_brief"), log)
    if not res and (brand or doms):
        res = logo_search(brand, doms, os.path.join(p.get("_dir") or CAMPAIGN_DIR, "_logo_web"), log)
    # otomatis hanya bila yakin: aset di brief, situs resmi brand, atau logo resmi Wikidata yang namanya cocok
    good = [r for r in res if r["score"] >= 2.5 and not r["source"].startswith(("Wikimedia", "Wikipedia"))]
    if not good:
        return ""
    dst = os.path.join(p.get("_dir") or CAMPAIGN_DIR, "logo.png")
    shutil.copy2(good[0]["path"], dst)
    log(f"      logo diambil: {good[0]['source']} -> logo.png (cek di Lihat / Edit aturan)")
    return dst


CAMP_STOP = set("""clip clips klip clipping clipper moment moments momen best terbaik from dari our kami partner streamer streamers
stream streams video videos ambil take focus fokus the and or of to a an in on for with yang dan atau di ke untuk dengan
content konten campaign kampanye highlight highlights bonus are is be must wajib harus please tolong only hanya also juga
make buat create bikin post upload short shorts reels tiktok youtube instagram vertical seconds detik our latest terbaru
live siaran main bermain play playing game games gameplay
dari channel creator kreator their his her them they any semua all new baru good bagus more lebih""".split())
FOCUS_QUERY = {"action": ("gameplay seru", "gameplay"), "lucu": ("momen lucu", "funny moments"),
               "emosional": ("momen emosional", "emotional moments"), "informatif": ("podcast", "podcast"),
               "otomatis": ("", "")}


KW_SYN = {"gta": ["grand theft auto", "gtavi", "gta6", "gtav", "gta 6", "gta vi"],
          "tembak": ["tembak", "shoot", "gun", "perang", "war", "fight", "baku tembak", "sniper"],
          "tembakan": ["tembak", "shoot", "gun", "perang", "war", "fight"],
          "lucu": ["lucu", "funny", "ngakak", "kocak", "wkwk", "lawak"],
          "funny": ["funny", "lucu", "ngakak", "laugh"], "horor": ["horor", "horror", "hantu", "seram"],
          "horror": ["horror", "horor", "scary"], "balapan": ["balap", "race", "racing", "drift"]}


STYLE_KW = {"tembak", "tembakan", "lucu", "funny", "horor", "horror", "balapan", "seru", "keren", "aksi", "action",
            "ngakak", "kocak", "momen", "fight"}


def kw_hits(title, kws):
    """Skor kecocokan judul: kata spesifik (nama game/topik) bobot 2, kata gaya (lucu, tembak) bobot 1,
    sinonim dihitung sekali."""
    tl = title.lower()
    sc, used = 0, set()
    for k in kws:
        syn = tuple(KW_SYN.get(k, [k]))
        grp = frozenset(syn) if k in KW_SYN else frozenset([k])
        if grp in used:
            continue
        if any(x in tl for x in syn):
            used.add(grp)
            sc += 1 if k in STYLE_KW else 2
    return sc


def _downloaded_ids():
    ids = set()
    for d_ in (os.path.join(APP_DIR, "unduhan"),):
        if os.path.isdir(d_):
            for f in os.listdir(d_):
                m_ = re.search(r"\[([\w-]{11})\]", f)
                if m_:
                    ids.add(m_.group(1))
    return ids


def _yt_id(url):
    m_ = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/)([\w-]{11})", url)
    return m_.group(1) if m_ else ""


class _QuietLog:
    def debug(self, m):
        pass
    info = warning = error = debug


def channel_videos(url, n=15, log=print):
    """Video/siaran terbaru dari link channel (YouTube @channel, Twitch, Kick, dll) lewat yt-dlp."""
    import yt_dlp
    u = url.strip().rstrip("/")
    tabs = [u]
    if re.search(r"youtube\.com/(@|channel/|c/|user/)", u) and not re.search(r"/(videos|streams|shorts|live)$", u):
        tabs = [u + "/streams", u + "/videos"]
    out = []
    for t in tabs:
        try:
            with yt_dlp.YoutubeDL(dict(quiet=True, no_warnings=True, skip_download=True, extract_flat="in_playlist",
                                       playlistend=n, ignoreerrors=True, logger=_QuietLog())) as y:
                info = y.extract_info(t, download=False) or {}
        except Exception as ex:
            log(f"      channel {t} tidak bisa dibaca: {str(ex)[:80]}")
            continue
        ch = info.get("channel") or info.get("uploader") or info.get("title") or ""
        for e in (info.get("entries") or [])[:n]:
            if not e:
                continue
            vu = e.get("url") or e.get("webpage_url") or ""
            if vu and not vu.startswith("http"):
                vu = f"https://www.youtube.com/watch?v={vu}"
            if not vu or e.get("live_status") in ("is_live", "is_upcoming"):
                continue
            out.append(dict(url=vu, title=e.get("title") or "", channel=e.get("channel") or ch,
                            views=int(e.get("view_count") or 0), sec=int(e.get("duration") or 0),
                            published="", source="channel dari brief"))
    return out


def campaign_keywords(p):
    words = re.findall(r"[A-Za-z0-9À-ɏ]+", (p.get("content_brief") or ""))
    kw = []
    for w in words:
        wl = w.lower()
        if len(wl) > 2 and wl not in CAMP_STOP and wl not in kw and not wl.isdigit():
            kw.append(wl)
    return kw[:6]


def campaign_find_videos(p, n=12, period="Bulan ini", log=print):
    """Cari video sumber yang cocok untuk campaign: link di brief, video terbaru channel di brief,
    lalu pencarian YouTube dari brand + isi brief + jenis momen. Return daftar video (terurut)."""
    import base64
    import math
    res, seen = [], set()
    have = _downloaded_ids()
    min_sec = max(240, int((p.get("max_sec") or 60) * 4))  # video sumber harus jauh lebih panjang dari klip

    def push(v, bonus=0.0):
        key = _yt_id(v["url"]) or v["url"]
        if key in seen:
            return
        seen.add(key)
        v["done"] = bool(_yt_id(v["url"]) and _yt_id(v["url"]) in have)
        v["rank"] = bonus + 1.3 * math.log10(max(10, v.get("views") or 10))
        res.append(v)
    brand = (p.get("brand") or "").strip()
    bw = {w.lower() for w in re.findall(r"\w+", brand) if len(w) > 2}
    kw = [k for k in campaign_keywords(p) if k not in bw]
    terms = set(kw) | bw
    chans, chans_brief = set(), set()
    # 1) link video langsung & channel dari brief (video yang judulnya cocok dengan brief paling atas)
    tkw = campaign_keywords({"content_brief": p.get("topic", "")})
    if tkw:
        kw = tkw + [k for k in kw if k not in tkw]
    for u in p.get("source_links", []):
        if re.search(r"watch\?v=|youtu\.be/|/live/[\w-]{11}|/videos/\d+|/video/\d+|fb\.watch|/reel/", u):
            v = dict(url=u, title="(link video dari brief)", channel="", views=0, sec=0, published="",
                     source="link di brief")
            try:
                import yt_dlp
                with yt_dlp.YoutubeDL(dict(quiet=True, no_warnings=True, skip_download=True,
                                           logger=_QuietLog())) as y:
                    inf = y.extract_info(u, download=False, process=False) or {}
                v.update(title=inf.get("title") or v["title"], channel=inf.get("channel") or inf.get("uploader") or "",
                         views=int(inf.get("view_count") or 0), sec=int(inf.get("duration") or 0))
                if v["channel"]:
                    chans_brief.add(v["channel"].lower())
            except Exception:
                pass
            push(v, 100)
        else:
            log(f"      membaca video terbaru dari channel di brief: {u}")
            for v in channel_videos(u, 30, log):
                chans.add(v["channel"].lower())
                if not v["sec"] or v["sec"] >= min_sec:
                    push(v, 50 + 6 * kw_hits(v["title"], kw))
    # 2) pencarian YouTube sesuai isi campaign
    lang = (p.get("language") or "id").lower()
    gl, hl = ("ID", "id") if lang == "id" else ("US", "en")
    fq = FOCUS_QUERY.get(p.get("focus") or "otomatis", ("", ""))[0 if lang == "id" else 1]
    queries = []
    pre = next((v["channel"] for v in res if v.get("channel")), "") if chans else ""
    topic = (p.get("topic") or "").strip()
    if pre:
        cands_ = [f"{pre} {' '.join(kw[:2])}", f"{pre} {' '.join(kw[2:4])}", f"{pre} {kw[0] if kw else fq}"]
    elif topic:  # campaign bertopik (mis. game): cari momen topik itu, termasuk dari streamer luar negeri
        cands_ = [f"{topic} {fq}", f"{topic} streamer", f"{topic} funny moments", f"{topic} gameplay"]
        cands_ += [f"{c_} {topic}" for c_ in sorted(chans_brief)][:2]
    else:
        cands_ = [" ".join(kw[:3] + [fq]), f"{brand} {' '.join(kw[:2])}", " ".join(kw[:4]), f"{brand} {fq}"]
    for q in cands_:
        q = re.sub(r"\s+", " ", q).strip()
        if len(q) > 3 and q.lower() not in [x.lower() for x in queries]:
            queries.append(q)
    sp = base64.b64encode(bytes([0x08, 0x03, 0x12, 0x04, 0x08, VIRAL_PERIOD.get(period, 4), 0x10, 0x01])).decode()
    banned = " ".join(p.get("banned", [])).lower()
    for qi, q in enumerate(queries[:6]):
        log(f"      mencari di YouTube: \"{q}\"")
        gl_, hl_ = (("US", "en") if topic and qi % 2 else (gl, hl))  # campuran streamer lokal & luar negeri
        for sp_ in (sp, ""):  # populer periode ini, lalu relevansi
            try:
                found = _yt_search(q, gl_, hl_, sp_)
            except Exception as ex:
                log(f"      pencarian gagal: {str(ex)[:80]}")
                found = []
            for v in found:
                if v["sec"] < min_sec or (chans and v["channel"].lower() not in chans):
                    continue  # brief menyebut channel -> hanya video channel itu (bukan reupload orang lain)
                tl = v["title"].lower()
                hit = kw_hits(v["title"], kw) + sum(1 for t in bw if t in tl)
                if tkw and not kw_hits(v["title"], tkw):
                    continue  # judul harus tentang topik campaign (mis. GTA VI)
                if not hit or any(w in tl for w in re.findall(r"\b(?:no|jangan|dilarang)\s+(\w+)", banned)):
                    continue
                if v["views"] < 2000:
                    continue  # terlalu sepi penonton -> kurang menarik untuk di-klip
                v["source"] = f"YouTube: {q}"
                push(v, 1.5 * min(hit, 3))
            if sum(1 for r_ in res if r_["source"].startswith("YouTube")) >= n:
                break
    res.sort(key=lambda v: (v["done"], -v["rank"]))
    log(f"      {min(n, len(res))} video sumber cocok ditemukan")
    return res[:n]


def campaign_list():
    if not os.path.isdir(CAMPAIGN_DIR):
        return []
    return sorted(d for d in os.listdir(CAMPAIGN_DIR) if os.path.exists(os.path.join(CAMPAIGN_DIR, d, "campaign.json")))


def campaign_load(ref):
    """ref: nama campaign (folder di campaign/) atau path campaign.json. None bila tidak ada."""
    if not ref or ref in ("(tanpa campaign)", "mati"):
        return None
    fp = (ref if ref.lower().endswith(".json") else os.path.join(ref, "campaign.json") if os.path.isdir(ref)
          else os.path.join(CAMPAIGN_DIR, ref, "campaign.json"))
    if not os.path.exists(fp):
        return None
    p = campaign_blank()
    p.update(_json_load(fp))
    p["_dir"] = os.path.dirname(os.path.abspath(fp))
    lf = p.get("logo_file") or ""
    if lf and not os.path.exists(lf):  # folder dipindah (mis. ke Google Colab) -> cari logo di folder campaign
        alt = os.path.join(p["_dir"], re.split(r"[\\/]", lf)[-1])
        if os.path.exists(alt):
            p["logo_file"] = alt
    return p


def campaign_save(p):
    d_ = p.get("_dir") or os.path.join(CAMPAIGN_DIR, safe_name(p.get("name") or "campaign")[:50])
    os.makedirs(d_, exist_ok=True)
    with open(os.path.join(d_, "campaign.json"), "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in p.items() if not k.startswith("_")}, f, ensure_ascii=False, indent=1)
    return os.path.basename(d_)


def campaign_analyze(files, api_key="", log=print, name=""):
    """Baca + analisa brief campaign, simpan ke campaign/<nama>/. Return (nama_folder, profil)."""
    files = [os.path.abspath(f) for f in files if os.path.splitext(f)[1].lower() in CAMPAIGN_EXT and os.path.exists(f)]
    if not files:
        raise ValueError("pilih file PDF / foto campaign")
    name = name or os.path.splitext(os.path.basename(files[0]))[0]
    d_ = os.path.join(CAMPAIGN_DIR, safe_name(name)[:50])
    os.makedirs(d_, exist_ok=True)
    log(f"Membaca brief campaign ({len(files)} file)...")
    text, imgs = campaign_read(files, d_, log)
    p = campaign_parse(text, name)
    if api_key or os.environ.get("ANTHROPIC_API_KEY"):
        try:
            log("      Claude AI membaca brief (PDF & gambar)...")
            c = campaign_claude(files, text, api_key, log)
            for k, v in c.items():
                if v not in ("", [], 0, None) or k in ("subtitles", "logo_required", "no_intro", "brand_intro"):
                    p[k] = v
            p["analyzed_by"] = "Claude AI"
        except Exception as ex:
            log(f"      analisa Claude gagal ({str(ex)[:140]}) -> pakai analisa lokal")
    p["hashtags"] = _uniq(("#" + h.lstrip("#")) for h in p["hashtags"])
    p["mentions"] = _uniq(("@" + h.lstrip("@")) for h in p["mentions"])
    p["name"] = p.get("name") or name
    logo = pick_logo(imgs)
    if logo:
        dst = os.path.join(d_, "logo.png")
        from PIL import Image
        Image.open(logo).convert("RGBA").save(dst)
        p["logo_file"] = dst
        p["logo_required"] = True if p["logo_required"] or logo.lower().endswith(".png") else p["logo_required"]
        log("      logo campaign ditemukan -> logo.png")
    if not p.get("logo_file") and (p.get("logo_required") or p.get("brand_intro")):
        p["_dir"] = d_
        try:
            lf_ = campaign_fetch_logo(p, text, log)
            if lf_:
                p["logo_file"], p["logo_required"] = lf_, True
            else:
                log("      logo wajib tapi belum ketemu otomatis -> Lihat / Edit aturan > 🌐 Cari di internet")
        except Exception as ex:
            log(f"      pencarian logo di internet gagal: {str(ex)[:120]}")
    import shutil as _sh
    kept = []
    for fp in files:
        dst = os.path.join(d_, os.path.basename(fp))
        if os.path.abspath(fp) != os.path.abspath(dst):
            _sh.copy2(fp, dst)
        kept.append(os.path.basename(fp))
    p["files"] = kept
    with open(os.path.join(d_, "brief_teks.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    p["_dir"] = d_
    try:
        log("Mencari video sumber yang sesuai campaign...")
        p["video_suggestions"] = campaign_find_videos(p, 12, log=log)
    except Exception as ex:
        log(f"      pencarian video sumber gagal: {str(ex)[:120]}")
    nm = campaign_save(p)
    log("      " + campaign_summary(p))
    return nm, p


def campaign_summary(p):
    if not p:
        return "Tanpa campaign"
    dur = (f"{p['min_sec']}-{p['max_sec']} dtk" if p.get("min_sec") and p.get("max_sec") else
           f"min {p['min_sec']} dtk" if p.get("min_sec") else f"maks {p['max_sec']} dtk" if p.get("max_sec") else "")
    bits = [p.get("brand") or p.get("name"), dur, p.get("aspect"), FOCUS.get(p.get("focus"), ("",))[0].split(" /")[0]
            if p.get("focus") not in ("", "otomatis") else "",
            " ".join(p.get("hashtags", [])[:4]), " ".join(p.get("mentions", [])[:2]),
            "logo ✓" if p.get("logo_file") else ("logo wajib (pilih file!)" if p.get("logo_required") else ""),
            f"teks: \"{p['overlay_text']}\"" if p.get("overlay_text") else ""]
    return " · ".join(b for b in bits if b)


def campaign_apply(opt, p, log=print):
    """Ubah pengaturan video sesuai aturan campaign."""
    u = {}
    if p.get("no_intro"):
        u.update(intro=False, outro=False, brand="mati")
    if p.get("brand_intro") and not p.get("no_intro"):
        u.update(brand="awal & akhir")
    if p.get("logo_file") and os.path.exists(p["logo_file"]):
        u["logo"] = p["logo_file"]
        if p.get("brand_intro"):
            u["brand_logo"] = p["logo_file"]
    if p.get("logo_pos"):
        u["logo_pos"] = p["logo_pos"]
    if p.get("watermark_text"):
        u["handle"] = p["watermark_text"]
    if p.get("brand_intro"):
        u["brand_text"] = p.get("watermark_text") or p.get("brand") or ""
    if p.get("cta"):
        u["cta"] = p["cta"]
    if p.get("overlay_text"):
        u["campaign_text"] = p["overlay_text"]
        u["campaign_pos"] = p.get("overlay_pos") or "atas"
    if p.get("subtitles"):
        u["subtitles"] = True
    if p.get("focus") and p["focus"] != "otomatis" and p["focus"] in FOCUS:
        u["focus"] = p["focus"]
    a = p.get("aspect")
    if a in ASPECT_FMT and not (a == "9:16" and opt.fmt.startswith("vertical")):
        u["fmt"] = ASPECT_FMT[a]
    # durasi: klip + pembuka/penutup harus masuk rentang campaign
    intro_on = u.get("intro", opt.intro)
    brand_on = u.get("brand", opt.brand) != "mati"
    extra = (5.0 if intro_on else 0.0) + (3.0 if brand_on else 0.0)
    d = opt.duration
    lo, hi = float(p.get("min_sec") or 0), float(p.get("max_sec") or 0)
    if hi:
        d = min(d, max(5.0, hi * 0.82 - extra))
    if lo:
        d = max(d, lo * 1.12 - extra)
    if hi and lo and d + extra > hi:
        d = max(5.0, (lo + hi) / 2 - extra)
    if abs(d - opt.duration) > 0.5:
        u["duration"] = round(d, 1)
    u["campaign"] = p.get("_dir", "")
    log(f"CAMPAIGN: {campaign_summary(p)}")
    if u.get("duration"):
        log(f"      durasi klip disesuaikan: {u['duration']:.0f} dtk (+ pembuka/penutup) agar masuk aturan campaign")
    if p.get("logo_required") and not u.get("logo") and not opt.logo:
        log("      PERHATIAN: campaign mewajibkan logo, tapi file logo belum dipilih (Campaign > Edit > Logo)")
    return Options(**{**asdict(opt), **u})


def campaign_kit(p, yt_title, desc, tags, api_key="", log=print, pre=None):
    """Judul & deskripsi sesuai aturan campaign. Return (judul, deskripsi, tag, checklist, versi_indonesia)."""
    base = yt_title.replace(" #shorts", "").strip()
    lang = (p.get("language") or "id").split("-")[0].lower()
    orig = None
    if lang != "id":
        tr = {lang: pre} if pre else translate_kit(base, desc, [lang], api_key, log,
                                                   keep=[p.get("brand", "")] + p.get("title_must", []))
        if lang in tr:
            orig = (base, desc)
            base, desc = tr[lang]
            base = base.replace(" #shorts", "").strip()
    miss = [k for k in p.get("title_must", []) if k.lower() not in base.lower()]
    if miss:
        base = f"{' '.join(miss)} | {base}"
    yt = p.get("platforms") or []
    short = not yt or any("YouTube" in x for x in yt)
    lim = 100 - (8 if short else 0)
    title = base[:lim].rstrip(" |-") + (" #shorts" if short else "")
    # deskripsi: buang baris hashtag lama, susun ulang
    body = "\n".join(l for l in desc.splitlines() if not (l.split() and all(w.startswith("#") for w in l.split())))
    parts = [body.strip()]
    if p.get("desc_must"):
        parts.append(p["desc_must"].strip())
    if p.get("cta") and p["cta"].lower() not in (body + " " + p.get("desc_must", "")).lower():
        parts.append(p["cta"])
    if p.get("links"):
        parts.append("\n".join(p["links"]))
    if p.get("mentions"):
        parts.append(" ".join(p["mentions"]))
    req = _uniq((p.get("disclosure", "").split()) + p.get("hashtags", []))
    extra_tags = [t for t in tags if t.lower() not in {r.lower() for r in req}][:max(0, 8 - len(req))]
    all_tags = _uniq(req + extra_tags)
    parts.append(" ".join(all_tags))
    d_ = "\n\n".join(x for x in parts if x)
    full = (title + "\n" + d_).lower()
    ck = [("✓" if all(h.lower() in full for h in p.get("hashtags", [])) else "✗") + " hashtag wajib: "
          + (" ".join(p.get("hashtags", [])) or "-")]
    if p.get("mentions"):
        ck.append(("✓" if all(m.lower() in full for m in p["mentions"]) else "✗") + " tag akun: " + " ".join(p["mentions"]))
    if p.get("links"):
        ck.append("✓ link di deskripsi: " + ", ".join(p["links"]))
    if p.get("disclosure"):
        ck.append("✓ label iklan: " + p["disclosure"])
    if lang != "id":
        ck.append(f"✓ judul & deskripsi dalam bahasa {LANG_NAMES.get(lang, lang)}" if orig else
                  f"✗ terjemahan ke bahasa {LANG_NAMES.get(lang, lang)} gagal - judul/deskripsi masih Bahasa Indonesia")
    if p.get("title_must"):
        ck.append(("✓" if all(k.lower() in title.lower() for k in p["title_must"]) else "✗")
                  + " kata wajib di judul: " + ", ".join(p["title_must"]))
    return title, d_, [t.lstrip("#") for t in all_tags], ck, orig


def campaign_check(p, clip, opt):
    """Checklist aturan video (otomatis) + pengingat manual."""
    ck = []
    D = clip.get("duration", 0)
    lo, hi = p.get("min_sec") or 0, p.get("max_sec") or 0
    if lo or hi:
        ok = (not lo or D >= lo) and (not hi or D <= hi)
        ck.append(f"{'✓' if ok else '✗'} durasi {D:.0f} dtk (aturan: {lo or '-'}-{hi or '-'} dtk)")
    if p.get("aspect"):
        got = {"vertical_face": "9:16", "vertical_blur": "9:16", "square": "1:1"}.get(opt.fmt, "asli")
        ck.append(f"{'✓' if got == p['aspect'] or (p['aspect'] == '4:5' and got == '1:1') else '✗'} format {got} "
                  f"(aturan: {p['aspect']})")
    if p.get("logo_required"):
        ck.append(("✓ logo campaign terpasang" if opt.logo and os.path.exists(opt.logo)
                   else "✗ logo WAJIB tapi belum dipilih"))
    if p.get("overlay_text"):
        ck.append(f"✓ tulisan di video: \"{p['overlay_text']}\"")
    if p.get("watermark_text"):
        ck.append(f"✓ watermark: {p['watermark_text']}")
    if p.get("subtitles"):
        ck.append(("✓" if opt.subtitles else "✗") + " subtitle")
    for b in p.get("banned", []):
        if p.get("no_intro") and re.search(r"intro|outro|end ?card|pembuka|penutup", b, re.I):
            ck.append(f"{'✓' if not (opt.intro or opt.outro) and opt.brand == 'mati' else '✗'} tanpa intro/outro ({b})")
        else:
            ck.append(f"☐ cek manual - larangan: {b}")
    auto_ = re.compile(r"#|@|hashtag|\btag\b|title|judul|caption|deskripsi|description|logo|watermark|subtitle|"
                       r"disclos|overlay|tulisan|text on|teks|vertical|vertikal|9:16|durasi|length|detik|seconds|"
                       r"format|aspect", re.I)
    for r in [r for r in p.get("rules", []) if not auto_.search(r)][:10]:
        ck.append(f"☐ cek manual - syarat: {r}")
    if p.get("platforms"):
        ck.append("☐ upload ke: " + ", ".join(p["platforms"]))
    if p.get("deadline"):
        ck.append("☐ batas waktu: " + p["deadline"])
    if p.get("payout"):
        ck.append("ℹ bayaran: " + p["payout"])
    return ck


def campaign_text_overlay(g, inputs, opt, W, H, D, cur):
    """Tulisan wajib campaign tampil di video sepanjang klip."""
    txt = (opt.campaign_text or "").strip()
    if not txt:
        return cur
    import hashlib
    tmp = os.path.join(APP_DIR, "_cache", "campaign_txt")
    png, pw, ph = hook_png(txt, W, tmp, hashlib.sha1(f"{txt}|{W}".encode()).hexdigest()[:10])
    idx = inputs.count("-i")
    inputs += ["-loop", "1", "-framerate", str(OUT_FPS), "-t", f"{D:.3f}", "-i", png]
    y = int(H * 0.13) if opt.campaign_pos != "bawah" else int(H * 0.93) - ph
    a = 3.7 if (opt.top_text and opt.campaign_pos != "bawah" and D > 8) else 0.0
    g.append(f"[{idx}:v]format=rgba[cmp]")
    g.append(f"{cur}[cmp]overlay=x=(W-w)/2:y={y}:enable='gte(t,{a:.2f})':eof_action=pass[cmpo]")
    return "[cmpo]"


# ============================================================================ render: gaya MONTASE

def shot_candidates(cuts, dur, fs):
    diff, bright, contrast = fs["diff"], fs["bright"], fs["contrast"]
    k = len(diff)
    bounds = [0.0] + list(cuts) + [dur]
    raw = []
    for sid, (a, b) in enumerate(zip(bounds[:-1], bounds[1:])):
        a2, b2 = a + 0.3, b - 0.15
        if b2 - a2 < 0.6:
            continue
        nsub = max(1, int((b2 - a2) / 2.4))
        step = (b2 - a2) / nsub
        for j in range(nsub):
            s, e = a2 + j * step, a2 + (j + 1) * step
            i0, i1 = int(s / FINE), max(int(s / FINE) + 1, int(e / FINE))
            if i0 >= k:
                continue
            br = float(bright[i0:i1].mean())
            if br < 22 or br > 240:
                continue
            raw.append(dict(s=s, e=e, shot=sid, mot=float(diff[i0:i1].mean()), ct=float(contrast[i0:i1].mean()), br=br))
    if not raw:
        return []
    mot = norm(np.array([c["mot"] for c in raw]))
    ct = norm(np.array([c["ct"] for c in raw]))
    for c, m, t in zip(raw, mot, ct):
        c["score"] = 0.6 * m + 0.3 * t + 0.1 * (1 - abs(c["br"] - 120) / 120)
    return raw


def best_subwindow(c, need, diff):
    if c["e"] - c["s"] <= need:
        return c["s"]
    best, bs = c["s"], -1
    for st in np.arange(c["s"], c["e"] - need, FINE):
        i0, i1 = int(st / FINE), int((st + need) / FINE) + 1
        v = diff[i0:i1].mean() if i1 <= len(diff) else 0
        if v > bs:
            best, bs = st, v
    return float(best)


def plan_montage(beats, P, strength, down, D_target):
    j0 = next((j for j in range(len(beats)) if (j - down) % 4 == 0 and beats[j] >= 0.3), None)
    if j0 is None:
        return None
    b = beats[j0:] - beats[j0]
    st = norm(smooth(strength[j0:], 4)) if len(b) > 4 else np.ones(len(b))
    nb_total = int(np.searchsorted(b, D_target + 0.01)) - 1
    nb_total -= nb_total % 4
    if nb_total < 8:
        return None
    slots, i = [], 0
    while i < nb_total:
        target = 1.8 if i == 0 else 1.4 - 0.7 * float(st[i])
        nb = max(int(np.ceil(0.55 / P)), int(round(target / P)))
        nb = min(nb, nb_total - i)
        slots.append((i, i + nb))
        i += nb
    return dict(start_rel=float(beats[j0]), beats=b[:nb_total + 1], strength=st[:nb_total + 1],
                slots=slots, D=float(b[nb_total]))


def render_montage(path, info, w0, plan, P, cands, used, fs, opt, out_dir, name, title, log, progress):
    tmp = os.path.join(out_dir, "_bahan", name)
    os.makedirs(tmp, exist_ok=True)
    b, slots, D = plan["beats"], plan["slots"], plan["D"]
    A0 = w0 + plan["start_rel"]
    diff = fs["diff"]
    pool = sorted(cands, key=lambda c: -(c["score"] - 0.35 * used.get(c["shot"], 0)))
    picked, per_shot = [], {}
    for c in pool:
        if per_shot.get(c["shot"], 0) >= (1 if len(pool) > 2 * len(slots) else 2):
            continue
        picked.append(c)
        per_shot[c["shot"]] = per_shot.get(c["shot"], 0) + 1
        if len(picked) >= len(slots):
            break
    while len(picked) < len(slots):
        picked.append(pool[len(picked) % len(pool)])
    picked.sort(key=lambda c: c["s"])
    for c in picked:
        used[c["shot"]] = used.get(c["shot"], 0) + 1
    kind = {"square": "square", "original": "original"}.get(opt.fmt, "cinematic")
    lst, slowmo = [], 0
    for k, (i, j) in enumerate(slots):
        check_cancel()
        L = float(b[j] - b[i])
        pos = next((x for x in range(k, min(k + 4, len(picked))) if picked[x]["e"] - picked[x]["s"] >= L), k)
        picked[k], picked[pos] = picked[pos], picked[k]
        c = picked[k]
        avail = c["e"] - c["s"]
        speed = 1.0
        if avail < L:
            speed = min(2.0, L / avail)
        elif k % 8 == 5 and plan["strength"][i] < 0.5 and avail >= L * 0.6 and L > 0.9:
            speed = 1.6
        src_len = L / speed
        slowmo += speed > 1.01
        st = best_subwindow(c, src_len, diff)
        frames = max(1, int(round(b[j] * OUT_FPS)) - int(round(b[i] * OUT_FPS)))
        lg, (W, H) = layout_graph(kind, info, "[pv]", "[v]")
        piece = f"p{k:03d}.mp4"
        run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{st:.3f}", "-t", f"{src_len + 0.6:.3f}", *info["dec"],
             "-i", os.path.abspath(path), "-filter_complex", f"[0:v]setpts=(PTS-STARTPTS)*{speed:.4f},fps={OUT_FPS}[pv];{lg}",
             "-map", "[v]", "-frames:v", str(frames), "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
             "-pix_fmt", "yuv420p", piece], cwd=tmp)
        lst.append(f"file '{piece}'")
        progress(0.8 * (k + 1) / len(slots))
    with open(os.path.join(tmp, "list.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lst) + "\n")
    W, H = (1080, 1920) if kind == "cinematic" else ((1080, 1080) if kind == "square" else (info["width"] // 2 * 2, info["height"] // 2 * 2))
    cut_times = [float(b[i]) for i, _ in slots]
    events, intro_end = [], 0.0
    chunks = kinetic_chunks(title) if (opt.creative and title) else []
    colors = ["&H00FFFFFF&", accent_color(opt) + "&", "&H00FFF04D&", "&H00FFFFFF&", accent_color(opt) + "&"]
    for k, ch in enumerate(chunks):
        a, z = k * 2 * P, (k + 1) * 2 * P
        if z > D * 0.35:
            break
        rz = random.choice([-6, -4, 4, 6])
        events.append(ev(5, a, z, "Kin", f"{{\\pos({W // 2},{int(H * 0.42)})\\c{colors[k % 5]}\\frz{rz}"
                         f"\\fscx30\\fscy30\\t(0,90,\\fscx125\\fscy125)\\t(90,170,\\fscx100\\fscy100)}}" + wrap2(esc(ch), 12)))
        intro_end = z
    events += common_events(W, H, D, title, opt, title_from=intro_end if chunks else 0.05)
    stickers, pops = [], []
    if opt.creative:
        for i in np.argsort(-plan["strength"]):
            t = float(b[i])
            if t < intro_end + 0.5 or t > D - 3.2 or any(abs(t - x) < 4 for x, _ in stickers):
                continue
            stickers.append((t, random.choice(["🔥", "💥", "⚡", "😎", "🤯"])))
            pops.append((t, random.choice(HYPE)))
            if len(stickers) >= max(2, int(D / 10)):
                break
        stickers.sort()
        events += [pop_event(t, w, W, H, accent_color(opt)) for t, w in pops]
    write_ass(os.path.join(out_dir, name + ".ass"), W, H, events)
    inputs = ["-f", "concat", "-safe", "0", "-i", os.path.join(tmp, "list.txt")]
    if info["has_audio"]:
        inputs += ["-ss", f"{A0:.3f}", "-t", f"{D:.3f}", "-i", os.path.abspath(path)]
    g, cur = [], "[0:v]"
    if opt.creative:
        g.append(f"{cur}zoompan=z='1+0.045*exp(-9*mod(it,{P:.4f}))':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2'"
                 f":d=1:s={W}x{H}:fps={OUT_FPS}[zm]")
        cur = "[zm]"
        si, sg_, cur = sticker_graph(stickers, W, H, D, cur, os.path.join(out_dir, "_bahan"), 2 if info["has_audio"] else 1)
        inputs += si
        g += sg_
    post = ["vignette=PI/5"]
    flashes = [t for t in cut_times if t > 0 and int(round(t / P)) % 8 == 0]
    if opt.creative and flashes:
        en = "+".join(f"between(t,{t:.3f},{t + 0.07:.3f})" for t in flashes)
        post.append(f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.55:t=fill:enable='{en}'")
    post.append(subs_filter(name))
    post.append(f"fade=t=in:st=0:d=0.15,fade=t=out:st={D - 0.4:.2f}:d=0.4")
    g.append(f"{cur}{','.join(post)}[v0]")
    lg = add_logo(g, inputs, opt, W, H, D, campaign_text_overlay(g, inputs, opt, W, H, D, "[v0]"))
    g.append(f"{lg}null[v]")
    maps = ["-map", "[v]"]
    if info["has_audio"]:
        g.append(f"[1:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,afade=t=in:st=0:d=0.05,"
                 f"afade=t=out:st={D - 1.2:.2f}:d=1.2[a]")
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    encode(inputs, g, maps, name, out_dir, D)
    shutil.rmtree(tmp, ignore_errors=True)
    log(f"      [montase] {len(slots)} potongan di beat, {slowmo} slow-mo, {len(flashes)} flash, {len(stickers)} stiker")
    return D, A0


# ============================================================================ pipeline

def slug(t, n=45):
    t = re.sub(r"[^\w\s-]", "", (t or "").lower()).strip()
    return re.sub(r"[\s_-]+", "-", t)[:n].strip("-") or "klip"


def safe_name(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s).strip(" .")
    return s[:60] or "video"


def select_windows(score, L, count, rel):
    n = len(score)
    if n <= L:
        return [(0, float(score.mean()))]
    cs = np.concatenate([[0.0], np.cumsum(score)])
    win = (cs[L:] - cs[:-L]) / L
    gap = int(4 / HOP)
    best = float(win.max())
    chosen = []
    for i in np.argsort(-win):
        if win[i] < rel * best:
            break
        if all(abs(int(i) - j) >= L + gap for j, _ in chosen):
            chosen.append((int(i), float(win[i])))
            if len(chosen) >= count:
                break
    return chosen


def process_video(source, opt: Options, log=print, progress=lambda p, s="": None):
    global FINE
    CANCEL.clear()
    camp_ = campaign_load(opt.campaign) if opt.campaign else None
    if opt.campaign and not camp_:
        log(f"PERHATIAN: campaign '{opt.campaign}' tidak ditemukan -> diproses tanpa campaign")
    if camp_:
        opt = campaign_apply(opt, camp_, log)
    G = GENRES.get(opt.genre, GENRES["otomatis"])
    if G["style"] and opt.style == "otomatis":
        opt = Options(**{**asdict(opt), "style": G["style"]})
    if opt.genre != "otomatis":
        opt = Options(**{**asdict(opt), "frame": G["frame"]})
        log(f"Jenis konten (dipilih): {G['label']} -> kamera: {FRAME_LABEL.get(G['frame'], G['frame'])}")
    t0 = time.time()
    out_dir, video_title = opt.out_dir, ""
    base_p = 0.0
    if is_url(source):
        path, video_title = download(source.strip(), os.path.join(APP_DIR, "unduhan"), opt.cookies, log,
                                     lambda p: progress(0.12 * p, "Mengunduh video"))
        out_dir = out_dir or os.path.join(APP_DIR, "hasil", safe_name(os.path.splitext(os.path.basename(path))[0]))
        base_p = 0.12
    else:
        path = os.path.abspath(source)

    def prog(p, stage):
        progress(base_p + (1 - base_p) * min(1.0, p), stage)

    base = os.path.splitext(os.path.basename(path))[0]
    video_title = clean_title(video_title or re.sub(r"\[[^\]]*\]$", "", base))
    out_dir = os.path.abspath(out_dir or os.path.join(os.path.dirname(path), base + "_klip"))
    os.makedirs(out_dir, exist_ok=True)

    info = probe(path)
    dur = info["duration"]
    n = max(1, int(np.ceil(dur / HOP)))
    cfg = resolve_profile(opt, dur)
    FINE = 1.0 / cfg["fps"]
    log(f"Video: {os.path.basename(path)} | {dur / 60:.1f} menit | {info['width']}x{info['height']} {info['codec']}")
    log(f"Perangkat: {hw_summary()}")
    log(f"Profil: {PROFILES[cfg['name']]} | pencarian momen: Whisper '{cfg['scan']}', subtitle klip: '{cfg['sub']}'")

    prog(0.0, "Menyiapkan analisis")
    plan = pick_decoder(path, info, cfg["fps"])
    info["dec"] = plan[4]
    import hashlib
    do_scan = opt.subtitles and info["has_audio"] and opt.style != "montase"
    st_ = os.stat(path)
    tail_ = f"|{st_.st_size}|{int(st_.st_mtime)}|{cfg['scan']}|{cfg['fps']}|{do_scan}|{opt.language}"
    # kunci cache dari NAMA file (bukan lokasi) -> tetap terpakai walau folder aplikasi dipindah
    ckey = hashlib.sha1(f"{os.path.basename(path)}{tail_}".encode()).hexdigest()[:16]
    cdir = os.path.join(APP_DIR, "_cache")
    cnpz, cjson = os.path.join(cdir, ckey + ".npz"), os.path.join(cdir, ckey + ".json")
    if not os.path.exists(cnpz):  # cache versi lama (kunci = lokasi lengkap, termasuk lokasi folder sebelum dipindah)
        olds = [path] + [os.path.join(d_, os.path.relpath(path, APP_DIR)) for d_ in load_config().get("old_dirs", [])
                         if os.path.abspath(path).lower().startswith(APP_DIR.lower())]
        for op_ in olds:
            k_ = hashlib.sha1(f"{op_}{tail_}".encode()).hexdigest()[:16]
            if os.path.exists(os.path.join(cdir, k_ + ".npz")) and os.path.exists(os.path.join(cdir, k_ + ".json")):
                os.replace(os.path.join(cdir, k_ + ".npz"), cnpz)
                os.replace(os.path.join(cdir, k_ + ".json"), cjson)
                log("      hasil analisis lama ditemukan (sebelum folder dipindah) -> dipakai ulang")
                break
    cached, vis_cached = None, None
    if os.path.exists(cnpz) and os.path.exists(cjson):
        try:
            z = np.load(cnpz)
            with open(cjson, encoding="utf-8") as f:
                cj = json.load(f)
            cached = (z["db"], dict(diff=z["diff"], bright=z["bright"], contrast=z["contrast"], motion=z["motion"]),
                      [tuple(w) for w in cj["words"]], cj["stats"])
            vis_cached = cj.get("vis") if cj.get("vis_v") == 2 else None
        except Exception:
            cached = None
    if cached:
        db, fs, words, stats = cached
        words = dedupe_words(words)
        log("[1/4] Memakai hasil analisis tersimpan -> langsung ke pemilihan momen")
    else:
        log(f"[1/4] Analisis gambar memakai {plan[0]} + analisis suara (berjalan bersamaan)")
        db = audio_db(path, n, info["has_audio"])

        t_frames = dur / (25 if plan[0] != "CPU" else 8)
        t_whisper = dur / model_speed(cfg["scan"], cfg) if do_scan else 0.0
        t_tot = t_frames + t_whisper
        state = dict(pf=0.0, pw=0.0, fs=None, err=None)
        eta_s = max(t_frames, t_whisper)
        log(f"      perkiraan waktu analisis: ±{eta_s / 60:.0f} menit")

        def an_prog():
            p = (state["pf"] * t_frames + state["pw"] * t_whisper) / max(t_tot, 1e-6)
            stage = "Menganalisis gambar & mentranskrip ucapan" if do_scan else "Menganalisis gambar"
            prog(0.72 * p, f"Tahap 1/3 · {stage}")

        def frames_job():
            try:
                state["fs"] = frame_stats(path, n, info, plan, lambda p: (state.__setitem__("pf", p), an_prog()))
            except BaseException as ex:  # noqa
                state["err"] = ex
        th = threading.Thread(target=frames_job, daemon=True)
        th.start()

        words, stats = [], None
        if do_scan:
            try:
                words, stats = transcribe(path, 0, dur, cfg["scan"], cfg, opt.language, log,
                                          lambda p: (state.__setitem__("pw", p), an_prog()))
                log(f"      transkrip selesai: {len(words)} kata")
            except Cancelled:
                raise
            except ImportError:
                log("      faster-whisper belum terpasang -> lanjut tanpa transkrip")
            except Exception as ex:
                log(f"      transkripsi gagal ({ex}) -> lanjut tanpa transkrip")
        while th.is_alive():
            th.join(0.3)
            check_cancel()
        if state["err"]:
            if isinstance(state["err"], Cancelled):
                raise state["err"]
            raise state["err"]
        fs = state["fs"]
        if fs is None or len(fs["diff"]) < 3:
            if plan[0] != "CPU":
                log("      decode GPU gagal -> ulang dengan CPU")
                plan = decode_plans(info, cfg["fps"])[-1]
                info["dec"] = []
                fs = frame_stats(path, n, info, plan, lambda p: prog(0.72 * p, "Tahap 1/3 · Menganalisis gambar (CPU)"))
        os.makedirs(cdir, exist_ok=True)
        np.savez_compressed(cnpz, db=db, **fs)
        with open(cjson, "w", encoding="utf-8") as f:
            json.dump(dict(words=words, stats=stats), f)
    cuts = scene_cuts(fs["diff"])
    cuts_pm = len(cuts) / max(dur / 60, 0.1)
    log(f"      {len(cuts)} potongan adegan ({cuts_pm:.0f}/menit) | analisis selesai dalam {(time.time() - t0) / 60:.1f} menit")
    prog(0.72, "Tahap 2/3 · AI memilih momen terbaik")

    music = bool(stats and stats["words"] > 0 and (stats["no_speech"] > 0.5 or stats["word_prob"] < 0.6))
    if stats and stats["words"] == 0:
        music = True
    wpm = (stats["words"] if stats else 0) / max(dur / 60, 0.1)
    cands = shot_candidates(cuts, dur, fs)

    # ---- analisa jenis konten (kalau belum dipilih)
    if opt.genre == "otomatis":
        prog(0.72, "Tahap 2/3 · Menganalisa jenis konten")
        vis = vis_cached or visual_profile(path, info, dur)
        if not vis_cached:
            try:
                with open(cjson, encoding="utf-8") as f:
                    cj = json.load(f)
                cj["vis"], cj["vis_v"] = vis, 2
                with open(cjson, "w", encoding="utf-8") as f:
                    json.dump(cj, f)
            except Exception:
                pass
        gk, why = detect_genre(video_title, words, music, wpm, cuts_pm, vis)
        if opt.ai == "claude" and (opt.api_key or os.environ.get("ANTHROPIC_API_KEY")):
            try:
                gk, why = claude_classify(video_title, words, vis, opt.api_key)
                why = "Claude: " + why
            except Exception as ex:
                log(f"      klasifikasi Claude gagal ({str(ex)[:100]}) -> pakai analisa lokal")
        G = GENRES[gk]
        opt = Options(**{**asdict(opt), "genre": gk, "frame": G["frame"],
                         "style": G["style"] if opt.style == "otomatis" and G["style"] else opt.style})
        log(f"      jenis konten terdeteksi: {G['label'].upper()} ({why})")
        log(f"      -> kamera: {FRAME_LABEL.get(G['frame'], G['frame'])}, gaya edit: {opt.style}")
    style = opt.style
    if style == "otomatis":
        montage_ok = info["has_audio"] and cuts_pm >= 3 and len(cands) >= 8
        style = "montase" if montage_ok and (music or wpm < 60 or not stats) else "cerita"
        log(f"      gaya edit otomatis: {style.upper()} ({'musik' if music else f'{wpm:.0f} kata/menit'}, "
            f"{cuts_pm:.0f} potongan adegan/menit)")
    if style == "montase" and (not info["has_audio"] or len(cands) < 3):
        style = "cerita"
    if music:
        words = []

    clips = []
    if style == "montase":
        loud = norm(db) + 0.5 * norm(np.maximum(0, db - smooth(db, 20)))
        L = max(2, int(opt.duration / HOP))
        count = min(opt.n_clips, max(1, int(dur / (opt.duration * 1.3))))
        wins = select_windows(loud, L, count, 0.5)
        used = {}
        for rank, (i, sc) in enumerate(wins, 1):
            check_cancel()
            w0 = max(0.0, i * HOP - 1.0)
            env, fr = onset_envelope(path, w0, min(dur - w0, opt.duration + 6))
            if len(env) < 100:
                continue
            bpm, P, beats, strength, down = beat_grid(env, fr)
            mplan = plan_montage(beats, P, strength, down, min(opt.duration, dur - w0 - 1))
            if not mplan:
                continue
            score = int(np.clip(round(100 * (0.4 + 0.6 * sc / max(1e-6, wins[0][1]) * 0.98)), 1, 99))
            name = f"klip_{rank:02d}_skor{score}_{hms(w0)}"
            title = opt.title.strip() or video_title or random.choice(FALLBACK_TITLES)
            log(f"   #{rank} montase, musik dari {hms(w0, ':')} | {bpm:.0f} BPM | skor {score}")
            D, A0 = render_montage(path, info, w0, mplan, P, cands, used, fs, opt, out_dir, name, title, log,
                                   lambda p: prog(0.75 + 0.25 * (rank - 1 + p) / len(wins),
                                                  f"Tahap 3/3 · Mengedit klip {rank}/{len(wins)}"))
            D += add_intro_outro(out_dir, name, title, opt, info["has_audio"], opt.language, log)
            clips.append(dict(rank=rank, file=name + ".mp4", start=round(A0, 2), end=round(A0 + D, 2),
                              duration=round(D, 2), score=score, title=title, category="Montase",
                              reason=f"Bagian musik paling bertenaga ({bpm:.0f} BPM) + shot paling hidup, dipotong di ketukan",
                              hashtags=default_hashtags("montage", video_title)))
            clips[-1].update(make_publish_kit(path, info, clips[-1], out_dir, name, video_title, log=log, logo=opt.logo,
                                         langs=[x.strip() for x in opt.desc_langs.split(",") if x.strip()],
                                         api_key=opt.api_key if opt.ai == "claude" else "", campaign=camp_, opt=opt))
    else:
        sents = build_sentences(words)
        feats = build_features(n, db, fs)
        if opt.focus == "action" or (opt.focus == "otomatis" and opt.genre in ("action", "ekstrem", "balapan")):
            try:
                prog(0.73, "AI penglihatan mencari adegan aksi")
                rect_ = parse_cam_box(opt.cam_box) or (facecam_for(path, info, log)[0] if info["width"] > info["height"] else None)
                vis_ = vision_action(path, info, rect_, 2.0 if dur < 3 * 3600 else 3.0, log,
                                     lambda p_: prog(0.73 + 0.02 * p_, "AI penglihatan mencari adegan aksi"))
                vis_ = np.pad(vis_, (0, max(0, n - len(vis_))))[:n]
                imp_ = game_impacts(path, info, words, n)
                feats["action"] = action_blend(vis_, imp_, feats["motion"])
                feats["vision_ok"] = True
                feats["vision_raw"] = smooth(vis_, 4)
                hot = float((feats["action"] > 0.6).mean())
                log(f"      adegan aksi terdeteksi di ~{hot:.0%} video (gambar + suara game + gerakan)")
            except Cancelled:
                raise
            except Exception as ex:
                log(f"      AI penglihatan gagal ({str(ex)[:120]}) -> pakai suara & gerakan saja")
        picks = []
        if sents:
            if opt.ai == "claude":
                try:
                    picks = claude_ai_select(sents, feats, n, opt.duration, opt.n_clips, video_title, opt.api_key, log,
                                             GENRES.get(opt.genre, GENRES["otomatis"])["brief"] + " "
                                             + FOCUS.get(opt.focus, FOCUS["otomatis"])[2]
                                             + (f" BRIEF CAMPAIGN (wajib diikuti): {camp_.get('content_brief', '')} "
                                                f"Larangan: {'; '.join(camp_.get('banned', []))}. "
                                                f"Judul/deskripsi dalam bahasa '{camp_.get('language', 'id')}'."
                                                if camp_ else ""))
                except Cancelled:
                    raise
                except Exception as ex:
                    log(f"      Claude AI gagal ({str(ex)[:160]}) -> pakai AI lokal")
            if not picks:
                log("      AI lokal menilai semua kandidat klip (hook, energi, reaksi, kata emosional, tempo, aksi)...")
                w8_ = FOCUS.get(opt.focus, FOCUS["otomatis"])[1] or GENRES.get(opt.genre, GENRES["otomatis"])["w"]
                if opt.focus != "otomatis":
                    log(f"      fokus momen: {FOCUS[opt.focus][0]}")
                if opt.focus == "action" and feats.get("vision_ok"):
                    log("      memilih jendela dengan adegan tembak-tembakan / ledakan / kecelakaan paling banyak...")
                    picks = action_select(feats, sents, n, opt.duration, opt.n_clips, dur,
                                          lambda ws: verify_action(path, info, rect_, ws, log=log), log)
                    if not picks:
                        log("      PERHATIAN: tidak ada adegan tembak-tembakan/ledakan/kecelakaan yang jelas di video ini "
                            "-> memakai momen paling seru biasa")
                        picks = local_ai_select(sents, feats, n, opt.duration, opt.n_clips, opt.strict, log, w8_)
                    elif len(picks) < opt.n_clips:
                        log(f"      video ini hanya punya {len(picks)} momen aksi kuat (tembak-tembakan/ledakan/kecelakaan) "
                            f"-> tidak dipaksakan jadi {opt.n_clips} klip")
                else:
                    picks = local_ai_select(sents, feats, n, opt.duration, opt.n_clips, opt.strict, log, w8_)
        if not picks:  # tanpa ucapan: pilih dari energi & gerakan
            score = (0.2 * feats["energy"] + 0.2 * feats["peak"] + 0.2 * feats["motion"] + 0.4 * feats["action"]
                     if opt.focus == "action" else 0.4 * feats["energy"] + 0.3 * feats["peak"] + 0.3 * feats["motion"])
            wins = select_windows(score, max(2, int(opt.duration / HOP)), opt.n_clips, STRICT.get(opt.strict, 0.7))
            top = wins[0][1] if wins else 1
            for i, sc in wins:
                picks.append(dict(start=i * HOP, end=min(dur, i * HOP + opt.duration), score=int(np.clip(100 * sc / top * 0.9, 1, 99)),
                                  title="", reason="Energi suara & aksi di layar paling tinggi", category="Aksi", hashtags=[]))
        picks.sort(key=lambda p: -p["score"])
        log(f"      {len(picks)} klip terpilih:")
        for rank, pk in enumerate(picks, 1):
            log(f"   #{rank} skor {pk['score']} | {hms(pk['start'], ':')}-{hms(pk['end'], ':')} | {pk['category']} | {pk['reason']}")
        if opt.cam_box.strip():
            save_manual_profile(path, opt.cam_box.strip())
        if opt.fmt == "vertical_face" and not opt.cam_box.strip() and opt.frame in ("auto", "split_game", "split_react", "split_screen", "present"):
            try:
                rect_, why_ = facecam_for(path, info, log)
                if rect_:
                    opt = Options(**{**asdict(opt), "cam_box": ",".join(f"{v * 100:.2f}" for v in rect_)})
                    log(f"      facecam streamer ({why_}) -> dipotong & ditaruh di {opt.cam_pos}")
            except Cancelled:
                raise
            except Exception as ex:
                log(f"      deteksi facecam gagal ({str(ex)[:100]}) -> per klip")
        for rank, pk in enumerate(picks, 1):
            check_cancel()
            stage = f"Tahap 3/3 · Klip {rank}/{len(picks)}"
            s, e = max(0.0, pk["start"] - 0.15), min(dur, pk["end"] + 0.3)
            segs = [x for x in sents if x["end"] > s and x["start"] < e]
            lang_ = (stats or {}).get("lang") or opt.language
            sub_m = cfg["sub"]
            if lang_ not in ("en", "auto") and sub_m in ("tiny", "base", "small") and segs:
                sub_m = "large-v3-turbo"  # bahasa Indonesia dll: model kecil sering salah dengar kata
                if rank == 1:
                    log("      subtitle & judul pakai model akurat 'large-v3-turbo' (bahasa non-Inggris)")
            accurate = False
            if (opt.subtitles or opt.ai != "claude") and segs and sub_m != cfg["scan"]:
                prog(0.75 + 0.25 * (rank - 1) / len(picks), stage + " · subtitle akurat")
                ctx_ = ", ".join(x for x in (video_title, (camp_ or {}).get("topic", ""), (camp_ or {}).get("brand", ""),
                                             opt.genre if opt.genre not in ("otomatis", "") else "") if x)
                try:
                    w2, _ = transcribe(path, max(0, s - 0.5), e - s + 1.0, sub_m, cfg, lang_, log, lambda p: None,
                                       prompt=f"{ctx_}." if ctx_ else "")
                    if w2:
                        segs = build_sentences(w2)
                        accurate = True
                except Cancelled:
                    raise
                except Exception as ex:
                    log(f"      subtitle akurat gagal ({ex}) -> pakai transkrip cepat")
            if accurate and opt.ai != "claude":  # judul lokal dihitung ulang dari transkrip akurat
                pk["title"], pk["hook"] = "", ""
            auto_t = fix_text(pk["title"]) or auto_title([x["text"] for x in segs], video_title)
            for k_ in ("yt_title", "description", "thumb_text", "hook"):
                if pk.get(k_):
                    pk[k_] = fix_text(pk[k_])
            title = opt.title.strip() or auto_t
            name = f"{rank:02d}_skor{pk['score']}_{slug(auto_t)}"
            log(f"      judul otomatis klip #{rank}: {sentence_case(auto_t)}")
            prog(0.75 + 0.25 * (rank - 0.6) / len(picks), stage + " · mengedit")
            hook = pk.get("hook") or local_hook(segs) or sentence_case(pk["title"] or title)
            D, kind, box = render_story(path, info, s, e, opt, segs, db, out_dir, name, title, log, hook=hook)
            prog(0.75 + 0.25 * (rank - 0.25) / len(picks), stage + " · pembuka & penutup")
            D += add_intro_outro(out_dir, name, sentence_case(pk.get("yt_title") or auto_t), opt, info["has_audio"],
                                 (stats or {}).get("lang") or opt.language, log)
            tags = pk["hashtags"] or default_hashtags(pk["category"], video_title)
            text = " ".join(x["text"] for x in segs)
            clip = dict(rank=rank, file=name + ".mp4", start=round(s, 2), end=round(e, 2), duration=round(D, 2),
                        score=pk["score"], title=auto_t,
                        category=pk["category"], reason=pk["reason"],
                        hashtags=tags, text=text, hook=hook, genre=opt.genre)
            for k in ("yt_title", "description", "thumb_text"):
                if pk.get(k):
                    clip[k] = pk[k]
            clip.update(make_publish_kit(path, info, clip, out_dir, name, opt.title.strip() or video_title, kind, box,
                                         badge=opt.title.strip(), log=log, logo=opt.logo,
                                         langs=[x.strip() for x in opt.desc_langs.split(",") if x.strip()],
                                         api_key=opt.api_key if opt.ai == "claude" else "", campaign=camp_, opt=opt))
            clips.append(clip)
            prog(0.75 + 0.25 * rank / len(picks), stage)
        if words:
            with open(os.path.join(out_dir, "transkrip.txt"), "w", encoding="utf-8") as f:
                for x in sents:
                    f.write(f"[{hms(x['start'], ':')}] {x['text']}\n")

    shutil.rmtree(os.path.join(out_dir, "_bahan"), ignore_errors=True)
    if camp_ and clips:
        with open(os.path.join(out_dir, "campaign_checklist.txt"), "w", encoding="utf-8") as f:
            f.write(f"CAMPAIGN: {campaign_summary(camp_)}\n(dianalisa oleh {camp_.get('analyzed_by', '-')})\n\n"
                    + "\n\n".join(f"{c_['file']}  |  {c_.get('yt_title', '')}\n"
                                    + "\n".join("  " + x for x in c_.get("campaign_check", [])) for c_ in clips) + "\n")
        bad_ = sum(1 for c_ in clips for x in c_.get("campaign_check", []) if x.startswith("✗"))
        log(f"      checklist campaign -> campaign_checklist.txt" + (f" ({bad_} poin belum sesuai, cek!)" if bad_ else
                                                                     " (semua aturan otomatis terpenuhi)"))
    o = asdict(opt)
    o["api_key"] = "***" if opt.api_key else ""
    with open(os.path.join(out_dir, "laporan.json"), "w", encoding="utf-8") as f:
        json.dump(dict(source=source, file=path, style=style, profile=cfg, options=o, clips=clips),
                  f, ensure_ascii=False, indent=2)
    log(f"Selesai! {len(clips)} klip dalam {(time.time() - t0) / 60:.1f} menit -> {out_dir}")
    progress(1.0, "Selesai")
    return out_dir, clips


def process_many(sources, opt: Options, log=print, progress=lambda p, s="": None, compile_=False):
    """Edit otomatis banyak video berurutan. compile_=True: klip terbaik tiap video digabung jadi 1 kompilasi."""
    sources = [x.strip().strip('"') for x in sources if x.strip()]
    N = len(sources)
    results, failed = [], []
    for i, src in enumerate(sources):
        check_cancel()
        log(f"========== Video {i + 1}/{N}: {src}")
        o = Options(**{**asdict(opt), "keep_core": compile_ or opt.keep_core})
        if opt.out_dir and N > 1:
            o.out_dir = os.path.join(opt.out_dir, f"video_{i + 1:02d}")

        def prog_i(p_, st="", i=i):
            progress((i + p_) / N * (0.92 if compile_ else 1.0), f"Video {i + 1}/{N} · {st}")
        try:
            out, clips = process_video(src, o, log, prog_i)
            results.append((out, clips))
        except Cancelled:
            raise
        except Exception as ex:
            log(f"GAGAL memproses video {i + 1}: {str(ex)[:300]}")
            failed.append(src)
    comp = None
    if compile_ and results:
        progress(0.93, "Membuat video kompilasi")
        parts = []
        for out, clips in results:
            if not clips:
                continue
            best = max(clips, key=lambda c: c["score"])
            core = os.path.join(out, "_inti", best["file"])
            parts.append(core if os.path.exists(core) else os.path.join(out, best["file"]))
        if parts:
            cdir = opt.out_dir or os.path.join(APP_DIR, "hasil")
            os.makedirs(cdir, exist_ok=True)
            name = "kompilasi_" + time.strftime("%Y%m%d_%H%M%S")
            lst = os.path.join(cdir, name + "_list.txt")
            with open(lst, "w", encoding="utf-8") as f:
                for p_ in parts:
                    f.write(f"file '{os.path.abspath(p_)}'\n".replace("\\", "/"))
            outp = os.path.join(cdir, name + ".mp4")
            try:
                run([ffmpeg_exe(), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                     "-movflags", "+faststart", outp])
            except Exception:
                run([ffmpeg_exe(), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                     "-ac", "2", "-movflags", "+faststart", outp])
            os.remove(lst)
            ctitle = opt.title.strip() or f"Kompilasi {len(parts)} momen terbaik"
            o2 = Options(**{**asdict(opt), "keep_core": False})
            D = probe(outp)["duration"] + add_intro_outro(cdir, name, ctitle, o2, True, opt.language, log)
            comp = dict(rank=0, file=name + ".mp4", score=max(c["score"] for _, cl in results for c in cl),
                        title=ctitle, duration=round(D, 2), category="Kompilasi",
                        reason=f"Gabungan klip terbaik dari {len(parts)} video", yt_title=ctitle, description="",
                        thumbnail="", out=cdir)
            shutil.rmtree(os.path.join(cdir, "_bahan"), ignore_errors=True)
            log(f"Kompilasi selesai: {outp} ({D:.0f} dtk)")
        for out, _ in results:
            shutil.rmtree(os.path.join(out, "_inti"), ignore_errors=True)
    if failed:
        log(f"{len(failed)} video gagal diproses: " + ", ".join(failed))
    progress(1.0, "Selesai")
    return results, comp


def watch_folder(folder, opt: Options, log=print, stop: threading.Event | None = None, interval=10):
    stop = stop or threading.Event()
    state_file = os.path.join(folder, ".autoclip_selesai.json")
    try:
        done = set(json.load(open(state_file, encoding="utf-8")))
    except Exception:
        done = set()
    sizes = {}
    log(f"Memantau folder: {folder} (cek setiap {interval} detik)")
    while not stop.is_set():
        for fn in sorted(os.listdir(folder)):
            p = os.path.join(folder, fn)
            if fn in done or os.path.splitext(fn)[1].lower() not in VIDEO_EXT or not os.path.isfile(p):
                continue
            size = os.path.getsize(p)
            if sizes.get(fn) != size:
                sizes[fn] = size
                continue
            log(f"Video baru terdeteksi: {fn}")
            o = Options(**{**asdict(opt), "out_dir": os.path.join(folder, "hasil_klip", os.path.splitext(fn)[0])})
            try:
                process_video(p, o, log)
            except Exception as ex:
                log(f"GAGAL memproses {fn}: {ex}")
            done.add(fn)
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(sorted(done), f)
            if stop.is_set():
                break
        stop.wait(interval)
    log("Pemantauan folder dihentikan.")


# ============================================================================ GUI

def has_whisper():
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(d):
    try:
        d = {**load_config(), **d}  # gabung: jangan hapus pengaturan lain
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def fmt_dur(sec):
    sec = int(max(0, sec))
    return f"{sec // 3600}j {sec % 3600 // 60}m" if sec >= 3600 else (f"{sec // 60}m {sec % 60:02d}d" if sec >= 60 else f"{sec}d")


def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    cfgfile = load_config()
    root = tk.Tk()
    root.title("AutoClip - Klip Video Otomatis")
    sh_ = root.winfo_screenheight()
    root.geometry(f"1000x{min(940, sh_ - 90)}+20+10")
    root.minsize(820, min(600, sh_ - 120))
    q = queue.Queue()
    busy = {"job": False, "t0": 0.0}
    watch_stop = {"ev": None}
    last_out = {"dir": None}

    # seluruh isi bisa di-scroll (layar laptop tidak cukup tinggi untuk semua pengaturan + hasil)
    canvas_ = tk.Canvas(root, highlightthickness=0, borderwidth=0)
    vsb_ = ttk.Scrollbar(root, orient="vertical", command=canvas_.yview)
    canvas_.configure(yscrollcommand=vsb_.set)
    vsb_.pack(side="right", fill="y")
    canvas_.pack(side="left", fill="both", expand=True)
    frm = ttk.Frame(canvas_, padding=12)
    win_id = canvas_.create_window(0, 0, window=frm, anchor="nw")

    def relayout(_e=None):
        canvas_.itemconfigure(win_id, width=canvas_.winfo_width(),
                              height=max(canvas_.winfo_height(), frm.winfo_reqheight()))
        canvas_.configure(scrollregion=canvas_.bbox("all"))
    canvas_.bind("<Configure>", relayout)
    frm.bind("<Configure>", lambda _e: canvas_.configure(scrollregion=canvas_.bbox("all")))

    def on_wheel(e):
        w_ = e.widget
        if isinstance(w_, (tk.Text, ttk.Treeview, tk.Listbox)) or str(w_).endswith("popdown.f.l"):
            return  # widget yang punya scroll sendiri
        try:
            if w_.winfo_toplevel() is not root:
                return
        except Exception:
            return
        canvas_.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")
    root.bind_all("<MouseWheel>", on_wheel)

    def scroll_bottom():
        root.update_idletasks()
        canvas_.yview_moveto(1.0)
    frm.columnconfigure(1, weight=1)
    ttk.Label(frm, text="AutoClip", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(frm, text="1) Tempel link atau pilih file   2) AI memilih & menilai momen terbaik   "
                        "3) Klip siap posting dengan subtitle & edit kreatif", foreground="#555"
              ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))

    def var(cls, key, default):
        return cls(value=cfgfile.get(key, default))
    v_video, v_out = tk.StringVar(), tk.StringVar()
    v_n = var(tk.IntVar, "n_clips", 5)
    v_dur = tk.DoubleVar(value=round(float(cfgfile.get("duration", 45)) / 60, 2))  # dalam MENIT
    v_fmt = var(tk.StringVar, "fmt", FORMATS["vertical_face"])
    v_style = var(tk.StringVar, "style", STYLES["otomatis"])
    v_sub = tk.BooleanVar(value=has_whisper() and cfgfile.get("subtitles", True))
    v_prof = var(tk.StringVar, "profile", PROFILES["otomatis"])
    v_smodel = var(tk.StringVar, "sub_model", "otomatis")
    v_lang = var(tk.StringVar, "language", "id")
    v_ai = var(tk.StringVar, "ai", AI_MODES["lokal"])
    v_key = var(tk.StringVar, "api_key", "")
    v_strict = var(tk.StringVar, "strict", "normal")
    v_cre, v_jc, v_sfx = var(tk.BooleanVar, "creative", True), var(tk.BooleanVar, "jumpcut", True), var(tk.BooleanVar, "sfx", True)
    v_look = var(tk.StringVar, "look", LOOKS["bersih"])
    v_genre = var(tk.StringVar, "genre", GENRES["otomatis"]["label"])
    v_focus = var(tk.StringVar, "focus", FOCUS["otomatis"][0])
    v_title, v_handle = tk.StringVar(), var(tk.StringVar, "handle", "")
    v_logo = var(tk.StringVar, "logo", "")
    v_logopos = var(tk.StringVar, "logo_pos", "kanan atas")
    v_brand = var(tk.StringVar, "brand", "awal & akhir")
    v_campos = var(tk.StringVar, "cam_pos", "bawah")
    v_cambox = var(tk.StringVar, "cam_box", "")
    v_blogo = var(tk.StringVar, "brand_logo", "")
    v_btext = var(tk.StringVar, "brand_text", "")
    v_top = var(tk.BooleanVar, "top_text", False)
    v_io = var(tk.BooleanVar, "intro", True)
    v_voice = var(tk.StringVar, "voice", VOICE_LABEL["pria"])
    v_tcol = var(tk.StringVar, "text_color", TEXT_COLORS["otomatis"][0])
    v_meme = var(tk.BooleanVar, "meme_sfx", True)
    v_dlang = var(tk.StringVar, "desc_langs", DESC_LANGS["en"])
    v_cta = var(tk.StringVar, "cta", Options.cta)
    v_cookies = var(tk.StringVar, "cookies", "tanpa")

    def add_lines(lines):
        cur = [x for x in src_box.get("1.0", "end").splitlines() if x.strip()]
        for ln in lines:
            if ln and ln not in cur:
                cur.append(ln)
        src_box.delete("1.0", "end")
        src_box.insert("1.0", "\n".join(cur))
        v_count.set(f"{len(cur)} video" if cur else "")

    def pick_video():
        ps = filedialog.askopenfilenames(title="Pilih satu atau beberapa video",
                                         filetypes=[("Video", " ".join("*" + x for x in sorted(VIDEO_EXT))),
                                                    ("Semua file", "*.*")])
        if ps:
            add_lines(list(ps))

    def pick_out():
        p = filedialog.askdirectory()
        if p:
            v_out.set(p)

    def paste_link():
        try:
            add_lines([x.strip() for x in re.split(r"[\s]+", root.clipboard_get()) if is_url(x.strip())]
                      or [root.clipboard_get().strip()])
        except Exception:
            pass

    def clear_src():
        src_box.delete("1.0", "end")
        v_count.set("")

    r = 2
    v_count = tk.StringVar()
    v_comp = var(tk.BooleanVar, "compile", False)
    ttk.Label(frm, text="Link / Video:\n(satu per baris)").grid(row=r, column=0, sticky="nw", pady=3)
    src_box = tk.Text(frm, height=3, wrap="none", font=("Segoe UI", 9))
    src_box.grid(row=r, column=1, sticky="ew", padx=6, pady=3)
    src_box.bind("<KeyRelease>", lambda _e: v_count.set(
        f"{len([x for x in src_box.get('1.0', 'end').splitlines() if x.strip()])} video"))
    bb = ttk.Frame(frm)
    bb.grid(row=r, column=2, sticky="n")
    ttk.Button(bb, text="Tempel Link", command=paste_link).grid(row=0, column=0, sticky="ew")
    ttk.Button(bb, text="File...", command=pick_video).grid(row=0, column=1, padx=(4, 0), sticky="ew")
    ttk.Button(bb, text="Kosongkan", command=clear_src).grid(row=1, column=0, sticky="ew", pady=(3, 0))

    def viral_win():
        win = tk.Toplevel(root)
        win.title("Cari video viral YouTube")
        win.geometry("1180x450")
        top_ = ttk.Frame(win, padding=8)
        top_.pack(fill="x")
        vc = tk.StringVar(value="Indonesia + Amerika")
        vk = tk.StringVar(value="Semua")
        vp = tk.StringVar(value="Minggu ini")
        ttk.Label(top_, text="Negara").pack(side="left")
        ttk.Combobox(top_, textvariable=vc, state="readonly", width=24,
                     values=["Indonesia + Amerika", "Indonesia + Amerika + Inggris + India", *VIRAL_COUNTRIES]
                     ).pack(side="left", padx=4)
        ttk.Label(top_, text="Kategori").pack(side="left", padx=(8, 0))
        ttk.Combobox(top_, textvariable=vk, values=list(VIRAL_CATS), state="readonly", width=18).pack(side="left", padx=4)
        ttk.Label(top_, text="Periode").pack(side="left", padx=(8, 0))
        ttk.Combobox(top_, textvariable=vp, values=list(VIRAL_PERIOD), state="readonly", width=10).pack(side="left", padx=4)
        st = tk.StringVar(value="Pilih negara & kategori lalu klik Cari (3 video teratas per negara).")
        cols_ = ("Negara", "Penonton", "Durasi", "Judul", "Channel", "Link")
        tv_ = ttk.Treeview(win, columns=cols_, show="headings", height=12)
        for c_, w_ in zip(cols_, (80, 70, 60, 420, 160, 330)):
            tv_.heading(c_, text=c_)
            tv_.column(c_, width=w_, anchor="w")
        tv_.pack(fill="both", expand=True, padx=8)
        urls = {}

        def do_search():
            names = vc.get().split(" + ")
            st.set("Mencari video viral...")
            tv_.delete(*tv_.get_children())
            urls.clear()

            def work():
                rows, errs = [], []
                for nm in names:
                    try:
                        for v in viral_videos(nm, vk.get(), vp.get()):
                            rows.append((nm, v))
                    except Exception as ex:
                        errs.append(f"{nm}: {str(ex)[:80]}")

                def show():
                    for nm, v in rows:
                        iid = tv_.insert("", "end", values=(nm, fmt_views(v["views"]), fmt_dur(v["sec"]),
                                                            v["title"], v["channel"], v["url"]))
                        urls[iid] = v["url"]
                    st.set(f"{len(rows)} video ditemukan." + (" Gagal: " + "; ".join(errs) if errs else "")
                           + " Pilih lalu klik Tambahkan (Ctrl+klik untuk beberapa).")
                win.after(0, show)
            threading.Thread(target=work, daemon=True).start()

        def add_sel(all_=False):
            ids = tv_.get_children() if all_ else tv_.selection()
            if ids:
                add_lines([urls[i] for i in ids if i in urls])
                st.set(f"{len(ids)} link ditambahkan ke daftar video.")

        def copy_links(all_=False):
            ids = tv_.get_children() if all_ else (tv_.selection() or tv_.get_children())
            links = [urls[i] for i in ids if i in urls]
            if links:
                win.clipboard_clear()
                win.clipboard_append("\n".join(links))
                st.set(f"{len(links)} link disalin - tinggal tempel (Ctrl+V).")
        tv_.bind("<Control-c>", lambda _e: copy_links())

        def open_sel(_e=None):
            for i in tv_.selection():
                import webbrowser
                webbrowser.open(urls[i])
        tv_.bind("<Double-1>", open_sel)
        ttk.Button(top_, text="Cari", command=do_search).pack(side="left", padx=8)
        bot_ = ttk.Frame(win, padding=8)
        bot_.pack(fill="x")
        ttk.Button(bot_, text="Tambahkan yang dipilih", command=add_sel).pack(side="left")
        ttk.Button(bot_, text="Tambahkan semua", command=lambda: add_sel(True)).pack(side="left", padx=4)
        ttk.Button(bot_, text="Salin link dipilih", command=copy_links).pack(side="left", padx=4)
        ttk.Button(bot_, text="Salin semua link", command=lambda: copy_links(True)).pack(side="left", padx=4)
        ttk.Button(bot_, text="Buka di browser", command=open_sel).pack(side="left", padx=4)
        ttk.Label(bot_, textvariable=st, foreground="#06c").pack(side="left", padx=8)
        ttk.Label(win, text="Catatan: pastikan Anda punya izin memakai ulang video orang lain (hak cipta YouTube).",
                  foreground="#777").pack(anchor="w", padx=8, pady=(0, 6))
        do_search()
    ttk.Button(bb, text="🔥 Video viral", command=viral_win).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(3, 0))
    ttk.Label(bb, textvariable=v_count, foreground="#06c").grid(row=1, column=1, pady=(3, 0))
    r += 1
    ttk.Checkbutton(frm, text="Banyak video: gabungkan klip terbaik tiap video jadi 1 video kompilasi",
                    variable=v_comp).grid(row=r, column=1, sticky="w", padx=6)
    r += 1
    ttk.Label(frm, text="Folder hasil:").grid(row=r, column=0, sticky="w", pady=3)
    ttk.Entry(frm, textvariable=v_out).grid(row=r, column=1, sticky="ew", padx=6)
    ttk.Button(frm, text="Pilih...", command=pick_out).grid(row=r, column=2, sticky="w")

    r += 1
    NOCAMP = "(tanpa campaign)"
    v_camp = var(tk.StringVar, "campaign", NOCAMP)
    if v_camp.get() != NOCAMP and v_camp.get() not in campaign_list():
        v_camp.set(NOCAMP)
    v_camp_info = tk.StringVar()
    boxc = ttk.LabelFrame(frm, text="🎯 Campaign clipper  (upload brief PDF / foto -> klip, logo, tulisan, judul & "
                                    "deskripsi otomatis ikut aturan campaign)", padding=8)
    boxc.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(8, 3))
    boxc.columnconfigure(6, weight=1)
    ttk.Label(boxc, text="Campaign").grid(row=0, column=0, sticky="w")
    cb_camp = ttk.Combobox(boxc, textvariable=v_camp, state="readonly", width=32, values=[NOCAMP] + campaign_list())
    cb_camp.grid(row=0, column=1, sticky="w", padx=6)

    def camp_refresh(*_a):
        cb_camp["values"] = [NOCAMP] + campaign_list()
        p_ = campaign_load(v_camp.get())
        v_camp_info.set(("Aturan: " + campaign_summary(p_)) if p_ else
                        "Tidak memakai campaign. Klik 'Upload brief' lalu pilih PDF / foto brief campaign.")
    v_camp.trace_add("write", camp_refresh)

    def camp_upload():
        fs_ = filedialog.askopenfilenames(title="Pilih brief campaign (PDF / foto, boleh beberapa sekaligus)",
                                          filetypes=[("Brief campaign", "*.pdf *.png *.jpg *.jpeg *.webp *.bmp *.txt"),
                                                     ("Semua file", "*.*")])
        if not fs_:
            return
        ai_ = next(k for k, v in AI_MODES.items() if v == v_ai.get())
        key_ = v_key.get().strip() if ai_ == "claude" else ""
        v_camp_info.set("Menganalisa brief campaign" + (" dengan Claude AI..." if key_ else " (AI lokal, offline)..."))

        def work():
            try:
                nm_, _p = campaign_analyze(list(fs_), key_, lambda m_: q.put(("log", m_)))
                root.after(0, lambda: (camp_refresh(), v_camp.set(nm_), camp_edit(),
                                       camp_videos() if _p.get("video_suggestions") else None))
            except Exception as ex:
                root.after(0, lambda: v_camp_info.set(f"Gagal membaca brief: {str(ex)[:150]}"))
        threading.Thread(target=work, daemon=True).start()

    def camp_edit():
        p_ = campaign_load(v_camp.get())
        if not p_:
            messagebox.showinfo("Campaign", "Pilih campaign dulu, atau klik 'Upload brief'.")
            return
        win = tk.Toplevel(root)
        win.title(f"Campaign: {p_.get('name')}")
        win.geometry(f"860x{min(780, root.winfo_screenheight() - 120)}")
        cv = tk.Canvas(win, highlightthickness=0)
        sb = ttk.Scrollbar(win, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        bot = ttk.Frame(win, padding=8)
        bot.pack(side="bottom", fill="x")
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        fr = ttk.Frame(cv, padding=10)
        wid = cv.create_window(0, 0, window=fr, anchor="nw")
        cv.bind("<Configure>", lambda e_: cv.itemconfigure(wid, width=e_.width))
        fr.bind("<Configure>", lambda _e: cv.configure(scrollregion=cv.bbox("all")))
        win.bind("<MouseWheel>", lambda e_: None if isinstance(e_.widget, tk.Text)
                 else cv.yview_scroll(int(-e_.delta / 120) or 1, "units"))
        fr.columnconfigure(1, weight=1)
        ttk.Label(fr, text=f"Dianalisa oleh: {p_.get('analyzed_by') or '-'}  ·  periksa & perbaiki bila perlu, lalu Simpan. "
                           "Semua aturan ini otomatis dipakai saat memproses video.", foreground="#06c", wraplength=780
                  ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        get = {}
        row = [1]

        def line(label, key, kind="str", values=None, h=2, hint=""):
            r_ = row[0]
            ttk.Label(fr, text=label).grid(row=r_, column=0, sticky="nw", pady=3)
            v0 = p_.get(key)
            if kind in ("text", "lines"):
                w_ = tk.Text(fr, height=h, wrap="word", font=("Segoe UI", 9))
                w_.insert("1.0", "\n".join(v0) if isinstance(v0, list) else str(v0 or ""))
                w_.grid(row=r_, column=1, sticky="ew", padx=6, pady=3)
                get[key] = ((lambda w=w_: [x.strip() for x in w.get("1.0", "end").splitlines() if x.strip()])
                            if kind == "lines" else (lambda w=w_: w.get("1.0", "end").strip()))
            elif kind == "bool":
                bv = tk.BooleanVar(value=bool(v0))
                ttk.Checkbutton(fr, text=hint, variable=bv).grid(row=r_, column=1, sticky="w", padx=6)
                get[key] = bv.get
                hint = ""
            elif kind == "choice":
                sv = tk.StringVar(value=str(v0 or ""))
                ttk.Combobox(fr, textvariable=sv, values=values, state="readonly", width=34
                             ).grid(row=r_, column=1, sticky="w", padx=6)
                get[key] = sv.get
            else:
                sv = tk.StringVar(value=(" ".join(v0) if kind == "words" else ", ".join(v0) if kind == "list"
                                         else str(v0 if v0 not in (None, 0) or kind != "int" else "")))
                ttk.Entry(fr, textvariable=sv).grid(row=r_, column=1, sticky="ew", padx=6, pady=3)
                if kind == "int":
                    get[key] = lambda sv=sv: int(float(sv.get() or 0)) if re.fullmatch(r"\s*\d+(\.\d+)?\s*", sv.get() or "0") else 0
                elif kind == "words":
                    get[key] = lambda sv=sv: [x for x in re.split(r"[\s,]+", sv.get()) if x]
                elif kind == "list":
                    get[key] = lambda sv=sv: [x.strip() for x in sv.get().split(",") if x.strip()]
                else:
                    get[key] = sv.get
                if key == "logo_file":
                    def pick_(sv=sv):
                        f_ = filedialog.askopenfilename(parent=win, title="Pilih logo campaign (PNG transparan)",
                                                        filetypes=[("Gambar", "*.png *.jpg *.jpeg *.webp")])
                        if f_:
                            sv.set(f_)
                    bf_ = ttk.Frame(fr)
                    bf_.grid(row=r_, column=2, sticky="w")
                    ttk.Button(bf_, text="Pilih...", command=pick_).pack(side="left")
                    ttk.Button(bf_, text="🌐 Cari di internet", command=lambda sv=sv: logo_web_win(sv)
                               ).pack(side="left", padx=(4, 0))
            if hint:
                ttk.Label(fr, text=hint, foreground="#777").grid(row=r_, column=2, sticky="w")
            row[0] += 1
        def logo_web_win(sv):
            lw = tk.Toplevel(win)
            lw.title("Cari logo campaign di internet")
            lw.geometry("880x600")
            top_ = ttk.Frame(lw, padding=8)
            top_.pack(fill="x")
            vb = tk.StringVar(value=(get["brand"]() if "brand" in get else p_.get("brand", "")))
            vd = tk.StringVar(value=", ".join(campaign_domains(p_)))
            ttk.Label(top_, text="Nama brand").pack(side="left")
            ttk.Entry(top_, textvariable=vb, width=24).pack(side="left", padx=4)
            ttk.Label(top_, text="Situs resmi (opsional)").pack(side="left", padx=(8, 0))
            ttk.Entry(top_, textvariable=vd, width=26).pack(side="left", padx=4)
            st2 = tk.StringVar(value="")
            ttk.Label(lw, textvariable=st2, foreground="#06c", padding=(8, 0)).pack(anchor="w")
            grid_ = ttk.Frame(lw, padding=8)
            grid_.pack(fill="both", expand=True)
            thumbs = []

            def use(r_):
                dst = os.path.join(p_["_dir"], "logo.png")
                shutil.copy2(r_["path"], dst)
                sv.set(dst)
                lw.destroy()

            def show(res):
                from PIL import Image, ImageTk
                for c_ in grid_.winfo_children():
                    c_.destroy()
                thumbs.clear()
                for i_, r_ in enumerate(res):
                    im = Image.open(r_["path"]).convert("RGBA")
                    im.thumbnail((180, 105))
                    bg = Image.new("RGBA", (190, 115), (205, 205, 205, 255))
                    bg.alpha_composite(im, ((190 - im.width) // 2, (115 - im.height) // 2))
                    ph = ImageTk.PhotoImage(bg)
                    thumbs.append(ph)
                    cell = ttk.Frame(grid_, padding=4, relief="groove")
                    cell.grid(row=i_ // 4, column=i_ % 4, padx=4, pady=4, sticky="n")
                    ttk.Label(cell, image=ph).pack()
                    ttk.Label(cell, text=r_["source"][:60], foreground="#555", wraplength=190).pack()
                    ttk.Button(cell, text="Pakai logo ini", command=lambda r_=r_: use(r_)).pack(pady=(2, 0))
                st2.set(f"{len(res)} kandidat (paling cocok di kiri atas; latar abu-abu = transparan). Klik 'Pakai logo ini'."
                        if res else "Logo tidak ditemukan. Ubah nama brand atau isi situs resminya (mis. redbull.com), "
                                    "lalu klik Cari.")

            def search():
                st2.set("Mencari logo di situs brand, Wikipedia & Wikimedia Commons...")
                doms = [re.sub(r"^https?://(www\.)?|/.*$", "", d.strip().lower()) for d in vd.get().split(",") if d.strip()]
                brand_ = vb.get().strip()

                def work():
                    try:
                        res = (logo_from_links(p_.get("logo_links", []), os.path.join(p_["_dir"], "_logo_brief"),
                                               lambda _m: None)
                               + logo_search(brand_, doms, os.path.join(p_["_dir"], "_logo_web"), lambda _m: None))
                    except Exception:
                        res = []
                    lw.after(0, lambda: show(res))
                threading.Thread(target=work, daemon=True).start()
            ttk.Button(top_, text="Cari", command=search).pack(side="left", padx=6)
            search()
        line("Nama campaign", "name")
        line("Brand / klien", "brand", hint="pemilik logo")
        line("Topik / game", "topic", hint="isi konten, mis. GTA VI")
        line("Platform", "platforms", "list", hint="pisah koma")
        line("Durasi min (dtk)", "min_sec", "int", hint="0 = bebas")
        line("Durasi maks (dtk)", "max_sec", "int", hint="0 = bebas")
        line("Format video", "aspect", "choice", ["", "9:16", "1:1", "4:5", "16:9"])
        line("Fokus momen", "focus", "choice", list(FOCUS))
        line("Momen yang diambil", "content_brief", "text", h=3)
        line("Logo file", "logo_file")
        line("Posisi logo", "logo_pos", "choice", [""] + LOGO_POS)
        line("Logo", "logo_required", "bool", hint="logo wajib tampil di video")
        line("Logo awal/akhir", "brand_intro", "bool", hint="logo besar + tulisan di awal & akhir video")
        line("Tulisan di video", "overlay_text", hint="tampil sepanjang klip")
        line("Posisi tulisan", "overlay_pos", "choice", ["atas", "bawah"])
        line("Teks watermark", "watermark_text", hint="mis. @akun")
        line("Kata wajib judul", "title_must", "list", hint="pisah koma")
        line("Hashtag wajib", "hashtags", "words", hint="#a #b")
        line("Tag akun", "mentions", "words", hint="@a @b")
        line("Link di deskripsi", "links", "lines", h=2)
        line("Teks wajib deskripsi", "desc_must", "text", h=2)
        line("Ajakan (CTA)", "cta")
        line("Label iklan", "disclosure", hint="mis. #ad")
        line("Bahasa judul/desk.", "language", "choice", ["id", "en", "ms", "es", "pt", "ar", "hi", "ja", "ko"])
        line("Subtitle", "subtitles", "bool", hint="wajib ada subtitle")
        line("Intro/outro", "no_intro", "bool", hint="DILARANG pakai pembuka/penutup")
        line("Larangan", "banned", "lines", h=4)
        line("Syarat lain", "rules", "lines", h=4)
        line("Bayaran", "payout")
        line("Batas waktu", "deadline")
        line("Video sumber", "source_links", "lines", h=3)
        st_ = tk.StringVar()

        def save_(close=True):
            for k_, g_ in get.items():
                try:
                    p_[k_] = g_()
                except Exception:
                    pass
            p_["hashtags"] = _uniq("#" + h.lstrip("#") for h in p_["hashtags"])
            p_["mentions"] = _uniq("@" + h.lstrip("@") for h in p_["mentions"])
            campaign_save(p_)
            camp_refresh()
            st_.set("Tersimpan.")
            if close:
                win.destroy()

        def add_src():
            save_(False)
            if p_.get("source_links"):
                add_lines(p_["source_links"])
                st_.set(f"{len(p_['source_links'])} link video sumber ditambahkan ke daftar video.")
            else:
                st_.set("Brief tidak berisi link video sumber.")
        ttk.Button(bot, text="Simpan", command=save_).pack(side="left")
        ttk.Button(bot, text="Tambahkan video sumber ke daftar", command=add_src).pack(side="left", padx=6)
        ttk.Button(bot, text="🔎 Cari video sesuai campaign", command=lambda: (save_(False), camp_videos(p_))
                   ).pack(side="left")
        ttk.Button(bot, text="Buka folder campaign", command=lambda: os.startfile(p_["_dir"])).pack(side="left")
        ttk.Button(bot, text="Tutup", command=win.destroy).pack(side="left", padx=6)
        ttk.Label(bot, textvariable=st_, foreground="#06c").pack(side="left", padx=6)

    def camp_videos(p_=None):
        p_ = p_ or campaign_load(v_camp.get())
        if not p_:
            messagebox.showinfo("Campaign", "Pilih campaign dulu, atau klik 'Upload brief'.")
            return
        win = tk.Toplevel(root)
        win.title(f"Video sesuai campaign: {p_.get('name')}")
        win.geometry("1180x470")
        top_ = ttk.Frame(win, padding=8)
        top_.pack(fill="x")
        vp = tk.StringVar(value="Bulan ini")
        ttk.Label(top_, text="Periode populer").pack(side="left")
        ttk.Combobox(top_, textvariable=vp, values=list(VIRAL_PERIOD), state="readonly", width=10).pack(side="left", padx=4)
        st = tk.StringVar()
        cols_ = ("Sumber", "Penonton", "Durasi", "Judul", "Channel", "Link")
        tv_ = ttk.Treeview(win, columns=cols_, show="headings", height=12)
        for c_, w_ in zip(cols_, (170, 70, 60, 400, 150, 320)):
            tv_.heading(c_, text=c_)
            tv_.column(c_, width=w_, anchor="w")
        tv_.tag_configure("done", foreground="#999")
        tv_.pack(fill="both", expand=True, padx=8)
        urls = {}

        def show(vids):
            tv_.delete(*tv_.get_children())
            urls.clear()
            for v in vids:
                iid = tv_.insert("", "end", tags=("done",) if v.get("done") else (),
                                 values=(("✓ sudah diunduh · " if v.get("done") else "") + v.get("source", ""),
                                         fmt_views(v.get("views") or 0) if v.get("views") else "-",
                                         fmt_dur(v["sec"]) if v.get("sec") else "-", v.get("title", ""),
                                         v.get("channel", ""), v["url"]))
                urls[iid] = v["url"]
            st.set(f"{len(vids)} video cocok (link brief & channel brief paling atas). Pilih lalu 'Tambahkan' "
                   "(Ctrl+klik untuk beberapa)." if vids else
                   "Belum ada video yang cocok. Isi 'Momen yang diambil' / 'Video sumber' di Lihat / Edit aturan.")

        def search():
            st.set("Mencari video sumber yang sesuai campaign...")

            def work():
                try:
                    vids = campaign_find_videos(p_, 15, vp.get(), lambda m_: q.put(("log", m_)))
                    p_["video_suggestions"] = vids
                    campaign_save(p_)
                except Exception as ex:
                    vids = []
                    win.after(0, lambda: st.set(f"Gagal mencari: {str(ex)[:120]}"))
                win.after(0, lambda: show(vids))
            threading.Thread(target=work, daemon=True).start()

        def add_sel(all_=False):
            ids = tv_.get_children() if all_ else tv_.selection()
            if ids:
                add_lines([urls[i] for i in ids if i in urls])
                st.set(f"{len(ids)} link ditambahkan ke daftar video. Campaign ini otomatis dipakai saat Proses.")

        def copy_links():
            ids = tv_.selection() or tv_.get_children()
            links = [urls[i] for i in ids if i in urls]
            if links:
                win.clipboard_clear()
                win.clipboard_append("\n".join(links))
                st.set(f"{len(links)} link disalin.")

        def open_sel(_e=None):
            import webbrowser
            for i in tv_.selection():
                webbrowser.open(urls[i])
        tv_.bind("<Double-1>", open_sel)
        ttk.Button(top_, text="🔎 Cari ulang", command=search).pack(side="left", padx=8)
        ttk.Label(top_, text="Sumber: link & channel di brief, lalu YouTube (brand + isi brief + jenis momen). "
                             "Video abu-abu = sudah pernah diunduh.", foreground="#777").pack(side="left")
        bot_ = ttk.Frame(win, padding=8)
        bot_.pack(fill="x")
        ttk.Button(bot_, text="Tambahkan yang dipilih", command=add_sel).pack(side="left")
        ttk.Button(bot_, text="Tambahkan semua", command=lambda: add_sel(True)).pack(side="left", padx=4)
        ttk.Button(bot_, text="Salin link", command=copy_links).pack(side="left", padx=4)
        ttk.Button(bot_, text="Buka di browser", command=open_sel).pack(side="left", padx=4)
        ttk.Label(bot_, textvariable=st, foreground="#06c").pack(side="left", padx=8)
        if p_.get("video_suggestions"):
            show(p_["video_suggestions"])
        search()  # selalu perbarui (video baru / urutan terbaru) di latar belakang

    def camp_delete():
        p_ = campaign_load(v_camp.get())
        if p_ and messagebox.askyesno("Hapus campaign", f"Hapus campaign '{p_.get('name')}' beserta file brief-nya?"):
            shutil.rmtree(p_["_dir"], ignore_errors=True)
            v_camp.set(NOCAMP)
    ttk.Button(boxc, text="📄 Upload brief (PDF / foto)...", command=camp_upload).grid(row=0, column=2, padx=(0, 4))
    ttk.Button(boxc, text="Lihat / Edit aturan", command=camp_edit).grid(row=0, column=3, padx=4)
    ttk.Button(boxc, text="🔎 Video sesuai campaign", command=camp_videos).grid(row=0, column=4, padx=4)
    ttk.Button(boxc, text="Hapus", command=camp_delete).grid(row=0, column=5, padx=4)
    ttk.Label(boxc, textvariable=v_camp_info, foreground="#06c", wraplength=900, justify="left"
              ).grid(row=1, column=0, columnspan=7, sticky="w", pady=(6, 0))
    camp_refresh()

    r += 1
    box0 = ttk.LabelFrame(frm, text="Perangkat & kecepatan", padding=8)
    box0.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(8, 3))
    ttk.Label(box0, text="Terdeteksi: " + hw_summary(), foreground="#06c").grid(row=0, column=0, columnspan=6, sticky="w")
    ttk.Label(box0, text="Profil").grid(row=1, column=0, sticky="w", pady=(6, 0))
    ttk.Combobox(box0, textvariable=v_prof, values=list(PROFILES.values()), state="readonly", width=38
                 ).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
    ttk.Label(box0, text="Model subtitle").grid(row=1, column=2, sticky="w", padx=(12, 0), pady=(6, 0))
    ttk.Combobox(box0, textvariable=v_smodel, values=SUB_MODELS, state="readonly", width=14
                 ).grid(row=1, column=3, sticky="w", padx=6, pady=(6, 0))
    ttk.Label(box0, text="Bahasa").grid(row=1, column=4, sticky="w", padx=(12, 0), pady=(6, 0))
    ttk.Combobox(box0, textvariable=v_lang, values=["auto", "id", "en", "jv", "su", "ms"], state="readonly", width=6
                 ).grid(row=1, column=5, sticky="w", padx=6, pady=(6, 0))

    r += 1
    box = ttk.LabelFrame(frm, text="AI pemilih momen", padding=8)
    box.grid(row=r, column=0, columnspan=3, sticky="ew", pady=3)
    box.columnconfigure(5, weight=1)
    ttk.Label(box, text="Genre").grid(row=4, column=0, sticky="w", pady=(6, 0))
    cbg = ttk.Combobox(box, textvariable=v_genre, values=[g["label"] for g in GENRES.values()], state="readonly", width=30)
    cbg.grid(row=4, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 0))
    ttk.Label(box, text="(menentukan kamera, durasi, cara AI menilai & warna subtitle)", foreground="#777"
              ).grid(row=4, column=4, columnspan=2, sticky="w", pady=(6, 0))
    ttk.Label(box, text="Fokus momen").grid(row=5, column=0, sticky="w", pady=(6, 0))
    ttk.Combobox(box, textvariable=v_focus, values=[v[0] for v in FOCUS.values()], state="readonly", width=34
                 ).grid(row=5, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 0))
    ttk.Label(box, text="(mis. Action: AI hanya mengambil pertempuran, tembak-tembakan, ledakan, kejar-kejaran)",
              foreground="#777").grid(row=5, column=4, columnspan=2, sticky="w", pady=(6, 0))

    def on_genre(_e=None):
        g = next((g for g in GENRES.values() if g["label"] == v_genre.get()), None)
        if g and g["dur"]:
            v_dur.set(round(g["dur"] / 60, 2))
            v_look.set(LOOKS[g["look"]])
    cbg.bind("<<ComboboxSelected>>", on_genre)
    ttk.Label(box, text="Penilai").grid(row=0, column=0, sticky="w")
    ttk.Combobox(box, textvariable=v_ai, values=list(AI_MODES.values()), state="readonly", width=26
                 ).grid(row=0, column=1, columnspan=2, sticky="w", padx=6)
    ttk.Label(box, text="API key Claude").grid(row=0, column=3, sticky="w", padx=(12, 0))
    ttk.Entry(box, textvariable=v_key, show="•", width=34).grid(row=0, column=4, columnspan=2, sticky="w", padx=6)
    ttk.Label(box, text="Gaya edit").grid(row=1, column=0, sticky="w", pady=(6, 0))
    ttk.Combobox(box, textvariable=v_style, values=list(STYLES.values()), state="readonly", width=26
                 ).grid(row=1, column=1, columnspan=2, sticky="w", padx=6, pady=(6, 0))
    ttk.Label(box, text="Seleksi").grid(row=1, column=3, sticky="w", padx=(12, 0), pady=(6, 0))
    ttk.Combobox(box, textvariable=v_strict, values=list(STRICT), state="readonly", width=9
                 ).grid(row=1, column=4, sticky="w", padx=6, pady=(6, 0))
    ttk.Label(box, text="Maks. klip").grid(row=2, column=0, sticky="w", pady=(6, 0))
    ttk.Spinbox(box, from_=1, to=30, textvariable=v_n, width=5).grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
    ttk.Label(box, text="Durasi klip (menit)").grid(row=2, column=2, sticky="w", pady=(6, 0))
    dfr = ttk.Frame(box)
    dfr.grid(row=2, column=3, columnspan=2, sticky="w", padx=(12, 0), pady=(6, 0))
    ttk.Spinbox(dfr, from_=0.25, to=10, increment=0.25, textvariable=v_dur, width=5, format="%.2f").pack(side="left")
    v_durlbl = tk.StringVar()

    def upd_dur(*_a):
        try:
            sec = float(v_dur.get()) * 60
            v_durlbl.set(f"= {int(sec // 60)} mnt {int(sec % 60):02d} dtk" + ("  (lebih dari 3 mnt: bukan Shorts)" if sec > 180 else ""))
        except (tk.TclError, ValueError):
            v_durlbl.set("")
    v_dur.trace_add("write", upd_dur)
    upd_dur()
    ttk.Label(dfr, textvariable=v_durlbl, foreground="#777").pack(side="left", padx=6)
    ttk.Label(box, text="Cookies").grid(row=3, column=0, sticky="w", pady=(6, 0))
    ttk.Combobox(box, textvariable=v_cookies, values=["tanpa", "chrome", "edge", "firefox"], state="readonly", width=9
                 ).grid(row=3, column=1, sticky="w", padx=6, pady=(6, 0))
    sfr_ = ttk.Frame(box)
    sfr_.grid(row=3, column=2, columnspan=3, sticky="w", pady=(6, 0))
    cb = ttk.Checkbutton(sfr_, text="Subtitle + transkrip ucapan", variable=v_sub)
    cb.pack(side="left")

    def edit_fix():
        fix_map()  # buat file bawaan bila belum ada
        try:
            os.startfile(FIX_FILE)
        except Exception as ex:
            messagebox.showinfo("Koreksi kata", f"Buka & edit file ini:\n{FIX_FILE}\n\n({ex})")
    ttk.Button(sfr_, text="✏️ Koreksi kata (salah = benar)...", command=edit_fix).pack(side="left", padx=8)
    if not has_whisper():
        cb.state(["disabled"])

    r += 1
    box2 = ttk.LabelFrame(frm, text="Edit kreatif", padding=8)
    box2.grid(row=r, column=0, columnspan=3, sticky="ew", pady=3)
    box2.columnconfigure(1, weight=1)
    ttk.Label(box2, text="Format").grid(row=0, column=0, sticky="w")
    f0 = ttk.Frame(box2)
    f0.grid(row=0, column=1, columnspan=3, sticky="w")
    ttk.Combobox(f0, textvariable=v_fmt, values=list(FORMATS.values()), state="readonly", width=40).pack(side="left", padx=6)
    ttk.Combobox(f0, textvariable=v_look, values=list(LOOKS.values()), state="readonly", width=32).pack(side="left", padx=6)
    ttk.Checkbutton(f0, text="Teks di atas video", variable=v_top).pack(side="left", padx=6)
    ttk.Checkbutton(f0, text="Hapus jeda", variable=v_jc).pack(side="left", padx=6)
    ttk.Checkbutton(f0, text="Efek suara", variable=v_sfx).pack(side="left", padx=6)
    ttk.Checkbutton(f0, text="Sound meme", variable=v_meme).pack(side="left", padx=2)
    ttk.Label(box2, text="Nama seri").grid(row=1, column=0, sticky="w", pady=3)
    ttk.Entry(box2, textvariable=v_title).grid(row=1, column=1, sticky="ew", padx=6)
    ttk.Label(box2, text="opsional, mis. GTA VI Part 1 (judul tiap klip dibuat AI otomatis)", foreground="#777"
              ).grid(row=1, column=2, columnspan=2, sticky="w")
    ttk.Label(box2, text="Ajakan akhir").grid(row=2, column=0, sticky="w")
    ttk.Entry(box2, textvariable=v_cta).grid(row=2, column=1, sticky="ew", padx=6)
    vf_ = ttk.Frame(box2)
    vf_.grid(row=2, column=2, columnspan=2, sticky="w")
    ttk.Checkbutton(vf_, text="Pembuka & penutup", variable=v_io).pack(side="left")
    ttk.Label(vf_, text="  Suara AI").pack(side="left")
    ttk.Combobox(vf_, textvariable=v_voice, values=list(VOICE_LABEL.values()), state="readonly", width=14
                 ).pack(side="left", padx=4)
    ttk.Label(vf_, text="  Warna teks").pack(side="left")
    ttk.Combobox(vf_, textvariable=v_tcol, values=[v[0] for v in TEXT_COLORS.values()], state="readonly", width=26
                 ).pack(side="left", padx=4)
    dl_ = ttk.Frame(box2)
    dl_.grid(row=5, column=1, columnspan=3, sticky="w")
    ttk.Combobox(dl_, textvariable=v_dlang, values=list(DESC_LANGS.values()), state="readonly", width=36
                 ).pack(side="left", padx=6)
    ttk.Label(dl_, text="judul & deskripsi diterjemahkan otomatis dari teks Indonesia", foreground="#777").pack(side="left")
    ttk.Label(box2, text="Bahasa lain").grid(row=5, column=0, sticky="w", pady=3)
    ttk.Label(box2, text="Logo watermark").grid(row=3, column=0, sticky="w", pady=3)
    ttk.Entry(box2, textvariable=v_logo).grid(row=3, column=1, sticky="ew", padx=6)
    lf = ttk.Frame(box2)
    lf.grid(row=3, column=2, columnspan=2, sticky="w")

    def pick_logo():
        fp = filedialog.askopenfilename(title="Pilih logo (PNG transparan paling bagus)",
                                        filetypes=[("Gambar", "*.png *.jpg *.jpeg *.webp"), ("Semua file", "*.*")])
        if fp:
            v_logo.set(fp)
    ttk.Button(lf, text="Unggah logo...", command=pick_logo).pack(side="left")
    ttk.Combobox(lf, textvariable=v_logopos, values=LOGO_POS, state="readonly", width=11).pack(side="left", padx=6)
    ttk.Label(box2, text="Teks watermark").grid(row=4, column=0, sticky="w")
    ttk.Entry(box2, textvariable=v_handle).grid(row=4, column=1, sticky="ew", padx=6)
    ttk.Label(box2, text="opsional, mis. @akunanda", foreground="#777").grid(row=4, column=2, sticky="w")
    ttk.Label(box2, text="Facecam\nstreamer").grid(row=7, column=0, sticky="nw", pady=3)
    cf_ = ttk.Frame(box2)
    cf_.grid(row=7, column=1, columnspan=3, sticky="ew", padx=6, pady=3)
    ttk.Label(cf_, text="Taruh di").grid(row=0, column=0, sticky="w")
    ttk.Combobox(cf_, textvariable=v_campos, values=["bawah", "atas"], state="readonly", width=7
                 ).grid(row=0, column=1, sticky="w", padx=4)
    ttk.Label(cf_, text="  Kotak manual (x, y, lebar, tinggi %)").grid(row=0, column=2, sticky="w")
    ttk.Entry(cf_, textvariable=v_cambox, width=20).grid(row=0, column=3, sticky="w", padx=4)

    def cam_preview():
        srcs = [x.strip().strip('"') for x in src_box.get("1.0", "end").splitlines() if x.strip()]
        f_ = next((x for x in srcs if os.path.isfile(x)), None)
        if not f_:
            messagebox.showinfo("AutoClip", "Pilih file video dulu (video dari link ada di folder 'unduhan' setelah diproses sekali).")
            return
        v_stage.set("Mendeteksi kotak facecam...")

        def work():
            try:
                img_, msg_, num_ = preview_facecam(f_, v_cambox.get())

                def show():
                    v_stage.set(msg_)
                    if num_ and not v_cambox.get().strip():
                        v_cambox.set(num_)
                    open_path(img_)
                root.after(0, show)
            except Exception as ex:
                root.after(0, lambda: v_stage.set(f"Deteksi facecam gagal: {str(ex)[:150]}"))
        threading.Thread(target=work, daemon=True).start()
    ttk.Button(cf_, text="Deteksi & lihat", command=cam_preview).grid(row=0, column=4, sticky="w", padx=4)
    ttk.Label(cf_, text="kosong = deteksi otomatis tiap klip  ·  'Deteksi & lihat' menandai kotaknya di gambar & mengisi angkanya",
              foreground="#777").grid(row=1, column=0, columnspan=5, sticky="w")
    ttk.Label(box2, text="Logo pembuka/\npenutup").grid(row=6, column=0, sticky="nw", pady=3)
    bf_ = ttk.Frame(box2)
    bf_.grid(row=6, column=1, columnspan=3, sticky="ew", padx=6, pady=3)
    ttk.Combobox(bf_, textvariable=v_brand, values=BRAND_POS, state="readonly", width=12).grid(row=0, column=0, sticky="w")
    ttk.Label(bf_, text=" Tulisan di bawah logo").grid(row=0, column=1, sticky="w")
    ttk.Entry(bf_, textvariable=v_btext, width=30).grid(row=0, column=2, sticky="w", padx=4)
    ttk.Label(bf_, text=" Logo").grid(row=0, column=3, sticky="w")
    ttk.Entry(bf_, textvariable=v_blogo, width=22).grid(row=0, column=4, sticky="ew", padx=4)

    def pick_blogo():
        fp = filedialog.askopenfilename(title="Pilih logo pembuka/penutup (PNG transparan paling bagus)",
                                        filetypes=[("Gambar", "*.png *.jpg *.jpeg *.webp"), ("Semua file", "*.*")])
        if fp:
            v_blogo.set(fp)
    ttk.Button(bf_, text="Pilih...", command=pick_blogo).grid(row=0, column=5, sticky="w")
    ttk.Label(bf_, text="logo kosong = pakai logo watermark · tulisan kosong = pakai teks watermark",
              foreground="#777").grid(row=1, column=0, columnspan=6, sticky="w")

    r += 1
    btns = ttk.Frame(frm)
    btns.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(8, 0))
    b_run = ttk.Button(btns, text="▶ Proses")
    b_run.pack(side="left")
    b_cancel = ttk.Button(btns, text="■ Batal", state="disabled")
    b_cancel.pack(side="left", padx=6)
    b_watch = ttk.Button(btns, text="Pantau Folder...")
    b_watch.pack(side="left")
    b_open = ttk.Button(btns, text="Buka Folder Hasil", state="disabled")
    b_open.pack(side="right")

    r += 1
    pf = ttk.Frame(frm)
    pf.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(8, 2))
    pf.columnconfigure(0, weight=1)
    pb = ttk.Progressbar(pf, maximum=100)
    pb.grid(row=0, column=0, sticky="ew")
    v_pct = tk.StringVar(value="0%")
    ttk.Label(pf, textvariable=v_pct, font=("Segoe UI", 11, "bold"), width=6, anchor="e").grid(row=0, column=1, padx=(8, 0))
    v_stage = tk.StringVar(value="Siap.")
    ttk.Label(frm, textvariable=v_stage, foreground="#333").grid(row=r + 1, column=0, columnspan=3, sticky="w")

    r += 2
    nb = ttk.Notebook(frm)
    nb.grid(row=r, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
    frm.rowconfigure(r, weight=1)
    tab_res = ttk.Frame(nb)
    tab_log = ttk.Frame(nb)
    nb.add(tab_res, text="Hasil (diurutkan AI)")
    tab_meta = ttk.Frame(nb, padding=6)
    nb.add(tab_meta, text="📋 Judul & Deskripsi")
    nb.add(tab_log, text="Log proses")
    cols = ("rank", "skor", "judul", "durasi", "kategori", "alasan")
    tv = ttk.Treeview(tab_res, columns=cols, show="headings", height=8)
    for c, t, w in zip(cols, ["#", "Skor", "Judul", "Durasi", "Kategori", "Kenapa bagus (penilaian AI)"],
                       [30, 50, 260, 60, 90, 420]):
        tv.heading(c, text=t)
        tv.column(c, width=w, anchor="center" if c in ("rank", "skor", "durasi") else "w", stretch=c in ("judul", "alasan"))
    tv.pack(fill="both", expand=True, side="left")
    sb = ttk.Scrollbar(tab_res, orient="vertical", command=tv.yview)
    sb.pack(side="right", fill="y")
    tv.configure(yscrollcommand=sb.set)
    rb = ttk.Frame(frm)
    rb.grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=(4, 0))
    clip_info = {}

    def sel_clip():
        sel = tv.selection()
        return clip_info.get(sel[0]) if sel else None

    def open_path(fp):
        if fp and os.path.exists(fp):
            os.startfile(fp) if os.name == "nt" else subprocess.Popen(["xdg-open", fp])

    def copy(key, label):
        c = sel_clip()
        if not c:
            messagebox.showinfo("AutoClip", "Pilih salah satu klip di tabel Hasil dulu.")
            return
        root.clipboard_clear()
        root.clipboard_append(c[1].get(key, ""))
        v_stage.set(f"{label} disalin - tinggal tempel di YouTube Studio.")
    ttk.Button(rb, text="▶ Putar klip", command=lambda: (c := sel_clip()) and open_path(os.path.join(c[0], c[1]["file"]))
               ).pack(side="left")
    ttk.Button(rb, text="Lihat thumbnail", command=lambda: (c := sel_clip()) and open_path(os.path.join(c[0], c[1].get("thumbnail", ""))
               )).pack(side="left", padx=4)
    ttk.Button(rb, text="Salin judul", command=lambda: copy("yt_title", "Judul")).pack(side="left", padx=4)
    ttk.Button(rb, text="Salin deskripsi", command=lambda: copy("description", "Deskripsi")).pack(side="left", padx=4)
    ttk.Button(rb, text="Salin bahasa lain", command=lambda: copy("translations", "Judul & deskripsi bahasa lain")
               ).pack(side="left", padx=4)
    ttk.Button(rb, text="📋 Judul & deskripsi lengkap", command=lambda: (fill_meta(), nb.select(tab_meta))
               ).pack(side="left", padx=4)
    ttk.Label(rb, text="  (klik dua kali untuk memutar)", foreground="#777").pack(side="left")
    # ---------------- tab Judul & Deskripsi: semua teks terlihat, bisa diedit, 1 klik salin
    tab_meta.columnconfigure(1, weight=1)
    v_mclip = tk.StringVar(value="Pilih klip di tab Hasil - judul & deskripsinya muncul di sini.")
    ttk.Label(tab_meta, textvariable=v_mclip, font=("Segoe UI", 9, "bold"), foreground="#06c"
              ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
    meta_w = {}

    def copy_text(key, label):
        w_ = meta_w[key]
        txt = (w_.get() if isinstance(w_, ttk.Entry) else w_.get("1.0", "end")).strip()
        if not txt:
            v_stage.set("Belum ada teks - pilih klip di tab Hasil dulu.")
            return
        root.clipboard_clear()
        root.clipboard_append(txt)
        v_stage.set(f"✔ {label} disalin - tempel (Ctrl+V) di YouTube Studio / TikTok / Instagram.")

    v_tcount = tk.StringVar()
    for row_, (key, label, h_) in enumerate([("yt_title", "Judul", 0), ("description", "Deskripsi", 6),
                                             ("tags", "Tag", 0), ("translations", "Bahasa lain", 5)], start=1):
        ttk.Label(tab_meta, text=label).grid(row=row_, column=0, sticky="nw", pady=3, padx=(0, 6))
        if h_:
            fr_ = ttk.Frame(tab_meta)
            fr_.grid(row=row_, column=1, sticky="nsew", pady=3)
            w_ = tk.Text(fr_, height=h_, wrap="word", font=("Segoe UI", 9), undo=True)
            sb_ = ttk.Scrollbar(fr_, orient="vertical", command=w_.yview)
            w_.configure(yscrollcommand=sb_.set)
            w_.pack(side="left", fill="both", expand=True)
            sb_.pack(side="right", fill="y")
            tab_meta.rowconfigure(row_, weight=1)
        else:
            w_ = ttk.Entry(tab_meta, font=("Segoe UI", 10))
            w_.grid(row=row_, column=1, sticky="ew", pady=3)
        meta_w[key] = w_
        ttk.Button(tab_meta, text=f"Salin {label.lower()}", width=16,
                   command=lambda k=key, l=label: copy_text(k, l)).grid(row=row_, column=2, sticky="n", padx=6, pady=3)
    ttk.Label(tab_meta, textvariable=v_tcount, foreground="#777").grid(row=5, column=1, sticky="w")
    meta_w["yt_title"].bind("<KeyRelease>", lambda _e: v_tcount.set(f"Judul: {len(meta_w['yt_title'].get())}/100 karakter"))
    mb_ = ttk.Frame(tab_meta)
    mb_.grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def copy_all():
        parts_ = [f"{lbl}:\n{(meta_w[k].get() if isinstance(meta_w[k], ttk.Entry) else meta_w[k].get('1.0', 'end')).strip()}"
                  for k, lbl in (("yt_title", "JUDUL"), ("description", "DESKRIPSI"), ("tags", "TAG"))]
        root.clipboard_clear()
        root.clipboard_append("\n\n".join(parts_))
        v_stage.set("✔ Judul + deskripsi + tag disalin sekaligus.")
    ttk.Button(mb_, text="Salin semuanya", command=copy_all).pack(side="left")
    ttk.Button(mb_, text="Buka file teks (.txt)", command=lambda: (c := sel_clip()) and open_path(
        os.path.join(c[0], c[1].get("youtube_txt", "")))).pack(side="left", padx=6)
    ttk.Button(mb_, text="Lihat thumbnail", command=lambda: (c := sel_clip()) and open_path(
        os.path.join(c[0], c[1].get("thumbnail", "")))).pack(side="left")
    ttk.Label(mb_, text="  Teks bisa diedit dulu sebelum disalin.", foreground="#777").pack(side="left")

    def fill_meta(_e=None):
        c = sel_clip()
        if not c:
            return
        info_ = c[1]
        v_mclip.set(f"Klip #{info_.get('rank', '')}  ·  skor {info_.get('score', '')}  ·  {info_.get('title', '')}")
        for key, w_ in meta_w.items():
            val = info_.get(key, "") or ""
            if key == "tags" and not val:
                val = ", ".join(t.lstrip("#") for t in info_.get("hashtags", []))
            if isinstance(w_, ttk.Entry):
                w_.delete(0, "end")
                w_.insert(0, val)
            else:
                w_.delete("1.0", "end")
                w_.insert("1.0", val or ("(tidak ada - aktifkan 'Bahasa lain' di pengaturan)" if key == "translations" else ""))
        v_tcount.set(f"Judul: {len(info_.get('yt_title', ''))}/100 karakter")
    tv.bind("<<TreeviewSelect>>", fill_meta, add="+")

    logbox = tk.Text(tab_log, height=10, wrap="word", font=("Consolas", 9), state="disabled")
    logbox.pack(fill="both", expand=True)
    clip_files = {}

    def play(_e=None):
        sel = tv.selection()
        if sel and sel[0] in clip_files and os.path.exists(clip_files[sel[0]]):
            os.startfile(clip_files[sel[0]]) if os.name == "nt" else subprocess.Popen(["xdg-open", clip_files[sel[0]]])
    tv.bind("<Double-1>", play)

    def options():
        fmt = next(k for k, v in FORMATS.items() if v == v_fmt.get())
        style = next(k for k, v in STYLES.items() if v == v_style.get())
        prof = next(k for k, v in PROFILES.items() if v == v_prof.get())
        ai = next(k for k, v in AI_MODES.items() if v == v_ai.get())
        return Options(n_clips=max(1, v_n.get()), duration=max(10, round(float(v_dur.get()) * 60)), fmt=fmt, style=style,
                       subtitles=v_sub.get(), profile=prof, sub_model=v_smodel.get(), language=v_lang.get(),
                       ai=ai, api_key=v_key.get().strip(), out_dir=v_out.get().strip() or None,
                       strict=v_strict.get(), creative=v_cre.get(), jumpcut=v_jc.get(), sfx=v_sfx.get(),
                       look=next(k for k, v in LOOKS.items() if v == v_look.get()),
                       genre=next((k for k, g in GENRES.items() if g["label"] == v_genre.get()), "otomatis"),
                       title=v_title.get(), handle=v_handle.get(), cta=v_cta.get(), logo=v_logo.get().strip(),
                       logo_pos=v_logopos.get(), top_text=v_top.get(), intro=v_io.get(), outro=v_io.get(),
                       brand=v_brand.get(), brand_logo=v_blogo.get().strip(), brand_text=v_btext.get().strip(),
                       cam_pos=v_campos.get(), cam_box=v_cambox.get().strip(),
                       focus=next((k for k, v in FOCUS.items() if v[0] == v_focus.get()), "otomatis"),
                       voice=next((k for k, v in VOICE_LABEL.items() if v == v_voice.get()), "pria"),
                       text_color=next((k for k, v in TEXT_COLORS.items() if v[0] == v_tcol.get()), "otomatis"),
                       meme_sfx=v_meme.get(), campaign="" if v_camp.get() == NOCAMP else v_camp.get(),
                       desc_langs=next((k for k, v in DESC_LANGS.items() if v == v_dlang.get()), "en"),
                       cookies="" if v_cookies.get() == "tanpa" else v_cookies.get())

    def remember():
        save_config(dict(n_clips=v_n.get(), duration=max(10, round(float(v_dur.get()) * 60)), fmt=v_fmt.get(), style=v_style.get(),
                         subtitles=v_sub.get(), profile=v_prof.get(), sub_model=v_smodel.get(), language=v_lang.get(),
                         ai=v_ai.get(), api_key=v_key.get().strip(), strict=v_strict.get(), creative=v_cre.get(),
                         jumpcut=v_jc.get(), sfx=v_sfx.get(), handle=v_handle.get(), cta=v_cta.get(), look=v_look.get(), genre=v_genre.get(),
                         logo=v_logo.get().strip(), logo_pos=v_logopos.get(), top_text=v_top.get(),
                         brand=v_brand.get(), brand_logo=v_blogo.get().strip(), brand_text=v_btext.get().strip(),
                       cam_pos=v_campos.get(), cam_box=v_cambox.get().strip(),
                       focus=v_focus.get(),
                         intro=v_io.get(), voice=v_voice.get(), compile=v_comp.get(),
                         text_color=v_tcol.get(), meme_sfx=v_meme.get(), desc_langs=v_dlang.get(),
                         campaign=v_camp.get(),
                         cookies=v_cookies.get()))

    def log(msg):
        q.put(("log", msg))

    def prog(p, stage=""):
        q.put(("prog", (p, stage)))

    def start():
        if busy["job"]:
            return
        srcs = [x.strip().strip('"') for x in src_box.get("1.0", "end").splitlines() if x.strip()]
        bad = [x for x in srcs if not (is_url(x) or os.path.isfile(x))]
        if not srcs:
            messagebox.showwarning("AutoClip", "Tempel link video atau pilih file video terlebih dahulu.")
            return
        if bad:
            messagebox.showwarning("AutoClip", "Tidak ditemukan / bukan link:\n" + "\n".join(bad[:5]))
            return
        compile_ = v_comp.get() and len(srcs) > 1
        opt = options()
        if opt.ai == "claude" and not (opt.api_key or os.environ.get("ANTHROPIC_API_KEY")):
            if not messagebox.askyesno("AutoClip", "API key Claude belum diisi. Lanjut dengan AI lokal?"):
                return
            opt.ai = "lokal"
        remember()
        busy.update(job=True, t0=time.time())
        scroll_bottom()  # tampilkan progres
        b_run.state(["disabled"])
        b_cancel.state(["!disabled"])
        pb["value"] = 0
        for it in tv.get_children():
            tv.delete(it)
        nb.select(tab_log)

        def job():
            try:
                results, comp = process_many(srcs, opt, log, prog, compile_)
                q.put(("done", (results, comp)))
            except Cancelled:
                q.put(("cancel", None))
            except Exception as ex:
                q.put(("error", str(ex)))
        threading.Thread(target=job, daemon=True).start()

    def cancel():
        CANCEL.set()
        v_stage.set("Membatalkan...")

    def toggle_watch():
        if watch_stop["ev"]:
            watch_stop["ev"].set()
            watch_stop["ev"] = None
            b_watch.config(text="Pantau Folder...")
            return
        d = filedialog.askdirectory(title="Pilih folder yang akan dipantau")
        if not d:
            return
        ev_ = threading.Event()
        watch_stop["ev"] = ev_
        b_watch.config(text="Stop Pantau")
        last_out["dir"] = os.path.join(d, "hasil_klip")
        b_open.state(["!disabled"])
        threading.Thread(target=watch_folder, args=(d, options(), log, ev_), daemon=True).start()

    def open_out():
        d = last_out["dir"]
        if d and os.path.isdir(d):
            os.startfile(d) if os.name == "nt" else subprocess.Popen(["xdg-open", d])

    b_run.config(command=start)
    b_cancel.config(command=cancel)
    b_watch.config(command=toggle_watch)
    b_open.config(command=open_out)
    eta = {"v": None}

    def finish():
        busy["job"] = False
        b_run.state(["!disabled"])
        b_cancel.state(["disabled"])

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    logbox.config(state="normal")
                    logbox.insert("end", val + "\n")
                    logbox.see("end")
                    logbox.config(state="disabled")
                elif kind == "prog":
                    p, stage = val
                    pb["value"] = 100 * p
                    v_pct.set(f"{100 * p:.0f}%")
                    el = time.time() - busy["t0"]
                    if p > 0.02 and p < 1:
                        new = el * (1 - p) / p
                        eta["v"] = new if eta["v"] is None else 0.8 * eta["v"] + 0.2 * new
                        v_stage.set(f"{stage}   ·   berjalan {fmt_dur(el)}   ·   sisa ±{fmt_dur(eta['v'])}")
                    else:
                        v_stage.set(f"{stage}   ·   berjalan {fmt_dur(el)}")
                elif kind == "done":
                    finish()
                    results, comp = val
                    total = 0
                    if comp:
                        iid = tv.insert("", "end", values=("★", comp["score"], "KOMPILASI: " + comp["title"],
                                                           fmt_dur(comp["duration"]), comp["category"], comp["reason"]))
                        clip_files[iid] = os.path.join(comp["out"], comp["file"])
                        clip_info[iid] = (comp["out"], comp)
                        last_out["dir"] = comp["out"]
                    for vi, (out, clips) in enumerate(results, 1):
                        last_out["dir"] = last_out["dir"] if comp else out
                        for c in clips:
                            rank = f"{vi}.{c['rank']}" if len(results) > 1 else c["rank"]
                            iid = tv.insert("", "end", values=(rank, c["score"], c["title"], fmt_dur(c["duration"]),
                                                               c.get("category", ""), c.get("reason", "")))
                            clip_files[iid] = os.path.join(out, c["file"])
                            clip_info[iid] = (out, c)
                            total += 1
                    b_open.state(["!disabled"])
                    nb.select(tab_res)
                    if tv.get_children():
                        tv.selection_set(tv.get_children()[0])
                        tv.focus(tv.get_children()[0])
                    scroll_bottom()  # hasil langsung terlihat
                    v_stage.set(f"Selesai: {total} klip dari {len(results)} video"
                                f"{' + 1 kompilasi' if comp else ''} dalam {fmt_dur(time.time() - busy['t0'])}")
                    eta["v"] = None
                elif kind == "cancel":
                    finish()
                    v_stage.set("Dibatalkan.")
                    log("Dibatalkan oleh pengguna.")
                    eta["v"] = None
                elif kind == "error":
                    finish()
                    log("ERROR: " + val)
                    v_stage.set("Gagal - lihat log.")
                    messagebox.showerror("AutoClip", val[:800])
                    eta["v"] = None
        except queue.Empty:
            pass
        root.after(150, poll)

    poll()
    root.mainloop()


# ============================================================================ CLI

def main():
    threading.stack_size(64 * 1024 * 1024)  # Whisper butuh stack besar di Windows
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(errors="replace")
        except Exception:
            pass
    if len(sys.argv) == 1:
        return run_gui()
    ap = argparse.ArgumentParser(description="AutoClip - klip video otomatis, dipilih & dinilai AI")
    ap.add_argument("video", nargs="*", help="satu atau beberapa file video / link")
    ap.add_argument("--gabung", action="store_true", help="gabungkan klip terbaik tiap video jadi 1 kompilasi")
    ap.add_argument("--watch", metavar="FOLDER")
    ap.add_argument("-n", "--clips", type=int, default=5)
    ap.add_argument("-d", "--duration", type=float, default=45, help="durasi klip (detik)")
    ap.add_argument("-m", "--menit", type=float, help="durasi klip dalam menit (mengganti --duration)")
    ap.add_argument("--viral", help="tampilkan 3 video viral per negara, mis. Indonesia,Amerika")
    ap.add_argument("--kategori", default="Semua", choices=list(VIRAL_CATS))
    ap.add_argument("-f", "--format", choices=list(FORMATS), default="vertical_face")
    ap.add_argument("--style", choices=list(STYLES), default="otomatis")
    ap.add_argument("--profile", choices=list(PROFILES), default="otomatis")
    ap.add_argument("--sub-model", choices=SUB_MODELS, default="otomatis")
    ap.add_argument("--ai", choices=list(AI_MODES), default="lokal")
    ap.add_argument("--api-key", default="", help="API key Claude (atau set ANTHROPIC_API_KEY)")
    ap.add_argument("-s", "--strict", choices=list(STRICT), default="normal")
    ap.add_argument("--no-subs", action="store_true")
    ap.add_argument("--plain", action="store_true")
    ap.add_argument("--look", choices=list(LOOKS), default="bersih", help="gaya visual")
    ap.add_argument("--genre", choices=list(GENRES), default="otomatis", help="preset genre")
    ap.add_argument("--no-jumpcut", action="store_true")
    ap.add_argument("--no-sfx", action="store_true")
    ap.add_argument("--no-meme", action="store_true", help="tanpa efek suara meme")
    ap.add_argument("--bahasa", default="en", help="bahasa tambahan judul+deskripsi, mis. en,ms,es ('' = tidak ada)")
    ap.add_argument("--warna", choices=list(TEXT_COLORS), default="otomatis", help="warna kata aktif subtitle")
    ap.add_argument("--title", default="")
    ap.add_argument("--handle", default="")
    ap.add_argument("--logo", default="", help="file logo PNG untuk watermark")
    ap.add_argument("--logo-pos", choices=LOGO_POS, default="kanan atas")
    ap.add_argument("--fokus", choices=list(FOCUS), default="otomatis", help="fokus momen yang dipilih AI")
    ap.add_argument("--cam-pos", choices=["bawah", "atas"], default="bawah", help="posisi facecam streamer")
    ap.add_argument("--cam-box", default="", help="kotak facecam manual x,y,lebar,tinggi dalam persen")
    ap.add_argument("--cek-facecam", action="store_true", help="hanya deteksi & simpan gambar kotak facecam")
    ap.add_argument("--logo-io", choices=BRAND_POS, default="mati", help="logo besar + tulisan di awal/akhir video")
    ap.add_argument("--logo-io-file", default="", help="file logo pembuka/penutup (kosong = --logo)")
    ap.add_argument("--logo-io-teks", default="", help="tulisan di bawah logo (kosong = --handle)")
    ap.add_argument("--top-text", action="store_true", help="tampilkan teks judul/hook di atas video")
    ap.add_argument("--no-intro", action="store_true", help="tanpa pembuka & penutup")
    ap.add_argument("--voice", choices=list(VOICE_LABEL), default="pria", help="suara AI pembuka/penutup")
    ap.add_argument("--cta", default=Options.cta)
    ap.add_argument("--cookies", default="")
    ap.add_argument("--campaign", default="", help="nama campaign tersimpan (folder campaign/) -> ikuti aturan brief")
    ap.add_argument("--campaign-baca", nargs="+", metavar="FILE", help="analisa brief campaign (PDF/foto) lalu simpan")
    ap.add_argument("--campaign-video", metavar="NAMA", help="cari video sumber yang sesuai campaign tersimpan")
    ap.add_argument("--lang", default="id")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()
    if a.cek_facecam and a.video:
        for v_ in a.video:
            print(preview_facecam(v_, a.cam_box)[1])
        return
    if a.campaign_video:
        p_ = campaign_load(a.campaign_video)
        if not p_:
            print(f"campaign '{a.campaign_video}' tidak ditemukan. Ada: {', '.join(campaign_list()) or '-'}")
            return
        for v in campaign_find_videos(p_, 15, log=print):
            print(f"  {fmt_views(v['views']) if v['views'] else '-':>8}  {fmt_dur(v['sec']) if v['sec'] else '-':>8}  "
                  f"{'[sudah diunduh] ' if v['done'] else ''}{v['title'][:70]}  ({v['source']})\n  {v['url']}")
        return
    if a.campaign_baca:
        nm_, p_ = campaign_analyze(a.campaign_baca, a.api_key if a.ai == "claude" else "", print)
        print(json.dumps({k: v for k, v in p_.items() if not k.startswith("_")}, ensure_ascii=False, indent=1))
        print(f"\nTersimpan sebagai campaign '{nm_}'. Pakai: --campaign \"{nm_}\"")
        return
    if a.viral:
        for nm in [x.strip() for x in a.viral.split(",") if x.strip()]:
            print(f"== {nm} ==")
            for v in viral_videos(nm, a.kategori):
                print(f"  {fmt_views(v['views']):>8}  {fmt_dur(v['sec']):>8}  {v['title'][:80]}\n  {v['url']}")
        return
    opt = Options(n_clips=a.clips, duration=a.menit * 60 if a.menit else a.duration, fmt=a.format, style=a.style, subtitles=not a.no_subs,
                  profile=a.profile, sub_model=a.sub_model, language=a.lang, ai=a.ai, api_key=a.api_key,
                  out_dir=a.out, strict=a.strict, creative=not a.plain, jumpcut=not a.no_jumpcut, sfx=not a.no_sfx, look=a.look, genre=a.genre,
                  title=a.title, handle=a.handle, cta=a.cta, cookies=a.cookies, logo=a.logo, logo_pos=a.logo_pos,
                  cam_pos=a.cam_pos, cam_box=a.cam_box, focus=a.fokus,
                  brand=a.logo_io, brand_logo=a.logo_io_file, brand_text=a.logo_io_teks,
                  top_text=a.top_text, intro=not a.no_intro, outro=not a.no_intro, voice=a.voice,
                  text_color=a.warna, meme_sfx=not a.no_meme, desc_langs=a.bahasa, campaign=a.campaign)
    last = {"p": -1}

    def cli_prog(p, stage=""):
        if int(p * 100) != last["p"]:
            last["p"] = int(p * 100)
            print(f"\r{100 * p:5.1f}%  {stage[:70]:<70}", end="", flush=True)

    def cli_log(msg):
        print("\r" + " " * 80 + "\r" + msg, flush=True)

    def work():
        if a.watch:
            watch_folder(a.watch, opt, cli_log)
        elif a.video:
            process_many(a.video, opt, cli_log, cli_prog, a.gabung and len(a.video) > 1)
        else:
            ap.print_help()
    th = threading.Thread(target=work, daemon=True)
    th.start()
    try:
        while th.is_alive():
            th.join(0.5)
    except KeyboardInterrupt:
        CANCEL.set()
        th.join(5)


if __name__ == "__main__":
    main()
