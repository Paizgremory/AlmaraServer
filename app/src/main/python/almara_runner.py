"""Jembatan Android -> server.py (server.py tidak diubah sama sekali)."""
import os
import sys
import threading

_state = {"httpd": None, "thread": None, "error": ""}
_log_lines = []
_log_lock = threading.Lock()


class _Tee:
    def __init__(self, orig):
        self.orig = orig

    def write(self, s):
        try:
            self.orig.write(s)
        except Exception:
            pass
        if s and s.strip():
            with _log_lock:
                _log_lines.extend(s.rstrip().splitlines())
                del _log_lines[:-300]

    def flush(self):
        try:
            self.orig.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return getattr(self.orig, "encoding", None) or "utf-8"

    def __getattr__(self, name):  # atribut lain (fileno, errors, ...) diteruskan ke stream asli
        return getattr(self.orig, name)


def get_log():
    with _log_lock:
        return "\n".join(_log_lines[-80:])


def get_error():
    return _state["error"]


class _FakePopen:
    """Menggantikan proses mpv: PCM s16le 16 kHz mono ditulis ke AudioTrack Android."""

    def __init__(self, player):
        self._player = player
        self._player.start()
        self.returncode = None
        self.stdin = self

    def write(self, data):
        self._player.write(bytes(data))

    def flush(self):
        pass

    def close(self):
        self._player.finish()
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode if self.returncode is not None else 0

    def kill(self):
        self._player.stop()
        self.returncode = -9


def _prepare_ffmpeg(native_dir, bin_dir):
    os.makedirs(bin_dir, exist_ok=True)
    for name in ("ffmpeg", "ffprobe"):
        src = os.path.join(native_dir, f"lib{name}.so")
        dst = os.path.join(bin_dir, name)
        if not os.path.exists(src):
            print(f"[ALMARA] PERINGATAN: lib{name}.so tidak ada di jniLibs (musik/efek suara tidak jalan)")
            continue
        try:
            if os.path.lexists(dst):
                os.remove(dst)
            os.symlink(src, dst)
        except OSError as e:
            print(f"[ALMARA] gagal membuat symlink {name}: {e}")
    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


def _wav_to_pcm16k(path, limit, hpf_hz):
    """WAV (mono, 16 bit, laju berapa pun) -> PCM s16le 16 kHz mono, tanpa ffmpeg.
    Setara kasar dengan filter ffmpeg di server.py: buang bass, normalkan, kunci puncak di `limit`."""
    import wave
    from array import array
    with wave.open(path, "rb") as w:
        ch, sw, rate, raw = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())
    if sw != 2:
        raise RuntimeError(f"WAV {sw * 8} bit tidak didukung tanpa ffmpeg")
    a = array("h")
    a.frombytes(raw[: len(raw) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    if ch > 1:
        a = a[::ch]
    if rate != 16000 and len(a):  # penurun/penaik laju sampel (interpolasi linear)
        try:
            import audioop  # ada di Python 3.12 (Chaquopy)
            b = a.tobytes()
            a = array("h")
            a.frombytes(audioop.ratecv(b, 2, 1, rate, 16000, None)[0])
        except ImportError:
            n = int(len(a) * 16000 / rate)
            step = rate / 16000.0
            out = array("h", bytes(2 * n))
            last = len(a) - 1
            for i in range(n):
                x = i * step
                j = int(x)
                f = x - j
                out[i] = int(a[j] * (1 - f) + a[min(j + 1, last)] * f)
            a = out
    if hpf_hz > 0 and len(a):  # penyaring bass (satu kutub): speaker kecil pecah kalau dipaksa
        import math
        rc = 1.0 / (2 * math.pi * hpf_hz)
        al = rc / (rc + 1.0 / 16000)
        y = 0.0
        px = float(a[0])
        out = array("h", bytes(2 * len(a)))
        for i, v in enumerate(a):
            y = al * (y + v - px)
            px = v
            out[i] = max(-32768, min(32767, int(y)))
        a = out
    peak = max((abs(v) for v in a), default=0)
    if peak > 0:  # normalkan sampai puncaknya tepat di batas speaker (maks. perbesar 8x)
        k = min(8.0, limit * 32767 / peak)
        a = array("h", (max(-32768, min(32767, int(v * k))) for v in a))
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def _install_no_ffmpeg_tts(server):
    """Kalau ffmpeg tidak ada: suara jawaban dibuat dari Gemini/ElevenLabs (keluarannya WAV) lalu diolah di Python."""
    import shutil
    import tempfile
    orig = server.synthesize

    def synthesize(text, voice=None):
        if shutil.which("ffmpeg"):
            return orig(text, voice)
        fd, wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            ok = False
            if getattr(server, "ELEVEN_KEYS", None) and server.eleven_tts_active():
                ok = server.eleven_tts(text, wav)
            if not ok and getattr(server, "GEMINI_API_KEY", ""):
                ok = server.gemini_tts(text, wav)
            if not ok:
                raise RuntimeError("Suara gagal dibuat: tanpa ffmpeg hanya Gemini/ElevenLabs yang bisa dipakai "
                                   "(isi GEMINI_API_KEY, atau taruh libffmpeg.so di jniLibs).")
            return _wav_to_pcm16k(wav, float(getattr(server, "SPEAKER_LIMIT", 0.55)),
                                  int(getattr(server, "EQ_HPF_HZ", 250)))
        finally:
            if os.path.exists(wav):
                os.remove(wav)

    server.synthesize = synthesize
    print("[ALMARA] ffmpeg " + ("ada" if shutil.which("ffmpeg") else "TIDAK ada: memakai pengolah suara Python (musik/radio tetap butuh ffmpeg)"))



def _jbytes(a):
    """byte[] Java -> bytes Python."""
    try:
        return bytes(a)
    except Exception:
        return bytes((x & 0xFF) for x in a)


def _scale(b, f):
    if f >= 0.999 or not b:
        return b
    try:
        import audioop
        return audioop.mul(b, 2, f)
    except ImportError:
        from array import array
        a = array("h")
        a.frombytes(b)
        return array("h", (int(v * f) for v in a)).tobytes()


class _PcmStdout:
    """Meniru proc.stdout milik ffmpeg: PCM dari dekoder Android, dengan batas lama dan pengecil volume."""

    def __init__(self, dec, max_bytes, factor):
        self._dec, self._left, self._f = dec, max_bytes, factor
        self._buf, self._eof = b"", False

    def _fill(self, n):
        while not self._eof and (n < 0 or len(self._buf) < n) and self._left > 0:
            raw = self._dec.read()
            if raw is None:
                self._eof = True
                break
            b = _jbytes(raw)
            if not b:
                continue
            b = b[: self._left // 2 * 2]
            self._left -= len(b)
            self._buf += _scale(b, self._f)
        if self._left <= 0:
            self._eof = True

    def read(self, n=-1):
        self._fill(n)
        if n is None or n < 0:
            out, self._buf = self._buf, b""
        else:
            out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def close(self):
        try:
            self._dec.close()
        except Exception:
            pass


class _FakeFfmpegProc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode if self.returncode is not None else 0

    def kill(self):
        self.stdout.close()
        self.returncode = -9

    terminate = kill


def _install_no_ffmpeg_music(server, Decoder):
    """Tanpa ffmpeg: lagu (file di HP, cuplikan, siaran http) didekode oleh MediaCodec Android."""
    import shutil
    import subprocess as real
    if shutil.which("ffmpeg"):
        return

    def parse(cmd):
        if "lavfi" in cmd:
            raise FileNotFoundError("efek suara sintetis butuh ffmpeg")
        src, seek, secs, i = None, 0.0, None, 1
        while i < len(cmd):
            a = cmd[i]
            if a == "-i" and i + 1 < len(cmd):
                src, i = cmd[i + 1], i + 2
            elif a == "-ss" and i + 1 < len(cmd):
                seek, i = float(cmd[i + 1]), i + 2
            elif a == "-t" and i + 1 < len(cmd):
                secs, i = float(cmd[i + 1]), i + 2
            else:
                i += 1
        return src, seek, secs

    def make(cmd, data=None):
        src, seek, secs = parse(cmd)
        tmp = None
        if src == "pipe:0":
            import tempfile
            fd, tmp = tempfile.mkstemp(suffix=".audio")
            with os.fdopen(fd, "wb") as f:
                f.write(data or b"")
            src = tmp
        limit = float(getattr(server, "SPEAKER_LIMIT", 0.55))
        gain = 10 ** (float(getattr(server, "MUSIC_GAIN_DB", 0)) / 20.0)
        dec = Decoder(src, float(seek))
        out = _PcmStdout(dec, int((secs if secs is not None else 86400) * 32000), min(1.0, limit * gain))
        if tmp:
            orig_close = out.close
            def close():
                orig_close()
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            out.close = close
        return out

    class Shim:
        def __getattr__(self, name):
            return getattr(real, name)

        @staticmethod
        def _is_ff(cmd):
            return isinstance(cmd, (list, tuple)) and bool(cmd) and cmd[0] == "ffmpeg"

        def Popen(self, cmd, *a, **kw):
            if self._is_ff(cmd):
                return _FakeFfmpegProc(make(cmd))
            return real.Popen(cmd, *a, **kw)

        def run(self, cmd, *a, **kw):
            if self._is_ff(cmd):
                out = make(cmd, kw.get("input"))
                try:
                    data = out.read(-1)
                finally:
                    out.close()
                if kw.get("check") and not data:
                    raise real.CalledProcessError(1, cmd)
                return real.CompletedProcess(cmd, 0, stdout=data, stderr=b"")
            return real.run(cmd, *a, **kw)

    server.subprocess = Shim()

    def song_duration(path):
        try:
            return int(Decoder.duration(path)) if path and os.path.isfile(path) else 0
        except Exception:
            return 0

    server.song_duration = song_duration
    print("[ALMARA] musik: memakai dekoder Android (MediaCodec) sebagai pengganti ffmpeg")



def _install_android_audio(server):
    """Tanpa ffmpeg: perintah ffmpeg/ffprobe milik server.py (musik dari HP, cuplikan lagu, durasi, cek siaran)
    dialihkan ke AudioDecoder Android. Hanya mengganti nama `subprocess` di dalam modul server, bukan global."""
    try:
        from com.almara.server import AudioDecoder
    except Exception as e:
        print("[ALMARA] AudioDecoder tidak bisa dimuat (musik tidak bisa diputar):", repr(e))
        return
    import subprocess as real
    import tempfile

    def jbytes(a):
        try:
            return bytes(a)
        except Exception:
            return bytes(x & 0xFF for x in a)  # byte[] Java bertanda

    def parse(cmd):
        cmd = list(cmd)
        if "-i" not in cmd or "pipe:1" not in cmd or "lavfi" in cmd:
            raise FileNotFoundError("ffmpeg: perintah ini tidak didukung tanpa ffmpeg")
        src = cmd[cmd.index("-i") + 1]
        seek = float(cmd[cmd.index("-ss") + 1]) if "-ss" in cmd else 0.0
        secs = float(cmd[cmd.index("-t") + 1]) if "-t" in cmd else 1e9
        gain = float(getattr(server, "SPEAKER_LIMIT", 0.55)) * 10 ** (float(getattr(server, "MUSIC_GAIN_DB", 0)) / 20)
        return src, seek, int(min(secs, 1e7) * 32000), gain

    class FfmpegProc:
        """Berperilaku seperti Popen ffmpeg yang stdout-nya berisi PCM s16le 16 kHz mono."""

        def __init__(self, cmd):
            self.src, self.seek, self.left, self.gain = parse(cmd)
            self.dec, self.buf, self.eof, self.killed = None, bytearray(), False, False
            self.returncode = None
            self.stdout = self
            self.stderr = None
            self._lock = threading.Lock()

        def _open(self):  # dibuka di pembaca pertama, supaya sambungan jaringan tidak menahan pemanggil
            with self._lock:
                if self.dec is not None or self.eof:
                    return
                try:
                    self.dec = AudioDecoder(self.src, self.seek, self.gain)
                except Exception as e:
                    print(f"[AUDIO] gagal membuka {str(self.src)[:70]}: {e}")
                    self.eof = True

        def read(self, n=-1):
            if self.dec is None:
                self._open()
            while not self.eof and not self.killed and (n < 0 or len(self.buf) < n) and self.left > 0:
                try:
                    chunk = self.dec.read()
                except Exception as e:
                    print(f"[AUDIO] gagal mendekode: {e}")
                    chunk = None
                if not chunk:
                    self.eof = True
                    break
                b = jbytes(chunk)[: self.left]
                self.left -= len(b)
                self.buf += b
            if n < 0:
                n = len(self.buf)
            out = bytes(self.buf[:n])
            del self.buf[:n]
            return out

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode or 0

        def kill(self):
            self.killed = True
            self.returncode = -9
            with self._lock:
                d, self.dec = self.dec, None
            if d is not None:
                try:
                    d.close()
                except Exception:
                    pass

        terminate = kill

    def decode_all(src, seek, left, gain):
        proc = FfmpegProc(["ffmpeg", "-ss", str(seek), "-i", src, "-t", str(left / 32000), "pipe:1"])
        proc.gain, proc.left = gain, left
        try:
            return proc.read(-1)
        finally:
            proc.kill()

    def run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "ffprobe":
            dur = 0.0
            try:
                dur = AudioDecoder.durationSec(str(cmd[-1]))
            except Exception:
                pass
            out = f"{dur}\n"
            return real.CompletedProcess(cmd, 0, out if kw.get("text") else out.encode(), b"")
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "ffmpeg":
            src, seek, left, gain = parse(cmd)
            tmp = None
            try:
                if src == "pipe:0":  # cuplikan lagu dikirim lewat stdin: simpan sementara
                    fd, tmp = tempfile.mkstemp(suffix=".mp3")
                    with os.fdopen(fd, "wb") as f:
                        f.write(kw.get("input") or b"")
                    src = tmp
                box = {}
                t = threading.Thread(target=lambda: box.update(pcm=decode_all(src, seek, left, gain)), daemon=True)
                t.start()
                t.join(kw.get("timeout"))
                if t.is_alive():
                    raise real.TimeoutExpired(cmd, kw.get("timeout"))
                pcm = box.get("pcm", b"")
                if kw.get("check") and not pcm:
                    raise real.CalledProcessError(1, cmd)
                return real.CompletedProcess(cmd, 0, pcm, b"")
            finally:
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
        return real.run(cmd, *a, **kw)

    def Popen(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "ffmpeg":
            return FfmpegProc(cmd)
        return real.Popen(cmd, *a, **kw)

    class Shim:
        def __getattr__(self, name):
            return getattr(real, name)

    shim = Shim()
    shim.run, shim.Popen = run, Popen
    server.subprocess = shim
    print("[ALMARA] musik: memakai pendekode audio Android (tanpa ffmpeg)")



def _to_env_dict(env):
    """env bisa berupa teks JSON (cara yang dipakai aplikasi), dict, atau Map Java."""
    if env is None:
        return {}
    if isinstance(env, (str, bytes)):
        import json
        return dict(json.loads(env or "{}"))
    if isinstance(env, dict):
        return dict(env)
    return {str(k): env[k] for k in env}  # Map Java: iterasi kunci lalu ambil nilainya


def _install_streams():
    if not isinstance(sys.stdout, _Tee):
        sys.stdout = _Tee(sys.__stdout__ or sys.stdout)
    if not isinstance(sys.stderr, _Tee):
        sys.stderr = _Tee(sys.__stderr__ or sys.stderr)


def start(files_dir, native_dir, env):
    """Mulai server di thread latar. TIDAK PERNAH melempar error ke Android (itu yang membuat aplikasi crash):
    kalau gagal, alasan dikembalikan sebagai teks dan juga tersimpan untuk ditampilkan di layar aplikasi."""
    try:
        if _state["thread"] and _state["thread"].is_alive():
            return ""
        _state["error"] = ""
        _install_streams()

        home = str(files_dir)
        os.environ["HOME"] = home
        os.makedirs(os.path.join(home, "robot-server"), exist_ok=True)
        try:  # kalau Python sampai crash di tingkat native, jejaknya tersimpan di file ini
            import faulthandler
            _state["fh"] = open(os.path.join(home, "native_crash.txt"), "w")
            faulthandler.enable(file=_state["fh"], all_threads=True)
        except Exception:
            pass
        for k in _state.get("env_keys", []):  # setelan yang dikosongkan sejak start sebelumnya harus hilang juga
            os.environ.pop(k, None)
        applied = []
        for k, v in _to_env_dict(env).items():
            if v is not None and str(v) != "":
                os.environ[str(k)] = str(v)
                applied.append(str(k))
        _state["env_keys"] = applied
        os.environ.setdefault("PORT", "8000")
        _prepare_ffmpeg(str(native_dir), os.path.join(home, "bin"))
    except BaseException as e:  # jangan sampai keluar ke Kotlin
        import traceback
        _state["error"] = f"Gagal menyiapkan server: {e!r}"
        traceback.print_exc()
        return _state["error"]

    def run():
        try:
            print("[ALMARA] memuat server.py ...")
            import importlib
            if "server" in sys.modules:  # dimulai ulang: baca ulang setelan (env) yang baru
                server = importlib.reload(sys.modules["server"])
            else:
                import server  # server.py asli

            player = None
            try:
                from com.almara.server import PcmPlayer
                player = PcmPlayer
            except Exception as e:
                print("[ALMARA] PcmPlayer tidak bisa dimuat (speaker HP mati, robot tetap jalan):", repr(e))

            if player is not None:
                server.phone_player_cmd = lambda: ["android-audiotrack"]
                server.ensure_pulseaudio = lambda: True

                def phone_stop():
                    with server._phone_lock:
                        p = server._phone["proc"]
                        server._phone["proc"] = None
                    if p is not None:
                        p.kill()

                def phone_start():
                    phone_stop()
                    p = _FakePopen(player)
                    print("[SPEAKER HP] memutar suara lewat AudioTrack")
                    with server._phone_lock:
                        server._phone["proc"] = p
                    return p

                server.phone_stop = phone_stop
                server.phone_start = phone_start

            _install_no_ffmpeg_tts(server)
            import shutil as _sh
            if not _sh.which("ffmpeg"):
                _install_android_audio(server)
            try:
                from com.almara.server import PcmDecoder
                _install_no_ffmpeg_music(server, PcmDecoder)
            except Exception as e:
                print("[ALMARA] dekoder musik Android tidak bisa dimuat:", repr(e))

            if not _state.get("reminder_started"):  # jangan menggandakan thread kalau server dimulai ulang
                _state["reminder_started"] = True
                server.start_reminder_thread()
            if not _state.get("telegram_started") and getattr(server, "TELEGRAM_TOKEN", ""):
                _state["telegram_started"] = True
                server.start_telegram_thread()
            if not getattr(server, "GROQ_API_KEY", "") and not getattr(server, "GEMINI_API_KEY", ""):
                print("[ALMARA] PERINGATAN: GROQ_API_KEY dan GEMINI_API_KEY kosong, robot belum bisa menjawab.")

            from werkzeug.serving import make_server
            port = int(os.environ["PORT"])
            httpd = make_server("0.0.0.0", port, server.app, threaded=True)
            _state["httpd"] = httpd
            print("=== Almara server jalan di port", port, "===")
            httpd.serve_forever()
        except SystemExit as e:
            _state["error"] = str(e) or "server berhenti"
            print("[ALMARA] server berhenti:", e)
        except BaseException as e:
            import traceback
            _state["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            _state["httpd"] = None

    t = threading.Thread(target=run, name="almara-server", daemon=True)
    _state["thread"] = t
    t.start()
    return ""


def stop():
    httpd = _state["httpd"]
    if httpd:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
    _state["httpd"] = None
    _state["thread"] = None


def is_running():
    t = _state["thread"]
    return bool(t and t.is_alive() and _state["httpd"])
