package com.almara.server

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.drawable.Icon
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class ServerService : Service() {
    private var wake: PowerManager.WakeLock? = null
    private var wifi: WifiManager.WifiLock? = null

    override fun onBind(i: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopServer()
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }
        try {
            startForegroundCompat()
        } catch (t: Throwable) {
            Diag.report("Layanan latar belakang gagal dimulai", t)
            stopSelf()
            return START_NOT_STICKY
        }
        // Python.start() lambat saat pertama kali (mengekstrak library): jangan di thread utama.
        Thread({ startServer() }, "almara-start").start()
        return START_STICKY
    }

    private fun startForegroundCompat() {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(
                NotificationChannel(CH, "Almara Server", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, ServerService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE
        )
        val n = Notification.Builder(this, CH)
            .setContentTitle("Almara server aktif")
            .setContentText("Port 8000 — robot bisa tersambung lewat hotspot")
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(Icon.createWithResource(this, android.R.drawable.ic_delete), "Berhenti", stop).build())
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(1, n)
        }
    }

    @Synchronized
    private fun startServer() {
        try {
            if (wake == null) {
                val pm = getSystemService(POWER_SERVICE) as PowerManager
                wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "almara:server").apply { acquire() }
                try {
                    val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
                    wifi = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "almara:wifi").apply { acquire() }
                } catch (t: Throwable) {
                    Diag.report("WifiLock", t)  // tidak fatal
                }
            }
            if (!Python.isStarted()) Python.start(AndroidPlatform(this))
            val py = Python.getInstance()
            val err = py.getModule("almara_runner").callAttr(
                "start", filesDir.absolutePath, applicationInfo.nativeLibraryDir, Prefs.envJson(this)
            ).toString()
            Diag.lastError = if (err == "None") "" else err
        } catch (t: Throwable) {
            Diag.report("Server gagal dimulai", t)
        }
    }

    private fun stopServer() {
        try {
            if (Python.isStarted()) Python.getInstance().getModule("almara_runner").callAttr("stop")
        } catch (_: Exception) {
        }
        try { wake?.let { if (it.isHeld) it.release() } } catch (_: Throwable) {}
        try { wifi?.let { if (it.isHeld) it.release() } } catch (_: Throwable) {}
        wake = null; wifi = null
    }

    override fun onDestroy() {
        stopServer()
        super.onDestroy()
    }

    companion object {
        const val CH = "almara_server"
        const val ACTION_STOP = "com.almara.server.STOP"
    }
}
