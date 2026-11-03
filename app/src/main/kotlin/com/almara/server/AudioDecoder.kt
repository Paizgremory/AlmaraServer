package com.almara.server

import android.media.AudioFormat
import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMetadataRetriever
import java.nio.ByteOrder

/**
 * Pengganti ffmpeg untuk memutar musik: mendekode file / URL audio apa pun yang dikenal Android
 * (mp3, m4a, ogg, flac, wav, aac, opus) menjadi PCM s16le 16 kHz mono. Dipanggil dari Python.
 * gain = pengali volume (sudah termasuk batas speaker).
 */
class AudioDecoder(src: String, seekSec: Double, private val gain: Float) {
    private val ex = MediaExtractor()
    private var codec: MediaCodec? = null
    private var rate = 44100
    private var ch = 2
    private var floatPcm = false
    private var inDone = false
    private var outDone = false
    private var closed = false
    private val info = MediaCodec.BufferInfo()
    private var last = 0f
    private var haveLast = false
    private var phase = 0.0

    init {
        try {
            ex.setDataSource(src)
            var idx = -1
            var fmt: MediaFormat? = null
            for (i in 0 until ex.trackCount) {
                val f = ex.getTrackFormat(i)
                if ((f.getString(MediaFormat.KEY_MIME) ?: "").startsWith("audio/")) {
                    idx = i
                    fmt = f
                    break
                }
            }
            if (idx < 0 || fmt == null) throw IllegalArgumentException("tidak ada jalur audio di file ini")
            ex.selectTrack(idx)
            if (fmt.containsKey(MediaFormat.KEY_SAMPLE_RATE)) rate = fmt.getInteger(MediaFormat.KEY_SAMPLE_RATE)
            if (fmt.containsKey(MediaFormat.KEY_CHANNEL_COUNT)) ch = fmt.getInteger(MediaFormat.KEY_CHANNEL_COUNT)
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

    /** Potongan PCM berikutnya, atau null kalau sudah habis / ditutup. */
    @Synchronized
    fun read(): ByteArray? {
        val c = codec ?: return null
        while (!closed) {
            if (!inDone) {
                val i = c.dequeueInputBuffer(0)
                if (i >= 0) {
                    val b = c.getInputBuffer(i)!!
                    val n = ex.readSampleData(b, 0)
                    if (n < 0) {
                        c.queueInputBuffer(i, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                        inDone = true
                    } else {
                        c.queueInputBuffer(i, 0, n, ex.sampleTime, 0)
                        ex.advance()
                    }
                }
            }
            if (outDone) return null
            val o = c.dequeueOutputBuffer(info, 10_000)
            if (o >= 0) {
                val out = convert(c, o)
                if ((info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) outDone = true
                c.releaseOutputBuffer(o, false)
                if (out != null && out.isNotEmpty()) return out
                if (outDone) return null
            } else if (o == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                val f = c.outputFormat
                rate = f.getInteger(MediaFormat.KEY_SAMPLE_RATE)
                ch = f.getInteger(MediaFormat.KEY_CHANNEL_COUNT)
                floatPcm = f.containsKey(MediaFormat.KEY_PCM_ENCODING) &&
                    f.getInteger(MediaFormat.KEY_PCM_ENCODING) == AudioFormat.ENCODING_PCM_FLOAT
            }
        }
        return null
    }

    private fun convert(c: MediaCodec, o: Int): ByteArray? {
        val buf = c.getOutputBuffer(o) ?: return null
        buf.position(info.offset)
        buf.limit(info.offset + info.size)
        val chn = if (ch < 1) 1 else ch
        val mono: FloatArray
        if (floatPcm) {
            val fb = buf.order(ByteOrder.nativeOrder()).asFloatBuffer()
            val frames = fb.remaining() / chn
            mono = FloatArray(frames)
            for (i in 0 until frames) {
                var s = 0f
                for (k in 0 until chn) s += fb.get()
                mono[i] = s / chn * 32767f
            }
        } else {
            val sb = buf.order(ByteOrder.nativeOrder()).asShortBuffer()
            val frames = sb.remaining() / chn
            mono = FloatArray(frames)
            for (i in 0 until frames) {
                var s = 0f
                for (k in 0 until chn) s += sb.get().toFloat()
                mono[i] = s / chn
            }
        }
        return resample(mono)
    }

    /** Turunkan laju ke 16 kHz (interpolasi linear, tersambung mulus antar potongan), terapkan gain. */
    private fun resample(m: FloatArray): ByteArray {
        if (m.isEmpty()) return ByteArray(0)
        val x: FloatArray
        if (haveLast) {
            x = FloatArray(m.size + 1)
            x[0] = last
            System.arraycopy(m, 0, x, 1, m.size)
        } else {
            x = m
        }
        val step = rate / 16000.0
        val ob = ByteArray((((x.size - phase) / step).toInt() + 2) * 2)
        var p = phase
        var k = 0
        while (p + 1 < x.size && k + 2 <= ob.size) {
            val i = p.toInt()
            val f = (p - i).toFloat()
            var v = (x[i] * (1 - f) + x[i + 1] * f) * gain
            if (v > 32767f) v = 32767f else if (v < -32768f) v = -32768f
            val s = v.toInt()
            ob[k++] = (s and 0xFF).toByte()
            ob[k++] = ((s shr 8) and 0xFF).toByte()
            p += step
        }
        phase = p - (x.size - 1)
        last = x[x.size - 1]
        haveLast = true
        return ob.copyOf(k)
    }

    @Synchronized
    fun close() {
        closed = true
        try { codec?.stop() } catch (_: Throwable) {}
        try { codec?.release() } catch (_: Throwable) {}
        codec = null
        try { ex.release() } catch (_: Throwable) {}
    }

    companion object {
        /** Lama lagu dalam detik (0 kalau tidak diketahui). */
        @JvmStatic
        fun durationSec(path: String): Double =
            try {
                val r = MediaMetadataRetriever()
                try {
                    r.setDataSource(path)
                    (r.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)?.toLongOrNull() ?: 0L) / 1000.0
                } finally {
                    r.release()
                }
            } catch (_: Throwable) {
                0.0
            }
    }
}
