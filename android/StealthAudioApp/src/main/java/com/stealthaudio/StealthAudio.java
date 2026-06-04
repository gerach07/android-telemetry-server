package com.stealthaudio;

import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.content.Context;

public class StealthAudio {
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
    public static void main(String[] args) {
        try {
            if (args.length == 0) return;
            if (args[0].equals("play")) {
                float volume = 1.0f;
                if (args.length > 1) {
                    try {
                        volume = Float.parseFloat(args[1]);
                    } catch (Exception e) {
                        e.printStackTrace();
                    }
                }
                playSiren(volume);
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
    }

    public static void playSiren(float volume) {
        int sampleRate = 44100;
        int duration = 30; // seconds
        int numSamples = duration * sampleRate;
        double sample[] = new double[numSamples];

        double highFreq = 1400.0;
        double lowFreq = 800.0;
        double segmentSeconds = 0.8;
        int segmentSamples = (int) (segmentSeconds * sampleRate);

        byte generatedSnd[] = new byte[2 * numSamples];
        for (int i = 0; i < numSamples; ++i) {
            double time = (double) i / sampleRate;
            int segmentIndex = (i / segmentSamples) % 2;
            double currentFreq = segmentIndex == 0 ? highFreq : lowFreq;
            sample[i] = Math.sin(2 * Math.PI * currentFreq * time);
        }

        int idx = 0;
        for (final double dVal : sample) {
            final short val = (short) ((dVal * 32767));
            generatedSnd[idx++] = (byte) (val & 0x00ff);
            generatedSnd[idx++] = (byte) ((val & 0xff00) >>> 8);
        }

        // Force system volume to max continuously in a background thread
        Thread volumeThread = new Thread(new Runnable() {
            @Override
            public void run() {
                Context context = getContext();
                if (context == null) return;

                AudioManager audioManager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
                if (audioManager == null) return;

                // Request audio focus so other apps (like music players or calls) cannot duck or mute our sound
                audioManager.requestAudioFocus(null, AudioManager.STREAM_ALARM, AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE);

                long end = System.currentTimeMillis() + (duration * 1000);
                int maxVolume = audioManager.getStreamMaxVolume(AudioManager.STREAM_ALARM);

                while (System.currentTimeMillis() < end) {
                    try {
                        // Aggressively set the max volume to instantly counter any physical volume button presses
                        audioManager.setStreamVolume(AudioManager.STREAM_ALARM, maxVolume, 0);
                        // 50ms interval ensures the user has zero effective time to keep the sound quiet
                        Thread.sleep(50);
                    } catch (Exception e) {
                        // Ignore thread interrupts
                    }
                }
                audioManager.abandonAudioFocus(null);
            }
        });
        volumeThread.start();

        AudioTrack audioTrack = new AudioTrack(AudioManager.STREAM_ALARM,
                sampleRate, AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT, generatedSnd.length,
                AudioTrack.MODE_STATIC);

        audioTrack.setVolume(volume);
        audioTrack.write(generatedSnd, 0, generatedSnd.length);
        audioTrack.play();
        try {
            Thread.sleep(duration * 1000);
        } catch (Exception e) {
            e.printStackTrace();
        }
        audioTrack.release();
    }
}
