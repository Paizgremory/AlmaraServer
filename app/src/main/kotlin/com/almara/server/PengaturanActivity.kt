package com.almara.server

import android.annotation.SuppressLint
import android.app.Activity
import android.os.Bundle
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import org.json.JSONObject

/** Menampilkan halaman pengaturan robot milik server (/pengaturan) di dalam aplikasi, token terisi otomatis. */
class PengaturanActivity : Activity() {
    private lateinit var web: WebView
    private var injected = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(b: Bundle?) {
        super.onCreate(b)
        val token = Prefs.get(this, "ROBOT_TOKEN").trim()
        val port = Prefs.envMap(this)["PORT"]?.trim()?.toIntOrNull() ?: 8000
        val url = "http://127.0.0.1:$port/pengaturan"

        web = WebView(this)
        setContentView(web)
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false  // tombol "coba suara" butuh memutar audio
        }
        web.webViewClient = object : WebViewClient() {
            override fun onPageFinished(v: WebView, u: String?) {
                // Halaman membaca token dari localStorage saat dimuat: isi sekali, lalu muat ulang.
                if (!injected && u != null && u.startsWith("http://127.0.0.1")) {
                    injected = true
                    v.evaluateJavascript("localStorage.setItem('tok', ${JSONObject.quote(token)});location.reload();", null)
                }
            }

            override fun onReceivedError(v: WebView, r: WebResourceRequest, e: WebResourceError) {
                if (r.isForMainFrame) {
                    v.loadData(
                        "<meta name='viewport' content='width=device-width,initial-scale=1'>" +
                            "<body style='font-family:sans-serif;padding:24px'><h3>Server belum jalan</h3>" +
                            "<p>Kembali ke layar utama, tekan <b>Mulai</b>, tunggu sampai tertulis " +
                            "\"Server JALAN\", lalu buka Pengaturan lagi.</p></body>",
                        "text/html", "utf-8"
                    )
                }
            }
        }
        web.loadUrl(url)
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else super.onBackPressed()
    }
}
