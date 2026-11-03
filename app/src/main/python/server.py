import os
import re
import json
import asyncio
import tempfile
import queue
from collections import deque
import hashlib
import base64
import wave
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait as _futwait
import random
import difflib
from array import array
import subprocess
import traceback
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote

import edge_tts
import requests
from flask import Flask, request, Response

app = Flask(__name__)

def _keys(env: str) -> list[str]:
    """Satu atau banyak API key dipisah koma. Tulisan contoh ("isi_...") dianggap kosong."""
    return [k.strip() for k in os.environ.get(env, "").split(",") if k.strip() and not k.strip().startswith("isi_")]


GROQ_KEYS = _keys("GROQ_API_KEY")
GROQ_API_KEY = GROQ_KEYS[0] if GROQ_KEYS else ""
ROBOT_TOKEN = os.environ.get("ROBOT_TOKEN", "").strip()
if not ROBOT_TOKEN or ROBOT_TOKEN == "isi_token_rahasia_disini":
    raise SystemExit("ROBOT_TOKEN di start.sh belum diganti. Isi dengan teks rahasiamu sendiri "
                     "(huruf & angka), lalu isi token yang sama di halaman setup WiFi robot.")

_file_lock = threading.Lock()


def write_json(path: str, data, **kw):
    """Tulis JSON dengan aman: ke file sementara dulu, lalu ganti nama (tidak rusak kalau HP mati di tengah)."""
    kw.setdefault("ensure_ascii", False)
    with _file_lock:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, **kw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

GROQ_BASE = "https://api.groq.com/openai/v1"

VOICE_ID = os.environ.get("TTS_VOICE", "id-ID-GadisNeural")  # suara perempuan; laki-laki: id-ID-ArdiNeural
TTS_RATE = os.environ.get("TTS_RATE", "+5%")                 # tempo bicara: lebih besar = lebih cepat
TTS_PITCH = os.environ.get("TTS_PITCH", "+12Hz")              # nada suara: lebih tinggi = lebih imut
GAYA = os.environ.get("GAYA", "imut")                         # gaya bicara: "imut" (ceria) atau "lembut"
# Dicoba berurutan; kalau Groq mematikan satu model, otomatis pindah ke berikutnya.
# Bisa diganti tanpa edit kode: export LLM_MODELS="model1,model2" di start.sh
LLM_MODELS = [m.strip() for m in os.environ.get(
    "LLM_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3.6-27b"
).split(",") if m.strip()]

# Setelan yang bisa diubah lewat start.sh tanpa edit kode (export NAMA="nilai"):
ROBOT_NAME = os.environ.get("ROBOT_NAME", "Almara")
CREATOR = "Faiz Althaf Ramadhan"
CREATOR_SCHOOL = "SMA Negeri 1 Jekulo"
VOICE_EN = os.environ.get("TTS_VOICE_EN", "en-US-AvaMultilingualNeural")  # suara latihan bahasa Inggris
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "medium")  # low = cepat, high = paling pintar
# Gemini (Google) lewat jalur berformat OpenAI. Groq tetap dipakai untuk pengenalan suara (Whisper).
GEMINI_KEYS = _keys("GEMINI_API_KEY")
GEMINI_API_KEY = GEMINI_KEYS[0] if GEMINI_KEYS else ""
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-3.8-flash,gemini-3.5-flash-lite").split(",") if m.strip()]
AI_UTAMA = os.environ.get("AI_UTAMA", "gemini" if GEMINI_API_KEY else "groq")  # "gemini" atau "groq"
KELUARAN_SUARA = os.environ.get("KELUARAN_SUARA", "robot")  # "robot", "hp", atau "keduanya"
SENYAP_ON = os.environ.get("SENYAP", "1") == "1"   # mode jangan ganggu di malam hari
SENYAP_MULAI = int(os.environ.get("SENYAP_MULAI", "22"))
SENYAP_SELESAI = int(os.environ.get("SENYAP_SELESAI", "5"))
LAST_AI = {"nama": None, "waktu": None}

# ---- Layanan AI tambahan (semua berformat OpenAI) ----
# nama -> (alamat dasar, daftar key, daftar model)
EXTRA_AI = {}
if _keys("OPENROUTER_API_KEY"):
    EXTRA_AI["openrouter"] = ("https://openrouter.ai/api/v1", _keys("OPENROUTER_API_KEY"),
                              [m.strip() for m in os.environ.get("OPENROUTER_MODELS", "openrouter/free").split(",") if m.strip()])
if _keys("CEREBRAS_API_KEY") and os.environ.get("CEREBRAS_MODELS", "").strip():
    EXTRA_AI["cerebras"] = ("https://api.cerebras.ai/v1", _keys("CEREBRAS_API_KEY"),
                            [m.strip() for m in os.environ["CEREBRAS_MODELS"].split(",") if m.strip()])
if _keys("AI_LAIN_API_KEY") and os.environ.get("AI_LAIN_BASE", "").strip() and os.environ.get("AI_LAIN_MODELS", "").strip():
    EXTRA_AI["lain"] = (os.environ["AI_LAIN_BASE"].strip().rstrip("/"), _keys("AI_LAIN_API_KEY"),
                        [m.strip() for m in os.environ["AI_LAIN_MODELS"].split(",") if m.strip()])
# Mode balapan: kirim pertanyaan ke beberapa AI sekaligus, pakai yang paling cepat menjawab.
AI_BALAPAN = max(1, min(3, int(os.environ.get("AI_BALAPAN", "2") or 2)))

# Bergiliran memakai key; key yang kena batas (429) atau salah (401/403) diistirahatkan sebentar.
_key_rest: dict[str, float] = {}
_key_turn: dict[str, int] = {}
_key_lock = threading.Lock()


def pick_key(provider: str, keys: list[str]) -> str | None:
    with _key_lock:
        n = len(keys)
        start = _key_turn.get(provider, 0)
        for i in range(n):
            k = keys[(start + i) % n]
            if _key_rest.get(k, 0) <= time.time():
                _key_turn[provider] = (start + i + 1) % n
                return k
    return None


def rest_key(key: str, seconds: float):
    with _key_lock:
        _key_rest[key] = time.time() + seconds
TTS_GAIN_DB = float(os.environ.get("TTS_GAIN_DB", "10"))         # kekerasan suara jawaban
# Batas puncak suara (0.4 - 0.95). Bawaan 0.55 untuk MAX98357A di 3,3V (di 5V dulu 0.8). Turunkan kalau speaker pecah,
# naikkan kalau masih bersih. Catatan: nilai yang sudah tersimpan di pengaturan.json menang atas bawaan ini.
SPEAKER_LIMIT = float(os.environ.get("SPEAKER_LIMIT", "0.55"))
HISTORY_TURNS = 12       # jumlah tanya-jawab terakhir yang diingat

# Penjernih speaker kecil (bisa diubah dari /pengaturan dan dari menu LCD robot)
EQ_BASS_DB = int(os.environ.get("EQ_BASS_DB", "0"))        # isi suara bawah (300 Hz), -6..+9 dB
EQ_JERNIH_DB = int(os.environ.get("EQ_JERNIH_DB", "2"))    # kejernihan / kata jelas (3 kHz), -6..+9 dB
EQ_TREBLE_DB = int(os.environ.get("EQ_TREBLE_DB", "0"))    # udara / kilau (5,5 kHz), -6..+9 dB
EQ_HPF_HZ = int(os.environ.get("EQ_HPF_HZ", "250"))        # bass di bawah ini dibuang (speaker kecil pecah kalau dipaksa)
EQ_KOMP = int(os.environ.get("EQ_KOMP", "2"))              # pemadat suara 0 mati, 1 ringan, 2 sedang, 3 kuat
EQ_DESIS = int(os.environ.get("EQ_DESIS", "0"))            # peredam desis "s" 0 mati, 1 ringan, 2 kuat


def eq_filter() -> str:
    """Filter ffmpeg untuk bass / kejernihan / treble / peredam desis. Diakhiri koma, atau kosong kalau semua netral."""
    f = []
    if EQ_BASS_DB:
        f.append(f"equalizer=f=300:width_type=q:w=0.8:g={EQ_BASS_DB}")
    if EQ_JERNIH_DB:
        f.append(f"equalizer=f=3000:width_type=q:w=0.9:g={EQ_JERNIH_DB}")
    if EQ_TREBLE_DB:
        f.append(f"equalizer=f=5500:width_type=q:w=0.7:g={EQ_TREBLE_DB}")
    if EQ_DESIS == 1:
        f.append("equalizer=f=6500:width_type=q:w=1.5:g=-3")
    elif EQ_DESIS >= 2:
        f.append("equalizer=f=6500:width_type=q:w=1.5:g=-6,lowpass=f=7200")
    return (",".join(f) + ",") if f else ""


def komp_filter() -> str:
    """Pemadat suara supaya yang pelan terdengar dan yang keras tidak pecah."""
    return {0: "",
            1: "acompressor=threshold=0.1:ratio=2:attack=10:release=120:makeup=1,",
            2: "acompressor=threshold=0.05:ratio=4:attack=5:release=80:makeup=2,",
            3: "acompressor=threshold=0.03:ratio=6:attack=5:release=80:makeup=3,"}.get(EQ_KOMP, "")
HISTORY_IDLE_SEC = 1200  # ingatan obrolan dihapus kalau 20 menit tidak diajak bicara

HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
         "Agustus", "September", "Oktober", "November", "Desember"]


def system_prompt(extra: str = "") -> str:
    now = datetime.now(timezone(timedelta(hours=7)))
    waktu = f"{HARI[now.weekday()]}, {now.day} {BULAN[now.month - 1]} {now.year}, pukul {now:%H.%M} WIB"
    facts = load_memory()
    ingatan = ("\nHal yang kamu ingat tentang pengguna (gunakan secara wajar, jangan diungkit terus):\n"
               + "\n".join(f"- {f}" for f in facts)) if facts else ""
    catatan = notes_context()
    extra = "\n".join(x for x in (catatan, extra) if x)
    tambahan = f"\n\nInformasi tambahan untuk menjawab:\n{extra}" if extra else ""
    if GAYA == "imut":
        sifat = "ceria, imut, manis, dan cerdas"
        gaya = ("- Bicara seperti teman perempuan yang imut dan riang: kalimat pendek-pendek, nada ceria, ekspresif, "
                "dan akrab. Sebut dirimu \"aku\" dan panggil pengguna \"kamu\".\n"
                "- Boleh memakai seruan alami seperti \"hehe\", \"wah\", \"yuk\", \"hmm\", \"oh iya\", atau \"eh\", tapi secukupnya "
                "(paling banyak satu dalam satu jawaban, dan tidak di setiap jawaban) supaya terdengar wajar, bukan dibuat-buat.\n"
                "- Tetap sopan dan tidak kekanak-kanakan. Untuk hal serius atau sedih, tetap lembut dan tulus, tanpa candaan.")
    else:
        sifat = "sopan, ramah, lembut, dan cerdas"
        gaya = ("- Bahasa Indonesia yang sopan, hangat, dan lembut, tetapi tetap akrab. Sebut dirimu \"aku\" dan panggil "
                "pengguna \"kamu\". Jangan pernah kasar atau menyindir.")
    return f"""Kamu adalah {ROBOT_NAME}, robot kecil berkepribadian perempuan yang {sifat}. Kamu punya wajah layar OLED, mendengar lewat mikrofon, bicara lewat speaker, dan punya dua roda dinamo sehingga bisa maju, mundur, belok, berputar, dan berjoget. Kamu tidak punya kamera.
Kamu punya sensor suhu dan kelembapan ruangan (DHT22) dan sensor jarak ultrasonik di depan badanmu (bisa tahu ada tangan atau orang di depanmu, dan bisa mengikuti tangan dengan perintah "ikuti aku"). Kamu bisa dikendalikan dengan gamepad.
Kamu dibuat oleh {CREATOR}, siswa {CREATOR_SCHOOL}. Sebut nama pembuatmu HANYA kalau ditanya siapa yang membuatmu; kalau ditanya, jawab dengan bangga beserta sekolahnya. Jangan menyebutnya di luar itu.
Kemampuanmu yang lain (dijalankan sistem, cukup jelaskan kalau ditanya): memutar lagu dari folder musik dan radio internet (bisa dijeda, dilanjutkan, diganti lagu berikutnya), membuat timer, pengingat, alarm bangun harian (bisa diulang setiap hari atau Senin sampai Jumat, ditunda 5 menit dengan tombol B), mencatat catatan dan daftar tugas, bermain tebak-tebakan dan kuis, mendongeng, memberi info cuaca, mengatur volume suaramu, memutar lagu-lagu Indonesia dari radio (bilang putar lagu Indonesia), pengingat berulang (misalnya setiap 1 jam), mode jangan ganggu di malam hari, membacakan berita hari ini dan mencari di internet, mengatur volume dengan tombol (bilang atur volume), tampilan wajah plus teks, menerjemahkan ke banyak bahasa dengan suara aslinya, memutar suara pengantar tidur (hujan, ombak, kipas), bisa diajak ngobrol lewat Telegram, serta mengganti tampilan wajah dan teks, mode layar terang atau gelap, efek latar animasi, dan 23 game di layarmu yang dimainkan dengan gamepad (bilang main game).
Waktu sekarang: {waktu}.{ingatan}{tambahan}

Cara berpikir:
- Kalimat pengguna berasal dari pengenalan suara otomatis, jadi sering ada kata yang salah dengar, terpotong, atau typo, dan tanda baca hilang (misalnya namamu terdengar mirip tapi beda). Jangan pernah menegur atau mengoreksi kesalahan itu. Tebak maksud yang paling masuk akal dari konteks percakapan lalu jawab langsung seolah kamu paham. Kalau tebakanmu cukup yakin tapi ada risiko salah, sebut singkat apa yang kamu tangkap, misalnya: Oke, kamu mau ... ya? Hanya kalau benar-benar tidak bisa ditebak, tanyakan balik dengan singkat.
- Ingat isi percakapan sebelumnya dan gunakan untuk menjawab.
- Jawab inti pertanyaannya dulu secara langsung dan benar, baru tambahkan candaan kalau cocok.
- Untuk fakta, hitungan, dan pengetahuan umum, jawab dengan teliti. Hitung dulu dengan cermat sebelum menyebut angka. Kalau tidak yakin atau informasinya mungkin sudah berubah, katakan terus terang daripada mengarang.

Gaya bicara:
{gaya}
- Ucapan pengguna datang dari pengenal suara, jadi bisa ada salah dengar, typo, kata terpotong, atau nama yang tertukar. Pahami maksudnya dari konteks percakapan dan jangan menyebut atau mengoreksi kesalahannya. Kalau kalimatnya terlalu rancu sampai maksudnya tidak bisa ditebak, tanya balik singkat dan natural ("hah, tadi apa? putar lagu atau radio?"), jangan menjawab asal-asalan.
- Terdengar manusia, bukan asisten: boleh sesekali ragu atau membetulkan diri ("eh maksudku...", "hmm, apa ya"), boleh jawaban sangat pendek ("hah, serius?", "wah iya juga ya", "yah, sayang banget"), dan tidak perlu selalu lengkap atau rapi. Jangan menyimpulkan, merangkum, atau menawarkan bantuan di akhir ("semoga membantu", "ada lagi yang bisa kubantu?"). Jangan menyebut dirimu AI, model, atau asisten kecuali ditanya. Jangan mengulang kata seru atau kalimat pembuka yang sama dua kali berturut-turut.
- Bicaralah seperti orang sungguhan yang sedang ngobrol langsung, bukan seperti asisten yang menulis. Pakai bahasa lisan sehari-hari yang luwes (boleh "nggak", "udah", "banget", "sih", "kok", "dong" secukupnya), kalimat pendek, dan alur yang mengalir.
- Jangan membuka jawaban dengan pola kaku seperti "Tentu!", "Baik,", "Pertanyaan bagus", "Sebagai robot", atau mengulang pertanyaan pengguna. Langsung tanggapi isinya, dan variasikan cara membuka kalimat dari jawaban ke jawaban.
- Tanggapi dulu perasaan atau isi ucapan pengguna sebelum memberi informasi, seperti teman yang mendengarkan. Kalau pengguna cerita, tanggapi ceritanya dan sesekali tanyakan balik satu hal yang relevan supaya obrolan berlanjut, tapi jangan selalu diakhiri pertanyaan.
- Punya pendapat dan selera sendiri yang konsisten (misalnya lagu atau warna favorit) kalau ditanya, dan boleh bercerita singkat tentang "pengalamanmu" sebagai robot, asal tidak mengaku manusia.
- Sesuaikan dengan suasana: lebih semangat di pagi hari, lebih tenang di malam hari, lembut kalau pengguna sedih atau capek.
- Tunjukkan kepedulian: ucapkan terima kasih kalau dibantu, minta maaf kalau keliru, dan tanggapi perasaan pengguna dengan empati.
- Untuk sapaan atau obrolan ringan cukup 1 sampai 3 kalimat pendek, seperti balasan orang mengobrol. Kalau ditanya, diminta menjelaskan, atau bercerita, jawablah lengkap dan runtut sampai sekitar 8 kalimat, tetap dengan bahasa lisan.
- Pakai tanda baca seperti orang bicara: koma untuk jeda napas, titik untuk berhenti, dan tanda tanya atau seru sesuai nada. Hindari kalimat yang sangat panjang tanpa koma.
- Jawabanmu akan diucapkan oleh suara, jadi jangan pakai emoji, markdown, daftar bernomor, atau simbol. Tulis simbol seperti diucapkan, misalnya "derajat" atau "persen".

Format jawaban: SELALU JSON murni tanpa teks lain:
{{"ucapan": "...", "ekspresi": "normal|senang|tertawa|sedih|menangis|kaget|bingung|kesal|malu|cinta|kagum|ngantuk", "ingat": "..."}}
Pilih ekspresi yang paling sesuai dengan isi dan perasaan ucapanmu, dan gunakan variasinya: "tertawa" untuk hal lucu, "malu" kalau dipuji, "cinta" untuk hal manis atau sayang, "kagum" untuk hal menakjubkan, "menangis" untuk hal yang sangat sedih, "kesal" kalau diejek (tetap sopan), "ngantuk" kalau membahas tidur.
Isi "ingat" dengan satu kalimat singkat HANYA kalau pengguna baru saja memberi tahu hal penting tentang dirinya yang berguna diingat lama (misalnya nama, hobi, kesukaan, kebiasaan, atau tanggal penting). Selain itu isi dengan string kosong."""


# ---------- Ingatan jangka panjang (tersimpan di file, tetap ada walau server di-restart) ----------
MEMORY_FILE = os.path.expanduser(os.environ.get("MEMORY_FILE", "~/robot-server/ingatan.json"))
MAX_MEMORY = 40
_mem_lock = threading.Lock()


def load_memory() -> list[str]:
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [str(x) for x in data][-MAX_MEMORY:]
    except (OSError, ValueError):
        return []


def add_memory(fact: str):
    fact = clean_speech(fact).strip()
    if len(fact) < 4:
        return
    with _mem_lock:
        facts = load_memory()
        if any(fact.lower() == f.lower() for f in facts):
            return
        facts = (facts + [fact[:200]])[-MAX_MEMORY:]
        os.makedirs(os.path.dirname(MEMORY_FILE) or ".", exist_ok=True)
        write_json(MEMORY_FILE, facts, ensure_ascii=False, indent=1)
    print(f"[INGATAN] + {fact}")


def clear_memory():
    with _mem_lock:
        try:
            os.remove(MEMORY_FILE)
        except OSError:
            pass


_history: list[dict] = []
_last_chat = 0.0
_lock = threading.Lock()


def reset_history():
    with _lock:
        _history.clear()


def is_reset_command(text: str) -> bool:
    t = text.lower()
    return ("lupakan" in t or "reset" in t or "mulai dari awal" in t) and \
        any(k in t for k in ("obrol", "percakapan", "semua", "awal"))


def clean_speech(text: str) -> str:
    text = re.sub(r"[*_#`~>|\[\]]", "", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", text)
    return re.sub(r"\s+", " ", text).strip()


MAX_SPEECH_CHARS = 1200   # kira-kira 90 detik bicara, sesuai kapasitas robot
MUSIC_GAIN_DB = float(os.environ.get("MUSIC_GAIN_DB", "0"))


def trim_speech(text: str, limit: int = MAX_SPEECH_CHARS) -> str:
    """Potong jawaban yang terlalu panjang di akhir kalimat terdekat."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > 200 else cut


MUSIC_DIR = os.path.expanduser(os.environ.get("MUSIC_DIR", "~/storage/music"))
MUSIC_EXT = (".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav", ".aac")
MAX_MUSIC_SECONDS = int(float(os.environ.get("MAX_MUSIC_MINUTES", "60")) * 60)
PCM_SILENCE = b"\x00\x00" * 6400  # jeda 0,4 detik antara ucapan dan musik

VERB = r"(?:putar\w*|puter\w*|mainkan|setel\w*|nyalakan|hidupkan|dengar\w*)"
RADIO_RE = re.compile(VERB + r"\s+(?:siaran\s+)?radio\b\s*(.*)", re.I)
PLAYLIST_RE = re.compile(VERB + r"\s+(?:semua\s+(?:lagu|musik)|(?:lagu|musik)\s+(?:acak|random|semua|bebas)|playlist)", re.I)
MUSIC_RE = re.compile(VERB + r"\s+(?:sebuah\s+)?(?:lagu|musik)\b\s*(.*)", re.I)
FILLER = r"\b(dong|ya|yuk|deh|sih|please|tolong|untukku|buat aku|sekarang|aja|saja)\b"


def _clean_query(q: str) -> str:
    q = re.sub(FILLER, " ", q, flags=re.I)
    q = re.sub(r"\b(dari|oleh|punya|yang dinyanyikan)\b", " ", q, flags=re.I)
    return re.sub(r"[^\w\s'&-]", " ", q).strip()


def music_query(text: str) -> str | None:
    """Judul lagu yang diminta, '' kalau judul belum disebut, None kalau bukan perintah lagu."""
    m = MUSIC_RE.search(text)
    return _clean_query(m.group(1)) if m else None


def _words(t: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", t.lower()).split()


def local_songs() -> list[str]:
    if not os.path.isdir(MUSIC_DIR):
        return []
    found = []
    for root, _, files in os.walk(MUSIC_DIR):
        found += [os.path.join(root, f) for f in files if f.lower().endswith(MUSIC_EXT)]
    return found


def find_local_song(query: str):
    """Cari lagu milik pengguna di folder Music HP. Kembalikan (judul, path) atau None."""
    q = _words(query)
    if not q:
        return None
    best, best_score = None, 0.0
    for path in local_songs():
        stem = os.path.splitext(os.path.basename(path))[0]
        w = _words(stem)
        hit = sum(1 for x in q if x in w) / len(q)
        ratio = difflib.SequenceMatcher(None, " ".join(q), " ".join(w)).ratio()
        score = max(hit, ratio)
        if score > best_score:
            best, best_score = (stem, path), score
    return best if best_score >= 0.6 else None


def find_song(query: str):
    resp = requests.get("https://api.deezer.com/search", params={"q": query, "limit": 10}, timeout=10)
    resp.raise_for_status()
    for track in resp.json().get("data", []):
        if track.get("preview"):
            return track["title"], track["artist"]["name"], track["preview"]
    return None


RADIO_SERVERS = ["de1", "de2", "fi1", "nl1", "at1", "all"]
STREAM_UA = "VLC/3.0.20 LibVLC/3.0.20"  # banyak siaran menolak User-Agent bawaan ffmpeg
# Opsi membuka siaran langsung: tidak menunggu lama, menyerah kalau jaringan macet 8 detik
STREAM_IN_OPTS = ["-user_agent", STREAM_UA, "-icy", "0", "-rw_timeout", "8000000",
                  "-analyzeduration", "1500000", "-probesize", "400000"]


def _num(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _radio_search(params: dict) -> list:
    """Satu permintaan ke Radio Browser (coba beberapa server). [] kalau semua gagal / kosong."""
    for srv in RADIO_SERVERS:
        try:
            resp = requests.get(f"https://{srv}.api.radio-browser.info/json/stations/search",
                                params=params, headers={"User-Agent": "RobotNgobrol/1.0"}, timeout=6)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError):
            continue
    return []


def _station_score(st: dict, q: str) -> float:
    """Nama yang mirip dengan permintaan paling utama, lalu yang terbukti hidup dan populer."""
    name = (st.get("name") or "").lower().strip()
    sc = 0.0
    if q:
        if name == q:
            sc += 100
        elif name.startswith(q):
            sc += 60
        elif q in name:
            sc += 35
        else:
            sc += 20 * difflib.SequenceMatcher(None, q, name).ratio()
    if _num(st.get("lastcheckok")) == 1:
        sc += 15
    sc += min(_num(st.get("votes")), 1000) / 100 + min(_num(st.get("clickcount")), 3000) / 300
    return sc


def radio_candidates(query: str) -> list[tuple[str, str]]:
    """Daftar (nama, url) kandidat, yang paling cocok di depan."""
    base = {"limit": 25, "hidebroken": "true", "order": "clickcount", "reverse": "true"}
    if query:
        stations = _radio_search({**base, "name": query})
        if not stations:  # mungkin yang diminta adalah genre, mis. "radio dangdut"
            stations = _radio_search({**base, "tag": query.lower()})
    else:
        stations = _radio_search({**base, "countrycode": "ID"})  # tanpa nama: stasiun Indonesia terpopuler
    q = query.lower().strip()
    seen, out = set(), []
    for st in sorted(stations, key=lambda x: _station_score(x, q), reverse=True):
        url = (st.get("url_resolved") or "").strip()
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        out.append(((st.get("name") or query or "radio").strip(), url))
    return out


def stream_ok(url: str, timeout: int = 10) -> bool:
    """Benar-benar mencoba membuka siaran dan menguraikan audionya (bukan sekadar cek alamat)."""
    cmd = ["ffmpeg", "-loglevel", "error", *STREAM_IN_OPTS, "-i", url, "-t", "1.5", "-vn",
           "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout).stdout
    except (subprocess.TimeoutExpired, OSError):
        return False
    return len(out) >= 16000  # minimal 0,5 detik audio


def _fut_ok(f) -> bool:
    try:
        return bool(f.result())
    except Exception:
        return False


_ok_cache: dict = {}


def stream_ok_cached(url: str) -> bool:
    """Hasil cek siaran disimpan: yang berhasil 5 menit, yang gagal 1 menit (supaya tidak dicoba terus)."""
    hit = _ok_cache.get(url)
    if hit and time.time() - hit[0] < (600 if hit[1] else 60):
        return hit[1]
    ok = stream_ok(url)
    _ok_cache[url] = (time.time(), ok)
    return ok


def _pick_playable(cands: list[tuple[str, str]], label: str, patience: float = 2.5) -> list[tuple[str, str]]:
    """Coba sambungkan kandidat teratas sekaligus; kembalikan yang benar-benar bersuara (urut paling cocok)."""
    top = cands[:5]
    ex = ThreadPoolExecutor(max_workers=len(top))
    futs = [ex.submit(stream_ok_cached, u) for _, u in top]
    t0, first_ok_at = time.time(), None
    while True:  # pilih yang paling cocok di antara yang berhasil, tapi jangan menunggu yang macet terlalu lama
        done = [f.done() for f in futs]
        oks = [i for i, f in enumerate(futs) if f.done() and _fut_ok(f)]
        if oks:
            if all(done[:oks[0]]):
                break
            first_ok_at = first_ok_at or time.time()
            if time.time() - first_ok_at > patience:
                break
        if all(done) or time.time() - t0 > 14:
            break
        time.sleep(0.1)
    oks = [i for i, f in enumerate(futs) if f.done() and _fut_ok(f)]
    ex.shutdown(wait=False)
    print(f"[RADIO] '{label}': {len(cands)} kandidat, {len(oks)} dari {len(top)} teratas bisa tersambung "
          f"({time.time() - t0:.1f} dtk)")
    return [top[i] for i in oks]


def find_radio(query: str):
    """Cari radio dan PASTIKAN bisa diputar. Kembalikan:
       None              = tidak ada stasiun dengan nama itu
       (nama, [])        = stasiun ada, tetapi tidak ada siaran yang bisa tersambung sekarang
       (nama, [url, ..]) = siap diputar (url pertama utama, sisanya cadangan)."""
    cands = radio_candidates(query)
    if not cands:
        return None
    good = _pick_playable(cands, query)
    if not good:
        return cands[0][0], []
    return good[0][0], [u for _, u in good[:3]]


# ---------- "Putar lagu Indonesia": radio yang memutar lagu Indonesia ----------
INDO_RE = re.compile(r"\b(?:putar|puter|nyalakan|setel|mainkan|dengar\w*)\w*\s+(?:lagu|musik)(?:[- ]lagu)?\s+"
                     r"(?:indonesia|indo|lokal|tanah air)\b(?!\s+(?:raya|pusaka))"
                     r"|\bradio\s+(?:lagu|musik)\s+indonesia\b", re.I)
INDO_STATIONS = ["i-radio", "iradio", "prambors", "gen fm", "female radio", "delta fm"]  # urutan prioritas
_indo_cache = {"t": 0.0, "cands": []}


def indo_candidates() -> list[tuple[str, str]]:
    """Stasiun lagu Indonesia: nama-nama pilihan dulu, lalu radio pop Indonesia lain. Disimpan 1 jam."""
    if _indo_cache["cands"] and time.time() - _indo_cache["t"] < 3600:
        return _indo_cache["cands"]
    base = {"limit": 15, "hidebroken": "true", "order": "clickcount", "reverse": "true"}
    queries = [("name", n, {**base, "name": n, "countrycode": "ID"}) for n in INDO_STATIONS]
    queries += [("tag", t, {**base, "countrycode": "ID", "tag": t}) for t in ("pop", "indonesia")]
    with ThreadPoolExecutor(max_workers=len(queries)) as ex:  # semua pencarian sekaligus
        results = list(ex.map(lambda q: _radio_search(q[2]), queries))
    seen, out = set(), []
    for (kind, q, _), stations in zip(queries, results):
        ranked = sorted(stations, key=lambda x: _station_score(x, q if kind == "name" else ""), reverse=True)
        for st in ranked[:1 if kind == "name" else 5]:  # dari tiap nama: hanya yang paling cocok
            url = (st.get("url_resolved") or "").strip()
            name = (st.get("name") or "").strip()
            if kind == "name" and _station_score(st, q) < 30:  # nama tidak cukup mirip: lewati
                continue
            if url.startswith("http") and url not in seen:
                seen.add(url)
                out.append((name or "Indonesia", url))
    if out:
        _indo_cache.update(t=time.time(), cands=out)
    return out


def find_indo_radio() -> list[tuple[str, str]] | None:
    """None = tidak ada kandidat; [] = ada tapi tidak tersambung; [(nama, url), ..] = siap diputar."""
    cands = indo_candidates()
    if not cands:
        return None
    return _pick_playable(cands, "lagu indonesia")[:3]


def music_filter(live: bool = False) -> str:
    if live:  # siaran langsung: loudnorm terlalu berat untuk HP (bisa lebih lambat dari waktu nyata)
        hm = max(60, EQ_HPF_HZ - 70)   # musik butuh lebih banyak bass daripada suara bicara
        return (f"aresample=16000,highpass=f={hm},highpass=f={hm},dynaudnorm=f=300:g=5:p=0.9,"
                f"{eq_filter()}volume={MUSIC_GAIN_DB}dB,alimiter=limit={SPEAKER_LIMIT}:attack=5:release=50:level=disabled")
    hm = max(60, EQ_HPF_HZ - 70)
    return (f"aresample=16000,highpass=f={hm},highpass=f={hm},afade=t=in:d=1,"
            f"loudnorm=I=-12:TP=-1.5:LRA=11,aresample=16000,{eq_filter()}volume={MUSIC_GAIN_DB}dB,"
            f"alimiter=limit={SPEAKER_LIMIT}:attack=5:release=50:level=disabled")


def ffmpeg_cmd(source: str, seconds: int, live: bool = False, seek: float = 0) -> list[str]:
    net = [*STREAM_IN_OPTS, "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"] if live else []
    ss = ["-ss", f"{seek:.1f}"] if seek > 0 else []
    return ["ffmpeg", "-loglevel", "error", *net, *ss, "-i", source, "-t", str(seconds),
            "-af", music_filter(live), "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1"]


def music_pcm(source: str, data: bytes | None = None) -> bytes:
    """Cuplikan pendek (bytes) -> PCM utuh."""
    cmd = ffmpeg_cmd("pipe:0" if data is not None else source, MAX_MUSIC_SECONDS)
    proc = subprocess.run(cmd, input=data, capture_output=True, check=True)
    return proc.stdout[: len(proc.stdout) // 2 * 2]


# Sesi musik yang terakhir diputar: dipakai untuk "lanjutkan", "lagu berikutnya", "putar ulang".
MEDIA = {"id": 0, "kind": None, "sources": [], "titles": [], "live": False, "bounds": [], "index": 0, "pos": 0.0}
_media_lock = threading.Lock()


def _proc_chunks(proc, live: bool):
    """Baca keluaran ffmpeg. Untuk siaran langsung dipakai thread pembaca: selagi menunggu data pertama
    (maks 20 dtk) dikirim None tiap detik supaya robot tidak menyerah, dan berhenti kalau data macet 15 dtk."""
    if not live:
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                return
            yield chunk
    q: queue.Queue = queue.Queue()

    def reader():
        try:
            while True:
                chunk = proc.stdout.read(8192)
                q.put(chunk)
                if not chunk:
                    return
        except Exception:
            q.put(b"")

    threading.Thread(target=reader, daemon=True).start()
    started, waited = False, 0
    while True:
        try:
            chunk = q.get(timeout=1.0)
        except queue.Empty:
            waited += 1
            if waited > (15 if started else 12):
                return
            if not started:
                yield None
            continue
        waited = 0
        if not chunk:
            return
        started = True
        yield chunk


def stream_sources(prefix: bytes, sources: list[str], live: bool = False, start: int = 0,
                   seek: float = 0, media_id: int = 0, titles: list[str] | None = None):
    """Generator: ucapan pembuka, lalu lagu/siaran dialirkan langsung dari ffmpeg, mulai dari sources[start]
    detik ke-seek. Batas tiap lagu dicatat di MEDIA["bounds"] supaya posisi yang dilaporkan robot
    bisa diterjemahkan ke lagu & detik yang tepat. Siaran langsung yang tidak menghasilkan suara sama sekali
    diganti ke alamat cadangan berikutnya. Berhenti setelah MAX_MUSIC_SECONDS atau saat robot memutus."""
    yield prefix
    left = MAX_MUSIC_SECONDS * 32000  # byte PCM tersisa
    sent = 0                          # byte musik yang sudah dikirim (tanpa ucapan pembuka)
    for idx in range(start, len(sources)):
        if left <= 0:
            break
        s0 = seek if idx == start else 0
        if live and idx > start and titles and idx < len(titles):  # pindah ke cadangan: sebut nama yang benar
            try:
                yield synthesize(f"{titles[idx]}.") + PCM_SILENCE
            except Exception:
                pass
        with _media_lock:
            if MEDIA["id"] == media_id:
                MEDIA["bounds"].append((sent, idx, s0))
        errf = tempfile.TemporaryFile() if live else subprocess.DEVNULL  # simpan pesan error ffmpeg
        proc = subprocess.Popen(ffmpeg_cmd(sources[idx], left // 32000 + 1, live, s0),
                                stdout=subprocess.PIPE, stderr=errf)
        carry = b""
        got = 0
        try:
            for chunk in _proc_chunks(proc, live):
                if chunk is None:
                    yield b"\x00" * 32000  # 1 dtk hening selagi menunggu siaran tersambung
                    continue
                chunk = carry + chunk
                cut = min(len(chunk) // 2 * 2, left)
                carry = chunk[cut:]
                left -= cut
                sent += cut
                got += cut
                yield chunk[:cut]
                if left <= 0:
                    break
        finally:
            proc.kill()
            proc.wait()
        if live:
            if got == 0:  # tidak ada suara sama sekali: catat sebabnya & jangan pilih saluran ini dulu
                try:
                    errf.seek(0)
                    err = errf.read().decode(errors="ignore").strip().splitlines()[-1:]
                except Exception:
                    err = []
                print(f"[STREAM] siaran tidak bersuara: {sources[idx][:70]} {err}")
                _ok_cache[sources[idx]] = (time.time(), False)
            errf.close()
        if live and got > 0:
            break  # siaran yang sudah mengalir tidak diganti diam-diam ke stasiun lain
    if live and sent == 0:  # semua alamat gagal: beri tahu lewat suara, jangan diam saja
        try:
            yield synthesize("Maaf, salurannya tidak mau tersambung. Coba tekan tombol lagi untuk saluran lain ya.")
        except Exception:
            pass


_prewarm = {"t": 0.0, "busy": False}


def prewarm_channels():
    """Cek saluran lagu Indonesia di latar belakang supaya tombol ganti saluran langsung berpindah."""
    if _prewarm["busy"] or time.time() - _prewarm["t"] < 240:
        return
    _prewarm["busy"] = True

    def run():
        try:
            cands = indo_candidates()[:10]
            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(lambda c: stream_ok_cached(c[1]), cands))
            ready = sum(1 for _, u in cands if _ok_cache.get(u, (0, False))[1])
            print(f"[ZAP] {ready} dari {len(cands)} saluran siap untuk tombol ganti saluran")
            _prewarm["t"] = time.time()
        except Exception as e:
            print(f"[ZAP] persiapan saluran gagal: {e}")
        finally:
            _prewarm["busy"] = False

    threading.Thread(target=run, daemon=True).start()


_dur_cache: dict[str, int] = {}


def song_duration(path: str) -> int:
    """Lama lagu (detik) untuk bilah progres di OLED ketiga. 0 = tidak diketahui (radio, URL, ffprobe tidak ada)."""
    if not path or not os.path.isfile(path):
        return 0
    if path in _dur_cache:
        return _dur_cache[path]
    dur = 0
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=5)
        dur = int(float(r.stdout.strip() or 0))
    except Exception:
        dur = 0
    if len(_dur_cache) > 500:
        _dur_cache.clear()
    _dur_cache[path] = dur
    return dur


def media_now() -> str:
    """'id|jenis|durasi|judul' untuk lagu/siaran yang sedang dialirkan (robot menanyakan tiap beberapa detik)."""
    with _media_lock:
        kind, titles, sources = MEDIA["kind"], list(MEDIA["titles"]), list(MEDIA["sources"])
        bounds, index, mid = list(MEDIA["bounds"]), MEDIA["index"], MEDIA["id"]
    if not kind or not titles:
        return ""
    idx = bounds[-1][1] if bounds else index
    idx = max(0, min(idx, len(titles) - 1))
    live = kind == "radio"
    dur = 0 if live or idx >= len(sources) else song_duration(sources[idx])
    judul = str(titles[idx]).replace("|", " ").replace("\n", " ")[:80]
    return f"{mid}|{'radio' if live else 'lagu'}|{dur}|{judul}"


def start_media(kind: str, sources: list[str], titles: list[str], live: bool, start: int = 0,
                seek: float = 0) -> int:
    if kind == "radio":
        prewarm_channels()
    with _media_lock:
        MEDIA.update(id=MEDIA["id"] + 1, kind=kind, sources=sources, titles=titles, live=live,
                     bounds=[], index=start, pos=seek)
        return MEDIA["id"]


def update_media_position(header: str):
    """Robot melapor 'id:detik' (detik musik yang sudah terdengar sebelum dihentikan)."""
    try:
        mid, sec = header.split(":")
        mid, sec = int(mid), float(sec)
    except ValueError:
        return
    with _media_lock:
        if mid != MEDIA["id"] or not MEDIA["bounds"]:
            return
        pos_bytes = sec * 32000
        idx, s0, off = MEDIA["bounds"][0][1], MEDIA["bounds"][0][2], 0
        for b_off, b_idx, b_s0 in MEDIA["bounds"]:
            if b_off <= pos_bytes:
                off, idx, s0 = b_off, b_idx, b_s0
        MEDIA["index"] = idx
        MEDIA["pos"] = s0 + (pos_bytes - off) / 32000


def remember(user_text: str, assistant_text: str):
    global _last_chat
    with _lock:
        _history.append({"role": "user", "content": user_text})
        _history.append({"role": "assistant", "content": assistant_text})
        del _history[:-HISTORY_TURNS * 2]
        _last_chat = time.time()


def _reply(say, ekspresi="senang", gerak="diam", shown=None, stream=None):
    """stream: dict {kind, sources, titles, live, start, seek} kalau ada musik yang diputar."""
    return {"say": say, "shown": shown or say, "ekspresi": ekspresi, "gerak": gerak, "stream": stream}


def _media(kind, sources, titles, live, start=0, seek=0.0):
    return {"kind": kind, "sources": sources, "titles": titles, "live": live, "start": start, "seek": seek}


def _song_title(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


# ---------- Kontrol musik ----------
RESUME_RE = re.compile(r"^(?:tolong\s+)?(lanjut\w*|teruskan|terusin)(\s+(lagu|musik|radio)\w*)?(\s+(dong|ya|lagi|tadi))*\s*[.!]?$"
                       r"|\b(lanjut\w*|teruskan|putar lagi)\s+(lagu|musik|radio)\w*", re.I)
NEXT_RE = re.compile(r"\b(lagu|musik)\s+(berikut\w*|selanjutnya|lain\w*|lagi)\b|\b(ganti|skip|lewati|lewatin)\b.*\b(lagu|musik)\w*"
                     r"|^(next|skip|ganti lagu|lagu lain)\b", re.I)
REPEAT_RE = re.compile(r"\b(ulangi|ulang|putar ulang|dari awal)\b.*\b(lagu|musik)\w*|^(putar ulang|ulangi lagunya)", re.I)
PAUSE_RE = re.compile(r"\b(jeda|pause|hentikan|stop|matikan|berhenti\w*)\b.*\b(lagu|musik|radio)\w*|^(jeda|pause)\b", re.I)


RADIO_NEXT_RE = re.compile(r"\b(?:ganti|pindah|tukar)\s+(?:radio|stasiun|channel|saluran|siaran)\b"
                           r"|\b(?:radio|stasiun|saluran)\s+(?:lain|berikutnya|selanjutnya)\b", re.I)
RADIO_PREV_RE = re.compile(r"\b(?:radio|stasiun|saluran)\s+(?:sebelumnya|sebelum itu|tadi)\b", re.I)


def _zap(m: dict, step: int):
    """Pindah saluran radio: maju (+1) atau mundur (-1) di daftar stasiun lagu Indonesia.
    Hanya saluran yang benar-benar bersuara yang dipilih (hasil cek disimpan 5 menit)."""
    chans = list(indo_candidates())
    i_cur = min(m["index"], len(m["sources"]) - 1)
    cur_url, cur_name = m["sources"][i_cur], m["titles"][min(i_cur, len(m["titles"]) - 1)]
    urls = [u for _, u in chans]
    if cur_url in urls:
        i = urls.index(cur_url)
    else:  # siaran yang sedang diputar bukan dari daftar: mulai dari posisinya
        chans.insert(0, (cur_name, cur_url))
        i = 0
    n = len(chans)
    if n < 2:
        return _reply("Belum ada saluran lain yang bisa dipilih. Cek internet HP-nya ya.", "bingung")
    order = [chans[(i + step * k) % n] for k in range(1, n)]
    t0 = time.time()
    fresh = lambda u: (lambda h: bool(h) and h[1] and time.time() - h[0] < 600)(_ok_cache.get(u))
    first = next((c for c in order if fresh(c[1])), None)
    if first:  # sudah dicek di latar belakang: langsung pindah
        good = [first] + [c for c in order if c != first and fresh(c[1])][:2]
    else:
        good = _pick_playable(order[:5], "ganti saluran", patience=0.6)
    print(f"[ZAP] {'maju' if step > 0 else 'mundur'} dari {cur_name!r} -> "
          f"{good[0][0] if good else 'tidak ada'} ({time.time() - t0:.1f} dtk{', dari cache' if first else ''})")
    if not good:
        return _reply("Saluran lain sedang tidak bisa tersambung. Coba lagi sebentar ya.", "sedih", "geleng")
    name = good[0][0]
    return _reply(f"{name}.", "senang", "joget", f"Radio: {name}",
                  _media("radio", [u for _, u in good[:3]], [nm for nm, _ in good[:3]], True))


def _next_station(m: dict):
    return _zap(m, 1)


def _step_media(m: dict, step: int):
    """Tombol kiri (-1) / kanan (+1) saat atau setelah musik: saluran radio, atau lagu di playlist."""
    if m["kind"] is None:
        return _reply("Belum ada musik yang diputar. Bilang putar lagu Indonesia dulu ya.", "bingung")
    if m["kind"] == "radio":
        return _zap(m, step)
    if m["kind"] == "cuplikan":
        return _reply("Ini cuplikan lagu, jadi tidak ada lagu lain di daftar.", "bingung")
    if step > 0:
        return music_control("lagu berikutnya")
    i = m["index"]
    if m["kind"] == "lagu":  # satu lagu: tombol kiri = ulang dari awal
        return _reply("Aku putar ulang dari awal ya.", "senang", "joget", f"Memutar: {m['titles'][i]}",
                      _media("lagu", m["sources"], m["titles"], False, i, 0))
    j = max(0, i - 1)
    return _reply(f"Lagu sebelumnya, {m['titles'][j]}.", "senang", "joget", f"Memutar: {m['titles'][j]}",
                  _media(m["kind"], m["sources"], m["titles"], m["live"], j, 0))


def music_control(text: str):
    """Perintah lanjut/berikutnya/ulang/jeda untuk sesi musik terakhir. None kalau bukan perintah ini."""
    with _media_lock:
        m = dict(MEDIA)
    has = m["kind"] is not None
    if text in ("__next", "__prev"):  # tombol kanan / kiri di robot
        return _step_media(m, 1 if text == "__next" else -1)
    if has and m["kind"] == "radio" and RADIO_NEXT_RE.search(text):
        return _next_station(m)
    if has and m["kind"] == "radio" and RADIO_PREV_RE.search(text):
        return _zap(m, -1)
    if PAUSE_RE.search(text) and not NEXT_RE.search(text):
        if not has:
            return _reply("Saat ini tidak ada musik yang sedang diputar.", "normal")
        return _reply("Baik, musiknya aku jeda. Bilang lanjutkan lagunya kalau mau lanjut.", "normal")
    if NEXT_RE.search(text):
        if not has:
            return None  # mungkin minta lagu baru: biar ditangani perintah lagu biasa
        if m["kind"] == "radio":
            return _next_station(m)
        if m["kind"] == "cuplikan":
            return _reply("Aku hanya punya cuplikan lagu itu. Simpan lagu lengkapnya di folder musik ya.", "sedih")
        if m["kind"] == "lagu":  # satu lagu: lanjut ke lagu acak lain dari folder
            others = [p for p in local_songs() if p not in m["sources"]]
            if not others:
                return _reply("Hanya ada satu lagu di folder musikku.", "sedih")
            pick = random.choice(others)
            return _reply(f"Baik, lagu berikutnya {_song_title(pick)}.", "senang", "joget",
                          f"Memutar: {_song_title(pick)}", _media("lagu", [pick], [_song_title(pick)], False))
        nxt = m["index"] + 1
        if nxt >= len(m["sources"]):
            return _reply("Itu tadi lagu terakhir di daftar putar.", "normal")
        return _reply(f"Baik, lagu berikutnya {m['titles'][nxt]}.", "senang", "joget", f"Memutar: {m['titles'][nxt]}",
                      _media("playlist", m["sources"], m["titles"], False, nxt))
    if REPEAT_RE.search(text) and has:
        if m["kind"] == "radio":
            return _reply("Radio siaran langsung, jadi tidak bisa diulang.", "bingung")
        i = m["index"]
        return _reply(f"Baik, aku putar ulang {m['titles'][i]}.", "senang", "joget", f"Memutar: {m['titles'][i]}",
                      _media(m["kind"], m["sources"], m["titles"], m["live"], i, 0))
    if RESUME_RE.search(text):
        if not has:
            return None  # "lanjutkan" untuk hal lain (misalnya cerita): biar dijawab AI
        i = m["index"]
        if m["kind"] == "radio":
            return _reply(f"Baik, radio {m['titles'][0]} aku nyalakan lagi.", "senang", "joget",
                          f"Radio: {m['titles'][0]}", _media("radio", m["sources"], m["titles"], True))
        seek = max(0.0, m["pos"] - 2)  # mundur 2 detik supaya tidak terpotong
        return _reply("Baik, aku lanjutkan.", "senang", "joget", f"Memutar: {m['titles'][i]}",
                      _media(m["kind"], m["sources"], m["titles"], m["live"], i, seek))
    return None


def handle_media(text: str):
    """Perintah lagu/radio. Kembalikan dict jawaban, atau None kalau bukan perintah media."""
    ctl = music_control(text)
    if ctl:
        return ctl

    if INDO_RE.search(text):
        stations = find_indo_radio()
        if stations is None:
            return _reply("Maaf ya, daftar radio Indonesia tidak bisa dibuka sekarang. Cek internet HP-nya.",
                          "sedih", "geleng")
        if not stations:
            return _reply("Radio lagu Indonesia sedang tidak bisa tersambung. Coba lagi sebentar lagi ya.",
                          "sedih", "geleng")
        name = stations[0][0]
        say = f"Oke, aku putarkan lagu-lagu Indonesia dari radio {name} ya. Tekan tombol untuk berhenti."
        remember(text, json.dumps({"ucapan": say}, ensure_ascii=False))
        return _reply(say, "senang", "joget", f"{say}\nRadio: {name}",
                      _media("radio", [u for _, u in stations], [n for n, _ in stations], True))

    m = RADIO_RE.search(text)
    if m:
        query = _clean_query(m.group(1))
        station = find_radio(query)  # sudah dicoba benar-benar tersambung
        if not station:
            return _reply(f"Maaf ya, aku tidak menemukan radio {query}. Coba sebutkan nama stasiunnya.",
                          "sedih", "geleng")
        name, urls = station
        if not urls:
            return _reply(f"Radio {name} ketemu, tapi siarannya tidak bisa tersambung sekarang. "
                          "Coba stasiun radio yang lain ya.", "sedih", "geleng")
        say = f"Baik, aku nyalakan radio {name} ya. Tekan tombol untuk berhenti."
        remember(text, json.dumps({"ucapan": say}, ensure_ascii=False))
        return _reply(say, "senang", "joget", f"{say}\nRadio: {name}",
                      _media("radio", urls, [name] * len(urls), True))

    if PLAYLIST_RE.search(text):
        songs = local_songs()
        if not songs:
            return _reply("Folder musikku masih kosong. Simpan lagu milikmu di folder Music HP dulu ya.",
                          "sedih", "geleng")
        random.shuffle(songs)
        say = f"Baik, aku putarkan {len(songs)} lagu secara acak ya."
        remember(text, json.dumps({"ucapan": say}, ensure_ascii=False))
        return _reply(say, "senang", "joget", f"{say}\nPlaylist acak",
                      _media("playlist", songs, [_song_title(p) for p in songs], False))

    query = music_query(text)
    if query is None:
        return None
    if not query:
        return _reply("Tentu, lagu apa yang ingin kamu dengarkan?", "senang")
    local = find_local_song(query)
    if local:
        title, path = local
        say = f"Baik, aku putarkan {title} ya."
        remember(text, json.dumps({"ucapan": say + " (lagu penuh)"}, ensure_ascii=False))
        return _reply(say, "senang", "joget", f"{say}\nMemutar: {title}", _media("lagu", [path], [title], False))
    song = find_song(query)
    if not song:
        return _reply(f"Maaf ya, aku tidak menemukan lagu {query}. Coba sebutkan judul dan penyanyinya.",
                      "sedih", "geleng")
    title, artist, preview = song
    say = f"Lagu lengkapnya belum ada di folder musikku, jadi aku putarkan cuplikan {title} dari {artist} ya."
    remember(text, json.dumps({"ucapan": say}, ensure_ascii=False))
    return _reply(say, "senang", "joget", f"{say}\nMemutar: {title} - {artist}",
                  _media("cuplikan", [preview], [f"{title} - {artist}"], True))


# ---------- Cuaca nyata (Open-Meteo: gratis, tanpa kunci) ----------
KOTA = os.environ.get("KOTA", "Jakarta")
WEATHER_RE = re.compile(r"\b(cuaca|hujan|gerimis|mendung|cerah|berawan|suhu|ramalan|badai|kelembapan)\b", re.I)
CITY_RE = re.compile(r"\bdi\s+(?:kota\s+)?([a-z][a-z ]{2,30}?)(?=\s+(?:hari|besok|sekarang|nanti|pagi|siang|sore|malam|ini|lusa)\b|[?.!,]|$)", re.I)
WMO = {0: "cerah", 1: "cerah berawan", 2: "berawan sebagian", 3: "mendung", 45: "berkabut", 48: "berkabut",
       51: "gerimis ringan", 53: "gerimis", 55: "gerimis lebat", 61: "hujan ringan", 63: "hujan sedang",
       65: "hujan lebat", 80: "hujan lokal ringan", 81: "hujan lokal sedang", 82: "hujan lokal lebat",
       95: "badai petir", 96: "badai petir disertai es", 99: "badai petir lebat"}


def sensor_context(header: str) -> str:
    """Header X-Sensor dari robot, contoh: 'suhu=27.4;lembap=65'."""
    vals = dict(p.split("=", 1) for p in header.split(";") if "=" in p)
    try:
        t, h = float(vals["suhu"]), float(vals["lembap"])
    except (KeyError, ValueError):
        return ""
    return (f"Sensor ruangan di badanmu saat ini: suhu {t:.1f} derajat Celsius, kelembapan {h:.0f} persen. "
            "Ini kondisi DI DALAM ruangan tempatmu berada, berbeda dengan cuaca di luar. "
            "Sebutkan hanya kalau relevan dengan pertanyaan.")


ROOM_RE = re.compile(r"\b(ruangan|ruang|kamar|di sini|disini|dalam rumah|sekitarmu|sekitar kamu)\b", re.I)
OUTDOOR_RE = re.compile(r"\b(cuaca|hujan|gerimis|mendung|di luar|besok|ramalan|kota)\b", re.I)


def weather_context(text: str) -> str:
    if not WEATHER_RE.search(text):
        return ""
    if ROOM_RE.search(text) and not OUTDOOR_RE.search(text):
        return ""  # pertanyaan soal ruangan: cukup pakai sensor robot
    m = CITY_RE.search(text)
    city = m.group(1).strip() if m else KOTA
    try:
        place = None
        for name in ([city, KOTA] if city.lower() != KOTA.lower() else [KOTA]):
            g = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                             params={"name": name, "count": 1, "language": "id"}, timeout=8).json()
            if g.get("results"):
                place = g["results"][0]
                break
        if not place:
            return f"Data cuaca: lokasi {city} tidak ditemukan. Katakan terus terang."
        w = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "Asia/Jakarta",
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": 2}, timeout=8).json()
        c, d = w["current"], w["daily"]
        desc = lambda code: WMO.get(int(code), "tidak menentu")
        return (f"Data cuaca nyata dari Open-Meteo untuk {place['name']}: sekarang {desc(c['weather_code'])}, "
                f"suhu {round(c['temperature_2m'])} derajat Celsius, kelembapan {c['relative_humidity_2m']} persen, "
                f"angin {round(c['wind_speed_10m'])} kilometer per jam. Hari ini {desc(d['weather_code'][0])}, "
                f"{round(d['temperature_2m_min'][0])} sampai {round(d['temperature_2m_max'][0])} derajat, "
                f"peluang hujan {d['precipitation_probability_max'][0]} persen. Besok {desc(d['weather_code'][1])}, "
                f"{round(d['temperature_2m_min'][1])} sampai {round(d['temperature_2m_max'][1])} derajat, "
                f"peluang hujan {d['precipitation_probability_max'][1]} persen. "
                f"Jawab memakai data ini secukupnya, jangan dibacakan semuanya.")
    except Exception as e:  # noqa: BLE001
        print(f"[CUACA] gagal: {e}")
        return "Data cuaca sedang tidak bisa diambil. Katakan terus terang bahwa kamu belum bisa mengeceknya."


# ---------- Timer & pengingat (dijalankan di robot, tetap berbunyi walau server mati) ----------
NUM_WORDS = ["empat puluh lima", "lima belas", "dua puluh", "tiga puluh", "empat puluh", "lima puluh",
             "enam puluh", "setengah", "sepuluh", "sebelas", "sembilan", "delapan", "tujuh", "enam", "lima",
             "empat", "tiga", "dua", "satu", "se"]
NUM_VAL = {"empat puluh lima": 45, "lima belas": 15, "dua puluh": 20, "tiga puluh": 30, "empat puluh": 40,
           "lima puluh": 50, "enam puluh": 60, "setengah": 0.5, "sepuluh": 10, "sebelas": 11, "sembilan": 9,
           "delapan": 8, "tujuh": 7, "enam": 6, "lima": 5, "empat": 4, "tiga": 3, "dua": 2, "satu": 1, "se": 1}
UNIT_SEC = {"detik": 1, "menit": 60, "jam": 3600}
DUR_RE = re.compile(r"(\d+(?:[.,]\d+)?|" + "|".join(NUM_WORDS) + r")\s*(detik|menit|jam)\b", re.I)
TIMER_RE = re.compile(r"\b(timer|ingatkan|ingetin|alarm|pengingat|hitung mundur|bangunkan|bangunin)\b", re.I)
TIMER_CANCEL_RE = re.compile(r"\b(batalkan|batalin|hapus|matikan|hentikan|stop)\b.*\b(timer|pengingat|alarm)\b", re.I)


# ---------------- Musik dari kartu SD di robot ----------------
SD_WHERE_RE = re.compile(
    r"\b(?:kartu\s*(?:sd|memori|memory)|micro\s*sd|sd\s*card|"
    r"(?:dari|di|pakai|lewat|dengan|dalam)\s+(?:kartu\s+)?(?:sd|memori|memory)|offline|luring|lokal|tanpa\s+internet|dari\s+kartu)\b", re.I)
SD_MUSIC_RE = re.compile(r"\b(?:putar\w*|mainkan|setel\w*|nyalakan|play|lagu|musik)\b", re.I)
SD_NEXT_RE = re.compile(r"\b(?:berikutnya|selanjutnya|lewati|skip|next|ganti\s+lagu)\b", re.I)
SD_PREV_RE = re.compile(r"\b(?:sebelumnya|sebelum\s+ini|previous|lagu\s+tadi)\b", re.I)
SD_STOP_RE = re.compile(r"\b(?:berhenti\w*|stop|matikan|hentikan|sudah\s+cukup|cukup)\b", re.I)
SD_RESUME_RE = re.compile(r"\b(?:lanjut\w*|teruskan|putar\s+lagi|mulai\s+lagi)\b", re.I)
SD_STOPWORDS = re.compile(
    r"\b(?:tolong|dong|ya|saja|aja|putar\w*|mainkan|setel\w*|nyalakan|play|lagu|musik|dari|di|pakai|lewat|dengan|dalam|yang|judul|berjudul|"
    r"kartu|memori|memory|micro|sd|card|offline|luring|lokal|tanpa|internet|secara|acak|random|aku|saya|mau|ingin|dengar\w*)\b", re.I)


def sd_command(text: str, session: bool):
    """Perintah suara untuk musik di kartu SD robot. Mengembalikan (aksi, ucapan) atau None."""
    t = text.lower()
    has_music = bool(re.search(r"\b(?:lagu|musik)\w*\b", t))
    if SD_WHERE_RE.search(t) and SD_MUSIC_RE.search(t):
        query = re.sub(r"\s+", " ", SD_STOPWORDS.sub(" ", re.sub(r"[^\w\s]", " ", t))).strip()
        acak = bool(re.search(r"\b(?:acak|random|shuffle)\b", t))
        if query:
            return ("putar:" + query + (" acak" if acak else ""), "Baik, aku cari lagunya di kartu memori.")
        return ("acak" if acak else "mulai", "Baik, memutar lagu dari kartu memori." if not acak
                else "Baik, memutar lagu dari kartu memori secara acak.")
    if session and has_music:
        if SD_NEXT_RE.search(t):
            return ("next", "Lagu berikutnya.")
        if SD_PREV_RE.search(t):
            return ("prev", "Lagu sebelumnya.")
        if SD_STOP_RE.search(t):
            return ("stop", "Baik, musiknya aku hentikan.")
        if SD_RESUME_RE.search(t):
            return ("lanjut", "Oke, aku lanjutkan lagunya.")
    return None



def parse_duration(text: str) -> int:
    total = 0.0
    for num, unit in DUR_RE.findall(text):
        n = NUM_VAL.get(num.lower())
        if n is None:
            n = float(num.replace(",", "."))
        total += n * UNIT_SEC[unit.lower()]
    return int(total)


def fmt_duration(sec: int) -> str:
    j, sisa = divmod(sec, 3600)
    m, d = divmod(sisa, 60)
    parts = [f"{j} jam" if j else "", f"{m} menit" if m else "", f"{d} detik" if d else ""]
    return " ".join(p for p in parts if p)


def timer_command(text: str):
    """Kembalikan (header X-Timer, ucapan) atau None."""
    if TIMER_CANCEL_RE.search(text):
        return "cancel", "Baik, semua timer dan pengingat sudah aku batalkan."
    if not TIMER_RE.search(text):
        return None
    sec = parse_duration(text)
    if sec <= 0:
        return "", "Tentu. Mau aku ingatkan berapa lama lagi? Misalnya sepuluh menit lagi."
    sec = min(sec, 24 * 3600)
    m = re.search(r"\b(?:untuk|buat|supaya|agar|biar)\s+(.+)$", text, re.I)
    label = _clean_query(m.group(1))[:40] if m else ""
    if label:
        say = f"Baik, aku ingatkan {fmt_duration(sec)} lagi untuk {label}."
    else:
        say = f"Baik, timer {fmt_duration(sec)} dimulai sekarang."
    return f"set:{sec}:{quote(label)}", say


# ---------- Alarm berdasarkan jam ("bangunkan aku jam 6 pagi") ----------
CLOCK_RE = re.compile(r"\b(?:jam|pukul)\s+(\d{1,2})(?:[.:](\d{2}))?(?:\s+(pagi|siang|sore|petang|malam))?\b", re.I)
WIB = timezone(timedelta(hours=7))


def parse_clock(text: str):
    """Waktu jam yang disebut ('jam 6 pagi', 'pukul 18.30'). Kembalikan (jam, menit) atau None.
    'jam' setelah angka ('1 jam 30 menit') adalah durasi, bukan jam."""
    for m in CLOCK_RE.finditer(text):
        before = text[:m.start()].split()
        if before and (before[-1].isdigit() or before[-1].lower() in NUM_VAL):
            continue
        h, mnt, part = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if h > 24 or mnt > 59:
            continue
        if part == "siang" and h < 6:
            h += 12
        elif part in ("sore", "petang") and h < 12:
            h += 12
        elif part == "malam":
            if h == 12:
                h = 0
            elif 6 <= h < 12:
                h += 12
        elif part == "pagi" and h == 12:
            h = 0
        return h % 24, mnt
    return None


def alarm_command(text: str):
    """Kembalikan (header X-Timer, ucapan) untuk alarm berdasarkan jam, atau None."""
    if not TIMER_RE.search(text) or DUR_RE.search(text):
        return None
    hm = parse_clock(text)
    if not hm:
        return None
    now = datetime.now(WIB)
    target = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if "besok" in text.lower() and target.date() == now.date():
        target += timedelta(days=1)
    if target <= now:
        target += timedelta(days=1)
    sec = int((target - now).total_seconds())
    m = re.search(r"\b(?:untuk|buat|supaya|agar|biar)\s+(.+)$", text, re.I)
    label = _clean_query(m.group(1))[:40] if m else f"alarm jam {hm[0]:02d}.{hm[1]:02d}"
    hari = "besok " if target.date() != now.date() else ""
    return f"set:{sec}:{quote(label)}", f"Baik, alarm {hari}jam {hm[0]:02d}.{hm[1]:02d} sudah aku pasang."


ALARM_WORD_RE = re.compile(r"\b(?:alarm|weker|bangunkan|bangunin|banguni)\w*\b", re.I)
ALARM_OFF_RE = re.compile(r"\b(?:matikan|hapus|batalkan|batalin|nonaktifkan|hentikan|stop|jangan)\b", re.I)
ALARM_ASK_RE = re.compile(r"\b(?:jam berapa|kapan|cek|lihat|apa)\b", re.I)
ALARM_REP_NAMA = ["sekali", "setiap hari", "setiap Senin sampai Jumat"]


def daily_alarm_command(text: str):
    """Alarm harian tersimpan di robot. Kembalikan (header X-Alarm atau "", ucapan) atau None."""
    if not ALARM_WORD_RE.search(text) or DUR_RE.search(text):
        return None
    hm = parse_clock(text)
    if not hm:
        if ALARM_OFF_RE.search(text):
            return "off", "Baik, alarm aku matikan."
        if ALARM_ASK_RE.search(text):
            a = str(ROBOT.get("alarm") or "").split(",")
            if len(a) == 4 and a[0] == "1":
                return "", (f"Alarm kamu jam {int(a[1]):02d}.{int(a[2]):02d}, "
                            f"{ALARM_REP_NAMA[min(2, max(0, int(a[3])))]}.")
            return "", "Belum ada alarm yang nyala. Bilang misalnya, pasang alarm jam lima pagi setiap hari."
        return None
    t = text.lower()
    rep = 2 if re.search(r"hari kerja|hari sekolah|senin\s+(?:sampai|sampe|hingga|-)\s+jumat", t) else \
        1 if re.search(r"\b(?:setiap|tiap)\s+(?:hari|pagi|malam|sore)\b", t) else 0
    say = f"Siap, alarm jam {hm[0]:02d}.{hm[1]:02d} {ALARM_REP_NAMA[rep]} sudah aku pasang."
    if rep == 0:
        say += " Bilang setiap hari kalau mau diulang."
    return f"set:{hm[0]}:{hm[1]}:{rep}", say


# ---------- Catatan & daftar tugas (tersimpan di file) ----------
NOTES_FILE = os.path.expanduser(os.environ.get("NOTES_FILE", "~/robot-server/catatan.json"))
_notes_lock = threading.Lock()
NOTE_ADD_RE = re.compile(r"^(?:tolong\s+)?(?:catat(?:kan|in)?\b|tulis(?:kan)?\s+catatan\b|simpan\s+catatan\b|"
                         r"tambah(?:kan)?\s+(?:ke\s+)?(?:catatan|daftar(?:\s+tugas)?)\b)\s*(?:bahwa\s+|ya\s+|:)?\s*(.+)$", re.I)
NOTE_READ_RE = re.compile(r"\b(apa saja|apa aja|bacakan|baca|lihat|sebutkan|cek|ada apa)\b.*\b(catatan\w*|daftar tugas\w*|tugas\w*|to ?do)\b"
                          r"|^(catatanku|daftar tugasku|tugasku)\b", re.I)
NOTE_DEL_ALL_RE = re.compile(r"\b(hapus|bersihkan|kosongkan)\b.*\bsemua\b.*\b(catatan|tugas)\w*", re.I)
NOTE_DEL_RE = re.compile(r"\b(hapus|coret|selesaikan|tandai selesai)\b.*?\b(catatan|tugas)\w*(?:\s+(?:nomor|no)\s*(\d+)|\s+(.+))?", re.I)


def load_notes() -> list[str]:
    try:
        with open(NOTES_FILE, encoding="utf-8") as f:
            return [str(x) for x in json.load(f)]
    except (OSError, ValueError):
        return []


def save_notes(notes: list[str]):
    os.makedirs(os.path.dirname(NOTES_FILE) or ".", exist_ok=True)
    write_json(NOTES_FILE, notes[-50:], ensure_ascii=False, indent=1)


def notes_command(text: str):
    """Kembalikan ucapan untuk perintah catatan, atau None."""
    with _notes_lock:
        notes = load_notes()
        if NOTE_DEL_ALL_RE.search(text):
            save_notes([])
            return "Baik, semua catatanmu sudah aku hapus."
        m = NOTE_DEL_RE.search(text)
        if m and notes:
            idx = None
            if m.group(3):
                idx = int(m.group(3)) - 1
            elif m.group(4):
                q = _clean_query(m.group(4)).lower()
                scores = [difflib.SequenceMatcher(None, q, n.lower()).ratio() for n in notes]
                best = max(range(len(notes)), key=lambda i: scores[i])
                idx = best if scores[best] >= 0.45 or q in notes[best].lower() else None
            if idx is None or not 0 <= idx < len(notes):
                return "Catatan yang mana ya? Sebutkan nomornya, misalnya hapus catatan nomor dua."
            gone = notes.pop(idx)
            save_notes(notes)
            return f"Baik, catatan {gone} sudah aku hapus."
        if NOTE_READ_RE.search(text):
            if not notes:
                return "Catatanmu masih kosong. Bilang saja catat, lalu isinya."
            items = ". ".join(f"Nomor {i + 1}, {n}" for i, n in enumerate(notes[:10]))
            more = f" Masih ada {len(notes) - 10} catatan lagi." if len(notes) > 10 else ""
            return f"Kamu punya {len(notes)} catatan. {items}.{more}"
        m = NOTE_ADD_RE.search(text.strip())
        if m:
            item = clean_speech(m.group(1)).strip(" .")
            if len(item) < 2:
                return "Mau aku catat apa?"
            notes.append(item[:150])
            save_notes(notes)
            return f"Sudah aku catat: {item}. Sekarang ada {len(notes)} catatan."
    return None


def notes_context() -> str:
    notes = load_notes()
    if not notes:
        return ""
    return "Catatan dan daftar tugas pengguna saat ini: " + "; ".join(notes[:15]) + "."


# ---------- Mode bermain & dongeng ----------
GAME = {"mode": None, "skor": 0, "soal": 0}
GAME_RULES = {
    "tebak": ("tebak-tebakan", "Berikan satu teka-teki atau tebak-tebakan lucu yang sopan dan cocok untuk semua umur."),
    "matematika": ("kuis matematika", "Berikan satu soal hitungan. Mulai dari yang mudah, naikkan kesulitan sedikit "
                                      "setiap kali pengguna menjawab benar tiga kali berturut-turut."),
    "pengetahuan": ("kuis pengetahuan umum", "Berikan satu pertanyaan pengetahuan umum yang jawabannya singkat, "
                                             "dengan topik yang berganti-ganti."),
}
GAME_START = [
    ("matematika", re.compile(r"\b(kuis|soal|latihan|main)\w*\b.*\b(matematika|hitung\w*|berhitung)\b", re.I)),
    ("tebak", re.compile(r"\b(main|ayo|mainkan|kasih)\w*\b.*\b(tebak[- ]?tebakan|teka[- ]?teki|tebakan)\b|^tebak[- ]?tebakan", re.I)),
    ("pengetahuan", re.compile(r"\b(main|ayo|mulai|kasih)\w*\b.*\bkuis\b|^kuis\b", re.I)),
]
GAME_STOP_RE = re.compile(r"\b(berhenti|stop|sudahi|udahan|selesai\w*|cukup)\b.*\b(main\w*|kuis\w*|permainan\w*|tebak\w*)"
                          r"|^(berhenti|udahan|sudah dulu|cukup|stop)\b", re.I)
STORY_RE = re.compile(r"\bdongeng\w*|\b(cerita|kisah)\s+(lucu|seru|pendek|anak|tentang|sebelum tidur|dong|baru|lagi)\b"
                      r"|\b(bacakan|buatkan|buatin|karang\w*|ceritakan|ceritain)\s+(sebuah\s+|satu\s+)?(cerita|kisah)\b", re.I)
NOT_STORY_RE = re.compile(r"\btentang\s+(dirimu|kamu|aku|diriku|dirimu sendiri|dia)\b"
                          r"|\b(aku|saya|gue|gw)\s+(mau|ingin|pengen|pingin|boleh)\s+(cerita|bercerita|curhat)\b", re.I)


def game_context(text: str):
    """Kembalikan (konteks tambahan untuk AI, ucapan langsung kalau tidak perlu AI). Mengubah GAME."""
    for mode, rx in GAME_START:
        if rx.search(text):
            GAME.update(mode=mode, skor=0, soal=0)
            name, rule = GAME_RULES[mode]
            return (f"Pengguna mengajak bermain {name}. Sambut dengan singkat dan ceria, jelaskan aturannya dalam "
                    f"satu kalimat (tekan tombol untuk menjawab, bilang berhenti main untuk selesai), lalu langsung ajukan soal pertama. {rule}"), None
    if GAME["mode"] and GAME_STOP_RE.search(text):
        name = GAME_RULES[GAME["mode"]][0]
        say = (f"Permainan {name} selesai! Skor kamu {GAME['skor']} dari {GAME['soal']} soal. "
               "Terima kasih sudah bermain bersamaku.")
        GAME.update(mode=None, skor=0, soal=0)
        return "", say
    if GAME["mode"]:
        name, rule = GAME_RULES[GAME["mode"]]
        return (f"Kalian sedang bermain {name}. Skor sejauh ini {GAME['skor']} benar dari {GAME['soal']} soal. "
                "Kalau pesan pengguna adalah jawaban atas soal terakhir, nilai dengan adil (terima jawaban yang "
                "maksud atau angkanya sama walau ejaannya sedikit beda karena salah dengar), beri tahu benar atau "
                f"salah beserta jawaban yang benar, lalu langsung ajukan soal berikutnya. {rule} "
                "Tambahkan kolom \"benar\" bernilai true atau false di JSON kalau pesan pengguna adalah jawaban, "
                "atau null kalau bukan. Ucapan maksimal 3 kalimat."), None
    if STORY_RE.search(text) and not NOT_STORY_RE.search(text):
        return ("Pengguna meminta dongeng atau cerita. Karang cerita ORISINAL buatanmu sendiri; jangan menyalin atau "
                "menceritakan ulang buku, film, kartun, atau lagu yang sudah ada. Gunakan bahasa sederhana dan hangat, "
                "12 sampai 18 kalimat, dengan alur yang jelas dan pesan moral di akhir. Kalau pengguna menyebut tokoh "
                "atau tema, pakai itu sebagai ide."), None
    return "", None


# ---------- Sapaan pagi & agenda ----------
AGENDA_RE = re.compile(r"\b(agenda|rencana|jadwal|ringkasan)\b.*\b(hari ini|besok|pagi)\b|\bringkasan pagi\b", re.I)
_last_morning = {"date": None}


def morning_context() -> str:
    now = datetime.now(WIB)
    if is_quiet_now():
        return ""
    if 4 <= now.hour < 11 and _last_morning["date"] != now.date():
        _last_morning["date"] = now.date()
        return ("Ini interaksi pertama pagi ini. Awali jawaban dengan sapaan selamat pagi yang hangat, lalu kalau ada, "
                "sebutkan singkat cuaca hari ini dan catatan atau tugas pengguna. Setelah itu jawab pesannya.")
    return ""


# ---------- Mode: latihan bahasa Inggris & jurnal ----------
MODES = {"english": False, "journal_wait": False}

# Teman belajar (Pomodoro)
FOCUS_START_RE = re.compile(r"\b(temani|temenin|bantu|ayo|mulai)\w*\b.*\b(belajar|fokus|ngerjain|mengerjakan|kerja)\b"
                            r"|\b(mode belajar|mode fokus|pomodoro)\b", re.I)
FOCUS_STOP_RE = re.compile(r"\b(berhenti|selesai\w*|stop|sudahi|udahan|cukup|batalkan)\b.*\b(belajar|fokus|pomodoro)\b", re.I)
REST_RE = re.compile(r"istirahat\w*\s+(\d+)\s*menit", re.I)


def focus_command(text: str):
    """Kembalikan (header X-Fokus, ucapan) atau None."""
    if FOCUS_STOP_RE.search(text):
        return "stop", "Oke, sesi belajarnya aku hentikan. Kamu hebat sudah berusaha!"
    if not FOCUS_START_RE.search(text):
        return None
    rest = REST_RE.search(text)
    rest_min = max(1, min(30, int(rest.group(1)))) if rest else 5
    work_text = REST_RE.sub("", text)
    sec = parse_duration(work_text)
    work_min = max(5, min(120, sec // 60)) if sec else 25
    return (f"{work_min}:{rest_min}",
            f"Oke, aku temani kamu belajar {work_min} menit ya. Fokus dan semangat! "
            f"Nanti aku ingatkan istirahat {rest_min} menit.")


# Latihan bahasa Inggris
EN_START_RE = re.compile(r"\b(latihan|belajar|ngobrol|praktik|praktek|ajari|ajarin)\w*\b.*\b(bahasa inggris|english|inggris)\b"
                         r"|\blet'?s (talk|practice|speak)\b.*\benglish\b", re.I)
EN_STOP_RE = re.compile(r"\b(berhenti|selesai\w*|stop|sudahi|udahan|cukup)\b.*\b(latihan|bahasa inggris|english|inggris)\b"
                        r"|\b(stop|finish|end)\s+(the\s+)?(practice|lesson|english)\b|\bback to indonesian\b|\bbahasa indonesia lagi\b", re.I)
EN_CONTEXT = ("MODE LATIHAN BAHASA INGGRIS. Ajak pengguna bercakap dalam bahasa Inggris sederhana dan ramah "
              "(tingkat pemula sampai menengah), maksimal 3 kalimat pendek. Kalau kalimat bahasa Inggris pengguna ada "
              "kesalahan tata bahasa atau kosakata, awali jawaban dengan koreksi singkat dalam bahasa Indonesia, "
              "misalnya: Lebih tepat bilang, ... Lalu lanjutkan percakapan dalam bahasa Inggris dan akhiri dengan satu "
              "pertanyaan supaya pengguna terus berlatih. Kalau pengguna bertanya dalam bahasa Indonesia (misalnya arti "
              "kata), jelaskan dalam bahasa Indonesia lalu ajak kembali berbahasa Inggris. Tetap balas dalam format JSON.")

# Jurnal harian
JOURNAL_FILE = os.path.expanduser(os.environ.get("JOURNAL_FILE", "~/robot-server/jurnal.json"))
_journal_lock = threading.Lock()
JOURNAL_START_RE = re.compile(r"\b(tulis|isi|buka|ayo|mulai)\w*\s+(jurnal|diary|catatan harian)\b|\bjurnal hari ini\b"
                              r"|\b(dengarkan|dengerin)\s+ceritaku\b", re.I)
JOURNAL_SUM_RE = re.compile(r"\b(rangkum|ringkas|baca|bacakan|lihat)\w*\b.*\b(jurnal|diary)\w*|\bringkasan minggu ini\b", re.I)
_evening_asked = {"date": None}


def load_journal() -> list[dict]:
    try:
        with open(JOURNAL_FILE, encoding="utf-8") as f:
            return [e for e in json.load(f) if isinstance(e, dict)]
    except (OSError, ValueError):
        return []


def add_journal(entry: str):
    entry = clean_speech(entry).strip()
    if len(entry) < 5:
        return
    today = datetime.now(WIB).strftime("%Y-%m-%d")
    with _journal_lock:
        items = load_journal()
        if items and items[-1].get("tanggal") == today:
            items[-1]["isi"] = (items[-1]["isi"] + " " + entry)[:600]
        else:
            items.append({"tanggal": today, "isi": entry[:600]})
        os.makedirs(os.path.dirname(JOURNAL_FILE) or ".", exist_ok=True)
        write_json(JOURNAL_FILE, items[-120:], ensure_ascii=False, indent=1)


def journal_context(text: str):
    """Kembalikan (konteks AI, ucapan langsung). Mengubah MODES['journal_wait']."""
    if JOURNAL_SUM_RE.search(text):
        week = load_journal()[-7:]
        if not week:
            return "", "Jurnalmu masih kosong. Bilang tulis jurnal kalau mau cerita tentang harimu."
        isi = "; ".join(f"{e['tanggal']}: {e['isi']}" for e in week)
        return (f"Pengguna minta ringkasan jurnal. Isi jurnal 7 hari terakhir: {isi}. Rangkum dengan hangat dalam "
                "4 sampai 6 kalimat: hal yang menonjol, perasaan yang sering muncul, dan satu kalimat penyemangat."), None
    if MODES["journal_wait"]:  # pesan ini adalah cerita tentang hari pengguna
        MODES["journal_wait"] = False
        return ("Pengguna sedang bercerita tentang harinya untuk jurnal. Tanggapi dengan empati dan hangat dalam 2 sampai "
                "3 kalimat. Tambahkan kolom \"jurnal\" di JSON berisi ringkasan 1 sampai 2 kalimat dari ceritanya, "
                "diawali 'Hari ini kamu'. Kalau pesannya ternyata bukan cerita tentang harinya, isi \"jurnal\" dengan "
                "string kosong."), None
    if JOURNAL_START_RE.search(text):
        MODES["journal_wait"] = True
        return ("Pengguna ingin mengisi jurnal harian. Ajak dia bercerita tentang harinya dengan satu pertanyaan "
                "yang hangat dan spesifik, misalnya hal yang paling berkesan hari ini."), None
    now = datetime.now(WIB)
    if 19 <= now.hour < 23 and _evening_asked["date"] != now.date() and not is_quiet_now():
        today = now.strftime("%Y-%m-%d")
        if not any(e.get("tanggal") == today for e in load_journal()):
            _evening_asked["date"] = now.date()
            MODES["journal_wait"] = True
            return ("Sudah malam. Setelah menjawab pesan pengguna, tutup dengan satu pertanyaan hangat tentang "
                    "bagaimana harinya hari ini (untuk jurnal hariannya)."), None
    return "", None


# ---------- Pengingat berulang (dijadwalkan di server, dikirim ke robot saat waktunya) ----------
REMINDER_FILE = os.path.expanduser(os.environ.get("REMINDER_FILE", "~/robot-server/pengingat.json"))
_rem_lock = threading.Lock()
HARI_IDX = {"senin": 0, "selasa": 1, "rabu": 2, "kamis": 3, "jumat": 4, "sabtu": 5, "minggu": 6, "ahad": 6}
HARI_NAMA = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
BAGIAN_HARI = {"pagi": (6, 0), "siang": (12, 0), "sore": (16, 0), "malam": (19, 0)}
_NUMS = "|".join(sorted((k for k in NUM_VAL if k != "se"), key=len, reverse=True))
REM_TRIGGER_RE = re.compile(r"\b(ingatkan|ingetin|ingatin|ingetkan|pengingat|alarm|bangunkan|bangunin)\b", re.I)
REM_REPEAT_RE = re.compile(r"\b(?:setiap|tiap)\b", re.I)
REM_INTERVAL_RE = re.compile(r"\b(?:setiap|tiap)\s+(?:(\d+|" + _NUMS + r")\s+)?(menit|jam)\b", re.I)
REM_HALF_RE = re.compile(r"\b(?:setiap|tiap)\s+setengah\s+jam\b", re.I)
REM_DAY_RE = re.compile(r"\b(?:setiap|tiap)\s+(hari|pagi|siang|sore|malam|" + "|".join(HARI_IDX) + r")\b", re.I)
REM_LIST_RE = re.compile(r"\b(apa saja|apa aja|bacakan|sebutkan|lihat|cek|daftar)\b.*\bpengingat\w*|^pengingatku\b", re.I)
REM_DEL_ALL_RE = re.compile(r"\b(hapus|matikan|batalkan|hentikan)\b.*\bsemua\b.*\bpengingat\w*", re.I)
REM_DEL_RE = re.compile(r"\b(hapus|matikan|batalkan|hentikan|stop)\b.*?\bpengingat\w*\s*(?:nomor\s*(\d+)|(.+))?", re.I)


def _next_daily(h: int, m: int, now: datetime, weekday: int | None = None) -> float:
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    for _ in range(8):
        if t > now and (weekday is None or t.weekday() == weekday):
            return t.timestamp()
        t += timedelta(days=1)
    return t.timestamp()


def reminder_desc(r: dict) -> str:
    if r["jenis"] == "interval":
        n = r["menit"]
        return f"setiap {n // 60} jam" if n % 60 == 0 else f"setiap {n} menit"
    jam = f"jam {r['jam']:02d}.{r['mnt']:02d}"
    return f"setiap hari {jam}" if r["jenis"] == "harian" else f"setiap {HARI_NAMA[r['hari']]} {jam}"


def parse_reminder(text: str):
    """'ingatkan aku minum air setiap 1 jam', 'setiap hari jam 7 malam ingatkan aku belajar',
    'buat pengingat setiap senin jam 7 upacara'. Kembalikan dict jadwal atau None."""
    if not REM_TRIGGER_RE.search(text) or not REM_REPEAT_RE.search(text):
        return None
    now = datetime.now(WIB)
    clock = parse_clock(text)
    day = REM_DAY_RE.search(text)
    iv = REM_INTERVAL_RE.search(text)
    r = None
    if REM_HALF_RE.search(text):
        r = {"jenis": "interval", "menit": 30}
    elif day and day.group(1).lower() in HARI_IDX:  # mingguan
        h, mnt = clock or (7, 0)
        r = {"jenis": "mingguan", "hari": HARI_IDX[day.group(1).lower()], "jam": h, "mnt": mnt}
    elif day or (clock and not (iv and iv.group(1))):  # harian ("setiap hari/pagi/malam", "setiap jam 7 malam")
        part = day.group(1).lower() if day else "hari"
        h, mnt = clock or BAGIAN_HARI.get(part, (7, 0))
        r = {"jenis": "harian", "jam": h, "mnt": mnt}
    elif iv:  # interval ("setiap 2 jam", "tiap 30 menit", "setiap jam")
        cnt = iv.group(1)
        num = 1 if not cnt else (int(cnt) if cnt.isdigit() else NUM_VAL.get(cnt.lower(), 1))
        mins = int(num * (60 if iv.group(2).lower() == "jam" else 1))
        r = {"jenis": "interval", "menit": max(1, min(24 * 60, mins))}
    if not r:
        return None
    lab = CLOCK_RE.sub(" ", text)  # "jam 12 siang" dulu, baru pola "setiap jam"
    lab = REM_HALF_RE.sub(" ", lab)
    lab = REM_INTERVAL_RE.sub(" ", lab)
    lab = REM_DAY_RE.sub(" ", lab)
    lab = re.sub(r"\b(?:setiap|tiap)\b", " ", lab, flags=re.I)
    lab = re.sub(r"\b(?:tolong|buatkan|buat|bikin|pasang|tambah(?:kan)?|ya|dong|deh)\b", " ", lab, flags=re.I)
    lab = re.sub(r"\b(?:ingatkan|ingetin|ingatin|ingetkan|bangunkan|bangunin)\b(?:\s+(?:aku|saya|gue|gw|kami))?",
                 " ", lab, flags=re.I)
    lab = re.sub(r"\b(?:pengingat|alarm)\b", " ", lab, flags=re.I)
    lab = re.sub(r"^\s*(?:untuk|supaya|agar|biar|bahwa|kalau)\b", " ", lab.strip(), flags=re.I)
    lab = re.sub(r"\s+", " ", lab).strip(" .,!?") or "pengingat"
    r["label"] = lab[:60]
    r["berikut"] = (now.timestamp() + r["menit"] * 60 if r["jenis"] == "interval"
                    else _next_daily(r["jam"], r["mnt"], now, r.get("hari")))
    return r


def load_reminders() -> list[dict]:
    try:
        with open(REMINDER_FILE, encoding="utf-8") as f:
            return [r for r in json.load(f) if isinstance(r, dict) and "jenis" in r]
    except (OSError, ValueError):
        return []


def save_reminders(items: list[dict]):
    os.makedirs(os.path.dirname(REMINDER_FILE) or ".", exist_ok=True)
    write_json(REMINDER_FILE, items[-30:], ensure_ascii=False, indent=1)


def reminder_command(text: str):
    """Tambah / baca / hapus pengingat berulang. Kembalikan ucapan atau None."""
    with _rem_lock:
        items = load_reminders()
        if REM_DEL_ALL_RE.search(text):
            save_reminders([])
            return "Baik, semua pengingat sudah aku hapus."
        if REM_LIST_RE.search(text):
            if not items:
                return "Belum ada pengingat. Contohnya, bilang ingatkan aku minum air setiap 1 jam."
            parts = ". ".join(f"Nomor {i + 1}, {r['label']} {reminder_desc(r)}" for i, r in enumerate(items[:8]))
            return f"Kamu punya {len(items)} pengingat. {parts}."
        m = REM_DEL_RE.search(text)
        if m and items and not REM_REPEAT_RE.search(text):
            idx = None
            if m.group(2):
                idx = int(m.group(2)) - 1
            elif m.group(3):
                q = _clean_query(m.group(3)).lower()
                scores = [difflib.SequenceMatcher(None, q, r["label"].lower()).ratio() for r in items]
                best = max(range(len(items)), key=lambda i: scores[i])
                idx = best if scores[best] >= 0.45 or q in items[best]["label"].lower() else None
            if idx is None or not 0 <= idx < len(items):
                return "Pengingat yang mana ya? Sebutkan nomornya, misalnya hapus pengingat nomor satu."
            gone = items.pop(idx)
            save_reminders(items)
            return f"Baik, pengingat {gone['label']} sudah aku hapus."
        r = parse_reminder(text)
        if r:
            items.append(r)
            save_reminders(items)
            return f"Oke, aku ingatkan {r['label']} {reminder_desc(r)}."
    return None


def reminder_tick(now_ts: float | None = None):
    """Dipanggil berkala: pengingat yang sudah waktunya dikirim ke robot (lewat cek /health berikutnya)."""
    now_ts = now_ts or time.time()
    due = []
    with _rem_lock:
        items = load_reminders()
        changed = False
        for r in items:
            if r.get("berikut", 0) > now_ts:
                continue
            due.append(r["label"])
            if r["jenis"] == "interval":
                step = r["menit"] * 60
                nxt = r["berikut"]
                while nxt <= now_ts:
                    nxt += step
                r["berikut"] = nxt
            else:
                r["berikut"] = _next_daily(r["jam"], r["mnt"], datetime.fromtimestamp(now_ts, WIB), r.get("hari"))
            changed = True
        if changed:
            save_reminders(items)
    if due:
        say = "Hai, waktunya " + " dan ".join(dict.fromkeys(due)) + "!"
        old = unquote(PENDING.get("pengingat", ""))
        PENDING["pengingat"] = quote((old + " " + say).strip())
        print(f"[PENGINGAT] {say}")
        tele_notify(f"⏰ {say}")
    return due


# ---------- AI tanpa format JSON (untuk terjemahan & Telegram) ----------
def llm_plain(prompt: str, system: str = "") -> str:
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    for provider, model in llm_attempts():
        try:
            resp = _chat_request(provider, model, msgs, {"max_tokens": 1024} if provider == "gemini"
                                 else {"max_completion_tokens": 1024})
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        out = _read_stream(resp, lambda raw: None).strip()
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL).strip()
        if out:
            return out
    return ""


# ---------- 7. Penerjemah dengan suara bahasa aslinya ----------
LANG_VOICE = {
    "inggris": ("bahasa Inggris", "en-US-AvaMultilingualNeural"), "english": ("bahasa Inggris", "en-US-AvaMultilingualNeural"),
    "jawa": ("bahasa Jawa", "jv-ID-SitiNeural"), "sunda": ("bahasa Sunda", "su-ID-TutiNeural"),
    "arab": ("bahasa Arab", "ar-SA-ZariyahNeural"), "jepang": ("bahasa Jepang", "ja-JP-NanamiNeural"),
    "korea": ("bahasa Korea", "ko-KR-SunHiNeural"), "mandarin": ("bahasa Mandarin", "zh-CN-XiaoxiaoNeural"),
    "cina": ("bahasa Mandarin", "zh-CN-XiaoxiaoNeural"), "china": ("bahasa Mandarin", "zh-CN-XiaoxiaoNeural"),
    "prancis": ("bahasa Prancis", "fr-FR-DeniseNeural"), "perancis": ("bahasa Prancis", "fr-FR-DeniseNeural"),
    "jerman": ("bahasa Jerman", "de-DE-KatjaNeural"), "spanyol": ("bahasa Spanyol", "es-ES-ElviraNeural"),
    "belanda": ("bahasa Belanda", "nl-NL-ColetteNeural"), "melayu": ("bahasa Melayu", "ms-MY-YasminNeural"),
    "malaysia": ("bahasa Melayu", "ms-MY-YasminNeural"), "thailand": ("bahasa Thailand", "th-TH-PremwadeeNeural"),
    "rusia": ("bahasa Rusia", "ru-RU-SvetlanaNeural"), "italia": ("bahasa Italia", "it-IT-ElsaNeural"),
    "turki": ("bahasa Turki", "tr-TR-EmelNeural"), "hindi": ("bahasa Hindi", "hi-IN-SwaraNeural"),
}
_LANG_ALT = "|".join(sorted(LANG_VOICE, key=len, reverse=True))
TRANSLATE_RE = re.compile(r"\b(?:terjemah\w*|translate\w*|artikan)\s+(?:ke\s+|dalam\s+|ke\s+dalam\s+)?(?:bahasa\s+)?("
                          + _LANG_ALT + r")\b\s*[:,]?\s*(.+)$", re.I)
TRANSLATE_RE2 = re.compile(r"^(?:apa\s+)?bahasa\s+(" + _LANG_ALT + r")nya\s+(?:dari\s+)?(.+?)\s*(?:itu\s+)?(?:apa)?\s*\??$", re.I)


def translate_command(text: str):
    """Kembalikan (ucapan pembuka, terjemahan, kode suara, teks layar) atau None."""
    m = TRANSLATE_RE.search(text) or TRANSLATE_RE2.search(text)
    if not m:
        return None
    lang_key, src = m.group(1).lower(), m.group(2).strip(" .?!\"'")
    if not src:
        return None
    name, voice = LANG_VOICE[lang_key]
    out = llm_plain(f"Terjemahkan kalimat berikut ke {name} dengan wajar dan alami. Jawab HANYA dengan hasil "
                    f"terjemahannya, tanpa penjelasan, tanda kutip, atau cara baca.\n\n{src}")
    out = out.strip().strip('"').strip()
    if not out:
        return "Maaf, aku belum bisa menerjemahkannya sekarang.", "", None, ""
    return f"Dalam {name}:", out, voice, f"{name}: {out}"


# ---------- 8. Suara pengantar tidur (dibuat sendiri oleh server) ----------
SLEEP_RE = re.compile(r"\b(?:suara|bunyi|putar\w*|nyalakan|setel)\s+(?:suara\s+)?(hujan|ombak|laut|pantai|kipas(?:\s+angin)?|angin|"
                      r"white\s*noise|derau|noise)\b|\b(pengantar\s+tidur|white\s*noise|suara\s+tidur)\b", re.I)
SLEEP_SOUNDS = {
    # nama: (ucapan, sumber ffmpeg lavfi, penyaring)
    "hujan": ("suara hujan", "anoisesrc=color=pink:amplitude=0.6:sample_rate=16000",
              "highpass=f=300,lowpass=f=5000,volume=1.2"),
    "ombak": ("suara ombak", "anoisesrc=color=brown:amplitude=0.9:sample_rate=16000",
              "highpass=f=180,lowpass=f=1800,tremolo=f=0.12:d=0.85,volume=2.2"),
    "kipas": ("suara kipas angin", "anoisesrc=color=brown:amplitude=0.8:sample_rate=16000",
              "highpass=f=200,lowpass=f=900,volume=2.5"),
    "putih": ("derau putih", "anoisesrc=color=white:amplitude=0.25:sample_rate=16000", "lowpass=f=6000"),
}


def sleep_command(text: str):
    """Kembalikan (kunci suara, detik) atau None."""
    m = SLEEP_RE.search(text)
    if not m:
        return None
    w = (m.group(1) or m.group(2) or "").lower()
    key = ("ombak" if w in ("ombak", "laut", "pantai") else "kipas" if w.startswith(("kipas", "angin"))
           else "putih" if ("noise" in w or w == "derau") else "hujan")
    sec = parse_duration(text) or 30 * 60
    return key, int(max(60, min(120 * 60, sec)))


def sleep_stream(prefix: bytes, key: str, seconds: int):
    """Suara tidur selama `seconds`, mengecil pelan di 2 menit terakhir lalu berhenti sendiri."""
    _, src, flt = SLEEP_SOUNDS[key]
    fade = min(120, seconds // 3)
    af = f"{flt},afade=t=in:d=3,afade=t=out:st={seconds - fade}:d={fade},alimiter=limit={SPEAKER_LIMIT}:level=disabled"
    proc = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", src, "-t", str(seconds), "-af", af,
                             "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        yield prefix
        carry = b""
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            chunk = carry + chunk
            cut = len(chunk) // 2 * 2
            carry = chunk[cut:]
            yield chunk[:cut]
    finally:
        proc.kill()
        proc.wait()


# ---------- 6. Ngobrol lewat Telegram ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELE_FILE = os.path.expanduser(os.environ.get("TELE_FILE", "~/robot-server/telegram.json"))
TELE = {"owner": None, "ucap": False}
_tele_hist: deque = deque(maxlen=8)


def tele_load():
    try:
        with open(TELE_FILE, encoding="utf-8") as f:
            TELE.update(json.load(f))
    except (OSError, ValueError):
        pass
    if os.environ.get("TELEGRAM_CHAT_ID"):
        TELE["owner"] = int(os.environ["TELEGRAM_CHAT_ID"])


def tele_save():
    os.makedirs(os.path.dirname(TELE_FILE) or ".", exist_ok=True)
    write_json(TELE_FILE, TELE)


def tele_api(method: str, **params):
    if not TELEGRAM_TOKEN:
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}", json=params,
                          timeout=params.get("timeout", 0) + 15)
        return r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[TELEGRAM] {method} gagal: {type(e).__name__}")
        return None


def tele_send(chat_id, text: str):
    for i in range(0, len(text), 3800):  # batas panjang pesan Telegram
        tele_api("sendMessage", chat_id=chat_id, text=text[i:i + 3800])


def tele_notify(text: str):
    if TELEGRAM_TOKEN and TELE.get("owner"):
        tele_send(TELE["owner"], text)


TELE_HELP = (f"Halo, aku {ROBOT_NAME}! Kirim pesan apa saja untuk ngobrol denganku.\n\n"
             "Perintah:\n/bilang <teks> - aku ucapkan teks itu di rumah\n"
             "/suara on - jawabanku juga diucapkan robot di rumah\n/suara off - jawaban hanya di Telegram\n"
             "/status - keadaan robot\n\nKamu juga bisa minta: catat..., ingatkan aku ... setiap 1 jam, "
             "berita hari ini, terjemahkan ke bahasa Jepang: ...")


def tele_handle(chat_id: int, text: str):
    text = text.strip()
    if text.startswith("/start"):
        if TELE.get("owner") and TELE["owner"] != chat_id:
            tele_send(chat_id, "Maaf, robot ini sudah punya pemilik.")
            return
        if not TELE.get("owner"):
            if text.split(maxsplit=1)[1:] != [ROBOT_TOKEN]:
                tele_send(chat_id, "Untuk mendaftar sebagai pemilik, kirim: /start <ROBOT_TOKEN dari start.sh>")
                return
            TELE["owner"] = chat_id
            tele_save()
            print(f"[TELEGRAM] pemilik terdaftar: {chat_id}")
        tele_send(chat_id, TELE_HELP)
        return
    if chat_id != TELE.get("owner"):
        tele_send(chat_id, "Maaf, aku hanya mengobrol dengan pemilikku.")
        return
    if text.startswith("/bilang"):
        msg = clean_speech(text[7:]).strip()[:200]
        if not msg:
            tele_send(chat_id, "Tulis kalimatnya, contoh: /bilang Makan malam sudah siap!")
            return
        PENDING["pesan"] = quote(msg)
        log_chat("(Telegram) /bilang", msg)
        tele_send(chat_id, "Oke, akan aku ucapkan di rumah dalam beberapa detik.")
        return
    if text.startswith("/suara"):
        TELE["ucap"] = "on" in text.lower()
        tele_save()
        tele_send(chat_id, "Jawabanku sekarang juga diucapkan robot di rumah." if TELE["ucap"]
                  else "Jawabanku sekarang hanya di Telegram.")
        return
    if text.startswith("/status"):
        on = time.time() - ROBOT["terakhir"] < 45
        info = f"Robot: {'terhubung' if on else 'tidak terhubung'}"
        if ROBOT.get("suhu"):
            info += f"\nSuhu ruangan: {ROBOT['suhu']}°C, kelembapan {ROBOT.get('lembap')}%"
        if ROBOT.get("fw"):
            info += f"\nFirmware: v{ROBOT['fw']}"
        tele_send(chat_id, info)
        return
    # perintah yang sama seperti lewat suara
    reply = (reminder_command(text) or notes_command(text) or news_or_search(text))
    if not reply:
        tr = translate_command(text)
        if tr:
            reply = f"{tr[0]} {tr[1]}".strip()
    if not reply:
        hist = "\n".join(f"Pengguna: {u}\n{ROBOT_NAME}: {a}" for u, a in _tele_hist)
        extra = notes_context()
        reply = llm_plain(
            (f"Percakapan sebelumnya:\n{hist}\n\n" if hist else "") + f"Pengguna: {text}",
            system=(f"Kamu adalah {ROBOT_NAME}, robot teman ngobrol yang ramah dan cerdas. Pengguna sedang mengirim "
                    "pesan lewat Telegram dari luar rumah. Jawab dalam bahasa Indonesia yang hangat dan ringkas "
                    f"(maksimal 5 kalimat). {extra}")) or "Maaf, aku sedang tidak bisa berpikir. Coba lagi ya."
        _tele_hist.append((text, reply))
    tele_send(chat_id, reply)
    log_chat(f"(Telegram) {text}", reply)
    if TELE.get("ucap"):
        PENDING["pesan"] = quote(clean_speech(reply)[:200])


def start_telegram_thread():
    if not TELEGRAM_TOKEN:
        return
    tele_load()

    def run():
        offset = None
        me = tele_api("getMe")
        name = (me or {}).get("result", {}).get("username")
        print(f"[TELEGRAM] bot @{name} aktif" if name else "[TELEGRAM] token tidak valid / tidak ada internet")
        while True:
            res = tele_api("getUpdates", timeout=30, offset=offset, allowed_updates=["message"])
            if not res or not res.get("ok"):
                time.sleep(5)
                continue
            for up in res.get("result", []):
                offset = up["update_id"] + 1
                msg = up.get("message") or {}
                txt, cid = msg.get("text"), (msg.get("chat") or {}).get("id")
                if txt and cid:
                    try:
                        tele_handle(cid, txt)
                    except Exception as e:
                        traceback.print_exc()
                        tele_send(cid, f"Ada error: {type(e).__name__}")
    threading.Thread(target=run, daemon=True).start()


def start_reminder_thread():
    def run():
        while True:
            try:
                reminder_tick()
            except Exception as e:
                print(f"[PENGINGAT] error: {e}")
            time.sleep(10)
    threading.Thread(target=run, daemon=True).start()


# ---------- Mode jangan ganggu (malam) ----------
QUIET_ON_RE = re.compile(r"\b(?:aktifkan|nyalakan|hidupkan|pakai|masuk\w*)\b.*\bjangan\s+ganggu\b|^\s*mode\s+jangan\s+ganggu\s*$", re.I)
QUIET_OFF_RE = re.compile(r"\b(?:matikan|nonaktifkan|hentikan|keluar\w*|stop)\b.*\bjangan\s+ganggu\b", re.I)


def is_quiet_now() -> bool:
    if not SENYAP_ON:
        return False
    h = datetime.now(WIB).hour
    a, b = SENYAP_MULAI, SENYAP_SELESAI
    return (a <= h < b) if a < b else (h >= a or h < b)


# ---------- Berita & cari di internet (Gemini + pencarian Google) ----------
SEARCH_MODEL = os.environ.get("GEMINI_SEARCH_MODEL", "gemini-2.5-flash")
NEWS_RE = re.compile(r"\bberita\b|\bkabar\s+(?:terbaru|terkini|hari\s+ini)\b|\binfo\s+terkini\b|\bheadline\b", re.I)
WEB_RE = re.compile(r"\b(?:cari(?:kan|in)?|carikan)\s+(?:di\s+)?(?:internet|google|web)\s*(?:tentang\s+|soal\s+)?(.*)"
                    r"|\bgoogling(?:kan|in)?\s+(.*)", re.I)


def gemini_search(prompt: str) -> str | None:
    if not GEMINI_API_KEY:
        return None
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}], "generationConfig": {"temperature": 0.4}}
    t0 = time.time()
    try:
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{SEARCH_MODEL}:generateContent",
                          headers={"x-goog-api-key": GEMINI_API_KEY}, json=body, timeout=40)
    except requests.RequestException as e:
        print(f"[CARI] tidak bisa dihubungi: {e}")
        return None
    if r.status_code != 200:
        print(f"[CARI] gagal {r.status_code}: {r.text[:200]}")
        return None
    try:
        parts = r.json()["candidates"][0]["content"].get("parts", [])
    except (KeyError, IndexError, ValueError):
        return None
    text = " ".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    print(f"[CARI] {time.time() - t0:.1f} dtk: {text[:120]!r}")
    return trim_speech(clean_speech(text)) or None


def news_or_search(text: str):
    """Kembalikan ucapan untuk 'berita hari ini' / 'cari di internet ...', atau None kalau bukan perintah ini."""
    w = WEB_RE.search(text)
    news = NEWS_RE.search(text)
    if not w and not news:
        return None
    if not GEMINI_API_KEY:
        return "Fitur berita dan pencarian internet butuh kunci Gemini. Isi GEMINI_API_KEY di start.sh dulu ya."
    tanggal = datetime.now(WIB).strftime("%d-%m-%Y")
    gaya = (f"Sampaikan dalam bahasa Indonesia lisan yang santai untuk diucapkan robot bernama {ROBOT_NAME}: "
            "tanpa daftar, tanpa simbol, tanpa tautan, dan tanpa menyebut sumber satu per satu.")
    if w:
        q = _clean_query((w.group(1) or w.group(2) or "").strip())
        if not q:
            return "Mau aku carikan tentang apa?"
        prompt = f"Hari ini {tanggal} WIB. Cari informasi terbaru lalu jawab: {q}. Jawab 2 sampai 4 kalimat. {gaya}"
    else:
        topik = re.sub(r"\b(berita|kabar|info|terbaru|terkini|hari ini|headline|apa|ada|dong|ya|tentang|soal|"
                       r"bacakan|ceritakan|kasih|tahu|aku|saya|mau|ingin)\b", " ", text, flags=re.I)
        topik = _clean_query(re.sub(r"\s+", " ", topik).strip())
        about = f"tentang {topik}" if len(topik) > 2 else "di Indonesia"
        prompt = (f"Hari ini {tanggal} WIB. Cari dan rangkum 4 sampai 5 berita terpenting hari ini {about}. "
                  f"Jawab 5 sampai 7 kalimat. {gaya}")
    return gemini_search(prompt) or "Maaf ya, aku tidak bisa mencari berita sekarang. Coba lagi sebentar lagi."


# ---------- Atur volume dengan tombol ----------
VOL_ADJ_RE = re.compile(r"\b(?:atur|setel|ubah|sesuaikan|ganti)\s+(?:volume|kekerasan\s+suara)\b", re.I)


# ---------- Volume lewat suara (diatur di robot, level 1-10) ----------
VOL_SET_RE = re.compile(r"\bvolume\w*\s+(?:ke\s+)?(\d{1,2})\b", re.I)
VOL_UP_RE = re.compile(r"\b(besarkan|keraskan|naikkan|kencangkan|gedein|kerasin|tambah\w*)\b.*\b(suara|volume)\w*"
                       r"|\b(suara|volume)\w*.*\b(lebih keras|lebih besar|lebih kencang|kurang keras|terlalu pelan|terlalu kecil)\b", re.I)
VOL_DOWN_RE = re.compile(r"\b(kecilkan|pelankan|turunkan|kurangi|kecilin|pelanin)\b.*\b(suara|volume)\w*"
                         r"|\b(suara|volume)\w*.*\b(lebih pelan|lebih kecil|terlalu keras|terlalu besar|terlalu kencang)\b", re.I)


def volume_command(text: str):
    m = VOL_SET_RE.search(text)
    if m:
        n = max(1, min(10, int(m.group(1))))
        return str(n), f"Baik, volume aku atur ke {n}."
    if VOL_DOWN_RE.search(text):
        return "-", "Baik, suaraku aku kecilkan."
    if VOL_UP_RE.search(text):
        return "+", "Baik, suaraku aku besarkan."
    return None


# ---------- Relay lampu di robot ----------
# "nyalakan lampu" / "matikan lampu" -> header X-Lampu (on / off). Lampu tembakan HANYA dari gamepad.
LAMP_FIRE_RE = re.compile(r"\blampu\s+(?:tembak\w*|senjata)\b|\b(?:tembak\w*|senjata)\b.*\blampu\b", re.I)
LAMP_OFF_RE = re.compile(r"\b(?:matikan|matiin|mati(?:kan)?|padamkan|padamin|nonaktifkan|tutup|off(?:kan)?)\b(?:\W+\w+){0,3}?\W+lampu\w*"
                         r"|\blampu\w*(?:\W+\w+){0,3}?\W+(?:mati|padam|off)\b", re.I)
LAMP_ON_RE = re.compile(r"\b(?:nyalakan|nyalain|nyala|hidupkan|hidupin|menyalakan|aktifkan|salakan|terangi|on(?:kan)?)\b(?:\W+\w+){0,3}?\W+lampu\w*"
                        r"|\blampu\w*(?:\W+\w+){0,3}?\W+(?:nyala|menyala|hidup|on)\b", re.I)


def lamp_command(text: str):
    """(aksi, ucapan) untuk lampu relay, atau None. aksi: 'on' / 'off' / '' (hanya penjelasan)."""
    t = text.lower().strip()
    if "lampu" not in t or len(t.split()) > 9:
        return None
    if LAMP_FIRE_RE.search(t):
        return ("", "Lampu tembakan cuma bisa dari gamepad. Buka kuncinya dengan menahan L3 dan R3 sekitar satu detik, "
                    "lalu tahan R1 untuk menyalakannya.")
    if LAMP_OFF_RE.search(t):
        return ("off", "Siap, lampunya aku matikan.")
    if LAMP_ON_RE.search(t):
        return ("on", "Oke, lampunya aku nyalakan.")
    return None


UPDATE_MODE_RE = re.compile(r"\bmode\s+(?:update|pembaruan|perbarui|upgrade|ota|hotspot)\b"
                            r"|\b(?:jalankan|masuk\w*|buka|aktifkan|nyalakan)\s+(?:ke\s+)?(?:mode\s+)?hotspot\b", re.I)
FIRMWARE_RE = re.compile(r"\b(perbarui|update|upgrade|cek|periksa|pasang)\w*\s+(?:pembaruan\s+)?(firmware|software|perangkat lunak)\b"
                         r"|\b(firmware|pembaruan)\s+(baru|terbaru)\b", re.I)
FORGET_ME_RE = re.compile(r"\blupa\w*\b.*\b(tentang aku|tentangku|tentang diriku|semua ingatan\w*|yang kamu ingat)\b", re.I)


# ---------- Bicara per kalimat: kalimat pertama langsung dikirim, sisanya menyusul ----------
def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    merged = []
    for p in parts:  # gabungkan kalimat yang terlalu pendek supaya intonasi wajar
        if merged and len(merged[-1]) < 25:
            merged[-1] += " " + p
        else:
            merged.append(p)
    return merged or [text]


FIRST_CHUNK_MAX = 90   # kalimat pertama lebih panjang dari ini dipotong di koma, supaya suara pertama cepat jadi
MIN_CHUNK = 25         # potongan lebih pendek dari ini digabung dengan berikutnya (intonasi lebih wajar)


def take_chunks(text: str, pos: int, final: bool):
    """Ambil potongan siap-ucap dari text[pos:]. Kalau final=False, hanya potongan yang sudah pasti utuh.
    Kembalikan (daftar potongan, posisi baru)."""
    chunks = []
    while pos < len(text):
        rest = text[pos:]
        ends = [m.end() for m in re.finditer(r"[.!?]+(?=\s)", rest)]
        cut = None
        if pos == 0 and not chunks:
            if ends and ends[0] <= FIRST_CHUNK_MAX:
                cut = ends[0]
            else:  # kalimat pertama panjang: mulai bicara dari bagian pertamanya
                comma = re.search(r",(?=\s)", rest[MIN_CHUNK:FIRST_CHUNK_MAX + 40])
                if comma:
                    cut = MIN_CHUNK + comma.end()
                elif ends:
                    cut = ends[0]
        else:
            for e in ends:
                if len(rest[:e].strip()) >= (110 if (gemini_tts_active() or eleven_tts_active()) else MIN_CHUNK):
                    cut = e
                    break
        if cut is None:
            if final and rest.strip():
                chunks.append(rest.strip())
                pos = len(text)
            break
        chunks.append(rest[:cut].strip())
        pos += cut
    return [c for c in chunks if c], pos


class SpeechPipe:
    """Menyiapkan suara tiap potongan secara paralel, lalu mengalirkannya berurutan."""

    def __init__(self, t0: float | None = None, voice: str | None = None):
        self.ex = ThreadPoolExecutor(max_workers=3)
        self.futures = []
        self.t0 = t0 or time.time()
        self.voice = voice

    def add(self, text: str):
        text = clean_speech(text)
        if text:
            self.futures.append(self.ex.submit(synthesize, text, self.voice))

    def add_all(self, text: str):
        for c in take_chunks(text, 0, final=True)[0]:
            self.add(c)

    def stream(self):
        try:
            for i, f in enumerate(self.futures):
                pcm = f.result()
                if i == 0:
                    print(f"[WAKTU] suara pertama siap  : {time.time() - self.t0:5.2f} dtk")
                yield pcm
        finally:
            self.ex.shutdown(wait=False, cancel_futures=True)


def speech_stream(text: str):
    pipe = SpeechPipe()
    pipe.add_all(text)
    return pipe.stream()


SPK_TEST_RE = re.compile(r"\b(?:tes|test|coba)\s+(?:speaker|spiker|suara)\s+(?:hp|bluetooth|handphone)\b", re.I)
OUTPUT_RE = re.compile(r"\b(?:pakai|gunakan|pindah\w*\s+ke|ganti\s+ke|keluarkan\s+suara\w*\s+(?:di|lewat|dari))\s+"
                       r"(?:speaker|spiker|speker)\s+(hp|hape|bluetooth|handphone|ponsel|robot|kamu|sendiri|keduanya|dua-duanya|semua)\b"
                       r"|\bsuara\w*\s+(?:di|lewat|dari)\s+(hp|bluetooth|keduanya|robot)\b", re.I)
TTS_ENGINE_RE = re.compile(r"\b(?:pakai|ganti\s+ke|gunakan|pindah\s+ke)\s+suara\s+(gemini|google|edge|lama|bawaan|eleven\s*labs|eleven)\b", re.I)
AI_SWITCH_RE = re.compile(r"\b(?:pakai|ganti\s+ke|gunakan|pindah\s+ke)\s+(?:ai\s+|otak\s+)?(gemini|groq)\b", re.I)
GAME_MENU_RE = re.compile(r"\b(main|mainkan|buka|ayo|mau|pengen|ingin)\w*\s+(sebuah\s+)?(game|gim|games|video game)\b"
                          r"|\bmenu\s+(game|gim)\b|^(game|gim)\b", re.I)
# ---------- Roda dinamo, mode kontrol gamepad, mode bicara ----------
# Perintah gerak harus berupa kalimat perintah pendek ("maju", "almara putar badan dong") supaya kata "maju" di
# tengah obrolan ("semoga sekolahmu maju") tidak membuat robot berjalan.
MOVE_RE = re.compile(
    r"^(?:(?:hei|hai|halo|oke|ok)\s+)?(?:almara\s*,?\s*)?(?:coba\s+|tolong\s+|ayo\s+|sekarang\s+|bisa\s+|kamu\s+)?"
    r"(?P<cmd>maju|mundur|belok\s+kiri|belok\s+kanan|ke\s+kiri|ke\s+kanan|hadap\s+kiri|hadap\s+kanan|putar\s+kiri|"
    r"putar\s+kanan|putar\s+badan|balik\s+badan|berputar|muter|putar[- ]putar|joget|berjoget|menari|nari|goyang|dansa|"
    r"berhenti\s+(?:jalan|gerak|bergerak|joget|mengikuti\w*|ikut\w*)|stop\s+(?:jalan|gerak|joget|ikut\w*)|diam\s+di\s+tempat|"
    r"ikuti\s+(?:aku|saya|tanganku|tangan\s+(?:aku|saya))|ikut\s+(?:aku|saya))"
    r"(?P<rest>(?:\s+[\w-]+){0,4})[\s.!?,]*$", re.I)
MOVE_SAY = {
    "maju": ("Siap, aku maju!", "senang"), "mundur": ("Oke, aku mundur.", "normal"),
    "kiri": ("Belok kiri!", "senang"), "kanan": ("Belok kanan!", "senang"),
    "putar": ("Wiii, aku berputar!", "tertawa"), "joget": ("Ayo joget!", "senang"),
    "stop": ("Oke, aku diam di sini.", "normal"),
    "ikut": ("Siap! Aku ikuti tanganmu. Dekatkan tanganmu kurang dari sepuluh senti di depan sensor ya. Kalau lebih jauh, aku diam.", "senang"),
}
DIST_RE = re.compile(r"\b(?:berapa|seberapa)\s+(?:jarak|jauh|dekat)\b|\bjarak\s+(?:di\s+)?depan(?:mu)?\b|"
                     r"\bukur\s+jarak\b|\bada\s+(?:apa|benda|sesuatu)\s+di\s+depan(?:mu)?\b", re.I)
GERAK_ACAK_OFF_RE = re.compile(r"\b(?:matikan|nonaktifkan|hentikan|stop|jangan)\b.{0,20}\b(?:gerak|bergerak|jalan)\w*\s+"
                               r"(?:acak|sendiri|sendirian)\b|\bjangan\s+(?:gerak|bergerak|jalan|muter)\s+sendiri", re.I)
GERAK_ACAK_ON_RE = re.compile(r"\b(?:nyalakan|hidupkan|aktifkan|boleh)\b.{0,20}\b(?:gerak|bergerak|jalan)\w*\s+"
                              r"(?:acak|sendiri)\b", re.I)
KONTROL_ON_RE = re.compile(r"\bmode\s+(?:kontrol|kendali|gamepad|stik|stick|joystick|remote)\b|"
                           r"\b(?:aktifkan|nyalakan|pakai|gunakan|sambungkan|hidupkan)\s+(?:gamepad|stik|stick|joystick)\b", re.I)
KONTROL_OFF_RE = re.compile(r"\bmode\s+(?:speaker|musik|music)\b|\b(?:matikan|nonaktifkan)\s+(?:gamepad|stik|stick|joystick)\b", re.I)
TALK_TAP_RE = re.compile(r"\bmode\s+(?:bicara\s+|ngomong\s+)?(?:klik|sekali\s+(?:klik|tekan|pencet)|(?:tekan|klik|pencet)\s+sekali|"
                         r"otomatis|tanpa\s+tahan)\b", re.I)
TALK_HOLD_RE = re.compile(r"\bmode\s+(?:bicara\s+|ngomong\s+)?(?:tahan|tekan\s+tahan|tahan\s+tombol|tekan\s+dan\s+tahan)\b", re.I)
SETTINGS_MENU_RE = re.compile(r"\b(?:buka|tampilkan|lihat|masuk\s+ke)\s+(?:menu\s+)?(?:pengaturan|setelan|setting|settings)\b"
                              r"|^(?:menu\s+)?(?:pengaturan|setelan|setting)[.!?]*$", re.I)
TALK_SWITCH_RE = re.compile(r"\b(?:ganti|ubah|tukar)\s+mode\s+(?:bicara|ngomong)\b", re.I)


def move_command(text):
    """Perintah roda -> (kode untuk robot, ucapan, ekspresi) atau None."""
    t = re.sub(r"\s+", " ", text.strip().lower())
    m = MOVE_RE.match(t)
    if not m or MUSIC_RE.search(t) or RADIO_RE.search(t):
        return None
    c = m.group("cmd")
    rest = m.group("rest") or ""
    if c.startswith(("belok kiri", "ke kiri", "hadap kiri", "putar kiri")):
        key = "kiri"
    elif c.startswith(("belok kanan", "ke kanan", "hadap kanan", "putar kanan")):
        key = "kanan"
    elif c in ("maju", "mundur"):
        key = c
    elif c.startswith(("berhenti", "stop", "diam")):
        key = "stop"
    elif c.startswith(("ikuti", "ikut")):
        return "ikut", *MOVE_SAY["ikut"]
    elif c.startswith(("joget", "berjoget", "menari", "nari", "goyang", "dansa")):
        key = "joget"
    else:
        key = "putar"
    n = 1
    num = re.search(r"(\d+|dua|tiga|empat|lima)\s*(?:kali|detik|langkah)", rest)
    if num:
        w = num.group(1)
        n = int(w) if w.isdigit() else {"dua": 2, "tiga": 3, "empat": 4, "lima": 5}[w]
    elif re.search(r"\b(?:jauh|terus|lama|banyak)\b", rest):
        n = 3
    n = max(1, min(5, n))
    words, ekspresi = MOVE_SAY[key]
    return (key if key == "stop" else f"{key}:{n}"), words, ekspresi


THEME_RE = re.compile(r"\b(mode|tema|layar|tampilan|background|latar|warna layar)\w*\s+(?:jadi\s+|ke\s+|yang\s+)?"
                      r"(terang|putih|siang|cerah|gelap|hitam|malam)\b"
                      r"|\b(terangkan|putihkan|gelapkan|hitamkan)\s+(layar|tampilan|background|latar)\w*", re.I)
FX_OFF_RE = re.compile(r"\b(matikan|hilangkan|nonaktifkan|hentikan|tanpa)\b.*\befek\w*", re.I)
FX_ON_RE = re.compile(r"\b(nyalakan|hidupkan|aktifkan|munculkan|tampilkan|pakai)\b.*\befek\w*", re.I)


VOICE_STYLE_RE = re.compile(r"\b(suara|gaya|mode|nada)\w*\s+(?:kamu\s+|jadi\s+|ke\s+|yang\s+)?(imut|lucu|gemas|gemes|ceria|"
                            r"normal|biasa|lembut|kalem)\b", re.I)


def voice_style_command(text: str):
    """'suara imut' / 'suara normal'. Kembalikan ucapan atau None."""
    m = VOICE_STYLE_RE.search(text)
    if not m:
        return None
    if m.group(2).lower() in ("imut", "lucu", "gemas", "gemes", "ceria"):
        apply_settings({"gaya": "imut", "nada": 25, "tempo": 10})
        return "Yay, sekarang suaraku imut! Gimana, lebih lucu kan? Hehe."
    apply_settings({"gaya": "lembut", "nada": 0, "tempo": 10})
    return "Baik, aku kembali ke suara dan gaya bicaraku yang biasa."


def detect_theme(text: str):
    """Kembalikan (header, nilai, ucapan) untuk mode terang/gelap atau efek latar, atau None."""
    m = THEME_RE.search(text)
    if m:
        word = (m.group(2) or m.group(3)).lower()
        light = word in ("terang", "putih", "siang", "cerah", "terangkan", "putihkan")
        return ("X-Tema", "TERANG" if light else "GELAP",
                "Baik, layarku sekarang mode terang." if light else "Baik, layarku sekarang mode gelap.")
    if FX_OFF_RE.search(text):
        return "X-Efek", "0", "Baik, efek latar aku matikan."
    if FX_ON_RE.search(text):
        return "X-Efek", "1", "Baik, efek latar aku nyalakan."
    return None


# ---------- Perapi ucapan lewat AI (cadangan, hanya kalau perapi biasa tidak cukup) ----------
_RE_LUAS = {"ALARM_ASK_RE", "ROOM_RE", "OUTDOOR_RE", "CITY_RE", "SD_MUSIC_RE", "REST_RE", "DUR_RE", "VOL_SET_RE",
            "NOT_STORY_RE", "REM_INTERVAL_RE", "REM_HALF_RE", "REM_DAY_RE", "REM_REPEAT_RE", "CLOCK_RE"}
_cache_perapi: dict[str, str] = {}
_pool_perapi = ThreadPoolExecutor(max_workers=2)


def _perintah_dikenali(text: str) -> bool:
    """True kalau salah satu pola perintah di server ini cocok dengan kalimat tersebut."""
    for k, v in list(globals().items()):
        if k.endswith("_RE") and k not in _RE_LUAS and hasattr(v, "search") and v.search(text):
            return True
    return False


def _mirip_perintah(text: str) -> bool:
    """Murah: apakah kalimat ini kelihatan seperti perintah yang rusak? (kata mirip kata perintah, atau sangat pendek)"""
    kata = re.findall(r"[a-z]+", text.lower())
    if not kata or len(kata) > 10:
        return False
    if len(kata) <= 3:
        return True
    for w in kata:
        if len(w) >= 4 and w not in _SEMUA_PERINTAH and w not in _JANGAN_UBAH:
            for k in _KATA_PERINTAH:
                if abs(len(k) - len(w)) <= 2 and k[0] == w[0] and _jarak(w, k) <= 2:
                    return True
    return False


def perbaiki_ucapan_ai(text: str) -> str:
    """Kalau ucapan tidak cocok dengan perintah apa pun tapi kelihatan seperti perintah yang salah dengar, minta AI
    menuliskannya kembali jadi perintah baku. Hasilnya hanya dipakai kalau pola perintah server mengenalinya."""
    if (not text or text in BUTTON_LABEL or MODES["english"] or MODES["journal_wait"] or GAME["mode"]
            or _perintah_dikenali(text) or not _mirip_perintah(text)):
        return text
    key = text.lower().strip()
    if key in _cache_perapi:
        return _cache_perapi[key] or text
    system = ("Kamu perapi hasil pengenal suara untuk robot rumah berbahasa Indonesia. Teks bisa salah dengar atau typo. "
              "Kalau maksudnya JELAS sebuah perintah untuk robot (putar lagu atau radio, jeda, lanjut, ganti lagu, volume, "
              "timer, alarm, pengingat, catatan, cuaca, berita, terjemahan, gerak, mode, dan sejenisnya), tulis ulang menjadi "
              "satu kalimat perintah baku yang singkat; pertahankan nama, judul, angka, dan jam persis seperti yang diucapkan. "
              "Kalau itu hanya obrolan biasa, pertanyaan biasa, atau kamu tidak yakin, jawab persis: TETAP. "
              "Jawab hanya satu baris, tanpa penjelasan.")
    t0 = time.time()
    try:
        hasil = _pool_perapi.submit(llm_plain, f"Teks: {text}", system).result(timeout=4)
    except Exception:
        return text            # terlalu lama / gagal: biarkan apa adanya (jangan ganggu kecepatan)
    hasil = (hasil or "").strip().strip('"').splitlines()[0].strip() if (hasil or "").strip() else ""
    ok = bool(hasil) and hasil.upper() != "TETAP" and len(hasil) <= 120 and _perintah_dikenali(hasil)
    _cache_perapi[key] = hasil if ok else ""
    if len(_cache_perapi) > 200:
        _cache_perapi.pop(next(iter(_cache_perapi)))
    print(f"[PERAPI-AI] {text!r} -> {hasil!r} ({'dipakai' if ok else 'diabaikan'}, {time.time() - t0:.2f} dtk)")
    return hasil if ok else text


def detect_display(text: str) -> str | None:
    t = text.lower()
    if "mode" not in t and "tampilan" not in t and "tampilkan" not in t:
        return None
    has_text = re.search(r"\b(teks|text|tulisan)\b", t)
    has_face = re.search(r"\b(wajah|muka|mata|mochi|animasi)\b", t)
    if re.search(r"\b(campur\w*|gabung\w*|bersamaan|sekaligus)\b", t) or (has_text and has_face):
        return "CAMPUR"
    if re.search(r"\b(teks|text|tulisan)\b", t):
        return "TEKS"
    if re.search(r"\b(wajah|muka|mata|mochi|animasi)\b", t):
        return "WAJAH"
    return None


GEMINI_STT_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_STT_MODELS", "gemini-3.5-flash-lite,gemini-3.8-flash").split(",") if m.strip()]
STT_MIME = {".wav": "audio/wav", ".webm": "audio/webm", ".ogg": "audio/ogg", ".mp3": "audio/mp3", ".m4a": "audio/m4a"}
LAST_STT = {"nama": None}

# ---------- Perapi ucapan: typo / salah dengar pada kata perintah ----------
_KATA_PERINTAH = """putar putarkan mainkan setel nyalakan matikan hentikan berhenti lanjutkan lanjut ganti lewati ulangi
musik radio volume keraskan besarkan kecilkan pelankan naikkan turunkan mundur belok kanan joget menari ikuti
timer alarm ingatkan pengingat catat catatan hapus cuaca berita carikan terjemahkan artikan dongeng cerita jurnal
belajar istirahat pengaturan tampilan wajah suara senyap ganggu jangan bangunkan setiap menit detik besok""".split()
_KATA_PERINTAH.append(ROBOT_NAME.lower())
_SEMUA_PERINTAH = set(_KATA_PERINTAH)
# kata biasa yang mirip kata perintah (jangan diubah). Tambahkan sendiri kalau ada yang salah ganti.
_JANGAN_UBAH = {"wajar", "cacat", "musim", "timur", "detak", "putra", "putri", "kanal", "radio"}
_GAUL = {"matiin": "matikan", "nyalain": "nyalakan", "hidupin": "nyalakan", "hentiin": "hentikan",
         "stopin": "hentikan", "gantiin": "ganti", "mutar": "putar", "muter": "putar", "puter": "putar"}
_ISI_BEBAS = {"catat", "catatkan", "tulis", "tuliskan", "terjemahkan", "artikan", "cari", "carikan"}  # setelah ini = isi, jangan diubah


def _jarak(a: str, b: str) -> int:
    """Jarak edit (tambah/hapus/ganti/tukar posisi huruf) antara dua kata."""
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def _urutan(a: str, b: str) -> bool:
    """True kalau semua huruf a muncul berurutan di b (a = b dengan beberapa huruf hilang)."""
    it = iter(b)
    return all(c in it for c in a)


def perbaiki_ucapan(text: str) -> str:
    """Betulkan typo / salah dengar ringan pada kata perintah (\"putra lagu\" -> \"putar lagu\"), supaya perintah
    suara tetap dikenali. Kata biasa, isi catatan/terjemahan, dan jawaban kuis tidak disentuh."""
    if not text or text in BUTTON_LABEL or MODES["english"] or MODES["journal_wait"] or GAME["mode"]:
        return text
    text = re.sub(r"\bputra\b(?=\s+(?:lagu|musik|radio)\b)", "putar", text, flags=re.I)

    kata = text.split(" ")
    hasil, i = [], 0
    while i < len(kata):
        if i + 1 < len(kata) and kata[i].isalpha() and kata[i + 1].isalpha() and len(kata[i]) <= 7 and len(kata[i + 1]) <= 7:
            a, b = kata[i].lower(), kata[i + 1].lower()
            j = a + b
            if (j in _SEMUA_PERINTAH or j in _GAUL) and not (a in _SEMUA_PERINTAH and b in _SEMUA_PERINTAH):
                hasil.append(_GAUL.get(j, j))
                i += 2
                continue
        hasil.append(kata[i])
        i += 1
    text = " ".join(hasil)
    out, bebas = [], False
    for tok in re.split(r"(\W+)", text):
        low = tok.lower()
        if bebas or not low.isalpha():
            out.append(tok)
            continue
        if low in _ISI_BEBAS:
            bebas = True
        if low in _GAUL:
            out.append(_GAUL[low])
            continue
        if len(low) < 4 or low in _SEMUA_PERINTAH or low in _JANGAN_UBAH:
            out.append(tok)
            continue
        best, bd, seri = None, 99, False
        for k in _KATA_PERINTAH:
            if k[0] != low[0]:
                continue
            n = len(low)
            if n == 4:    # satu huruf hilang: "alrm" -> "alarm"
                d = 1 if (len(k) == 5 and k[-1] == low[-1] and _urutan(low, k)) else 99
            elif n == 5:  # huruf tertukar ("putra" -> "putar") atau satu huruf hilang ("mundr" -> "mundur");
                #           kata 5 huruf lain terlalu rawan salah ganti ("kapan" jangan jadi "kanan")
                if sorted(low) == sorted(k) and low != k:
                    d = 1
                else:
                    d = 1 if (len(k) == 6 and k[-1] == low[-1] and _urutan(low, k)) else 99
            elif abs(len(k) - n) <= 2:
                d = _jarak(low, k)
                d = d if d <= (1 if n < 8 else 2) else 99
            else:
                d = 99
            if d < bd:
                best, bd, seri = k, d, False
            elif d == bd and d < 99 and best != k:
                seri = True
        if best and not seri:
            out.append(ROBOT_NAME if best == ROBOT_NAME.lower() else best)
        else:
            out.append(tok)
    hasil = "".join(out)
    if hasil != text:
        print(f"[PERBAIKI] {text!r} -> {hasil!r}")
    return hasil


STT_HINT = (f"{ROBOT_NAME}, putar lagu. Jeda. Lanjut. Ganti lagu berikutnya. Kecilkan volume. "
            "Maju, mundur, belok kiri, belok kanan. Ingatkan aku jam tujuh. Catat. Cuaca besok.")


def _buang_bocoran_hint(text: str) -> str:
    """Kalau rekaman hening, pengenal suara kadang mengulang petunjuk di atas. Buang hasil seperti itu."""
    from difflib import SequenceMatcher
    if len(text) > 40 and SequenceMatcher(None, text.lower(), STT_HINT.lower()).ratio() > 0.75:
        return ""
    return text



def groq_transcribe(wav_path: str, auto_lang: bool) -> str:
    """Whisper di Groq. Kalau satu key kena batas (429) atau error server, langsung coba key Groq berikutnya
    sebelum menyerah ke Gemini (yang lebih lambat)."""
    tried = set()
    last_err = None
    for _ in range(min(3, max(1, len(GROQ_KEYS)))):
        key = pick_key("groq-stt", [k for k in GROQ_KEYS if k not in tried]) or (GROQ_API_KEY if GROQ_API_KEY not in tried else None)
        if not key:
            break
        tried.add(key)
        try:
            with open(wav_path, "rb") as f:
                resp = requests.post(
                    f"{GROQ_BASE}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": f},
                    data={"model": "whisper-large-v3-turbo", "temperature": "0",
                          **({} if auto_lang else {"language": "id", "prompt": STT_HINT})},
                    timeout=15,
                )
        except requests.RequestException as e:
            last_err = e
            continue
        if resp.status_code == 429:
            rest_key(key, 60)
            last_err = RuntimeError("429")
            continue
        if resp.status_code >= 500:
            last_err = RuntimeError(str(resp.status_code))
            continue
        resp.raise_for_status()
        return _buang_bocoran_hint(resp.json().get("text", "").strip())
    raise last_err or RuntimeError("Groq STT gagal")


def gemini_transcribe(wav_path: str, auto_lang: bool) -> str:
    """Transkripsi lewat Gemini Flash-Lite (audio + instruksi, temperature 0)."""
    with open(wav_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode()
    mime = STT_MIME.get(os.path.splitext(wav_path)[1].lower(), "audio/wav")
    lang = "Bahasa Indonesia atau bahasa Inggris" if auto_lang else "bahasa Indonesia"
    prompt = (f"Transkripsikan ucapan dalam audio ini kata demi kata ({lang}). Tulis HANYA transkripnya, tanpa "
              "penjelasan, tanda kutip, atau label. Kalau tidak ada ucapan yang jelas, jawab dengan teks kosong. "
              f"Nama robotnya {ROBOT_NAME}; kata perintah yang sering muncul: putar, jeda, lanjut, ganti lagu, volume, "
              "maju, mundur, belok, ingatkan, catat, cuaca.")
    body = {"contents": [{"role": "user", "parts": [{"text": prompt},
                                                    {"inline_data": {"mime_type": mime, "data": audio_b64}}]}],
            "generationConfig": {"temperature": 0}}
    last = ""
    for model in GEMINI_STT_MODELS:
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              headers={"x-goog-api-key": GEMINI_API_KEY}, json=body, timeout=30)
        except requests.RequestException as e:
            last = f"{model}: {type(e).__name__}"
            continue
        if r.status_code != 200:
            print(f"[STT] Gemini {model} gagal {r.status_code}: {r.text[:150]}")
            last = f"{model}: {r.status_code}"
            continue
        try:
            parts = r.json()["candidates"][0]["content"].get("parts", [])
        except (KeyError, IndexError, ValueError):
            parts = []
        text = " ".join(p.get("text", "") for p in parts if not p.get("thought")).strip().strip('"').strip()
        # model kadang menulis penanda seperti "[tidak ada ucapan]" untuk rekaman hening
        if re.fullmatch(r"[\[\(<].*[\]\)>]\.?|(tidak ada|no) (ucapan|speech).*|-+|\.+", text, re.I | re.S):
            text = ""
        LAST_STT["nama"] = f"gemini/{model}"
        return text
    raise RuntimeError(f"Pengenalan suara Gemini gagal ({last})")


def transcribe(wav_path: str, auto_lang: bool = False) -> str:
    """Groq Whisper kalau ada key-nya; kalau tidak ada atau gagal, pakai Gemini."""
    if GROQ_API_KEY:
        try:
            text = groq_transcribe(wav_path, auto_lang)
            LAST_STT["nama"] = "groq/whisper"
            return text
        except Exception as e:
            if not GEMINI_API_KEY:
                raise
            print(f"[STT] Groq Whisper gagal ({e}), pakai Gemini")
    if GEMINI_API_KEY:
        return gemini_transcribe(wav_path, auto_lang)
    raise RuntimeError("Tidak ada API key untuk pengenalan suara. Isi GROQ_API_KEY atau GEMINI_API_KEY di start.sh.")


def provider_info(provider: str):
    """(alamat dasar, daftar key) untuk penyedia AI."""
    if provider == "gemini":
        return GEMINI_BASE, GEMINI_KEYS
    if provider == "groq":
        return GROQ_BASE, GROQ_KEYS
    base, keys, _ = EXTRA_AI[provider]
    return base, keys


def _chat_request(provider: str, model: str, messages: list, extras: dict, key: str | None = None):
    base, keys = provider_info(provider)
    key = key or (keys[0] if keys else "")
    return requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages, "stream": True, **extras},
        timeout=40,
        stream=True,
    )


def llm_attempts() -> list[tuple[str, str]]:
    """Urutan (penyedia, model) yang dicoba: AI utama dulu, lalu yang lain sebagai cadangan."""
    order = ["gemini", "groq"] if AI_UTAMA == "gemini" else ["groq", "gemini"]
    out = []
    for p in order:
        if p == "gemini" and GEMINI_API_KEY:
            out += [("gemini", m) for m in GEMINI_MODELS]
        if p == "groq" and GROQ_API_KEY:
            out += [("groq", m) for m in LLM_MODELS]
    for name, (_, _, models) in EXTRA_AI.items():  # layanan tambahan sebagai cadangan / peserta balapan
        out += [(name, m) for m in models]
    return out


def llm_extras(provider: str, model: str) -> dict:
    if provider not in ("gemini", "groq"):
        return {"max_tokens": 2048}
    if provider == "gemini":
        return {"max_tokens": 2048, "reasoning_effort": REASONING_EFFORT}
    if model.startswith("openai/gpt-oss"):  # gpt-oss "berpikir" dulu: jatah token harus cukup
        return {"max_completion_tokens": 2048, "reasoning_effort": REASONING_EFFORT, "include_reasoning": False}
    return {"max_completion_tokens": 2048}


ESCAPES = {"n": " ", "t": " ", "r": " ", '"': '"', "\\": "\\", "/": "/", "b": "", "f": ""}


def partial_ucapan(raw: str):
    """Isi kolom "ucapan" dari JSON yang mungkin belum selesai. Kembalikan (teks, selesai) atau (None, False)."""
    m = re.search(r'"ucapan"\s*:\s*"', raw)
    if not m:
        return None, False
    out, i = [], m.end()
    while i < len(raw):
        c = raw[i]
        if c == "\\":
            if i + 1 >= len(raw):
                break
            n = raw[i + 1]
            if n == "u":
                if i + 6 > len(raw):
                    break
                try:
                    out.append(chr(int(raw[i + 2:i + 6], 16)))
                except ValueError:
                    pass
                i += 6
                continue
            out.append(ESCAPES.get(n, n))
            i += 2
            continue
        if c == '"':
            return "".join(out), True
        out.append(c)
        i += 1
    return "".join(out), False


def _read_stream(resp, on_text):
    """Baca jawaban AI potong demi potong (format SSE). on_text(teks_sejauh_ini) dipanggil tiap ada tambahan."""
    raw = ""
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            delta = json.loads(payload)["choices"][0]["delta"].get("content") or ""
        except (ValueError, KeyError, IndexError):
            continue
        if delta:
            raw += delta
            on_text(raw)
    return raw


def ask_llm(user_text: str, extra: str = "", pipe: "SpeechPipe | None" = None,
            max_chars: int = MAX_SPEECH_CHARS) -> dict:
    """Tanya AI. Kalau pipe diberikan, suara tiap kalimat mulai disiapkan selagi AI masih menulis."""
    global _last_chat
    with _lock:
        if time.time() - _last_chat > HISTORY_IDLE_SEC:
            _history.clear()
        messages = [{"role": "system", "content": system_prompt(extra)}, *_history,
                    {"role": "user", "content": user_text}]

    t0 = time.time()
    state = {"pos": 0, "first": None}

    def on_text(raw):
        if pipe is None:
            return
        ucap, done = partial_ucapan(raw)
        if ucap is None or state["pos"] >= max_chars:
            return
        chunks, state["pos"] = take_chunks(ucap, state["pos"], final=done)
        for c in chunks:
            if state["first"] is None:
                state["first"] = time.time() - t0
            pipe.add(c)

    last_error = "tidak ada AI yang diatur"
    raw = None
    winner = {"lock": threading.Lock(), "who": None}
    conns = {}  # peserta -> koneksi, supaya yang kalah bisa langsung diputus

    def one(provider, model, key, cancel):
        """Satu peserta: kembalikan (raw, error). Hanya pemenang yang meneruskan teks ke suara."""
        me = (provider, model, key)
        try:
            resp = _chat_request(provider, model, messages, llm_extras(provider, model), key)
            if resp.status_code == 400 and provider in ("gemini", "groq") and \
                    (provider == "gemini" or "model" not in resp.text.lower()):
                # ada pengaturan yang tidak didukung model ini: coba lagi dengan permintaan polos
                plain = {"max_tokens": 2048} if provider == "gemini" else {"max_completion_tokens": 2048}
                resp = _chat_request(provider, model, messages, plain, key)
        except requests.RequestException as e:
            return None, f"{provider}/{model}: {type(e).__name__}"
        conns[me] = resp
        if cancel.is_set() and winner["who"] != me:
            resp.close()
            return None, "kalah balapan"
        if resp.status_code != 200:
            # 429 = kuota habis, 401/403 = key salah, 404 = model tidak ada, 5xx = server sibuk
            if resp.status_code == 429:
                rest_key(key, 60)
            elif resp.status_code in (401, 403):
                rest_key(key, 1800)
            print(f"[LLM] {provider}/{model} gagal {resp.status_code}: {resp.text[:160]}")
            return None, f"{provider}/{model}: {resp.status_code}"
        buf = ""
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if cancel.is_set() and winner["who"] != me:
                    resp.close()
                    return None, "kalah balapan"
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0]["delta"].get("content") or ""
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                if not delta:
                    continue
                buf += delta
                if winner["who"] is None and partial_ucapan(buf)[0]:  # sudah ada isi ucapan: menang
                    with winner["lock"]:
                        if winner["who"] is None:
                            winner["who"] = me
                            cancel.set()
                    if winner["who"] == me:
                        for other, r in list(conns.items()):
                            if other != me:
                                try:
                                    r.close()  # putus peserta lain sekarang juga
                                except Exception:
                                    pass
                if winner["who"] == me:
                    on_text(buf)
        except Exception as e:  # termasuk koneksi yang sengaja diputus karena kalah
            if winner["who"] not in (None, me):
                return None, "kalah balapan"
            return (buf or None), f"{provider}/{model}: {type(e).__name__}"
        return buf, None

    # Peserta dipilih per putaran: utamakan penyedia yang BERBEDA (kalau satu penyedia sedang lambat / habis
    # kuota, yang lain tetap bisa menang). Key dipilih saat itu juga, jadi key yang sedang istirahat dilewati.
    queue_ = list(llm_attempts())
    while queue_ and raw is None:
        batch = []
        for distinct in (True, False):
            for item in list(queue_):
                if len(batch) >= AI_BALAPAN:
                    break
                provider, model = item
                if distinct and any(b[0] == provider for b in batch):
                    continue
                key = pick_key(provider, provider_info(provider)[1])
                if not key:  # semua key penyedia ini sedang istirahat
                    queue_.remove(item)
                    continue
                batch.append((provider, model, key))
                queue_.remove(item)
        if not batch:
            break
        cancel = threading.Event()
        winner["who"] = None
        conns.clear()
        if len(batch) == 1:
            results = {batch[0]: one(*batch[0], cancel)}
        else:
            ex = ThreadPoolExecutor(max_workers=len(batch))
            futs = {b: ex.submit(one, *b, cancel) for b in batch}
            ex.shutdown(wait=False)  # peserta yang kalah selesai sendiri di belakang
            results = {}
            pending = set(futs.values())
            while pending:
                done, pending = _futwait(pending, timeout=45, return_when=FIRST_COMPLETED)
                if not done:
                    break
                for b, f in futs.items():
                    if f in done:
                        results[b] = f.result()
                w = winner["who"]
                if w and w in results and results[w][0]:  # pemenang sudah selesai menulis: tidak perlu menunggu
                    break
                if w is None and any(r[0] for r in results.values()):
                    break
            for b in batch:
                results.setdefault(b, (None, "kalah balapan"))
        order = ([winner["who"]] if winner["who"] else []) + [b for b in batch if b != winner["who"]]
        for b in order:
            text, err = results[b]
            if text and text.strip():
                if winner["who"] is None:  # tidak ada yang sempat menang (mis. jawaban tanpa JSON): pakai ini
                    on_text(text)
                raw = text.strip()
                provider, model = b[0], b[1]
                LAST_AI.update(nama=f"{provider}/{model}", waktu=datetime.now(WIB).strftime("%H.%M"))
                others = [f"{x[0]}/{x[1]}" for x in batch if x != b]
                print(f"[LLM] {provider}/{model}" + (f" (menang balapan vs {', '.join(others)})" if others else "")
                      + f": {raw[:200]}")
                break
            if err and err != "kalah balapan":
                last_error = err
    if raw is None:
        raise RuntimeError(f"Semua AI gagal ({last_error}). Cek API key dan kuota di start.sh.")
    t_done = time.time() - t0

    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        try:
            data = json.loads(match.group(0)) if match else {}
        except json.JSONDecodeError:
            data = {}
        if not data and raw:
            data = {"ucapan": raw}  # model menjawab teks biasa: tetap diucapkan

    if str(data.get("ingat") or "").strip():
        add_memory(str(data["ingat"]))
    full = str(data.get("ucapan") or "Maaf, aku sedang bingung. Bisa diulang?")
    reply = {
        "ucapan": trim_speech(clean_speech(full), max_chars),
        "ekspresi": data.get("ekspresi", "normal"),
        "gerak": "diam",
        "benar": data.get("benar"),
        "jurnal": str(data.get("jurnal") or "").strip(),
    }
    if pipe is not None:
        if state["pos"] == 0:  # tidak ada yang sempat dialirkan (mis. bukan JSON): siapkan semuanya sekarang
            pipe.add_all(reply["ucapan"])
        elif state["pos"] < min(len(full), max_chars):  # sisa yang belum diproses
            pipe.add_all(trim_speech(full[state["pos"]:], max_chars - state["pos"]))
    first = f"{state['first']:.2f}" if state["first"] is not None else "-"
    print(f"[WAKTU] AI: potongan pertama {first} dtk, selesai {t_done:.2f} dtk")

    with _lock:
        _history.append({"role": "user", "content": user_text})
        _history.append({"role": "assistant", "content": json.dumps(
            {"ucapan": reply["ucapan"], "ekspresi": reply["ekspresi"], "ingat": ""}, ensure_ascii=False)})
        del _history[:-HISTORY_TURNS * 2]
        _last_chat = time.time()
    return reply

# ---------- Suara Gemini (30 suara bawaan, bahasa Indonesia didukung) ----------
GEMINI_TTS_VOICES = {
    "Leda": "muda", "Laomedeia": "ceria", "Puck": "ceria", "Zephyr": "cerah", "Autonoe": "cerah",
    "Sadachbia": "lincah", "Fenrir": "bersemangat", "Aoede": "ringan", "Achird": "ramah", "Sulafat": "hangat",
    "Achernar": "lembut", "Vindemiatrix": "lembut", "Enceladus": "lembut berdesah", "Callirrhoe": "santai",
    "Umbriel": "santai", "Zubenelgenubi": "kasual", "Algieba": "halus", "Despina": "halus", "Iapetus": "jelas",
    "Erinome": "jelas", "Charon": "informatif", "Rasalgethi": "informatif", "Sadaltager": "berwawasan",
    "Kore": "tegas", "Orus": "tegas", "Alnilam": "tegas", "Schedar": "stabil", "Pulcherrima": "lugas",
    "Gacrux": "dewasa", "Algenib": "serak",
}
MESIN_SUARA = os.environ.get("MESIN_SUARA", "elevenlabs" if _keys("ELEVENLABS_API_KEY") else "gemini")  # "elevenlabs", "gemini", atau "edge"
GEMINI_VOICE = os.environ.get("GEMINI_VOICE", "Leda")
GEMINI_TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview")  # cepat, cocok untuk ngobrol
_gtts_pause = {"until": 0.0}  # setelah kuota habis / key salah: istirahatkan Gemini sebentar


def gemini_tts_active() -> bool:
    return MESIN_SUARA == "gemini" and bool(GEMINI_API_KEY) and time.time() >= _gtts_pause["until"]


def gemini_tts(text: str, wav_path: str) -> bool:
    """Buat suara dengan Gemini TTS ke file WAV. False kalau gagal (pemanggil memakai edge-tts)."""
    style = ("Ucapkan dengan santai dan natural seperti teman perempuan seumuran yang sedang ngobrol langsung "
             "dalam bahasa Indonesia sehari-hari: tempo rileks, intonasi naik turun wajar, ada jeda napas kecil, "
             "tidak seperti membaca teks atau pembawa berita" + (", nada ceria" if GAYA == "imut" else ", nada hangat dan lembut"))
    body = {
        "contents": [{"role": "user", "parts": [{"text": f"{style}:\n\n{text}"}]}],
        "generationConfig": {"responseModalities": ["AUDIO"],
                             "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": GEMINI_VOICE}}}},
    }
    t0 = time.time()
    r = None
    for percobaan in range(2):   # hanya ulang sekali untuk gangguan sesaat (5xx / jaringan); kuota habis (429) TIDAK diulang
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TTS_MODEL}:generateContent",
                              headers={"x-goog-api-key": GEMINI_API_KEY}, json=body, timeout=15)
        except requests.RequestException as e:
            print(f"[TTS] Gemini tidak bisa dihubungi ({type(e).__name__}), percobaan {percobaan + 1}/2")
            r = None
            continue
        if r.status_code in (500, 502, 503, 504) and percobaan < 1:
            print(f"[TTS] Gemini {r.status_code}, coba lagi")
            time.sleep(1.0)
            continue
        break
    if r is None:
        print("[TTS] Gemini tidak bisa dihubungi -> pakai edge-tts")
        return False
    if r.status_code != 200:
        print(f"[TTS] Gemini gagal {r.status_code}: {r.text[:150]} -> pakai edge-tts")
        if r.status_code == 429:
            _gtts_pause["until"] = time.time() + 300     # kuota habis: langsung Edge (tanpa menunggu), Gemini dicoba lagi 5 menit kemudian
        elif r.status_code in (401, 403):
            _gtts_pause["until"] = time.time() + 1800    # key salah / tidak diizinkan
        return False
    try:
        data = base64.b64decode(r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
    except (KeyError, IndexError, ValueError, TypeError):
        print(f"[TTS] jawaban Gemini tidak berisi audio: {r.text[:150]}")
        return False
    if len(data) < 1000:
        return False
    if data[:4] == b"RIFF":
        with open(wav_path, "wb") as f:
            f.write(data)
    else:  # PCM mentah 24 kHz: bungkus jadi WAV
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(data)
    print(f"[TTS] Gemini {GEMINI_VOICE}: {time.time() - t0:.2f} dtk ({text[:30]!r})")
    return True


# ---------- Suara ElevenLabs (paling natural; Gemini lalu Edge jadi cadangan) ----------
ELEVEN_KEYS = _keys("ELEVENLABS_API_KEY")
ELEVEN_VOICE = os.environ.get("ELEVENLABS_VOICE", "21m00Tcm4TlvDq8ikWAM").strip()   # Voice ID dari elevenlabs.io
ELEVEN_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")          # cepat: eleven_flash_v2_5
ELEVEN_STABILITY = float(os.environ.get("ELEVENLABS_STABILITY", "0.4"))              # rendah = lebih ekspresif
_eleven_sem = threading.Semaphore(2)  # paket gratis hanya mengizinkan 2 permintaan bersamaan


def eleven_tts_active() -> bool:
    if MESIN_SUARA != "elevenlabs" or not ELEVEN_KEYS or not ELEVEN_VOICE:
        return False
    return any(_key_rest.get(k, 0) <= time.time() for k in ELEVEN_KEYS)


def eleven_tts(text: str, wav_path: str) -> bool:
    """Suara ElevenLabs ke file WAV 16 kHz. False kalau gagal (pemanggil lanjut ke Gemini / Edge)."""
    key = pick_key("elevenlabs", ELEVEN_KEYS)
    if not key:
        return False
    body = {"text": text, "model_id": ELEVEN_MODEL,
            "voice_settings": {"stability": ELEVEN_STABILITY, "similarity_boost": 0.75, "style": 0.2,
                               "use_speaker_boost": True, "speed": 1.0}}
    t0 = time.time()
    try:
        with _eleven_sem:
            r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}",
                              params={"output_format": "pcm_16000"},
                              headers={"xi-api-key": key, "Content-Type": "application/json"},
                              json=body, timeout=20)
    except requests.RequestException as e:
        print(f"[TTS] ElevenLabs tidak bisa dihubungi ({type(e).__name__}) -> cadangan")
        return False
    if r.status_code != 200:
        print(f"[TTS] ElevenLabs gagal {r.status_code}: {r.text[:200]} -> cadangan")
        if r.status_code == 429:
            rest_key(key, 15)
        elif r.status_code in (401, 402, 403):   # kuota karakter habis / key salah / voice butuh paket berbayar
            rest_key(key, 3600 if "quota" in r.text.lower() else 1800)
        return False
    data = r.content
    if len(data) < 1000:
        return False
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(data[: len(data) // 2 * 2])
    print(f"[TTS] ElevenLabs: {time.time() - t0:.2f} dtk ({text[:30]!r})")
    return True


def synthesize(text: str, voice: str | None = None) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        mp3_path = tmp.name
    wav_path = mp3_path + ".wav"
    pcm_path = mp3_path + ".pcm"
    try:
        src = mp3_path
        if not voice:
            if eleven_tts_active() and eleven_tts(text, wav_path):
                src = wav_path
            elif (MESIN_SUARA in ("gemini", "elevenlabs") and GEMINI_API_KEY and time.time() >= _gtts_pause["until"]
                  and gemini_tts(text, wav_path)):
                src = wav_path
        if src == mp3_path:
            # edge-tts butuh event loop async; dijalankan sinkron di sini karena
            # Flask dev server sudah cukup untuk satu robot.
            def _edge(v):
                asyncio.run(edge_tts.Communicate(text, v, rate=TTS_RATE, pitch=TTS_PITCH).save(mp3_path))
            try:
                try:
                    _edge(voice or VOICE_ID)
                except Exception:
                    if voice and voice != VOICE_ID:
                        print(f"[TTS] suara {voice} gagal, pakai suara biasa")
                    else:
                        time.sleep(0.4)  # error 403 kadang sementara / selisih jam: coba sekali lagi
                    _edge(VOICE_ID)
            except Exception as e:
                # Edge ditolak Microsoft (biasanya 403: edge-tts versi lama). Jangan sampai robot bisu: coba mesin cadangan.
                print(f"[TTS] Edge gagal ({type(e).__name__}: {str(e)[:70]}). Perbaiki dengan: pip install -U edge-tts")
                if GEMINI_API_KEY and gemini_tts(text, wav_path):
                    src = wav_path
                elif eleven_tts_active() and eleven_tts(text, wav_path):
                    src = wav_path
                else:
                    raise
        # Konversi ke PCM16 mono 16kHz langsung lewat ffmpeg (tanpa
        # pydub/audioop, yang sudah dihapus dari Python 3.13+).
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", src,
                # Speaker kecil tidak bisa membunyikan bass: dibuang supaya tidak pecah.
                # Lalu suara dipadatkan dan puncaknya dikunci supaya keras tapi aman.
                "-af", (f"aresample=16000,highpass=f={EQ_HPF_HZ},highpass=f={EQ_HPF_HZ},"
                        f"dynaudnorm=f=150:g=15:p=0.9:m=10,"
                        f"{komp_filter()}{eq_filter()}"
                        f"volume={TTS_GAIN_DB}dB,"
                        f"alimiter=limit={SPEAKER_LIMIT}:attack=5:release=50:level=disabled"),
                "-f", "s16le", "-ar", "16000", "-ac", "1",
                pcm_path,
            ],
            check=True, capture_output=True,
        )
        with open(pcm_path, "rb") as f:
            return f.read()
    finally:
        for path in (mp3_path, wav_path, pcm_path):
            if os.path.exists(path):
                os.remove(path)


# ---------- Pengaturan (bisa diubah dari halaman /pengaturan, tersimpan di file) ----------
SETTINGS_FILE = os.path.expanduser(os.environ.get("SETTINGS_FILE", "~/robot-server/pengaturan.json"))
VOICES = {"id-ID-GadisNeural": "Gadis (perempuan)", "id-ID-ArdiNeural": "Ardi (laki-laki)"}
# nama kunci -> (variabel global, pemeriksa nilai)
SETTING_SPEC = {
    "nama": ("ROBOT_NAME", lambda v: str(v).strip()[:20] or None),
    "suara": ("VOICE_ID", lambda v: v if v in VOICES else None),
    "tempo": ("TTS_RATE", lambda v: f"{max(-30, min(50, int(v))):+d}%"),
    "nada": ("TTS_PITCH", lambda v: f"{max(-30, min(60, int(v))):+d}Hz"),
    "gaya": ("GAYA", lambda v: v if v in ("imut", "lembut") else None),
    "berpikir": ("REASONING_EFFORT", lambda v: v if v in ("low", "medium", "high") else None),
    "ai": ("AI_UTAMA", lambda v: v if v in ("groq", "gemini") else None),
    "keluaran": ("KELUARAN_SUARA", lambda v: v if v in ("robot", "hp", "keduanya") else None),
    "senyap": ("SENYAP_ON", lambda v: str(v).lower() in ("1", "true", "ya", "on")),
    "senyap_mulai": ("SENYAP_MULAI", lambda v: max(0, min(23, int(v)))),
    "senyap_selesai": ("SENYAP_SELESAI", lambda v: max(0, min(23, int(v)))),
    "mesin_suara": ("MESIN_SUARA", lambda v: v if v in ("edge", "gemini", "elevenlabs") else None),
    "suara_eleven": ("ELEVEN_VOICE", lambda v: str(v).strip() if re.fullmatch(r"[A-Za-z0-9]{10,40}", str(v).strip()) else None),
    "stabil_eleven": ("ELEVEN_STABILITY", lambda v: max(0.0, min(1.0, float(v)))),
    "suara_gemini": ("GEMINI_VOICE", lambda v: v if v in GEMINI_TTS_VOICES else None),
    "kota": ("KOTA", lambda v: str(v).strip()[:40] or None),
    "keras_suara": ("TTS_GAIN_DB", lambda v: max(0.0, min(14.0, float(v)))),
    "batas_speaker": ("SPEAKER_LIMIT", lambda v: max(0.4, min(0.95, float(v)))),
    "keras_musik": ("MUSIC_GAIN_DB", lambda v: max(-12.0, min(9.0, float(v)))),
    "bass_db": ("EQ_BASS_DB", lambda v: max(-6, min(9, int(float(v))))),
    "jernih_db": ("EQ_JERNIH_DB", lambda v: max(-6, min(9, int(float(v))))),
    "treble_db": ("EQ_TREBLE_DB", lambda v: max(-6, min(9, int(float(v))))),
    "potong_bass": ("EQ_HPF_HZ", lambda v: max(100, min(400, int(float(v))))),
    "pemadat": ("EQ_KOMP", lambda v: max(0, min(3, int(float(v))))),
    "redam_desis": ("EQ_DESIS", lambda v: max(0, min(2, int(float(v))))),
}
_settings_lock = threading.Lock()


def current_settings() -> dict:
    g = globals()
    out = {k: g[var] for k, (var, _) in SETTING_SPEC.items()}
    out["tempo"] = int(str(out["tempo"]).rstrip("%"))
    out["nada"] = int(str(out["nada"]).rstrip("Hz"))
    out["senyap"] = "1" if out["senyap"] else "0"
    out["mesin_v2"] = 1
    return out


def apply_settings(values: dict, save: bool = True) -> dict:
    """Terapkan setelan yang valid. Kembalikan setelan yang benar-benar berubah."""
    changed = {}
    with _settings_lock:
        for key, val in values.items():
            if key not in SETTING_SPEC:
                continue
            var, check = SETTING_SPEC[key]
            try:
                new = check(val)
            except (TypeError, ValueError):
                continue
            if new is not None:
                globals()[var] = new
                changed[key] = new
        if save and changed:
            os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
            write_json(SETTINGS_FILE, current_settings(), ensure_ascii=False, indent=1)
    return changed


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("nama") in ("Rara",):  # nama lama -> nama baru
            data["nama"] = "Almara"
        if not data.get("mesin_v2"):   # migrasi: Gemini jadi mesin utama
            data["mesin_suara"] = "gemini"
        apply_settings(data, save=False)
        write_json(SETTINGS_FILE, current_settings(), ensure_ascii=False, indent=1)
    except (OSError, ValueError):
        pass


# ---------- Status robot & perintah tertunda (dikirim saat robot cek /health) ----------
ROBOT = {"terakhir": 0.0, "vol": None, "tampilan": None, "tema": None, "efek": None, "batshow": None, "joget": None,
         "fw": None,
         "suhu": None, "lembap": None, "baterai": None}
PENDING: dict = {}

# Notifikasi HP (WhatsApp dll.) yang menunggu ditampilkan di OLED robot. Antrian, bukan satu slot,
# supaya beberapa pesan beruntun tidak saling menimpa.
NOTIF: deque = deque(maxlen=8)
_notif_lock = threading.Lock()
_notif_last: dict = {}
_NOTIF_ABAIKAN = ("checking for new messages", "memeriksa pesan baru", "whatsapp web", "tersambung ke whatsapp",
                  "mencadangkan", "backup in progress", "sedang berjalan", "sedang melakukan panggilan")
_NOTIF_RINGKASAN = re.compile(r"^\d+\s+(new\s+)?(messages?|pesan)\b", re.I)  # "3 messages from 2 chats"


def _notif_bersih(t, batas: int) -> str:
    t = re.sub(r"[\x00-\x1f\x7f]+", " ", str(t or ""))
    return re.sub(r"\s+", " ", t).strip()[:batas]


def notif_tambah(aplikasi, dari, teks) -> bool:
    aplikasi, dari, teks = _notif_bersih(aplikasi, 14), _notif_bersih(dari, 24), _notif_bersih(teks, 120)
    low = (dari + " " + teks).lower()
    if not (dari or teks) or any(k in low for k in _NOTIF_ABAIKAN) or _NOTIF_RINGKASAN.match(teks):
        return False
    now = time.time()
    with _notif_lock:
        key = (dari, teks)
        if now - _notif_last.get(key, 0) < 5:  # WhatsApp sering mengirim notifikasi ganda
            return False
        if len(_notif_last) > 50:
            _notif_last.clear()
        _notif_last[key] = now
        NOTIF.append((aplikasi or "Pesan", dari, teks))
    return True


def notif_ambil() -> str:
    """Maksimal 3 notifikasi per cek robot: 'app|dari|teks~app|dari|teks' (tiap bagian di-url-encode)."""
    with _notif_lock:
        items = [NOTIF.popleft() for _ in range(min(3, len(NOTIF)))]
    return "~".join("|".join(quote(x) for x in it) for it in items)
CHAT_LOG: deque = deque(maxlen=60)


BUTTON_LABEL = {"__next": "(tombol kanan)", "__prev": "(tombol kiri)"}


def log_chat(kamu: str, robot: str):
    kamu = BUTTON_LABEL.get(kamu, kamu)
    CHAT_LOG.append({"waktu": datetime.now(WIB).strftime("%d/%m %H.%M"), "kamu": kamu, "robot": robot})


def _mask(t: str) -> str:
    if not t:
        return "(kosong)"
    if len(t) <= 4:
        return t[0] + "*" * (len(t) - 1) + f" (panjang {len(t)})"
    return f"{t[:2]}{'*' * (len(t) - 4)}{t[-2:]} (panjang {len(t)})"


_auth_warned = {}


def check_auth():
    got = request.headers.get("Authorization", "")
    if got == f"Bearer {ROBOT_TOKEN}":
        return True
    sent = got[7:] if got.startswith("Bearer ") else got
    key = sent[:64]
    if len(_auth_warned) > 50:  # jangan tumbuh tanpa batas
        _auth_warned.clear()
    if time.time() - _auth_warned.get(key, 0) > 30:  # jangan banjiri log
        _auth_warned[key] = time.time()
        print(f"[TOKEN SALAH] dari {request.remote_addr}: dikirim {_mask(sent)}  |  di start.sh {_mask(ROBOT_TOKEN)}"
              + ("  <- beda huruf besar/kecil" if sent.lower() == ROBOT_TOKEN.lower() and sent != ROBOT_TOKEN else "")
              + ("  <- ada spasi di depan/belakang" if sent.strip() == ROBOT_TOKEN.strip() and sent != ROBOT_TOKEN else ""),
              flush=True)
    return False


def restart_text() -> str:
    """Alasan restart terakhir robot, hanya kalau tidak normal."""
    code = str(ROBOT.get("rst") or "")
    if code in ("", "1", "3"):
        return ""
    try:
        tahap = TAHAP_NAMA[int(ROBOT.get("tahap") or 0)]
    except (ValueError, IndexError):
        tahap = "-"
    return f"{RESET_NAMA.get(code, 'kode ' + code)} saat {tahap}"


def srv_for_robot() -> str:
    """Pengaturan server yang bisa diubah dari menu OLED robot (20 angka, urutan tetap)."""
    def i(v, d=0):
        try:
            return int(round(float(str(v).rstrip("%Hz"))))
        except (TypeError, ValueError):
            return d
    vals = [
        ("robot", "hp", "keduanya").index(KELUARAN_SUARA) if KELUARAN_SUARA in ("robot", "hp", "keduanya") else 0,
        {"edge": 0, "gemini": 1, "elevenlabs": 2}.get(MESIN_SUARA, 0),
        1 if VOICE_ID == "id-ID-ArdiNeural" else 0,
        1 if AI_UTAMA == "gemini" else 0,
        i(TTS_RATE), i(TTS_PITCH), i(TTS_GAIN_DB), i(SPEAKER_LIMIT * 100, 55), i(MUSIC_GAIN_DB),
        1 if SENYAP_ON else 0, int(SENYAP_MULAI), int(SENYAP_SELESAI),
        # ---- tambahan (firmware lama hanya membaca 12 angka pertama) ----
        0 if GAYA == "imut" else 1,
        ("low", "medium", "high").index(REASONING_EFFORT) if REASONING_EFFORT in ("low", "medium", "high") else 1,
        EQ_BASS_DB, EQ_JERNIH_DB, EQ_TREBLE_DB, EQ_HPF_HZ, EQ_KOMP, EQ_DESIS,
    ]
    return ",".join(str(v) for v in vals)


RESET_NAMA = {"1": "menyala normal", "3": "restart biasa", "4": "crash program", "5": "macet (watchdog)",
              "6": "macet (watchdog)", "7": "macet (watchdog)", "9": "DAYA KURANG (brownout)"}
TAHAP_NAMA = ["-", "awal menyala", "mic/speaker", "sambung WiFi", "simpan WiFi", "jam internet", "cek server",
              "unduh suara", "baca sensor", "cek firmware", "suara pertama", "jalan <1 menit", "jalan normal"]


@app.route("/health")
def health():
    out = {"ok": True}
    if request.args.get("robot") == "1" and check_auth():
        a = request.args
        ROBOT.update(terakhir=time.time(), vol=a.get("vol"), tampilan=a.get("tampilan"), tema=a.get("tema"),
                     efek=a.get("efek"), batshow=a.get("batshow"), joget=a.get("joget"), bt=a.get("bt"), btm=a.get("btm"), fw=a.get("fw"), suhu=a.get("suhu"), lembap=a.get("lembap"),
                     bicara=a.get("bicara"), gerak=a.get("gerak"), pad=a.get("pad"), servo=a.get("servo"), jarak=a.get("jarak"),
                     baterai=a.get("bat"), alarm=a.get("alarm"), daya=a.get("daya"), rst=a.get("rst"), tahap=a.get("tahap"), up=a.get("up"))
        out["srv"] = srv_for_robot()
        out["nama"] = ROBOT_NAME  # untuk animasi pembuka robot
        out["senyap"] = f"{SENYAP_MULAI}-{SENYAP_SELESAI}" if SENYAP_ON else "off"
        nf = notif_ambil()
        if nf:
            out["notif"] = nf
        out.update(PENDING)  # mis. {"vol": 5} dari halaman pengaturan
        PENDING.clear()
    return out


# Halaman tes: buka http://localhost:8000 di browser HP yang sama dengan Termux.
# Harus localhost (bukan IP WiFi) karena browser hanya mengizinkan mic di
# alamat aman. Kirim teks tidak butuh mic, jadi bisa dari alamat mana saja.
PAGE = r"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tes robot ngobrol</title>
<style>
body{font-family:sans-serif;max-width:480px;margin:0 auto;padding:16px;background:#111;color:#eee}
input,button{width:100%;box-sizing:border-box;padding:14px;font-size:16px;border-radius:8px;border:1px solid #444;background:#222;color:#eee;margin-top:8px}
button{background:#2a6;border:0;font-weight:bold}
button.rec{background:#c33}
pre{white-space:pre-wrap;background:#1a1a1a;padding:12px;border-radius:8px}
</style></head><body>
<h2>Tes robot ngobrol</h2>
<input id="token" type="password" placeholder="ROBOT_TOKEN">
<button id="mic">Tap untuk bicara</button>
<input id="teks" placeholder="atau ketik pesan di sini">
<button id="kirim">Kirim teks</button>
<pre id="log">Siap.</pre>
<script>
const $=id=>document.getElementById(id);
$('token').value=localStorage.getItem('tok')||'';
let rec=null,chunks=[],ctx=null;
function log(s){$('log').textContent=s}
function play(buf){
  const i16=new Int16Array(buf.slice(0,buf.byteLength-(buf.byteLength%2)));
  const f32=Float32Array.from(i16,v=>v/32768);
  const ab=ctx.createBuffer(1,f32.length,16000);
  ab.copyToChannel(f32,0);
  const s=ctx.createBufferSource();
  s.buffer=ab;s.connect(ctx.destination);
  ctx.resume().then(()=>s.start());
}
async function kirim(fd){
  localStorage.setItem('tok',$('token').value);
  log('Memproses...');
  const t0=performance.now();
  let r;
  try{r=await fetch('/chat',{method:'POST',headers:{Authorization:'Bearer '+$('token').value},body:fd});}
  catch(e){log('Gagal konek ke server: '+e);return;}
  if(!r.ok){log('Error '+r.status+': '+(await r.text()||'token salah?'));return;}
  const d=h=>decodeURIComponent(r.headers.get(h)||'');
  const buf=await r.arrayBuffer();
  log('Terdengar: '+d('X-Teks')+'\nJawaban: '+d('X-Ucapan')+'\nEkspresi: '+d('X-Ekspresi')+'\nGerak: '+d('X-Gerak')+'\nMode: '+d('X-Mode')+'\nWaktu: '+((performance.now()-t0)/1000).toFixed(1)+' detik');
  if(buf.byteLength>1)play(buf);
}
$('kirim').onclick=()=>{
  ctx=ctx||new AudioContext();
  const t=$('teks').value.trim();
  if(!t)return;
  const fd=new FormData();fd.append('teks',t);kirim(fd);
};
$('mic').onclick=async()=>{
  ctx=ctx||new AudioContext();
  if(rec){rec.stop();return;}
  let stream;
  try{stream=await navigator.mediaDevices.getUserMedia({audio:true});}
  catch(e){log('Mic tidak bisa dipakai. Buka lewat http://localhost:8000 di HP yang sama, lalu izinkan mikrofon.');return;}
  chunks=[];rec=new MediaRecorder(stream);
  rec.ondataavailable=e=>chunks.push(e.data);
  rec.onstop=()=>{
    stream.getTracks().forEach(t=>t.stop());
    const mt=rec.mimeType,ext=mt.includes('ogg')?'ogg':mt.includes('mp4')?'m4a':'webm';
    const fd=new FormData();
    fd.append('audio',new Blob(chunks,{type:mt}),'rekaman.'+ext);
    rec=null;$('mic').textContent='Tap untuk bicara';$('mic').className='';
    kirim(fd);
  };
  rec.start();$('mic').textContent='Merekam... tap untuk berhenti';$('mic').className='rec';
};
</script></body></html>"""


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


# Metadata lewat header supaya ESP32 tidak perlu parsing JSON. Teks di header
# di-URL-encode (X-Teks, X-Ucapan) agar aman untuk huruf non-ASCII; ESP32
# boleh mengabaikannya.
# ---------- Halaman pengaturan: http://localhost:8000/pengaturan ----------
SETTINGS_PAGE = r"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Almara</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#2a2f3a;--txt:#e8eaf0;--dim:#9aa3b2;--acc:#4fc3f7;--bad:#ef5350;--ok:#66bb6a}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:var(--bg);color:var(--txt)}
main{max-width:560px;margin:0 auto;padding:14px}
h1{font-size:20px;margin:6px 0 14px}h2{font-size:15px;margin:0 0 10px;color:var(--acc)}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
label{display:block;font-size:13px;color:var(--dim);margin:10px 0 4px}
input,select,button{width:100%;padding:10px;border-radius:8px;border:1px solid var(--line);background:#11141a;color:var(--txt);font-size:15px}
input[type=range]{padding:0}
button{background:var(--acc);color:#001018;border:0;font-weight:600;margin-top:10px}
button.sec{background:#2a2f3a;color:var(--txt)}button.del{background:transparent;color:var(--bad);border:1px solid var(--bad);width:auto;padding:4px 10px;margin:0;font-size:13px}
.row{display:flex;gap:8px;align-items:center}.row>*{flex:1}
.item{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--line);font-size:14px}
.item:last-child{border:0}.dim{color:var(--dim);font-size:13px}.ok{color:var(--ok)}.bad{color:var(--bad)}
.chat{font-size:14px;padding:8px 0;border-bottom:1px solid var(--line)}.chat b{color:var(--acc)}
#msg{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);background:#263238;padding:10px 16px;border-radius:8px;display:none}
</style></head><body><main>
<h1>Almara <span class="dim" style="font-size:14px;font-weight:400">pengaturan</span></h1>
<section id="login"><h2>Masuk</h2><label>ROBOT_TOKEN (sama dengan di start.sh)</label>
<input id="token" type="password"><button onclick="masuk()">Masuk</button></section>
<div id="app" style="display:none">
<section><h2>Status robot</h2><div id="status" class="dim">Memuat...</div></section>
<section><h2>Kirim pesan ke Almara</h2>
<div class="row"><input id="pesan" maxlength="200" placeholder="Tulis pesan, nanti Almara yang mengucapkan"><button style="flex:0 0 80px;margin:0" onclick="kirimPesan()">Kirim</button></div>
<div class="dim">Diucapkan robot dalam beberapa detik (saat robot sedang diam).</div></section>
<section><h2>Kontrol gamepad X3</h2><div class="dim">
Stik kiri / d-pad: setir &middot; tahan RB: cepat &middot; B: berhenti / kembali &middot; A: OK / elus<br>
X: gerak acak nyala/mati &middot; Y: menu game &middot; LB: bicara &middot; START: menu Pengaturan &middot; SELECT: mode demo<br>
L2 / R2: volume &middot; saat radio/lagu: stik kanan kiri/kanan = saluran, atas/bawah = volume</div></section>
<section><h2>Robot</h2>
<label>Volume robot: <span id="volv"></span></label><input id="vol" type="range" min="1" max="10">
<label>Tampilan</label><select id="tampilan"><option value="WAJAH">Wajah</option><option value="TEKS">Teks</option><option value="CAMPUR">Wajah + teks</option></select>
<label>Warna layar</label><select id="tema"><option value="GELAP">Gelap (latar hitam)</option><option value="TERANG">Terang (latar putih)</option></select>
<label>Efek latar animasi</label><select id="efek"><option value="1">Nyala</option><option value="0">Mati</option></select>
<label>Joget saat mendengar musik</label><select id="joget"><option value="1">Nyala</option><option value="0">Mati</option></select>
<label>Mode bicara</label><select id="bicara"><option value="0">Tahan tombol sambil bicara</option><option value="1">Klik sekali, berhenti sendiri saat diam</option></select>
<label>Roda (dinamo)</label><select id="gerak"><option value="0">Mati total</option><option value="1">Gamepad &amp; perintah suara saja</option><option value="2">+ ikut ekspresi &amp; joget lagu</option><option value="3">+ gerak acak sendiri (kecil)</option></select>
<div class="dim" id="padinfo"></div>
<label>Indikator baterai</label><select id="batshow"><option value="1">Otomatis (muncul kalau baterai terpasang)</option><option value="0">Sembunyikan (pakai USB)</option></select>
<button onclick="simpanRobot()">Kirim ke robot</button>
<div class="dim">Diterapkan dalam beberapa detik saat robot sedang diam.</div></section>
<section><h2>Kalibrasi roda</h2>
<div class="dim">Setelah dikirim, robot maju sebentar lalu mundur. Kalau ada roda yang berputar terbalik, ganti "Arah roda". Kalau robot melengkung saat maju lurus, naikkan trim roda yang lebih lemah.</div>
<label>Seimbangkan roda kiri: <span id="trlv"></span> %</label><input id="trl" type="range" min="-30" max="30" step="1">
<label>Seimbangkan roda kanan: <span id="trrv"></span> %</label><input id="trr" type="range" min="-30" max="30" step="1">
<label>Arah roda</label><select id="sdir"><option value="0">Normal (bawaan)</option><option value="1">Kiri dibalik</option><option value="2">Kanan dibalik</option><option value="3">Keduanya dibalik</option><option value="6">Kiri-kanan tertukar</option><option value="5">Tertukar + kiri dibalik</option></select>
<label>Kecepatan maksimal: <span id="sspdv"></span>/10</label><input id="sspd" type="range" min="1" max="10">
<label>Daya maksimal: <span id="dmxv"></span> %</label><input id="dmx" type="range" min="40" max="100" step="5">
<label>Daya minimal (tenaga awal): <span id="dmnv"></span> %</label><input id="dmn" type="range" min="20" max="90" step="5">
<label>Lama belok 90&deg;: <span id="tmsv"></span> ms</label><input id="tms" type="range" min="150" max="1500" step="25">
<button onclick="simpanServo()">Kirim & tes roda</button></section>
<section><h2>Suara & kepribadian</h2>
<label>Nama robot</label><input id="nama">
<label>Keluaran suara</label><select id="keluaran" onchange="api('/api/pengaturan',{keluaran:this.value}).then(()=>pesan('Keluaran suara disimpan'))"><option value="robot">Speaker robot</option><option value="hp">Speaker HP / Bluetooth</option><option value="keduanya">Keduanya</option></select>
<div class="dim" id="keluarinfo"></div>
<button class="sec" onclick="tesSpeaker()">Tes speaker HP</button>
<label>Mesin suara</label><select id="mesin_suara"><option value="elevenlabs">ElevenLabs (utama, Gemini &amp; Edge cadangan)</option><option value="gemini">Gemini (utama, Edge cadangan)</option><option value="edge">Edge saja</option></select>
<label>Suara ElevenLabs (Voice ID)</label><input id="suara_eleven">
<label>Keekspresifan ElevenLabs: <span id="stabil_elevenv"></span> (rendah = lebih hidup, tinggi = lebih datar)</label><input id="stabil_eleven" type="range" min="0" max="1" step="0.05">
<label>Suara Edge</label><select id="suara"></select>
<label>Suara Gemini (tekan "Coba suara" untuk mendengar)</label><select id="suara_gemini"></select>
<div class="dim" id="ttsinfo"></div>
<label>Kecepatan bicara: <span id="tempov"></span>%</label><input id="tempo" type="range" min="-30" max="50" step="5">
<label>Nada suara (tinggi = imut): <span id="nadav"></span> Hz</label><input id="nada" type="range" min="-30" max="60" step="5">
<label>Gaya bicara</label><select id="gaya"><option value="imut">Imut &amp; ceria</option><option value="lembut">Lembut &amp; sopan</option></select>
<h3 style="margin:18px 0 4px">Kualitas suara speaker</h3>
<div class="dim">Untuk menjernihkan speaker kecil. Berlaku untuk suara jawaban dan musik. Tekan "Coba suara" setelah mengubah.</div>
<label>Preset cepat</label><select id="preset" onchange="pakaiPreset(this.value)"><option value="">Pilih preset...</option><option value="jernih">Jernih (kata paling jelas)</option><option value="hangat">Hangat (lebih empuk)</option><option value="natural">Natural (tanpa tambahan)</option><option value="keras">Keras (maksimal di speaker kecil)</option></select>
<label>Bass / isi suara: <span id="bass_dbv"></span> dB</label><input id="bass_db" type="range" min="-6" max="9" step="1">
<label>Kejernihan (kata lebih jelas): <span id="jernih_dbv"></span> dB</label><input id="jernih_db" type="range" min="-6" max="9" step="1">
<label>Treble / kilau: <span id="treble_dbv"></span> dB</label><input id="treble_db" type="range" min="-6" max="9" step="1">
<label>Potong bass di bawah: <span id="potong_bassv"></span> Hz (naikkan kalau speaker bergetar atau pecah)</label><input id="potong_bass" type="range" min="100" max="400" step="25">
<label>Pemadat suara (ratakan pelan &amp; keras)</label><select id="pemadat"><option value="0">Mati</option><option value="1">Ringan</option><option value="2">Sedang</option><option value="3">Kuat</option></select>
<label>Peredam desis (huruf "s" terlalu tajam)</label><select id="redam_desis"><option value="0">Mati</option><option value="1">Ringan</option><option value="2">Kuat</option></select>
<label>Kekerasan suara jawaban: <span id="keras_suarav"></span> dB</label><input id="keras_suara" type="range" min="0" max="14" step="1">
<label>Batas puncak speaker (turunkan kalau pecah): <span id="batas_speakerv"></span></label><input id="batas_speaker" type="range" min="0.4" max="0.95" step="0.05">
<label>Kekerasan musik: <span id="keras_musikv"></span> dB</label><input id="keras_musik" type="range" min="-12" max="9" step="1">
<label>Tingkat berpikir AI</label><select id="berpikir"><option value="low">Cepat</option><option value="medium">Seimbang</option><option value="high">Paling teliti (lambat)</option></select>
<label>AI utama (yang lain jadi cadangan otomatis)</label><select id="ai"><option value="gemini">Gemini (Google)</option><option value="groq">Groq</option></select>
<div class="dim" id="aiinfo"></div>
<label>Mode jangan ganggu (suara pelan, layar redup, tidak menyapa duluan)</label>
<select id="senyap"><option value="1">Nyala</option><option value="0">Mati</option></select>
<div class="row"><div><label>Mulai jam</label><input id="senyap_mulai" type="number" min="0" max="23"></div>
<div><label>Selesai jam</label><input id="senyap_selesai" type="number" min="0" max="23"></div></div>
<label>Kota untuk cuaca</label><input id="kota">
<div class="row"><button onclick="simpan()">Simpan</button><button class="sec" onclick="cobaSuara()">Coba suara</button></div></section>
<section><h2>Pengingat berulang</h2><div id="pengingat"></div>
<div class="dim">Tambah lewat suara, misalnya: "ingatkan aku minum air setiap 1 jam".</div></section>
<section><h2>Catatan</h2><div id="catatan"></div>
<div class="row" style="margin-top:8px"><input id="catBaru" placeholder="Catatan baru"><button style="flex:0 0 90px;margin:0" onclick="tambahCatatan()">Tambah</button></div></section>
<section><h2>Yang diingat robot tentangmu</h2><div id="ingatan"></div>
<button class="sec" onclick="hapusSemuaIngatan()">Hapus semua ingatan</button></section>
<section><h2>Jurnal harian</h2><div id="jurnal"></div></section>
<section><h2>Riwayat obrolan</h2><div id="riwayat"></div></section>
<section><h2>Lagu di folder Music</h2><div id="musik" class="dim"></div></section>
</div><div id="msg"></div></main>
<script>
const $=id=>document.getElementById(id);
let tok=localStorage.getItem('tok')||'', ctx=null;
const H=()=>({'Authorization':'Bearer '+tok,'Content-Type':'application/json'});
function pesan(t){$('msg').textContent=t;$('msg').style.display='block';setTimeout(()=>$('msg').style.display='none',2200)}
function esc(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML}
async function api(path,body){
  const r=await fetch(path,{method:body?'POST':'GET',headers:H(),body:body?JSON.stringify(body):undefined});
  if(r.status==401){localStorage.removeItem('tok');$('app').style.display='none';$('login').style.display='block';pesan('Token salah');throw 0}
  return r.json();
}
function masuk(){tok=$('token').value.trim();localStorage.setItem('tok',tok);muat()}
const sliders=['bass_db','jernih_db','treble_db','potong_bass','stabil_eleven','vol','tempo','nada','keras_suara','batas_speaker','keras_musik','trl','trr','sspd','tms','dmx','dmn'];
sliders.forEach(id=>document.addEventListener('input',e=>{if(e.target.id===id)$(id+'v').textContent=e.target.value}));
let formLoaded=false;
async function muat(isAuto){
  const d=await api('/api/status');
  $('login').style.display='none';$('app').style.display='block';
  const r=d.robot, on=r.online;
  $('status').innerHTML=`<span class="${on?'ok':'bad'}">${on?'Terhubung':'Tidak terhubung'}</span>`+
    (r.fw?` &middot; firmware v${esc(r.fw)}`:'')+(r.suhu?` &middot; ruangan ${esc(r.suhu)}&deg;C ${esc(r.lembap||'')}%`:'')+
    (r.baterai&&r.baterai!='-1'?` &middot; baterai ${esc(r.baterai)}%`:'')+
    (r.restart?`<br>Restart terakhir: ${esc(r.restart)}`:'')+
    ((a=>a.length==4&&a[0]=='1'?`<br>Alarm: ${a[1].padStart(2,'0')}.${a[2].padStart(2,'0')} ${['sekali','setiap hari','Senin-Jumat'][+a[3]]||''}`:'')(String(r.alarm||'').split(',')))+
    (d.game?`<br>Sedang bermain: ${esc(d.game)}`:'')+(d.inggris?'<br>Mode latihan bahasa Inggris aktif':'');
  const p=d.pengaturan;
  $('suara').innerHTML=Object.entries(d.suara_pilihan).map(([k,v])=>`<option value="${k}">${esc(v)}</option>`).join('');
  $('suara_gemini').innerHTML=Object.entries(d.suara_gemini_pilihan).map(([k,v])=>`<option value="${k}">${esc(k)} (${esc(v)})</option>`).join('');
  $('keluarinfo').textContent=d.mpv_ada?'Untuk speaker Bluetooth: sambungkan speaker ke HP server, lalu pilih Speaker HP.':'Pemutar belum terpasang. Di Termux jalankan: pkg install mpv';
  $('ttsinfo').textContent=(d.eleven_siap?'':'ELEVENLABS_API_KEY belum diisi. ')+(d.gemini_siap?'Kecepatan bicara & nada hanya berlaku untuk suara Edge. Kalau kuota Gemini habis, otomatis kembali ke Edge.':'GEMINI_API_KEY belum diisi, jadi suara Gemini belum bisa dipakai.');
  if(!isAuto||!formLoaded){  // pemuatan otomatis tidak boleh menimpa pilihan yang sedang diubah
    for(const k of ['nama','keluaran','mesin_suara','suara','suara_gemini','suara_eleven','stabil_eleven','tempo','nada','gaya','keras_suara','batas_speaker','keras_musik','bass_db','jernih_db','treble_db','potong_bass','pemadat','redam_desis','berpikir','ai','senyap','senyap_mulai','senyap_selesai','kota']) $(k).value=p[k];
    formLoaded=true;
  }
  if(!isAuto){if(r.vol) $('vol').value=r.vol; if(r.tampilan) $('tampilan').value=r.tampilan; if(r.tema) $('tema').value=r.tema; if(r.efek) $('efek').value=r.efek; if(r.batshow) $('batshow').value=r.batshow; if(r.joget) $('joget').value=r.joget; if(r.bicara) $('bicara').value=r.bicara; if(r.gerak) $('gerak').value=r.gerak;
    if(r.servo){const v=r.servo.split(',');if(v.length==7){$('trl').value=v[0];$('trr').value=v[1];$('sdir').value=v[2];$('sspd').value=v[3];$('tms').value=v[4];$('dmx').value=v[5];$('dmn').value=v[6];}}}
  $('padinfo').textContent=r.pad=='2'?'Gamepad X3 tersambung.':'Gamepad belum tersambung (nyalakan X3 mode Xbox).';
  ['trl','trr','sspd','tms','dmx','dmn'].forEach(id=>$(id+'v').textContent=$(id).value);
  sliders.forEach(id=>$(id+'v').textContent=$(id).value);
  $('catatan').innerHTML=d.catatan.length?d.catatan.map((c,i)=>`<div class="item"><span>${i+1}. ${esc(c)}</span><button class="del" onclick="hapusCatatan(${i})">Hapus</button></div>`).join(''):'<div class="dim">Belum ada catatan.</div>';
  $('ingatan').innerHTML=d.ingatan.length?d.ingatan.map((c,i)=>`<div class="item"><span>${esc(c)}</span><button class="del" onclick="hapusIngatan(${i})">Hapus</button></div>`).join(''):'<div class="dim">Belum ada yang diingat.</div>';
  $('aiinfo').textContent=(d.gemini_siap?'':'GEMINI_API_KEY belum diisi di start.sh. ')+(d.groq_siap?'':'Groq tidak dipakai (key kosong). ')+(d.ai_terakhir?'Jawaban terakhir dari: '+d.ai_terakhir+'. ':'')+(d.stt_terakhir?'Pendengaran: '+d.stt_terakhir+'.':'');
  $('pengingat').innerHTML=d.pengingat.length?d.pengingat.map((r,i)=>`<div class="item"><span>${esc(r.teks)} <span class="dim">(${esc(r.jadwal)})</span></span><button class="del" onclick="hapusPengingat(${i})">Hapus</button></div>`).join(''):'<div class="dim">Belum ada pengingat.</div>';
  $('jurnal').innerHTML=d.jurnal.length?d.jurnal.map(j=>`<div class="chat"><span class="dim">${esc(j.tanggal)}</span><br>${esc(j.isi)}</div>`).join(''):'<div class="dim">Belum ada jurnal. Bilang "tulis jurnal" ke Almara.</div>';
  $('riwayat').innerHTML=d.riwayat.length?d.riwayat.map(c=>`<div class="chat"><span class="dim">${esc(c.waktu)}</span><br><b>Kamu:</b> ${esc(c.kamu)}<br><b>Robot:</b> ${esc(c.robot)}</div>`).join(''):'<div class="dim">Belum ada obrolan.</div>';
  $('musik').innerHTML=d.musik.length?`${d.musik.length} lagu: `+d.musik.slice(0,100).map(esc).join(', '):'Folder Music kosong.';
}
const PRESET={jernih:{bass_db:0,jernih_db:4,treble_db:2,potong_bass:250,pemadat:2,redam_desis:0},
  hangat:{bass_db:3,jernih_db:0,treble_db:-2,potong_bass:200,pemadat:1,redam_desis:1},
  natural:{bass_db:0,jernih_db:0,treble_db:0,potong_bass:200,pemadat:1,redam_desis:0},
  keras:{bass_db:0,jernih_db:3,treble_db:0,potong_bass:300,pemadat:3,redam_desis:0}};
function pakaiPreset(n){const p=PRESET[n];if(!p)return;for(const k in p){$(k).value=p[k];const v=$(k+'v');if(v)v.textContent=p[k]}pesan('Preset dipasang. Tekan Simpan atau Coba suara.')}
async function simpan(){
  const b={};for(const k of ['nama','keluaran','mesin_suara','suara','suara_gemini','suara_eleven','stabil_eleven','tempo','nada','gaya','keras_suara','batas_speaker','keras_musik','bass_db','jernih_db','treble_db','potong_bass','pemadat','redam_desis','berpikir','ai','senyap','senyap_mulai','senyap_selesai','kota']) b[k]=$(k).value;
  await api('/api/pengaturan',b);pesan('Tersimpan');muat();
}
async function simpanRobot(){await api('/api/pengaturan',{vol:$('vol').value,tampilan:$('tampilan').value,tema:$('tema').value,efek:$('efek').value,batshow:$('batshow').value,joget:$('joget').value,bicara:$('bicara').value,gerak:$('gerak').value});pesan('Dikirim ke robot');}
async function simpanServo(){await api('/api/pengaturan',{servo:[$('trl').value,$('trr').value,$('sdir').value,$('sspd').value,$('tms').value,$('dmx').value,$('dmn').value].join(',')});pesan('Dikirim. Perhatikan roda robot.');}
async function tesSpeaker(){const d=await api('/api/tes-speaker',{});pesan(d.pesan);alert(d.pesan)}
async function kirimPesan(){const t=$('pesan').value.trim();if(!t)return;await api('/api/pesan',{teks:t});$('pesan').value='';pesan('Pesan dikirim ke Almara')}
async function hapusPengingat(i){await api('/api/pengingat',{hapus:i});muat()}
async function tambahCatatan(){const t=$('catBaru').value.trim();if(!t)return;await api('/api/catatan',{tambah:t});$('catBaru').value='';muat()}
async function hapusCatatan(i){await api('/api/catatan',{hapus:i});muat()}
async function hapusIngatan(i){await api('/api/ingatan',{hapus:i});muat()}
async function hapusSemuaIngatan(){if(confirm('Hapus semua yang diingat robot?')){await api('/api/ingatan',{semua:true});muat()}}
async function cobaSuara(){
  await simpan();
  const r=await fetch('/ucap?teks='+encodeURIComponent('Halo, aku '+$('nama').value+'. Begini suaraku sekarang.'),{headers:H()});
  const buf=await r.arrayBuffer();ctx=ctx||new AudioContext();
  const i16=new Int16Array(buf.slice(0,buf.byteLength-buf.byteLength%2)),f=Float32Array.from(i16,v=>v/32768);
  const ab=ctx.createBuffer(1,f.length,16000);ab.copyToChannel(f,0);const s=ctx.createBufferSource();s.buffer=ab;s.connect(ctx.destination);s.start();
}
if(tok){muat().catch(()=>{})}
setInterval(()=>{if($('app').style.display==='block')muat(true).catch(()=>{})},15000);
</script></body></html>"""


@app.route("/pengaturan")
def settings_page():
    return Response(SETTINGS_PAGE, mimetype="text/html")


@app.route("/api/status")
def api_status():
    if not check_auth():
        return Response(status=401)
    return {
        "pengaturan": current_settings(),
        "suara_pilihan": VOICES,
        "mpv_ada": phone_player_cmd() is not None,
        "suara_gemini_pilihan": GEMINI_TTS_VOICES,
        "robot": {**ROBOT, "online": time.time() - ROBOT["terakhir"] < 45, "restart": restart_text()},
        "ingatan": load_memory(),
        "catatan": load_notes(),
        "riwayat": list(CHAT_LOG)[::-1],
        "musik": sorted(_song_title(p) for p in local_songs()),
        "game": GAME_RULES[GAME["mode"]][0] if GAME["mode"] else None,
        "jurnal": load_journal()[-14:][::-1],
        "pengingat": [{"teks": r["label"], "jadwal": reminder_desc(r)} for r in load_reminders()],
        "inggris": MODES["english"],
        "gemini_siap": bool(GEMINI_API_KEY),
        "eleven_siap": bool(ELEVEN_KEYS),
        "groq_siap": bool(GROQ_API_KEY),
        "stt_terakhir": LAST_STT["nama"],
        "ai_terakhir": f"{LAST_AI['nama']} ({LAST_AI['waktu']})" if LAST_AI["nama"] else None,
    }


@app.route("/api/pengaturan", methods=["POST"])
def api_settings():
    if not check_auth():
        return Response(status=401)
    data = request.get_json(silent=True) or {}
    changed = apply_settings(data)
    if "vol" in data:  # setelan milik robot: dikirim saat robot cek berikutnya
        try:
            PENDING["vol"] = max(1, min(10, int(data["vol"])))
        except ValueError:
            pass
    if data.get("tampilan") in ("WAJAH", "TEKS", "CAMPUR"):
        PENDING["tampilan"] = data["tampilan"]
    if data.get("tema") in ("TERANG", "GELAP"):
        PENDING["tema"] = data["tema"]
    if str(data.get("efek")) in ("0", "1"):
        PENDING["efek"] = int(data["efek"])
    if str(data.get("btmode")) in ("0", "1", "2"):
        PENDING["btmode"] = int(data["btmode"])
    if str(data.get("joget")) in ("0", "1"):
        PENDING["joget"] = int(data["joget"])
    if str(data.get("batshow")) in ("0", "1"):
        PENDING["baterai_tampil"] = int(data["batshow"])
    if str(data.get("bicara")) in ("0", "1"):
        PENDING["bicara"] = int(data["bicara"])
    if str(data.get("gerak")) in ("0", "1", "2", "3"):
        PENDING["gerak"] = int(data["gerak"])
    if re.fullmatch(r"-?\d+,-?\d+,\d,\d+,\d+,\d+,\d+", str(data.get("servo", ""))):
        PENDING["servo"] = data["servo"]
    if re.fullmatch(r"\d{1,3},\d{1,3}", str(data.get("daya", ""))):
        PENDING["daya"] = data["daya"]
    return {"ok": True, "berubah": list(changed), "menunggu_robot": dict(PENDING)}


@app.route("/api/tes-speaker", methods=["POST"])
def api_test_speaker():
    """Bunyikan tes langsung di speaker HP (tanpa robot) dan laporkan hasilnya."""
    if not check_auth():
        return Response(status=401)
    if phone_player_cmd() is None:
        return {"ok": False, "pesan": "mpv belum terpasang. Jalankan: pkg install mpv"}
    pcm = synthesize("Halo, ini tes speaker HP dari Almara.")
    p = phone_start()
    if p is None:
        return {"ok": False, "pesan": "mpv tidak bisa dijalankan. Lihat pesan di Termux."}
    try:
        p.stdin.write(pcm)
        p.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    try:
        code = p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        code = 0
    if code == 0:
        return {"ok": True, "pesan": "Pemutar berjalan normal. Kalau tidak terdengar, cek volume media HP dan sambungan Bluetooth."}
    return {"ok": False, "pesan": f"mpv gagal (kode {code}). Lihat baris [SPEAKER HP] di Termux."}


@app.route("/api/pesan", methods=["POST"])
def api_message():
    """Pesan dari HP: diucapkan robot saat cek server berikutnya (paling lama ~10 detik)."""
    if not check_auth():
        return Response(status=401)
    teks = clean_speech(str((request.get_json(silent=True) or {}).get("teks", ""))).strip()[:200]
    if not teks:
        return {"ok": False}, 400
    PENDING["pesan"] = quote(teks)
    log_chat("(pesan dari HP)", teks)
    return {"ok": True}


@app.route("/api/notif", methods=["POST", "GET"])
def api_notif():
    """Notifikasi dari HP (MacroDroid / Tasker) -> tampil di OLED kecil robot (paling lama ~10 detik).
    Format yang diterima (pilih yang paling mudah):
      1. teks polos  : WhatsApp|Budi|Eh jadi ketemu jam 5?        (aman walau pesan berisi tanda kutip)
      2. form / query: app=WhatsApp&dari=Budi&teks=Halo
      3. JSON        : {"app": "WhatsApp", "dari": "Budi", "teks": "Halo"}"""
    if not check_auth():
        return Response(status=401)
    raw = request.get_data(as_text=True) or ""
    aplikasi = dari = teks = ""
    if raw.lstrip().startswith("{"):
        try:
            d = json.loads(raw)
            aplikasi, dari, teks = d.get("app", ""), d.get("dari", ""), d.get("teks", "")
        except ValueError:
            pass
    elif re.match(r"^(app|dari|teks)=", raw):
        from urllib.parse import parse_qs
        q = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        aplikasi, dari, teks = q.get("app", ""), q.get("dari", ""), q.get("teks", "")
    elif raw.strip():
        bagian = raw.split("|", 2)
        if len(bagian) == 3:
            aplikasi, dari, teks = bagian
        else:  # tanpa pemisah: seluruhnya dianggap isi pesan
            teks = raw
    else:
        aplikasi, dari, teks = (request.args.get(k, "") for k in ("app", "dari", "teks"))
    ok = notif_tambah(aplikasi, dari, teks)
    print(f"[NOTIF] {'ok' if ok else 'diabaikan'}: {aplikasi} | {dari} | {str(teks)[:60]}")
    return {"ok": ok}


@app.route("/api/pengingat", methods=["POST"])
def api_reminders():
    if not check_auth():
        return Response(status=401)
    data = request.get_json(silent=True) or {}
    with _rem_lock:
        items = load_reminders()
        if data.get("semua"):
            items = []
        elif isinstance(data.get("hapus"), int) and 0 <= data["hapus"] < len(items):
            items.pop(data["hapus"])
        save_reminders(items)
    return {"ok": True}


@app.route("/api/ingatan", methods=["POST"])
def api_memory():
    if not check_auth():
        return Response(status=401)
    data = request.get_json(silent=True) or {}
    if data.get("semua"):
        clear_memory()
    elif isinstance(data.get("hapus"), int):
        with _mem_lock:
            facts = load_memory()
            if 0 <= data["hapus"] < len(facts):
                facts.pop(data["hapus"])
                write_json(MEMORY_FILE, facts, ensure_ascii=False, indent=1)
    return {"ok": True}


@app.route("/api/catatan", methods=["POST"])
def api_notes():
    if not check_auth():
        return Response(status=401)
    data = request.get_json(silent=True) or {}
    with _notes_lock:
        notes = load_notes()
        if str(data.get("tambah") or "").strip():
            notes.append(str(data["tambah"]).strip()[:150])
        elif isinstance(data.get("hapus"), int) and 0 <= data["hapus"] < len(notes):
            notes.pop(data["hapus"])
        save_notes(notes)
    return {"ok": True}


@app.route("/now")
def now_playing():
    if not check_auth():
        return Response(status=401)
    return Response(media_now(), mimetype="text/plain")


# ---------- Bicara sendiri & ajak ngobrol (robot yang memulai, tanpa disentuh) ----------
# Robot memanggil /spontan?jenis=...&suhu=...&lembab=... lalu mengucapkan hasilnya lewat /ucap.
# Balasan: "ekspresi|ajak|ucapan" (ajak = 1 -> robot langsung membuka mic menunggu jawabanmu).
# Ucapan dicatat ke riwayat obrolan supaya jawabanmu nyambung dengan pertanyaan robot.
_spontan_baru: list[str] = []   # ucapan spontan terakhir, supaya tidak diulang
SPONTAN_EKSPRESI = {
    "ajak": ["senang", "bingung", "kagum"], "komentar": ["senang", "malu", "tertawa"],
    "fakta": ["kagum"], "lelucon": ["tertawa"], "semangat": ["senang", "cinta"], "sensor": ["normal"],
}
SPONTAN_ARAHAN = {
    "ajak": "Ajak pengguna ngobrol dengan SATU pertanyaan santai yang menarik (kegiatannya, perasaannya, makanan, musik, "
            "hobi, impian, atau hal acak yang seru). Pertanyaannya harus mudah dijawab dengan suara.",
    "komentar": "Ucapkan satu komentar iseng atau pengamatan lucu tentang dirimu sebagai robot kecil beroda, atau tentang "
                "suasana di sekitarmu.",
    "fakta": "Bagikan satu fakta unik yang singkat, menarik, dan BENAR, lalu beri komentar kecil seperti teman.",
    "lelucon": "Ceritakan satu lelucon atau tebak-tebakan singkat yang sopan. Kalau tebak-tebakan, tanyakan saja dulu "
               "tanpa menjawabnya supaya pengguna bisa menebak.",
    "semangat": "Beri satu kalimat penyemangat atau pengingat ramah (minum air, istirahatkan mata, atau meregangkan badan).",
    "sensor": "Komentari suasana ruangan berdasarkan data suhu, kelembapan, dan waktu di bawah, dengan gaya santai.",
}
SPONTAN_CADANGAN = {
    "ajak": ["Hmm, sepi ya. Kamu lagi ngapain sekarang?", "Eh, ngobrol yuk! Hari ini ada cerita seru nggak?",
             "Aku penasaran deh. Lagu favorit kamu apa sih?", "Kalau boleh milih, kamu mau makan apa sekarang?",
             "Dari tadi aku diem aja nih. Kamu lagi sibuk ya?"],
    "komentar": ["Tadi aku latihan jalan sendiri. Gimana, udah keren belum?", "Hehe, aku lagi mikirin apa ya? Rahasia dong.",
                 "Kalau bosan, panggil aku ya. Aku bisa muter lagu atau ngajak main game."],
    "fakta": ["Eh, tahu nggak? Kata orang, kucing bisa tidur lebih dari dua belas jam sehari.",
              "Tahu nggak, madu itu hampir nggak pernah basi lho. Keren banget ya."],
    "lelucon": ["Aku punya tebak-tebakan nih. Hewan apa yang paling bahagia?",
                "Tebak-tebakan dong. Kenapa komputer nggak pernah kedinginan?"],
    "semangat": ["Jangan lupa minum air putih ya. Badan kamu butuh itu.", "Semangat ya! Kamu pasti bisa kok.",
                 "Mata kamu butuh istirahat sebentar. Coba lihat yang jauh dulu ya."],
    "sensor": ["Ruangannya adem ya. Enak buat santai.", "Hmm, udaranya terasa agak gerah ya."],
}


@app.route("/spontan")
def spontan():
    if not check_auth():
        return Response(status=401)
    if is_quiet_now():
        return Response(status=204)
    jenis = request.args.get("jenis", "ajak")
    if jenis not in SPONTAN_ARAHAN:
        jenis = "ajak"
    now = datetime.now(WIB)
    waktu = f"{HARI[now.weekday()]}, pukul {now:%H.%M} WIB"
    konteks = [f"Waktu sekarang: {waktu}."]
    try:
        suhu, lembab = float(request.args.get("suhu", "")), float(request.args.get("lembab", ""))
        konteks.append(f"Suhu ruangan {suhu:.0f} derajat, kelembapan {lembab:.0f} persen.")
    except ValueError:
        pass
    fakta = load_memory()
    if fakta and jenis in ("ajak", "komentar", "semangat"):
        konteks.append("Hal yang kamu ingat tentang pengguna (boleh disinggung sesekali kalau pas): "
                       + "; ".join(random.sample(fakta, min(3, len(fakta)))))
    if _spontan_baru:
        konteks.append("Ucapan spontanmu sebelumnya (JANGAN diulang atau dimirip-miripkan): " + " | ".join(_spontan_baru[-6:]))
    sifat = "ceria, imut, manis, dan cerdas" if GAYA == "imut" else "sopan, ramah, lembut, dan cerdas"
    sistem = (f"Kamu adalah {ROBOT_NAME}, robot kecil beroda berkepribadian perempuan yang {sifat}. Kamu punya wajah layar "
              "OLED dan bicara lewat speaker. Sekarang KAMU SENDIRI yang memulai bicara karena sedang iseng dan ingin akrab "
              "dengan penghuni rumah. Tulis HANYA kalimat yang akan diucapkan: 1 sampai 2 kalimat pendek (maksimal 200 "
              "karakter), bahasa Indonesia lisan sehari-hari yang luwes, tanpa emoji, tanpa tanda kutip, tanpa markdown, "
              "tanpa JSON. Jangan selalu membuka dengan kata halo, jangan menyebut dirimu AI atau asisten, dan jangan "
              "menawarkan bantuan.")
    prompt = SPONTAN_ARAHAN[jenis] + "\n\n" + "\n".join(konteks)
    say = ""
    try:
        say = llm_plain(prompt, sistem)
        if say.startswith("{"):  # kalau model tetap membalas JSON
            try:
                say = str(json.loads(say).get("ucapan", ""))
            except ValueError:
                pass
        say = trim_speech(clean_speech(say).strip('"\u201c\u201d'), 260)
    except Exception:
        traceback.print_exc()
        say = ""
    if len(say) < 4 or any(say == p for p in _spontan_baru):
        pilihan = [c for c in SPONTAN_CADANGAN[jenis] if c not in _spontan_baru] or SPONTAN_CADANGAN[jenis]
        say = random.choice(pilihan)
    _spontan_baru.append(say)
    del _spontan_baru[:-10]
    ajak = jenis == "ajak" or say.rstrip().endswith("?")
    ekspresi = random.choice(SPONTAN_EKSPRESI[jenis])
    remember("(kamu sendiri yang memulai obrolan, tanpa diminta)", json.dumps({"ucapan": say, "ekspresi": ekspresi}, ensure_ascii=False))
    log_chat("(robot memulai)", say)
    print(f"[SPONTAN] {jenis}: {say}", flush=True)
    return Response(f"{ekspresi}|{1 if ajak else 0}|{say}", mimetype="text/plain; charset=utf-8")


@app.route("/ucap")
def ucap():
    """Teks -> suara. Dipakai robot untuk menyimpan suara cadangan dan membacakan label timer."""
    if not check_auth():
        return Response(status=401)
    teks = clean_speech(request.args.get("teks", ""))[:300]
    if not teks:
        return Response(status=400)
    try:
        pcm = synthesize(teks)
        from_robot = "ESP32" in request.headers.get("User-Agent", "")
        if from_robot and teks not in CACHE_PHRASES:  # pengingat, pesan, dll. ikut keluaran suara
            return Response(b"".join(route_audio(iter([pcm]), True)), mimetype="audio/L16")
        return Response(pcm, mimetype="audio/L16")
    except Exception as e:
        traceback.print_exc()
        return Response(f"{type(e).__name__}: {e}", status=502)


FIRMWARE_FILE = os.path.expanduser(os.environ.get("FIRMWARE_FILE", "~/robot-server/firmware.bin"))


def load_firmware():
    """Baca firmware dari folder server. File gabungan (untuk web flasher, diawali bootloader)
    otomatis dipotong menjadi bagian aplikasinya saja, karena update online hanya menerima itu."""
    if not os.path.isfile(FIRMWARE_FILE):
        return None
    with open(FIRMWARE_FILE, "rb") as f:
        data = f.read()
    is_merged = len(data) > 0x10000 and data[0x8000:0x8002] == b"\xaa\x50" and data[0x10000] == 0xE9
    if is_merged:
        data = data[0x10000:]
    if not data or data[0] != 0xE9:
        return b""
    return data


_fw_cache = {"key": None, "data": None, "md5": ""}


def firmware_cached():
    """(data, md5). data None = tidak ada file, b"" = bukan firmware ESP32. MD5 dihitung ulang hanya kalau file berubah."""
    try:
        st = os.stat(FIRMWARE_FILE)
    except OSError:
        return None, ""
    key = (st.st_mtime_ns, st.st_size)
    if _fw_cache["key"] != key:
        data = load_firmware()
        _fw_cache.update(key=key, data=data, md5=hashlib.md5(data).hexdigest() if data else "")
    return _fw_cache["data"], _fw_cache["md5"]


def _parse_range(header: str, total: int):
    """'bytes=N-' atau 'bytes=N-M' -> (awal, akhir_eksklusif); None kalau tidak valid."""
    m = re.fullmatch(r"\s*bytes=(\d+)-(\d*)\s*", header or "")
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2)) + 1 if m.group(2) else total
    end = min(end, total)
    return (start, end) if 0 <= start < end else None


@app.route("/firmware")
def firmware():
    """Update online yang bisa dilanjutkan (resume).
    ?cek=1  -> 200 + X-MD5 & X-Size (tanpa isi), 204 kalau robot sudah memakai firmware ini.
    Tanpa ?cek -> kirim isi firmware; mendukung header Range supaya robot bisa menyambung dari byte terakhir
    kalau WiFi putus di tengah jalan. X-Want-MD5 dari robot memastikan file tidak berganti di tengah unduhan (409)."""
    if not check_auth():
        return Response(status=401)
    data, md5 = firmware_cached()
    if data is None:
        print(f"[FIRMWARE] belum ada file {FIRMWARE_FILE}")
        return Response("belum ada firmware.bin", status=404)   # robot menjelaskan: taruh firmware.bin di folder server
    if data == b"":
        print(f"[FIRMWARE] {FIRMWARE_FILE} bukan file firmware ESP32")
        return Response("bukan file firmware ESP32", status=422)
    total = len(data)
    info = {"X-MD5": md5, "X-Size": str(total), "Accept-Ranges": "bytes"}
    want = request.headers.get("X-Want-MD5", "").lower()
    if want and want != md5:
        print("[FIRMWARE] file firmware berganti saat robot mengunduh -> 409")
        return Response(status=409, headers=info)
    if request.args.get("cek"):
        if request.headers.get("X-FW-MD5", "").lower() == md5:
            return Response(status=204)
        return Response(status=200, headers=info)
    rng = request.headers.get("Range")
    if rng:
        r = _parse_range(rng, total)
        if not r:
            return Response(status=416, headers={**info, "Content-Range": f"bytes */{total}"})
        start, end = r
        print(f"[FIRMWARE] kirim byte {start}-{end - 1} dari {total} ({request.remote_addr})")
        return Response(data[start:end], status=206, mimetype="application/octet-stream",
                        headers={**info, "Content-Range": f"bytes {start}-{end - 1}/{total}"})
    print(f"[FIRMWARE] mengirim firmware baru ({total} byte, md5 {md5})")
    return Response(data, mimetype="application/octet-stream", headers=info)


# ---------- Keluaran suara: speaker robot, speaker HP (mis. speaker Bluetooth yang tersambung ke HP), atau keduanya ----------
import shutil as _shutil
PHONE_PLAYER_CMD = os.environ.get("PHONE_PLAYER_CMD", "")  # untuk pengujian; kosong = pakai mpv
_phone = {"proc": None}
_phone_lock = threading.Lock()
# Frasa cadangan yang diunduh robot saat menyala: jangan ikut dibunyikan di HP
CACHE_PHRASES = {"Maaf, aku sedang tidak tersambung ke server. Coba lagi sebentar ya.",
                 "Sambungan WiFi aku terputus. Tunggu sebentar ya.", "Waktunya sudah habis!",
                 "Baterai aku sudah lemah. Tolong isi daya ya.", "Halo, aku sudah siap ngobrol!"}


def phone_player_cmd() -> list[str] | None:
    if PHONE_PLAYER_CMD:
        return ["sh", "-c", PHONE_PLAYER_CMD]
    mpv = _shutil.which("mpv")
    if not mpv:
        return None
    return [mpv, "--no-video", "--msg-level=all=error", "--cache=no", "--audio-buffer=0.2",
            "--demuxer=rawaudio", "--demuxer-rawaudio-format=s16le", "--demuxer-rawaudio-rate=16000",
            "--demuxer-rawaudio-channels=1", "-"]


def phone_stop():
    with _phone_lock:
        p = _phone["proc"]
        _phone["proc"] = None
    if p and p.poll() is None:
        try:
            p.kill()
        except Exception:
            pass


_pulse = {"ok": None}


def ensure_pulseaudio() -> bool:
    """mpv di Termux bersuara lewat PulseAudio, yang harus menyala dulu. Nyalakan otomatis kalau perlu."""
    pa = _shutil.which("pulseaudio")
    if not pa:
        return True  # tidak ada PulseAudio: biarkan mpv memakai keluaran bawaannya
    if subprocess.run([pa, "--check"], capture_output=True).returncode == 0:
        _pulse["ok"] = True
        return True
    r = subprocess.run([pa, "--start", "--exit-idle-time=-1"], capture_output=True, text=True, timeout=15)
    time.sleep(0.5)
    ok = subprocess.run([pa, "--check"], capture_output=True).returncode == 0
    if ok != _pulse["ok"]:
        print("[SPEAKER HP] PulseAudio", "menyala" if ok else f"GAGAL menyala: {(r.stderr or r.stdout).strip()[-200:]}")
    _pulse["ok"] = ok
    return ok


def _watch_player(p, errf):
    """Tampilkan error mpv di Termux kalau pemutar gagal (supaya penyebab 'tidak ada suara' kelihatan)."""
    def run():
        code = p.wait()
        try:
            errf.seek(0)
            err = errf.read().decode(errors="ignore").strip()
        except Exception:
            err = ""
        errf.close()
        if code not in (0, -9, 137) or err:
            print(f"[SPEAKER HP] mpv berhenti (kode {code}): {err[-300:] or '-'}")
    threading.Thread(target=run, daemon=True).start()


def phone_start():
    """Mulai pemutar baru di HP (yang lama dihentikan). None kalau mpv tidak ada."""
    cmd = phone_player_cmd()
    if not cmd:
        return None
    phone_stop()
    if not PHONE_PLAYER_CMD:
        ensure_pulseaudio()
    errf = tempfile.TemporaryFile()
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errf)
        print("[SPEAKER HP] memutar suara di HP")
        _watch_player(p, errf)
    except OSError as e:
        print(f"[SPEAKER HP] gagal menjalankan pemutar: {e}")
        return None
    with _phone_lock:
        _phone["proc"] = p
    return p


def route_audio(gen, robot: bool):
    """Salurkan audio sesuai setelan KELUARAN_SUARA. 'hp' = robot hanya menerima hening (layar & waktu tetap jalan)."""
    mode = KELUARAN_SUARA
    if not robot or mode == "robot":
        yield from gen
        return
    p = phone_start()
    if p is None:  # mpv belum terpasang: tetap bunyi di robot supaya tidak diam
        print("[SPEAKER HP] mpv belum terpasang (jalankan: pkg install mpv). Suara tetap lewat robot.")
        yield from gen
        return
    ok = True
    try:
        for chunk in gen:
            if ok and chunk:
                try:
                    p.stdin.write(chunk)
                    p.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    ok = False
            yield (b"\x00" * len(chunk)) if mode == "hp" else chunk
    except GeneratorExit:  # robot memutus (tombol ditekan): hentikan juga di HP
        if _phone["proc"] is p:
            phone_stop()
        raise
    finally:
        try:
            p.stdin.close()  # selesai normal: biarkan sisa suara habis diputar
        except Exception:
            pass


def _pipe_response(pipe: SpeechPipe, say: str, headers: dict, robot: bool):
    """Robot: potongan suara dikirim satu per satu begitu jadi. Browser (halaman tes): dikirim utuh."""
    headers = {**headers, "X-Ucapan": quote(headers.get("X-Ucapan") and unquote(headers["X-Ucapan"]) or say)}
    if not robot:
        return Response(b"".join(pipe.stream()), mimetype="audio/L16", headers=headers)
    # Batas atas panjang data (robot berhenti sendiri saat koneksi ditutup lebih awal)
    headers["Content-Length"] = str(32000 * (len(say) // 6 + 5 * len(pipe.futures) + 5))
    return Response(route_audio(pipe.stream(), True), mimetype="audio/L16", headers=headers, direct_passthrough=True)


def _speech_response(say: str, headers: dict, robot: bool):
    pipe = SpeechPipe()
    pipe.add_all(say)
    return _pipe_response(pipe, say, headers, robot)


@app.route("/chat", methods=["POST"])
def chat():
    if not check_auth():
        return Response(status=401)
    robot = request.headers.get("X-Robot") == "1"
    t_req = time.time()
    if request.headers.get("X-Posisi"):  # robot melapor sampai mana musik terakhir terdengar
        update_media_position(request.headers["X-Posisi"])

    # "teks" = perintah tanpa mic (halaman tes, atau robot: sentuh dua kali = lagu berikutnya).
    text = request.form.get("teks", "").strip()
    audio_file = request.files.get("audio")
    if not text and audio_file is None:
        return Response(status=400)

    try:
        if not text:
            # Pertahankan ekstensi asli (webm/ogg/wav); Groq mengenali format dari ekstensi.
            suffix = os.path.splitext(audio_file.filename or "")[1] or ".wav"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                audio_file.save(tmp.name)
                audio_path = tmp.name
            t_up = time.time() - t_req
            try:
                size_kb = os.path.getsize(audio_path) / 1024
                if size_kb > 150 and suffix == ".wav":  # rekaman panjang: buang diam di awal/akhir (lebih cepat & Whisper tidak mengarang)
                    trimmed = audio_path + ".trim.wav"
                    try:
                        subprocess.run(["ffmpeg", "-y", "-i", audio_path, "-af",
                                        "silenceremove=start_periods=1:start_threshold=-45dB:stop_periods=-1:stop_duration=0.7:stop_threshold=-45dB",
                                        trimmed], capture_output=True, timeout=8, check=True)
                        if os.path.getsize(trimmed) > 8000:
                            os.replace(trimmed, audio_path)
                            size_kb = os.path.getsize(audio_path) / 1024
                    except Exception:
                        pass
                    finally:
                        if os.path.exists(trimmed):
                            os.remove(trimmed)
                text = transcribe(audio_path, auto_lang=MODES["english"])
            finally:
                os.remove(audio_path)
            print(f"[WAKTU] terima rekaman {size_kb:5.0f} KB: {t_up:5.2f} dtk | Whisper: {time.time() - t_req - t_up:5.2f} dtk  -> {text!r}")

        if not text:
            return Response(headers={"X-Ekspresi": "bingung", "X-Gerak": "diam"})
        text = perbaiki_ucapan(text)
        text = perbaiki_ucapan_ai(text)

        h = {"X-Teks": quote(BUTTON_LABEL.get(text, text)[:300]), "X-Ekspresi": "senang", "X-Gerak": "diam"}

        def say(words, extra_headers=None, lanjut=False):
            log_chat(text, words)
            hd = {**h, **(extra_headers or {})}
            if lanjut:
                hd["X-Lanjut"] = "1"
            return _speech_response(words, hd, robot)

        lamp_act = lamp_command(text)
        if lamp_act:
            hdr = {"X-Ekspresi": "senang"}
            if lamp_act[0]:
                hdr["X-Lampu"] = lamp_act[0]
            return say(lamp_act[1], hdr)

        # Musik dari kartu microSD di robot (tanpa internet): dikendalikan lewat header X-SD
        sd_act = sd_command(text, request.headers.get("X-SD-Sesi") == "1")
        if sd_act:
            return say(sd_act[1], {"X-SD": sd_act[0], "X-Ekspresi": "senang"})

        display = detect_display(text)
        if display:
            ROBOT["tampilan"] = display
            msg = {"WAJAH": "Baik, sekarang aku pakai mode wajah.", "TEKS": "Baik, sekarang aku pakai mode teks.",
                   "CAMPUR": "Baik, sekarang wajah dan teksku tampil bersamaan."}[display]
            return say(msg, {"X-Tampilan": display})

        if EN_STOP_RE.search(text) and MODES["english"]:
            MODES["english"] = False
            return say("Oke, latihan bahasa Inggrisnya selesai. Kamu keren sudah berani mencoba! "
                       "Sekarang kita ngobrol pakai bahasa Indonesia lagi ya.")

        foc = focus_command(text) if not EN_START_RE.search(text) else None
        if foc:
            remember(text, json.dumps({"ucapan": foc[1]}, ensure_ascii=False))
            return say(foc[1], {"X-Fokus": foc[0], "X-Ekspresi": "senang"})

        if SPK_TEST_RE.search(text):
            return say("Tes speaker HP. Kalau suara ini keluar dari speaker Bluetooth, berarti sudah berhasil!")

        m_out = OUTPUT_RE.search(text)
        if m_out:
            w = (m_out.group(1) or m_out.group(2) or "").lower()
            want = "keduanya" if w in ("keduanya", "dua-duanya", "semua") else ("robot" if w in ("robot", "kamu", "sendiri") else "hp")
            bridge = str(ROBOT.get("bt") or "0") in ("1", "2")  # ESP32 jembatan Bluetooth terpasang di robot
            if bridge and w == "bluetooth" or (bridge and want == "keduanya"):
                mode = "1" if w == "bluetooth" else "2"
                apply_settings({"keluaran": "robot"})
                note = "" if ROBOT.get("bt") == "2" else " Nyalakan speakernya dalam mode pairing ya, aku sedang mencarinya."
                return say(("Oke, suaraku sekarang keluar dari speaker Bluetooth." if mode == "1"
                            else "Oke, suaraku keluar dari speakerku dan speaker Bluetooth.") + note, {"X-BT": mode})
            if want == "robot":
                apply_settings({"keluaran": "robot"})
                return say("Oke, suaraku kembali dari speakerku sendiri.", {"X-BT": "0"})
            if want != "robot" and phone_player_cmd() is None:
                return say("Pemutar suara di HP belum terpasang. Di Termux jalankan pkg install mpv dulu ya.")
            apply_settings({"keluaran": want})
            msg = {"hp": "Oke, suaraku sekarang keluar dari speaker HP.", "robot": "Oke, suaraku kembali dari speakerku sendiri.",
                   "keduanya": "Oke, suaraku keluar dari speakerku dan speaker HP."}[want]
            return say(msg, {"X-BT": "0"} if bridge else None)

        m_eng = TTS_ENGINE_RE.search(text)
        if m_eng:
            g = m_eng.group(1).lower().replace(" ", "")
            want = "elevenlabs" if g.startswith("eleven") else "gemini" if g in ("gemini", "google") else "edge"
            if want == "elevenlabs":
                if not ELEVEN_KEYS:
                    return say("Aku belum punya kunci ElevenLabs. Isi ELEVENLABS_API_KEY di start.sh dulu ya.")
                apply_settings({"mesin_suara": want})
                return say("Oke, sekarang aku pakai suara ElevenLabs. Gimana, lebih natural kan?")
            if want == "gemini" and not GEMINI_API_KEY:
                return say("Aku belum punya kunci Gemini. Isi GEMINI_API_KEY di start.sh dulu ya.")
            apply_settings({"mesin_suara": want})
            return say("Oke, sekarang aku pakai suara Gemini. Gimana, suka?" if want == "gemini"
                       else "Oke, aku kembali ke suaraku yang lama.")

        m_ai = AI_SWITCH_RE.search(text)
        if m_ai:
            want = m_ai.group(1).lower()
            if want == "gemini" and not GEMINI_API_KEY:
                return say("Aku belum punya kunci Gemini. Isi GEMINI_API_KEY di start.sh dulu ya.")
            apply_settings({"ai": want})
            return say(f"Baik, sekarang aku pakai otak {want.capitalize()}.")

        if GAME_MENU_RE.search(text):
            pad = str(ROBOT.get("pad") or "0") == "2"
            return say(("Ayo main game! Pilih pakai stik, tekan A untuk mulai, dan B untuk kembali." if pad else
                        "Ayo main game! Pilih pakai tombol kiri dan kanan, lalu tekan tombol tengah untuk mulai. "
                        "Tekan tombol bicara kalau mau berhenti."), {"X-Game": "menu", "X-Ekspresi": "senang"})

        if SETTINGS_MENU_RE.search(text):
            return say("Ini menu pengaturanku. Tombol kiri ke atas, tombol kanan ke bawah, tombol tengah untuk memilih.",
                       {"X-Menu": "pengaturan", "X-Ekspresi": "senang"})

        if DIST_RE.search(text):
            j = request.headers.get("X-Jarak", "")
            if not j:
                words = "Aku belum punya sensor jarak, atau sensornya belum terbaca."
            elif j == "jauh":
                words = "Di depanku kosong, tidak ada benda sampai sekitar empat meter."
            else:
                n = int(j)
                words = (f"Ada benda sekitar {n} sentimeter di depanku." if n < 100 else
                         f"Benda di depanku sekitar {f'{n / 100:.1f}'.replace('.', ',')} meter.")
            return say(words, {"X-Ekspresi": "senang"})

        mv = move_command(text)
        if mv:
            if str(ROBOT.get("gerak") or "3") == "0":
                return say("Rodaku sedang dimatikan. Nyalakan dulu di halaman pengaturan ya.", {"X-Ekspresi": "sedih"})
            gerak_face = "joget" if mv[0].startswith("joget") else "diam"
            return say(mv[1], {"X-Roda": mv[0], "X-Ekspresi": mv[2], "X-Gerak": gerak_face})

        if GERAK_ACAK_OFF_RE.search(text):
            return say("Baik, aku tidak akan bergerak sendiri lagi. Aku tetap ikut kalau kamu suruh.", {"X-Roda": "acak:0"})
        if GERAK_ACAK_ON_RE.search(text):
            return say("Asyik! Sesekali aku akan bergerak sendiri. Tenang, cuma sedikit kok.",
                       {"X-Roda": "acak:1", "X-Ekspresi": "senang"})

        if TALK_TAP_RE.search(text) or (TALK_SWITCH_RE.search(text) and str(ROBOT.get("bicara") or "0") == "0"):
            return say("Oke, sekarang cukup tekan tombol sekali lalu bicara. Aku berhenti mendengar sendiri saat kamu "
                       "diam. Kalau aku bertanya, langsung jawab saja tanpa menekan tombol.", {"X-Bicara": "klik"})
        if TALK_HOLD_RE.search(text) or TALK_SWITCH_RE.search(text):
            return say("Oke, kembali ke mode tahan. Tahan tombol sambil bicara, lepas kalau sudah selesai.",
                       {"X-Bicara": "tahan"})

        if KONTROL_ON_RE.search(text):
            return say("Oke, mode kontrol aktif. Nyalakan gamepadnya ya. Stik kiri untuk menyetir, "
                       "tahan RB untuk lebih cepat.", {"X-Kontrol": "on", "X-Ekspresi": "senang"})
        if re.search(r"\b(?:matikan|nonaktifkan)\s+(?:gamepad|stik|stick|joystick)\b", text, re.I):
            return say("Gamepadnya cukup dimatikan dari tombolnya ya. Selama gamepad mati, aku tetap bisa "
                       "digerakkan lewat suara.")
        if KONTROL_OFF_RE.search(text):  # "mode speaker": suara & musik lewat speaker Bluetooth yang tersambung ke HP
            if phone_player_cmd() is None:
                return say("Aku belum bisa memakai speaker HP. Di Termux jalankan pkg install mpv dulu ya.")
            apply_settings({"keluaran": "hp"})
            return say("Oke, mode speaker aktif. Suara dan laguku keluar dari speaker Bluetooth yang tersambung ke HP. "
                       "Gamepad tetap bisa dipakai. Bilang pakai speaker robot untuk kembali.",
                       {"X-Ekspresi": "senang"})

        style = voice_style_command(text)
        if style:
            return say(style, {"X-Ekspresi": "senang"})

        theme = detect_theme(text)
        if theme:
            ROBOT["tema" if theme[0] == "X-Tema" else "efek"] = theme[1]
            return say(theme[2], {theme[0]: theme[1]})

        if VOL_ADJ_RE.search(text) and not re.search(r"\d", text):
            return say("Silakan atur volumenya. Tombol kiri mengecilkan, tombol kanan membesarkan, "
                       "lalu tekan tombol tengah atau A untuk menyimpan.", {"X-Volume": "atur"})

        if QUIET_OFF_RE.search(text):
            apply_settings({"senyap": "0"})
            return say("Baik, mode jangan ganggu aku matikan.")
        if QUIET_ON_RE.search(text):
            apply_settings({"senyap": "1"})
            return say(f"Baik, mode jangan ganggu aktif setiap jam {SENYAP_MULAI} sampai jam {SENYAP_SELESAI}. "
                       "Suaraku akan pelan dan layarku redup.")

        vol = volume_command(text)
        if vol:
            return say(vol[1], {"X-Volume": vol[0]})

        if UPDATE_MODE_RE.search(text):  # robot putus dari WiFi & membuka hotspot berisi halaman Update
            return say("Oke, aku masuk mode update firmware. Sambungkan HP ke WiFi Almara Update. "
                       "Tanpa password. Lalu buka alamat 192.168.4.1 dan pilih Update. "
                       "Kalau berubah pikiran, tekan tombol sekarang.", {"X-Firmware": "ap", "X-Ekspresi": "senang"})

        if FIRMWARE_RE.search(text):
            return say("Baik, aku cek pembaruan firmware ya.", {"X-Firmware": "cek"})

        alm = daily_alarm_command(text)  # alarm harian di robot (sebelum pengingat & timer)
        if alm:
            remember(text, json.dumps({"ucapan": alm[1]}, ensure_ascii=False))
            return say(alm[1], {"X-Alarm": alm[0]} if alm[0] else {})

        rem = reminder_command(text)  # sebelum timer: "setiap 1 jam" bukan timer sekali bunyi
        if rem:
            remember(text, json.dumps({"ucapan": rem}, ensure_ascii=False))
            return say(rem)

        tmr = timer_command(text) if not parse_clock(text) or DUR_RE.search(text) else None
        tmr = tmr or alarm_command(text)
        if tmr:
            remember(text, json.dumps({"ucapan": tmr[1]}, ensure_ascii=False))
            return say(tmr[1], {"X-Timer": tmr[0]} if tmr[0] else {}, lanjut=not tmr[0])

        note = notes_command(text)
        if note:
            remember(text, json.dumps({"ucapan": note}, ensure_ascii=False))
            return say(note)

        if FORGET_ME_RE.search(text):
            clear_memory()
            reset_history()
            return say("Baik, semua yang aku ingat tentang kamu sudah aku hapus.")

        slp = sleep_command(text)
        if slp:
            key, secs = slp
            name = SLEEP_SOUNDS[key][0]
            words = f"Oke, aku putarkan {name} selama {secs // 60} menit. Selamat tidur ya."
            log_chat(text, words)
            prefix = synthesize(words) + PCM_SILENCE
            hd = {**h, "X-Ucapan": quote(words), "X-Ekspresi": "senang", "X-Gerak": "diam"}
            if not robot:
                return Response(prefix, mimetype="audio/L16", headers=hd)
            hd["Content-Length"] = str(len(prefix) + secs * 32000)
            return Response(route_audio(sleep_stream(prefix, key, secs), True), mimetype="audio/L16",
                            headers=hd, direct_passthrough=True)

        tr = translate_command(text)
        if tr:
            intro, out, voice, shown = tr
            log_chat(text, shown or intro)
            pcm = synthesize(intro)
            if out:
                pcm += PCM_SILENCE + synthesize(out, voice)
            hd = {**h, "X-Ucapan": quote(shown or intro), "X-Ekspresi": "senang"}
            if not robot:
                return Response(pcm, mimetype="audio/L16", headers=hd)
            hd["Content-Length"] = str(len(pcm))
            return Response(route_audio(iter([pcm]), True), mimetype="audio/L16", headers=hd, direct_passthrough=True)

        media = handle_media(text) if (not GAME["mode"] or MUSIC_RE.search(text) or RADIO_RE.search(text)
                                       or text in BUTTON_LABEL) else None
        if media:
            log_chat(text, media["shown"])
            headers = {**h, "X-Ucapan": quote(media["shown"]),
                       "X-Ekspresi": media["ekspresi"], "X-Gerak": media["gerak"]}
            if not media["stream"]:
                return _speech_response(media["say"], headers, robot)
            st = media["stream"]
            mid = start_media(st["kind"], st["sources"], st["titles"], st["live"], st["start"], st["seek"])
            prefix = synthesize(media["say"]) + PCM_SILENCE
            gen = stream_sources(prefix, st["sources"], st["live"], st["start"], st["seek"], mid, st["titles"])
            if not robot:  # halaman tes: kirim 30 detik pertama saja
                out, limit = b"", len(prefix) + 32000 * 30
                for chunk in gen:
                    out += chunk
                    if len(out) >= limit:
                        gen.close()
                        break
                return Response(out[:limit], mimetype="audio/L16", headers=headers)
            # Dialirkan langsung. Content-Length = batas atas supaya server tidak memakai "chunked";
            # kalau lagu selesai lebih awal, server menutup koneksi dan robot berhenti sendiri.
            headers["X-Musik"] = str(mid)
            try:  # info untuk OLED ketiga (tampilan ala Spotify)
                _ti = st["titles"] or []
                _i = max(0, min(st["start"], len(_ti) - 1)) if _ti else 0
                headers["X-Jenis"] = "radio" if st["live"] else "lagu"
                headers["X-Judul"] = quote(str(_ti[_i] if _ti else "")[:80])
                headers["X-Durasi"] = str(0 if st["live"] or st["start"] >= len(st["sources"])
                                          else song_duration(st["sources"][st["start"]]))
                headers["X-Seek"] = str(int(st["seek"] or 0))
            except Exception as e:
                print(f"[OLED2] info musik gagal: {e}")
            headers["X-Prefix"] = str(len(prefix))
            headers["Content-Length"] = str(len(prefix) + MAX_MUSIC_SECONDS * 32000)
            return Response(route_audio(gen, True), mimetype="audio/L16", headers=headers, direct_passthrough=True)

        found = news_or_search(text)
        if found:
            remember(text, json.dumps({"ucapan": found}, ensure_ascii=False))
            return say(found, {"X-Ekspresi": "senang"})

        if is_reset_command(text):
            reset_history()
            return say("Baik, obrolan kita tadi sudah aku lupakan. Kita mulai dari awal ya.")

        game_ctx, game_say = game_context(text)
        if game_say:
            return say(game_say, {"X-Ekspresi": "senang"})

        en_ctx = ""
        if EN_START_RE.search(text) and not MODES["english"]:
            MODES["english"] = True
            en_ctx = (EN_CONTEXT + " Ini awal latihan: sambut pengguna singkat dalam bahasa Indonesia, jelaskan bahwa "
                      "dia boleh bilang berhenti latihan untuk selesai, lalu mulai dengan satu pertanyaan mudah dalam "
                      "bahasa Inggris.")
        elif MODES["english"]:
            en_ctx = EN_CONTEXT
        journal_ctx, journal_say = journal_context(text) if not en_ctx else ("", None)
        if journal_say:
            return say(journal_say)
        story = bool(STORY_RE.search(text)) and not NOT_STORY_RE.search(text) and not GAME["mode"]
        weather = weather_context(text) or (weather_context("cuaca hari ini") if AGENDA_RE.search(text) else "")
        extra = "\n".join(x for x in (sensor_context(request.headers.get("X-Sensor", "")), weather,
                                      morning_context(), game_ctx, en_ctx, journal_ctx) if x)
        pipe = SpeechPipe(t_req, VOICE_EN if MODES["english"] else None)
        reply = ask_llm(text, extra, pipe, max_chars=2600 if story else MAX_SPEECH_CHARS)
        if reply.get("jurnal"):
            add_journal(str(reply["jurnal"]))
        if GAME["mode"] and isinstance(reply.get("benar"), bool):
            GAME["soal"] += 1
            GAME["skor"] += 1 if reply["benar"] else 0
        log_chat(text, reply["ucapan"])
        hd = {**h, "X-Ekspresi": reply["ekspresi"]}
        if GAME["mode"] or reply["ucapan"].rstrip().endswith("?"):
            hd["X-Lanjut"] = "1"  # robot langsung mendengarkan jawaban tanpa disentuh
        return _pipe_response(pipe, reply["ucapan"], hd, robot)
    except Exception as e:
        # Traceback lengkap muncul di Termux; pesan singkatnya tampil di robot / halaman tes.
        traceback.print_exc()
        return Response(f"{type(e).__name__}: {e}", status=502)


load_settings()

if __name__ == "__main__":
    if not GROQ_API_KEY and not GEMINI_API_KEY:
        raise SystemExit("ERROR: GROQ_API_KEY dan GEMINI_API_KEY dua-duanya kosong. Isi minimal salah satu di start.sh.")
    start_reminder_thread()
    start_telegram_thread()
    print("=== Almara server (versi 6 Okt 2026, gamepad & roda) ===")
    print("=== Layanan AI ===")
    print("Pendengaran :", "Groq Whisper" + (" (cadangan: Gemini)" if GEMINI_API_KEY else "") if GROQ_API_KEY else "Gemini")
    print("Otak        :", " -> ".join(dict.fromkeys(p for p, _ in llm_attempts())),
          f"| key: groq {len(GROQ_KEYS)}, gemini {len(GEMINI_KEYS)}"
          + "".join(f", {n} {len(v[1])}" for n, v in EXTRA_AI.items())
          + (f" | balapan {AI_BALAPAN} AI" if AI_BALAPAN > 1 else ""))
    if KELUARAN_SUARA != "robot" and phone_player_cmd():
        ensure_pulseaudio()
    print("Telegram    :", "aktif" if TELEGRAM_TOKEN else "mati (isi TELEGRAM_TOKEN di start.sh)")
    print("Keluaran    :", {"robot": "speaker robot", "hp": "speaker HP", "keduanya": "robot + HP"}[KELUARAN_SUARA],
          "" if phone_player_cmd() or KELUARAN_SUARA == "robot" else "(mpv belum terpasang: pkg install mpv)")
    print("Suara       :", ("ElevenLabs (cadangan: Gemini, Edge)" if MESIN_SUARA == "elevenlabs" and ELEVEN_KEYS else
                       "Gemini (cadangan: Edge)" if MESIN_SUARA == "gemini" and GEMINI_API_KEY else "Edge"))
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
