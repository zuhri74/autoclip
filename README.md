# ✂️ AutoClip

**Ubah video panjang (stream, podcast, vlog, gameplay) jadi klip pendek vertikal siap upload ke YouTube Shorts, TikTok & Reels – otomatis.**

AutoClip mendengarkan seluruh video dengan AI, memilih momen paling seru (tawa, reaksi, aksi, kalimat menarik), lalu mengedit setiap klip:

- 📱 Bingkai vertikal 9:16 yang mengikuti wajah / aksi, tidak goyang
- 💬 Subtitle otomatis berwarna kata-per-kata (Bahasa Indonesia, Inggris, dll.)
- 🎬 Kartu pembuka & penutup dengan suara AI, efek suara, jump cut
- 🏷️ Judul, deskripsi, hashtag, thumbnail 16:9 & cover Shorts
- 🖼️ (Opsional) thumbnail dibuat AI Google Gemini

## 🚀 Cara 1 – Langsung di browser (gratis, tanpa install)

Proses berjalan di server Google memakai akun Google Anda sendiri. Laptop tidak terbebani.

[![Buka di Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/zuhri74/autoclip/blob/main/AutoClip_Colab.ipynb)

1. Klik tombol **Open in Colab** di atas, lalu login dengan akun Google.
2. Menu **Runtime → Ubah jenis runtime → GPU T4 → Simpan**.
3. Klik ▶ di kotak **1** (izinkan akses Google Drive), lalu isi link video di kotak **2** dan klik ▶.
4. Hasil tersimpan di **Google Drive → My Drive/AutoClip/hasil**. Kotak **3** menampilkan judul & thumbnail.

**Tips:**
- Video dari laptop/HP: upload ke **My Drive/AutoClip/video**, lalu tulis nama filenya.
- YouTube menolak unduhan dari server Google? Upload videonya ke Drive, atau simpan file `cookies.txt` YouTube di **My Drive/AutoClip/**. File ini berisi data login Anda – jangan dibagikan.
- Colab gratis punya batas waktu per sesi; GPU kadang tidak tersedia saat ramai.

## 💻 Cara 2 – Di komputer sendiri (Windows)

1. Pasang **Python 3.10+** dari python.org (centang *Add Python to PATH*).
2. Unduh project ini (**Code → Download ZIP**), lalu ekstrak.
3. Klik dua kali **`Pasang.bat`** (sekali saja).
4. Klik dua kali **`AutoClip Studio.bat`** – tampilan website terbuka di browser (hanya di komputer Anda).

Kartu grafis NVIDIA membuat proses jauh lebih cepat, tapi tidak wajib.

## 🔑 API key (opsional)

| Fitur | Dari mana |
|---|---|
| Pemilihan momen & judul lebih pintar (Claude) | console.anthropic.com |
| Thumbnail AI (Google Gemini) | aistudio.google.com → Get API key |

Tanpa API key semuanya tetap jalan dengan AI lokal gratis. Key hanya disimpan di komputer / sesi Colab Anda.

## ⚠️ Penting – hak cipta

Potong hanya video **milik sendiri**, video yang **sudah diizinkan** pemiliknya, atau sesuai **aturan campaign clipper** yang Anda ikuti. Pengguna sepenuhnya bertanggung jawab atas video yang diunduh dan diunggah ulang. Patuhi aturan platform (YouTube, TikTok, dll.).

## 📄 Lisensi

Kode: MIT (lihat `LICENSE`). Font di `fonts/`: SIL Open Font License 1.1.

---

### English (short)

AutoClip turns long videos into vertical short clips with AI moment detection, auto-framing, word-by-word captions, intro/outro voice-over, titles, descriptions and thumbnails. Run it free in Google Colab (button above) or locally on Windows (`Pasang.bat`, then `AutoClip Studio.bat`). Only clip content you own or have permission to use.
