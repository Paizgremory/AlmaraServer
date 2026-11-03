package com.almara.server

import android.content.Context
import org.json.JSONObject

object Prefs {
    private const val FILE = "almara"
    const val EXTRA = "EXTRA_ENV"

    /** choices: pasangan (nilai, tulisan di pilihan); null = kolom teks biasa. Kolom kosong = pakai bawaan server. */
    class Field(
        val key: String,
        val label: String,
        val hint: String = "",
        val secret: Boolean = false,
        val number: Boolean = false,
        val choices: List<Pair<String, String>>? = null,
    )

    class Group(val title: String, val note: String, val open: Boolean, val fields: List<Field>)

    val GROUPS = listOf(
        Group(
            "Akun & kunci API", "Isi minimal GROQ atau GEMINI. Beberapa key boleh dipisah koma.", true,
            listOf(
                Field("ROBOT_TOKEN", "ROBOT_TOKEN (wajib)", "sama persis dengan di firmware robot", secret = true),
                Field("GROQ_API_KEY", "Groq API key", "pendengaran (Whisper) & AI", secret = true),
                Field("GEMINI_API_KEY", "Gemini API key", "AI & suara Gemini", secret = true),
                Field("ELEVENLABS_API_KEY", "ElevenLabs API key", "opsional, suara paling natural", secret = true),
                Field("OPENROUTER_API_KEY", "OpenRouter API key", "opsional, AI cadangan", secret = true),
                Field("CEREBRAS_API_KEY", "Cerebras API key", "opsional, butuh CEREBRAS_MODELS di bawah", secret = true),
                Field("TELEGRAM_TOKEN", "Telegram bot token", "opsional, supaya bisa ngobrol lewat Telegram", secret = true),
                Field("TELEGRAM_CHAT_ID", "Telegram chat ID pemilik", "angka, opsional", number = true),
            ),
        ),
        Group(
            "Model & layanan AI", "Kosongkan untuk memakai bawaan server. Beberapa model dipisah koma (dicoba berurutan).", false,
            listOf(
                Field(
                    "AI_BALAPAN", "Mode balapan (berapa AI dikirimi sekaligus)", "",
                    choices = listOf("" to "Bawaan (2 AI)", "1" to "1 AI (hemat kuota)", "2" to "2 AI", "3" to "3 AI (tercepat)"),
                ),
                Field("LLM_MODELS", "Model Groq", "openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3.6-27b"),
                Field("GEMINI_MODELS", "Model Gemini", "gemini-3.8-flash,gemini-3.5-flash-lite"),
                Field("GEMINI_STT_MODELS", "Model Gemini untuk mendengar", "gemini-3.5-flash-lite,gemini-3.8-flash"),
                Field("GEMINI_SEARCH_MODEL", "Model Gemini untuk berita & pencarian", "gemini-2.5-flash"),
                Field("OPENROUTER_MODELS", "Model OpenRouter", "openrouter/free"),
                Field("CEREBRAS_MODELS", "Model Cerebras", "wajib diisi kalau memakai Cerebras"),
                Field("AI_LAIN_BASE", "AI lain: alamat dasar (format OpenAI)", "mis. https://api.example.com/v1"),
                Field("AI_LAIN_API_KEY", "AI lain: API key", "", secret = true),
                Field("AI_LAIN_MODELS", "AI lain: model", "nama model, pisah koma"),
            ),
        ),
        Group(
            "Suara", "Pilihan suara utama, tempo, nada, dan EQ ada di tombol Pengaturan robot.", false,
            listOf(
                Field("TTS_VOICE_EN", "Suara latihan bahasa Inggris", "en-US-AvaMultilingualNeural"),
                Field("GEMINI_TTS_MODEL", "Model suara Gemini", "gemini-3.1-flash-tts-preview"),
                Field("ELEVENLABS_MODEL", "Model suara ElevenLabs", "eleven_multilingual_v2"),
            ),
        ),
        Group(
            "Musik", "Lagu dari memori HP. Memutar musik butuh ffmpeg (lihat PANDUAN).", false,
            listOf(
                Field("MUSIC_DIR", "Folder musik", "/storage/emulated/0/Music"),
                Field("MAX_MUSIC_MINUTES", "Batas lama putar (menit)", "60", number = true),
            ),
        ),
    )

    val ALL_FIELDS: List<Field> get() = GROUPS.flatMap { it.fields }

    fun get(c: Context, k: String): String =
        c.getSharedPreferences(FILE, Context.MODE_PRIVATE).getString(k, "") ?: ""

    fun set(c: Context, k: String, v: String) {
        c.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit().putString(k, v).apply()
    }

    /** Gabungan semua setelan menjadi peta env untuk server.py. */
    fun envMap(c: Context): Map<String, String> {
        val m = LinkedHashMap<String, String>()
        for (f in ALL_FIELDS) get(c, f.key).trim().takeIf { it.isNotEmpty() }?.let { m[f.key] = it }
        get(c, EXTRA).lines().forEach { line ->
            val s = line.trim()
            if (s.isEmpty() || s.startsWith("#") || !s.contains("=")) return@forEach
            val k = s.substringBefore("=").trim().removePrefix("export ").trim()
            val v = s.substringAfter("=").trim().trim('"', '\'')
            if (k.isNotEmpty()) m[k] = v
        }
        if (!m.containsKey("MUSIC_DIR")) m["MUSIC_DIR"] = "/storage/emulated/0/Music"
        return m
    }

    /** Dikirim ke Python sebagai teks JSON (lebih aman daripada Map Java, yang tidak bisa langsung di-dict()). */
    fun envJson(c: Context): String = JSONObject(envMap(c) as Map<*, *>).toString()
}
