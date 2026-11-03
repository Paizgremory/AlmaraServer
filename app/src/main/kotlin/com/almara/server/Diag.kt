package com.almara.server

import android.content.Context
import android.util.Log
import java.io.File

/** Pencatat error: supaya penyebab crash / server gagal jalan kelihatan di layar aplikasi, bukan hilang begitu saja. */
object Diag {
    @Volatile var lastError: String = ""

    fun report(where: String, t: Throwable) {
        lastError = "$where: ${t.javaClass.simpleName}: ${t.message}"
        Log.e("Almara", where, t)
    }

    private fun crashFile(c: Context) = File(c.filesDir, "crash.txt")

    /** Kalau aplikasi mati karena exception, tulis jejaknya ke file lalu serahkan ke penanganan bawaan. */
    fun installCrashHandler(c: Context) {
        val app = c.applicationContext
        val old = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { th, e ->
            try {
                crashFile(app).writeText("Thread ${th.name}\n" + Log.getStackTraceString(e))
            } catch (_: Throwable) {
            }
            old?.uncaughtException(th, e)
        }
    }

    /** Crash terakhir (Kotlin/Java) atau crash native Python; kosong kalau tidak ada. */
    fun lastCrash(c: Context): String {
        val parts = mutableListOf<String>()
        try { crashFile(c).takeIf { it.exists() && it.length() > 0 }?.let { parts += it.readText().take(2500) } } catch (_: Throwable) {}
        try {
            File(c.filesDir, "native_crash.txt").takeIf { it.exists() && it.length() > 0 }
                ?.let { parts += "Crash native Python:\n" + it.readText().take(2500) }
        } catch (_: Throwable) {}
        return parts.joinToString("\n\n")
    }

    fun clearCrash(c: Context) {
        try { crashFile(c).delete() } catch (_: Throwable) {}
        try { File(c.filesDir, "native_crash.txt").delete() } catch (_: Throwable) {}
    }
}
