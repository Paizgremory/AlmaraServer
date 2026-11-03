package com.almara.server

import android.Manifest
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import com.chaquo.python.Python
import java.net.NetworkInterface

class MainActivity : Activity() {
    private val inputs = HashMap<String, View>()   // EditText atau Spinner, menurut kunci setelan
    private lateinit var extra: EditText
    private lateinit var statusDot: TextView
    private lateinit var statusText: TextView
    private lateinit var errView: TextView
    private lateinit var ipView: TextView
    private lateinit var crashView: TextView
    private lateinit var logView: TextView
    private val h = Handler(Looper.getMainLooper())

    private val BG = 0xFFF3F5F9.toInt()
    private val CARD = Color.WHITE
    private val TXT = 0xFF1B1F27.toInt()
    private val DIM = 0xFF6B7280.toInt()
    private val ACC = 0xFF2F6BFF.toInt()
    private val OK = 0xFF1E9E5A.toInt()
    private val BAD = 0xFFC62828.toInt()
    private val LINE = 0xFFE3E7EE.toInt()
    private val MATCH = ViewGroup.LayoutParams.MATCH_PARENT
    private val WRAP = ViewGroup.LayoutParams.WRAP_CONTENT

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    // ---------- bahan tampilan ----------
    private fun shape(color: Int, radiusDp: Int, strokeColor: Int = 0): GradientDrawable =
        GradientDrawable().apply {
            setColor(color)
            cornerRadius = dp(radiusDp).toFloat()
            if (strokeColor != 0) setStroke(dp(1), strokeColor)
        }

    private fun tv(t: String, size: Float, color: Int, bold: Boolean = false): TextView =
        TextView(this).apply {
            text = t
            textSize = size
            setTextColor(color)
            if (bold) typeface = Typeface.DEFAULT_BOLD
        }

    private fun lp(w: Int, hgt: Int, top: Int = 0): LinearLayout.LayoutParams =
        LinearLayout.LayoutParams(w, hgt).apply { topMargin = dp(top) }

    private fun btn(label: String, filled: Boolean, onClick: () -> Unit): Button =
        Button(this).apply {
            text = label
            isAllCaps = false
            textSize = 15f
            setTextColor(if (filled) Color.WHITE else ACC)
            background = shape(if (filled) ACC else Color.TRANSPARENT, 12, ACC)
            stateListAnimator = null
            minHeight = dp(48)
            minimumHeight = dp(48)
            setOnClickListener { onClick() }
        }

    private fun card(): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = shape(CARD, 14, LINE)
            setPadding(dp(16), dp(4), dp(16), dp(4))
        }

    /** Kartu yang bisa dilipat: ketuk judulnya untuk membuka / menutup isinya. */
    private fun collapsible(title: String, open: Boolean, body: LinearLayout): LinearLayout {
        val c = card()
        body.visibility = if (open) View.VISIBLE else View.GONE
        body.setPadding(0, 0, 0, dp(14))
        val head = tv((if (open) "▾  " else "▸  ") + title, 16f, TXT, true).apply {
            setPadding(0, dp(14), 0, dp(14))
        }
        head.setOnClickListener {
            val show = body.visibility != View.VISIBLE
            body.visibility = if (show) View.VISIBLE else View.GONE
            head.text = (if (show) "▾  " else "▸  ") + title
        }
        c.addView(head)
        c.addView(body)
        return c
    }

    private fun fieldView(f: Prefs.Field): View {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(12), 0, 0)
        }
        val head = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        head.addView(tv(f.label, 13f, TXT, true), LinearLayout.LayoutParams(0, WRAP, 1f))
        box.addView(head)
        val saved = Prefs.get(this, f.key)
        val choices = f.choices
        if (choices != null) {
            val sp = Spinner(this)
            sp.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, choices.map { it.second })
            sp.setSelection(choices.indexOfFirst { it.first == saved }.coerceAtLeast(0))
            inputs[f.key] = sp
            box.addView(sp, lp(MATCH, WRAP, 4))
            return box
        }
        val e = EditText(this).apply {
            setText(saved)
            hint = f.hint
            textSize = 14f
            setTextColor(TXT)
            setHintTextColor(0xFF9AA3B2.toInt())
            background = shape(0xFFF7F8FA.toInt(), 10, LINE)
            setPadding(dp(12), dp(10), dp(12), dp(10))
            inputType = when {
                f.number -> InputType.TYPE_CLASS_NUMBER
                f.secret -> InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
                else -> InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            }
            setSingleLine()
        }
        inputs[f.key] = e
        if (f.secret) {
            var visible = false
            val toggle = tv("Tampilkan", 12f, ACC).apply { setPadding(dp(8), 0, 0, 0) }
            toggle.setOnClickListener {
                visible = !visible
                e.inputType = InputType.TYPE_CLASS_TEXT or
                    (if (visible) InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD else InputType.TYPE_TEXT_VARIATION_PASSWORD)
                e.setSelection(e.text.length)
                toggle.text = if (visible) "Sembunyikan" else "Tampilkan"
            }
            head.addView(toggle)
        }
        box.addView(e, lp(MATCH, WRAP, 4))
        return box
    }

    // ---------- layar ----------
    override fun onCreate(b: Bundle?) {
        super.onCreate(b)
        Diag.installCrashHandler(this)
        window.decorView.setBackgroundColor(BG)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(20), dp(16), dp(28))
        }
        setContentView(ScrollView(this).apply {
            setBackgroundColor(BG)
            addView(root)
        })

        root.addView(tv("Almara Server", 26f, TXT, true))
        root.addView(tv("Server robot Almara yang jalan di HP ini", 13f, DIM))

        // Kartu status + tombol utama
        val st = card().apply { setPadding(dp(16), dp(14), dp(16), dp(16)) }
        val stRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        statusDot = tv("●", 18f, DIM)
        statusText = tv("Memeriksa...", 16f, TXT, true).apply { setPadding(dp(8), 0, 0, 0) }
        stRow.addView(statusDot)
        stRow.addView(statusText)
        st.addView(stRow)
        errView = tv("", 13f, BAD).apply { visibility = View.GONE; setPadding(0, dp(6), 0, 0) }
        st.addView(errView)
        ipView = tv("", 12f, DIM).apply { setPadding(0, dp(8), 0, 0) }
        st.addView(ipView)
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        row.addView(btn("Mulai", true) { saveAll(); startSvc() }, LinearLayout.LayoutParams(0, WRAP, 1f))
        row.addView(
            btn("Berhenti", false) {
                startService(Intent(this, ServerService::class.java).setAction(ServerService.ACTION_STOP))
            },
            LinearLayout.LayoutParams(0, WRAP, 1f).apply { leftMargin = dp(10) }
        )
        st.addView(row, lp(MATCH, WRAP, 12))
        root.addView(st, lp(MATCH, WRAP, 16))

        crashView = tv("", 11f, BAD).apply {
            typeface = Typeface.MONOSPACE
            visibility = View.GONE
            setPadding(0, dp(10), 0, 0)
        }
        root.addView(crashView)

        root.addView(
            btn("Pengaturan robot  ›", true) {
                saveAll()
                startActivity(Intent(this, PengaturanActivity::class.java))
            },
            lp(MATCH, WRAP, 14)
        )
        root.addView(tv("Suara, tampilan, volume, EQ, mode senyap, pengingat, ingatan, dll.", 12f, DIM).apply {
            setPadding(dp(4), dp(4), 0, 0)
        })

        // Setelan yang dibaca server saat menyala (dikelompokkan, bisa dilipat)
        root.addView(tv("Setelan server", 13f, DIM, true), lp(WRAP, WRAP, 22))
        for (g in Prefs.GROUPS) {
            val body = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
            if (g.note.isNotEmpty()) body.addView(tv(g.note, 12f, DIM))
            for (f in g.fields) body.addView(fieldView(f))
            root.addView(collapsible(g.title, g.open, body), lp(MATCH, WRAP, 10))
        }

        val exBody = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        exBody.addView(tv("Satu per baris, format KEY=NILAI. Untuk setelan yang belum punya kolom, mis. KELUARAN_SUARA, SENYAP, SPEAKER_LIMIT.", 12f, DIM))
        extra = EditText(this).apply {
            setText(Prefs.get(this@MainActivity, Prefs.EXTRA))
            minLines = 4
            gravity = Gravity.TOP
            textSize = 13f
            typeface = Typeface.MONOSPACE
            setTextColor(TXT)
            background = shape(0xFFF7F8FA.toInt(), 10, LINE)
            setPadding(dp(12), dp(10), dp(12), dp(10))
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        }
        exBody.addView(extra, lp(MATCH, WRAP, 8))
        root.addView(collapsible("Setelan lain (lanjutan)", false, exBody), lp(MATCH, WRAP, 10))

        root.addView(tv("Perubahan setelan server berlaku setelah menekan Mulai (jalan ulang otomatis kalau server sedang menyala).", 12f, DIM).apply {
            setPadding(dp(4), dp(10), dp(4), 0)
        })

        root.addView(
            btn("Izinkan jalan di latar belakang", false) { askBattery() },
            lp(MATCH, WRAP, 14)
        )

        // Log
        val logBody = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        logBody.addView(btn("Salin log", false) { copyLog() }, lp(MATCH, WRAP, 4))
        logView = tv("", 11f, TXT).apply {
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
            setPadding(0, dp(10), 0, 0)
        }
        logBody.addView(logView)
        root.addView(collapsible("Log server", false, logBody), lp(MATCH, WRAP, 14))

        val perms = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= 33) {
            perms += Manifest.permission.POST_NOTIFICATIONS
            perms += Manifest.permission.READ_MEDIA_AUDIO
        } else perms += Manifest.permission.READ_EXTERNAL_STORAGE
        requestPermissions(perms.toTypedArray(), 1)
    }

    // ---------- simpan & jalankan ----------
    private fun readValue(f: Prefs.Field): String {
        val v = inputs[f.key]
        return when (v) {
            is EditText -> v.text.toString().trim()
            is Spinner -> f.choices?.getOrNull(v.selectedItemPosition)?.first ?: ""
            else -> ""
        }
    }

    private fun saveAll() {
        for (f in Prefs.ALL_FIELDS) Prefs.set(this, f.key, readValue(f))
        Prefs.set(this, Prefs.EXTRA, extra.text.toString())
    }

    private fun isRunning(): Boolean =
        try {
            Python.isStarted() && Python.getInstance().getModule("almara_runner").callAttr("is_running").toBoolean()
        } catch (_: Throwable) {
            false
        }

    private fun startSvc() {
        if (Prefs.get(this, "ROBOT_TOKEN").isBlank()) {
            Toast.makeText(this, "Isi ROBOT_TOKEN dulu", Toast.LENGTH_LONG).show()
            return
        }
        Diag.clearCrash(this)
        Diag.lastError = ""
        if (isRunning()) {  // sedang jalan: hentikan dulu supaya setelan baru terbaca
            Toast.makeText(this, "Menjalankan ulang dengan setelan baru...", Toast.LENGTH_SHORT).show()
            startService(Intent(this, ServerService::class.java).setAction(ServerService.ACTION_STOP))
            h.postDelayed({ launchSvc() }, 1800)
        } else {
            launchSvc()
        }
    }

    private fun launchSvc() {
        try {
            startForegroundService(Intent(this, ServerService::class.java))
        } catch (t: Throwable) {
            Diag.report("Tidak bisa memulai layanan", t)
            Toast.makeText(this, "Gagal memulai: ${t.message}", Toast.LENGTH_LONG).show()
        }
    }

    private fun askBattery() {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        if (!pm.isIgnoringBatteryOptimizations(packageName)) {
            startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))
            )
        } else Toast.makeText(this, "Sudah diizinkan", Toast.LENGTH_SHORT).show()
    }

    private fun copyLog() {
        val text = listOf(statusText.text.toString(), errView.text.toString(), crashView.text.toString(), logView.text.toString())
            .filter { it.isNotBlank() }.joinToString("\n\n")
        val cm = getSystemService(CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText("Log Almara", text))
        Toast.makeText(this, "Log disalin", Toast.LENGTH_SHORT).show()
    }

    private fun ips(): String =
        try {
            NetworkInterface.getNetworkInterfaces().toList().flatMap { ni ->
                ni.inetAddresses.toList()
                    .filter { !it.isLoopbackAddress && it.hostAddress?.contains(":") == false }
                    .map { "${ni.name}: ${it.hostAddress}" }
            }.joinToString("\n")
        } catch (_: Exception) { "?" }

    private val tick = object : Runnable {
        override fun run() {
            var running = false
            var log = ""
            var err = Diag.lastError
            if (Python.isStarted()) {
                try {
                    val m = Python.getInstance().getModule("almara_runner")
                    running = m.callAttr("is_running").toBoolean()
                    log = m.callAttr("get_log").toString()
                    m.callAttr("get_error").toString().takeIf { it.isNotEmpty() && it != "None" }?.let { err = it }
                } catch (_: Exception) {}
            }
            statusDot.setTextColor(if (running) OK else if (err.isNotEmpty()) BAD else DIM)
            statusText.text = if (running) "Server jalan (port 8000)" else "Server mati"
            errView.text = if (err.isNotEmpty()) "Error: $err" else ""
            errView.visibility = if (err.isNotEmpty()) View.VISIBLE else View.GONE
            ipView.text = "IP HP (robot memakai IP hotspot, biasanya 192.168.x.1):\n" + ips()
            logView.text = log
            val crash = Diag.lastCrash(this@MainActivity)
            crashView.text = if (crash.isEmpty()) "" else "Crash terakhir (kirim teks ini ke pembuat):\n$crash"
            crashView.visibility = if (crash.isEmpty()) View.GONE else View.VISIBLE
            h.postDelayed(this, 2000)
        }
    }

    override fun onResume() { super.onResume(); h.post(tick) }
    override fun onPause() { super.onPause(); h.removeCallbacks(tick); saveAll() }
}
