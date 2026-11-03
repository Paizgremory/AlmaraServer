package com.almara.server

import android.media.AudioFormat
import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMetadataRetriever
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Pengganti ffmpeg untuk memutar lagu: mendekode file/URL audio (mp3, m4a, aac, ogg, flac, wav, ...) lewat
 * MediaCodec bawaan Android, keluarannya PCM s16le 16 kHz mono (format yang dipakai server & robot).
 * Dipanggil dari Python: dec = PcmDecoder(path, detikMulai); while True: b = dec.read(); ...; dec.close()
 */
class PcmDecoder(path: String, seekSec: Double) {
    private val ex = MediaExtractor()
    private var codec: MediaCodec? = null
    private val info = MediaCodec.BufferInfo()
    private var inDone = false
    private var outDone = false
    private var closed = false
    private var srcRate = 44100
    private var channels = 2
    private var floatPcm = false
    private var pos = 0       // penurun laju: rata-rata sampel sumber di sela-selanya
    private var acc = 0L
    private var cnt = 0

    init {
        try {
            if (path.startsWith("http")) {
                ex.setDataSource(path, mapOf("User-Agent" to "VLC/3.0.20 LibVLC/3.0.20"))
            } else {
                ex.setDataSource(path)
            }
            var fmt: MediaFormat? = null
            for (i in 0 until ex.trackCount) {
                val f = ex.getTrackFormat(i)
                if ((f.getString(MediaFormat.KEY_MIME) ?: "").startsWith("audio/")) {
                    ex.selectTrack(i)
                    fmt = f
                    break
                }
            }
            if (fmt == null) throw IllegalStateException("tidak ada trek audio di file ini")
            srcRate = fmt.getInteger(MediaFormat.KEY_SAMPLE_RATE)
            channels = fmt.getInteger(MediaFormat.KEY_CHANNEL_COUNT)
            if (seekSec > 0) ex.seekTo((seekSec * 1_000_000).toLong(), MediaExtractor.SEEK_TO_CLOSEST_SYNC)
            val c = MediaCodec.createDecoderByType(fmt.getString(MediaFormat.KEY_MIME)!!)
            c.configure(fmt, null, null, 0)
            c.start()
            codec = c
        } catch (t: Throwable) {
            close()
            throw t
        }
    }

    private fun emit(mono: Int, out: ByteArrayOutputStream) {
        acc += mono
        cnt++
        pos += 16000
        if (pos >= srcRate) {
            val v = (acc / cnt).toInt().coerceIn(-32768, 32767)
            acc = 0
            cnt = 0
            while (pos >= srcRate) {
                out.write(v and 0xFF)
                out.write((v shr 8) and 0xFF)
                pos -= srcRate
            }
        }
    }

    private fun consume(ob: ByteBuffer, out: ByteArrayOutputStream) {
        ob.order(ByteOrder.nativeOrder())
        val ch = if (channels < 1) 1 else channels
        if (floatPcm) {
            val fb = ob.asFloatBuffer()
            while (fb.remaining() >= ch) {
                var s = 0f
                for (k in 0 until ch) s += fb.get()
                emit((s / ch * 32767f).toInt(), out)
            }
        } else {
            val sb = ob.asShortBuffer()
            while (sb.remaining() >= ch) {
                var s = 0
                for (k in 0 until ch) s += sb.get().toInt()
                emit(s / ch, out)
            }
        }
    }

    /** Potongan PCM berikutnya (sekitar 0,5 dtk). Kosong = belum ada data, coba lagi. null = selesai / ditutup. */
    @Synchronized
    fun read(): ByteArray? {
        val c = codec
        if (closed || c == null) return null
        val out = ByteArrayOutputStream()
        var guard = 0
        while (out.size() < 16000 && !outDone && guard++ < 100) {
            if (!inDone) {
                val ii = c.dequeueInputBuffer(2000)
                if (ii >= 0) {
                    val buf = c.getInputBuffer(ii)
                    val n = if (buf != null) ex.readSampleData(buf, 0) else -1
                    if (n < 0) {
                        c.queueInputBuffer(ii, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                        inDone = true
                    } else {
                        c.queueInputBuffer(ii, 0, n, ex.sampleTime, 0)
                        ex.advance()
                    }
                }
            }
            val oi = c.dequeueOutputBuffer(info, 2000)
            if (oi >= 0) {
                val ob = c.getOutputBuffer(oi)
                if (ob != null && info.size > 0) {
                    ob.position(info.offset)
                    ob.limit(info.offset + info.size)
                    consume(ob, out)
                }
                c.releaseOutputBuffer(oi, false)
                if ((info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) outDone = true
            } else if (oi == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                val f = c.outputFormat
                if (f.containsKey(MediaFormat.KEY_SAMPLE_RATE)) srcRate = f.getInteger(MediaFormat.KEY_SAMPLE_RATE)
                if (f.containsKey(MediaFormat.KEY_CHANNEL_COUNT)) channels = f.getInteger(MediaFormat.KEY_CHANNEL_COUNT)
                floatPcm = f.containsKey(MediaFormat.KEY_PCM_ENCODING) &&
                    f.getInteger(MediaFormat.KEY_PCM_ENCODING) == AudioFormat.ENCODING_PCM_FLOAT
            }
        }
        return if (out.size() == 0 && outDone) null else out.toByteArray()
    }

    @Synchronized
    fun close() {
        if (closed) return
        closed = true
        try { codec?.stop() } catch (_: Throwable) {}
        try { codec?.release() } catch (_: Throwable) {}
        try { ex.release() } catch (_: Throwable) {}
        codec = null
    }

    companion object {
        /** Lama lagu dalam detik (0 = tidak diketahui). Pengganti ffprobe. */
        @JvmStatic
        fun duration(path: String): Int =
            try {
                val r = MediaMetadataRetriever()
                try {
                    r.setDataSource(path)
                    ((r.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)?.toLongOrNull() ?: 0L) / 1000).toInt()
                } finally {
                    r.release()
                }
            } catch (_: Throwable) {
                0
            }
    }
}
