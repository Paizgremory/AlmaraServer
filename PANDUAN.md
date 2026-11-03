# Almara Server (APK) — pengganti Termux

server.py dipakai **apa adanya** (tidak diubah). Aplikasi ini menjalankannya lewat Chaquopy (Python di dalam APK)
sebagai foreground service, dan mengganti `mpv` dengan AudioTrack Android untuk "speaker HP".

## Cara build lewat GitHub Actions
Upload isi folder ini ke repo GitHub, tab Actions > Build APK > unduh artifact "AlmaraServer-apk".

## Cara build (Android Studio)
1. Install Android Studio. File > Open > pilih folder `AlmaraServer`. Biarkan Gradle sync (butuh internet; Gradle wrapper dibuat otomatis).
2. (Opsional tapi disarankan) taruh `libffmpeg.so` dan `libffprobe.so` (ffmpeg Android arm64) di
   `app/src/main/jniLibs/arm64-v8a/`. Butuh build yang dikompilasi untuk Android (bionic). Build static Linux/glibc tidak jalan.
3. Colok HP (USB debugging aktif) lalu Run, atau Build > Build APK(s) dan pasang APK-nya.

## Pemakaian
1. Buka app, isi ROBOT_TOKEN (sama persis dengan di firmware), GROQ_API_KEY dan/atau GEMINI_API_KEY, lalu tekan **Mulai**.
2. Tekan "Izinkan jalan di latar belakang" dan pilih Izinkan. Di Xiaomi/Oppo/Vivo/Samsung, kunci juga app di recent apps dan
   matikan pembatasan baterai/autostart khusus merk.
3. Nyalakan hotspot HP, sambungkan robot. Firmware `server=auto` memakai IP gateway = IP hotspot HP, port 8000, jadi firmware tidak perlu diubah.
4. Setelan lain (KELUARAN_SUARA, TTS_VOICE, KOTA, MUSIC_DIR, dll) isi di kotak "Setelan lain", format `KEY=NILAI` per baris.
   Data (ingatan, catatan, pengaturan) tersimpan di penyimpanan internal app, bukan `~/robot-server` Termux. Salin manual jika mau memindahkannya.

## Batasan jujur
- Belum pernah dikompilasi/diuji di sini (sandbox tidak bisa mengakses Android SDK). Kemungkinan perlu penyesuaian kecil saat sync/build pertama.
- `edge-tts` memakai aiohttp; kalau pip Chaquopy gagal memasangnya, ubah versi Chaquopy/Python di `app/build.gradle.kts`.
- Musik dari `/storage/emulated/0/Music` perlu izin audio dan ffmpeg.
- Saat hotspot aktif HP tidak punya internet lewat WiFi, jadi server memakai data seluler untuk memanggil AI. Pastikan kuota ada.

## Kalau server tidak mau jalan / aplikasi crash
- Alasan error sekarang tampil di bagian atas layar aplikasi ("Error: ...") dan di kotak Log.
- Kalau aplikasi sempat crash, jejaknya disimpan dan tampil merah ("Crash terakhir") saat aplikasi dibuka lagi. Kirim teks itu untuk diperiksa.
- Tombol Mulai tidak lagi menyebabkan crash walau Python gagal: error ditangkap dan ditampilkan.

## Pengaturan robot di dalam aplikasi
Tombol **Pengaturan robot** di layar utama membuka halaman `/pengaturan` milik server (semua setelan suara, tampilan, volume, EQ,
mode senyap, pengingat, ingatan, dll). Token terisi otomatis. Server harus sudah dalam keadaan "JALAN".
Perangkat lain di hotspot yang sama juga bisa membukanya lewat browser: `http://IP-HP:8000/pengaturan`.

## Layar utama (baru)
- **Status** di bagian atas (titik hijau = jalan), tombol **Mulai / Berhenti**. Menekan Mulai saat server sudah jalan = jalan ulang otomatis dengan setelan baru.
- **Pengaturan robot** membuka halaman setelan lengkap server.
- **Setelan server** dikelompokkan dan bisa dilipat: Akun & kunci API, Model & layanan AI, Suara, Musik, serta Setelan lain (KEY=NILAI).
  Kolom kosong = memakai bawaan server (nilai bawaan tampil sebagai petunjuk abu-abu).
- **Log server** punya tombol **Salin log** untuk dikirim ke pembuat kalau ada masalah.

## Musik tanpa ffmpeg
Kalau `libffmpeg.so` tidak ada, lagu dari memori HP didekode oleh MediaCodec bawaan Android (`PcmDecoder.kt`): mp3, m4a/aac, ogg, flac, wav.
Butuh izin audio (diminta saat aplikasi dibuka). Belum didukung tanpa ffmpeg: suara sintetis (hujan/ombak/kipas) dan filter EQ musik;
radio internet dicoba lewat MediaCodec tetapi tidak semua siaran bisa dibuka.

## Musik tanpa ffmpeg
Kalau `libffmpeg.so` tidak ada, musik dari HP, cuplikan lagu, dan durasi lagu memakai pendekode audio Android (`AudioDecoder.kt`):
mp3, m4a, ogg, flac, wav, aac, opus. Lagu dibaca dari folder `MUSIC_DIR` (bawaan `/storage/emulated/0/Music`) dan butuh izin audio.
Yang tetap butuh ffmpeg sungguhan: efek suara buatan (hujan, ombak, kipas) dan siaran radio yang tidak bisa dibuka Android.
Kalau lagu tidak mau jalan, buka "Log server" > Salin log: baris `[AUDIO] gagal ...` menunjukkan sebabnya.
