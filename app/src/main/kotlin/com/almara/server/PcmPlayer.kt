package com.almara.server

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack

/** Pengganti mpv: memutar PCM s16le 16 kHz mono lewat AudioTrack. Dipanggil dari Python. */
object PcmPlayer {
    private var track: AudioTrack? = null
    private val lock = Any()

    @JvmStatic
    fun start() = synchronized(lock) {
        releaseLocked()
        val rate = 16000
        val min = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val t = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()
            )
            .setAudioFormat(
                AudioFormat.Builder().setSampleRate(rate)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build()
            )
            .setBufferSizeInBytes(maxOf(min * 4, 32000))
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        t.play()
        track = t
    }

    @JvmStatic
    fun write(data: ByteArray) {
        val t = synchronized(lock) { track } ?: return
        var off = 0
        while (off < data.size) {
            val n = t.write(data, off, data.size - off) // blocking
            if (n <= 0) break
            off += n
        }
    }

    /** Selesai normal: biarkan sisa buffer habis diputar, lalu lepas. */
    @JvmStatic
    fun finish() {
        val t = synchronized(lock) { track } ?: return
        Thread {
            try {
                t.stop() // MODE_STREAM: stop() memutar sisa data lalu berhenti
                Thread.sleep(300)
            } catch (_: Exception) {
            }
            synchronized(lock) { if (track === t) track = null }
            try { t.release() } catch (_: Exception) {}
        }.start()
    }

    @JvmStatic
    fun stop() = synchronized(lock) { releaseLocked() }

    private fun releaseLocked() {
        track?.let {
            try { it.pause(); it.flush(); it.release() } catch (_: Exception) {}
        }
        track = null
    }
}
