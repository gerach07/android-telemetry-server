package com.stealthaudio;

import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.content.Context;
import android.util.Log;

import java.io.File;
import java.io.FileWriter;
import java.util.Date;

public class StealthAudio {
    private static final String TAG = "StealthAudio";

    private static void logError(String msg) {
        Log.e(TAG, msg);
        try (FileWriter w = new FileWriter(new File("/data/local/tmp/audio_errors.txt"), true)) {
            w.write(new Date().toString() + " [StealthAudio] " + msg + "\n");
        } catch (Exception ignored) {}
    }
    private static Context getContext() {
        try {
            Class<?> activityThreadClass = Class.forName("android.app.ActivityThread");
            Object activityThread = activityThreadClass.getMethod("currentActivityThread").invoke(null);
            return (Context) activityThreadClass.getMethod("getApplication").invoke(activityThread);
        } catch (Exception e) {
            e.printStackTrace();
            return null;
        }
    }


    public static void playSound(Context context, int type, float volume, int loops) {
        Log.i(TAG, "playSound called: type=" + type + " volume=" + volume + " loops=" + loops + " context=" + (context != null ? context.getClass().getName() : "null"));
        if (type == 1) {
            playCellBroadcastAlarm(volume, loops);
        } else if (type == 2) {
            playRaw(context, "audio_1", volume, loops);
        } else if (type == 3) {
            playRaw(context, "audio_2", volume, loops);
        } else {
            logError("Unknown audio type: " + type);
        }
    }

    public static void playRaw(final Context context, String resName, final float volume, final int loops) {
        try {
            Log.i(TAG, "playRaw: resName=" + resName + " pkg=" + context.getPackageName());
            int resId = context.getResources().getIdentifier(resName, "raw", context.getPackageName());
            if (resId == 0) {
                logError("Resource not found: " + resName + " in package " + context.getPackageName());
                return;
            }
            Log.i(TAG, "playRaw: resolved resId=0x" + Integer.toHexString(resId));
            final android.media.MediaPlayer player = android.media.MediaPlayer.create(context, resId);
            if (player == null) {
                logError("MediaPlayer.create returned null for resId=0x" + Integer.toHexString(resId));
                return;
            }
            
            // Use STREAM_MUSIC — STREAM_ALARM triggers AudioHardening blocks on
            // MIUI/HyperOS and causes silent muting. STREAM_MUSIC plays fine.
            // No audio focus request — it's intercepted and silently dropped by
            // HardeningEnforcer on restricted packages, so we skip it entirely.
            Thread volumeThread = new Thread(new Runnable() {
                @Override
                public void run() {
                    AudioManager audioManager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
                    if (audioManager == null) return;
                    int maxVolume = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
                    while (player.isPlaying() && !Thread.currentThread().isInterrupted()) {
                        try {
                            audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, maxVolume, 0);
                            Thread.sleep(100);
                        } catch (Exception e) {
                            break;
                        }
                    }
                }
            });
            volumeThread.setDaemon(true);

            player.setAudioStreamType(AudioManager.STREAM_MUSIC);
            player.setVolume(volume, volume);
            
            volumeThread.start();

            if (loops <= 0) {
                player.setLooping(true);
                player.start();
                // Play infinitely until interrupted by service stop
                while (!Thread.currentThread().isInterrupted()) {
                    Thread.sleep(1000);
                }
            } else {
                player.setLooping(false);
                for (int i = 0; i < loops; i++) {
                    if (Thread.currentThread().isInterrupted()) break;
                    player.start();
                    // Wait for completion (using sleep to avoid blocking the interrupt check too rigidly, or use setOnCompletionListener but loop is simpler here with a small poll)
                    while (player.isPlaying() && !Thread.currentThread().isInterrupted()) {
                        Thread.sleep(100);
                    }
                    // Rewind for next loop
                    if (!Thread.currentThread().isInterrupted()) {
                        player.seekTo(0);
                    }
                }
            }
            
            volumeThread.interrupt();
            player.stop();
            player.release();
        } catch (Exception e) {
            logError("playRaw exception for " + resName + ": " + e.toString());
            e.printStackTrace();
        }
    }

    public static void playSiren(float volume, int loops) {
        playCellBroadcastAlarm(volume, loops);
    }

    public static void playCellBroadcastAlarm(float volume, int loops) {
        int sampleRate = 44100;
        int duration = 30; // seconds per cycle
        int numSamples = duration * sampleRate;

        // EAS dual-tone frequencies (inherited from U.S. Emergency Broadcast System)
        double freq1 = 853.0;  // Hz
        double freq2 = 960.0;  // Hz

        // 2-1-1 cadence pattern (in seconds): ON, OFF, ON, OFF, ON, OFF
        double[] cadence = { 2.0, 0.5, 1.0, 0.5, 1.0, 0.5 };
        double cycleDuration = 0;
        for (double d : cadence) cycleDuration += d;  // = 5.5 seconds per cycle

        double[] sample = new double[numSamples];
        byte[] generatedSnd = new byte[2 * numSamples];

        for (int i = 0; i < numSamples; i++) {
            double t = (double) i / sampleRate;

            // Position within the current cadence cycle
            double posInCycle = t % cycleDuration;

            // Walk the cadence steps to determine tone-on or silence
            boolean toneOn = false;
            double cursor = 0.0;
            for (int step = 0; step < cadence.length; step++) {
                if (posInCycle < cursor + cadence[step]) {
                    toneOn = (step % 2 == 0); // even steps = ON, odd steps = OFF
                    break;
                }
                cursor += cadence[step];
            }

            if (toneOn) {
                // Mix the two sine waves at equal amplitude, normalised to [-1, 1]
                double s1 = Math.sin(2 * Math.PI * freq1 * t);
                double s2 = Math.sin(2 * Math.PI * freq2 * t);
                sample[i] = (s1 + s2) / 2.0;
            } else {
                sample[i] = 0.0;
            }
        }

        // Convert to 16-bit PCM, little-endian
        int idx = 0;
        for (double dVal : sample) {
            short val = (short) (dVal * 32767 * volume);
            generatedSnd[idx++] = (byte) (val & 0x00ff);
            generatedSnd[idx++] = (byte) ((val & 0xff00) >>> 8);
        }

        // STREAM_MUSIC avoids HardeningEnforcer; no audio focus — it's silently
        // blocked for this package by the ROM and would just suppress audio.
        Thread volumeThread = new Thread(new Runnable() {
            @Override
            public void run() {
                Context context = getContext();
                if (context == null) return;

                AudioManager audioManager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
                if (audioManager == null) return;

                long end = System.currentTimeMillis() + ((long) duration * 1000L);
                int maxVolume = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC);

                while (System.currentTimeMillis() < end && !Thread.currentThread().isInterrupted()) {
                    try {
                        audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, maxVolume, 0);
                        Thread.sleep(100);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        break;
                    } catch (Exception e) {
                        // ignore SecurityException on some ROMs
                    }
                }
            }
        });
        volumeThread.setDaemon(true);
        volumeThread.start();

        AudioTrack audioTrack = new AudioTrack(AudioManager.STREAM_MUSIC,
                sampleRate, AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT, generatedSnd.length,
                AudioTrack.MODE_STATIC);

        audioTrack.setVolume(volume);
        audioTrack.write(generatedSnd, 0, generatedSnd.length);
        audioTrack.play();
        try {
            if (loops <= 0) {
                // Infinite loop: keep writing data until interrupted
                while (!Thread.currentThread().isInterrupted()) {
                    audioTrack.write(generatedSnd, 0, generatedSnd.length);
                }
            } else {
                // Loop a specific number of times
                for (int i = 0; i < loops; i++) {
                    if (Thread.currentThread().isInterrupted()) break;
                    if (i > 0) { // First loop was already written before play()
                         audioTrack.write(generatedSnd, 0, generatedSnd.length);
                    }
                    Thread.sleep((long) duration * 1000L);
                }
            }
        } catch (Exception e) {
            e.printStackTrace();
        } finally {
            // Interrupt the volume thread BEFORE releasing the track so it
            // doesn't call setStreamVolume after we've cleaned up.
            volumeThread.interrupt();
            // stop() must precede release() — releasing a playing track without
            // stopping first can trigger a native crash in libaudioflinger.
            try { audioTrack.stop(); } catch (Exception ignored) {}
            audioTrack.release();
        }
    }


}
