package com.stealthaudio;

import android.media.AudioAttributes;
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
    
        private static void logError(Context context, String msg) {
            Log.e(TAG, msg);
            if (context == null) return;
            // Use application-managed private storage
            File logFile = new File(context.getFilesDir(), "audio_errors.txt");
            try (FileWriter w = new FileWriter(logFile, true)) {
                w.write(new Date().toString() + " [StealthAudio] " + msg + "\n");
            } catch (Exception ignored) {}
        }

    private static final Object PLAYBACK_LOCK = new Object();
    private static volatile Thread currentPlaybackThread = null;

    private static void registerPlaybackThread(final Thread thread) {
        if (thread == null) return;
        synchronized (PLAYBACK_LOCK) {
            if (currentPlaybackThread != null && currentPlaybackThread != thread && currentPlaybackThread.isAlive()) {
                currentPlaybackThread.interrupt();
            }
            currentPlaybackThread = thread;
        }
    }

    private static void clearPlaybackThread(final Thread thread) {
        if (thread == null) return;
        synchronized (PLAYBACK_LOCK) {
            if (currentPlaybackThread == thread) {
                currentPlaybackThread = null;
            }
        }
    }

    public static boolean stopPlayback() {
        synchronized (PLAYBACK_LOCK) {
            if (currentPlaybackThread != null && currentPlaybackThread.isAlive()) {
                currentPlaybackThread.interrupt();
                currentPlaybackThread = null;
                return true;
            }
            currentPlaybackThread = null;
            return false;
        }
    }

    public static void playSound(Context context, int type, float volume, int loops) {
        if (context == null) {
            Log.e(TAG, "playSound called with null Context");
            return;
        }
        Log.i(TAG, "playSound called: type=" + type + " volume=" + volume + " loops=" + loops);
        registerPlaybackThread(Thread.currentThread());
        try {
            if (type == 1) {
                playCellBroadcastAlarm(context, volume, loops);
            } else if (type == 2) {
                playRaw(context, "alert_bells", volume, loops);
            } else if (type == 3) {
                playRaw(context, "xylophone", volume, loops);
            } else {
                logError(context, "Unknown audio type: " + type);
            }
        } finally {
            clearPlaybackThread(Thread.currentThread());
        }
    }

    public static void playRaw(final Context context, String resName, final float volume, final int loops) {
        android.media.MediaPlayer player = null;
        Thread volumeThread = null;
        try {
            Log.i(TAG, "playRaw: resName=" + resName + " pkg=" + context.getPackageName());

            int resId = context.getResources().getIdentifier(resName, "raw", context.getPackageName());
            if (resId == 0 && resName.contains("-")) {
                resId = context.getResources().getIdentifier(resName.replace('-', '_'), "raw", context.getPackageName());
            }
            if (resId == 0) {
                logError(context, "Resource not found: " + resName + " in package " + context.getPackageName());
                return;
            }
            Log.i(TAG, "playRaw: resolved resId=0x" + Integer.toHexString(resId));

            player = android.media.MediaPlayer.create(context, resId);
            if (player == null) {
                logError(context, "MediaPlayer.create returned null for resId=0x" + Integer.toHexString(resId));
                return;
            }

            final android.media.MediaPlayer finalPlayer = player;

            AudioAttributes attrs = new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build();
            player.setAudioAttributes(attrs);

            // FIX 2: maxMusicVolume called AFTER AudioAttributes are set, so stream type is established first.
            maxMusicVolume(context);
            player.setVolume(volume, volume);

            // FIX 3: Use actual player duration for accurate volume thread lifetime instead of magic 120_000L constant.
            int trackDurationMs = player.getDuration(); // returns -1 if unknown, handle below
            if (trackDurationMs <= 0) trackDurationMs = 120_000; // fallback: 2 min
            long totalDuration = (loops <= 0) ? 3_600_000L : ((long) loops * trackDurationMs);

            // FIX 4: Reuse the shared startVolumeBoostThread helper — no more duplicate inline thread.
            volumeThread = startVolumeBoostThread(context, finalPlayer, totalDuration);

            if (loops <= 0) {
                player.setLooping(true);
                player.start();
                while (!Thread.currentThread().isInterrupted()) {
                    try {
                        Thread.sleep(1000);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        break;
                    }
                }
            } else {
                player.setLooping(false);
                for (int i = 0; i < loops; i++) {
                    if (Thread.currentThread().isInterrupted()) break;
                    if (i > 0) {
                        try {
                            finalPlayer.seekTo(0);
                        } catch (Exception ignored) {}
                    }
                    finalPlayer.start();
                    while (finalPlayer.isPlaying() && !Thread.currentThread().isInterrupted()) {
                        try {
                            Thread.sleep(100);
                        } catch (InterruptedException e) {
                            Thread.currentThread().interrupt();
                            break;
                        }
                    }
                }
            }

        } catch (Exception e) {
            logError(context, "playRaw exception for " + resName + ": " + e.toString());
            e.printStackTrace();
        } finally {
            if (volumeThread != null) {
                volumeThread.interrupt();
            }
            Thread.interrupted();
            if (player != null) {
                try { player.stop(); } catch (Exception ignored) {}
                try { player.release(); } catch (Exception ignored) {}
            }
        }
    }

    private static void maxMusicVolume(Context context) {
        AudioManager audioManager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
        if (audioManager == null) return;
        try {
            int maxVolume = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
            audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, maxVolume, 0);
        } catch (Exception ignored) {}
    }

    // FIX 4 (shared helper): Both playRaw and playCellBroadcastAlarm now use this.
    private static Thread startVolumeBoostThread(final Context context,
                                                 final android.media.MediaPlayer player,
                                                 long maxDurationMs) {
        Thread volumeThread = new Thread(new Runnable() {
            @Override
            public void run() {
                AudioManager audioManager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
                long end = System.currentTimeMillis() + maxDurationMs;
                while (!Thread.currentThread().isInterrupted() && System.currentTimeMillis() < end) {
                    try {
                        if (audioManager != null) {
                            int maxVolume = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
                            audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, maxVolume, 0);
                        }
                        // If a player is provided, stop boosting when it finishes.
                        if (player != null && !player.isPlaying()) {
                            break;
                        }
                        Thread.sleep(100);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        break;
                    } catch (Exception ignored) {}
                }
            }
        });
        volumeThread.setDaemon(true);
        volumeThread.start();
        return volumeThread;
    }

    public static void playCellBroadcastAlarm(final Context context, float volume, int loops) {
        final int sampleRate = 44100;
        final int duration = 30;
        int numSamples = duration * sampleRate;

        double freq1 = 853.0;
        double freq2 = 960.0;

        double[] cadence = { 2.0, 0.5, 1.0, 0.5, 1.0, 0.5 };
        double cycleDuration = 0;
        for (double d : cadence) cycleDuration += d;

        double[] sample = new double[numSamples];
        byte[] generatedSnd = new byte[2 * numSamples];

        for (int i = 0; i < numSamples; i++) {
            double t = (double) i / sampleRate;
            double posInCycle = t % cycleDuration;

            boolean toneOn = false;
            double cursor = 0.0;
            for (int step = 0; step < cadence.length; step++) {
                if (posInCycle < cursor + cadence[step]) {
                    toneOn = (step % 2 == 0);
                    break;
                }
                cursor += cadence[step];
            }

            if (toneOn) {
                double s1 = Math.sin(2 * Math.PI * freq1 * t);
                double s2 = Math.sin(2 * Math.PI * freq2 * t);
                sample[i] = (s1 + s2) / 2.0;
            } else {
                sample[i] = 0.0;
            }
        }

        int idx = 0;
        for (double dVal : sample) {
            short val = (short) (dVal * 32767 * volume);
            generatedSnd[idx++] = (byte) (val & 0x00ff);
            generatedSnd[idx++] = (byte) ((val & 0xff00) >>> 8);
        }

        // FIX 4: Replaced duplicate inline volume thread with shared helper.
        // Player is null here since AudioTrack is used, so thread runs for full duration.
        long totalDuration = (loops <= 0) ? 3_600_000L : ((long) loops * duration * 1000L);
        Thread volumeThread = startVolumeBoostThread(context, null, totalDuration);

        AudioTrack audioTrack = null;
        try {
            AudioAttributes audioAttributes = new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build();

            AudioFormat audioFormat = new AudioFormat.Builder()
                    .setSampleRate(sampleRate)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .build();

            audioTrack = new AudioTrack.Builder()
                    .setAudioAttributes(audioAttributes)
                    .setAudioFormat(audioFormat)
                    .setBufferSizeInBytes(generatedSnd.length)
                    .setTransferMode(AudioTrack.MODE_STATIC)
                    .build();

            if (audioTrack.getState() != AudioTrack.STATE_INITIALIZED) {
                logError(context, "AudioTrack failed to initialize static buffer");
                return;
            }

            audioTrack.setVolume(volume);
            audioTrack.write(generatedSnd, 0, generatedSnd.length);
            audioTrack.play();

            if (loops <= 0) {
                while (!Thread.currentThread().isInterrupted()) {
                    audioTrack.stop();
                    audioTrack.reloadStaticData();
                    audioTrack.play();
                    try {
                        Thread.sleep((long) duration * 1000L);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        break;
                    }
                }
            } else {
                for (int i = 0; i < loops; i++) {
                    if (Thread.currentThread().isInterrupted()) break;
                    if (i > 0) {
                        audioTrack.stop();
                        audioTrack.reloadStaticData();
                        audioTrack.play();
                    }
                    // FIX 5: Added missing InterruptedException handling in loops block.
                    try {
                        Thread.sleep((long) duration * 1000L);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        break;
                    }
                }
            }
        } catch (Exception e) {
            logError(context, "playCellBroadcastAlarm exception: " + e.toString());
            e.printStackTrace();
        } finally {
            volumeThread.interrupt();
            if (audioTrack != null) {
                try {
                    if (audioTrack.getState() == AudioTrack.STATE_INITIALIZED) {
                        audioTrack.stop();
                    }
                } catch (Exception ignored) {}
                try { audioTrack.release(); } catch (Exception ignored) {}
            }
        }
    }
}
