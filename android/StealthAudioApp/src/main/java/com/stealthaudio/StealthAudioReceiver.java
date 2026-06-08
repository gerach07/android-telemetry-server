package com.stealthaudio;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

import java.io.File;
import java.io.FileWriter;
import java.util.Date;

public class StealthAudioReceiver extends BroadcastReceiver {
    private static final String TAG = "StealthAudio";

    private static void logError(String msg, Throwable e) {
        String fullMsg = new Date().toString() + " [StealthAudioReceiver] " + msg + (e != null ? ": " + e.toString() : "");
        Log.e(TAG, fullMsg, e);
        try {
            FileWriter writer = new FileWriter(new File("/data/local/tmp/audio_errors.txt"), true);
            writer.write(fullMsg + "\n");
            writer.flush();
            writer.close();
        } catch (Exception ignored) {}
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        try {
            if (intent == null) return;

            String action = intent.getStringExtra("action");
            if (action == null) {
                action = intent.getStringExtra("play");
            }

            if ("play".equals(action)) {
                float volume = 1.0f;
                String volumeString = intent.getStringExtra("volume");
                if (volumeString != null) {
                    try {
                        volume = Float.parseFloat(volumeString);
                    } catch (Exception e) {
                        logError("Invalid volume value: " + volumeString, e);
                    }
                }
                final float finalVolume = volume;
                new Thread(new Runnable() {
                    @Override
                    public void run() {
                        try {
                            StealthAudio.playSiren(finalVolume);
                        } catch (Exception e) {
                            logError("Siren playback failed", e);
                        }
                    }
                }).start();
            } else {
                logError("Unknown or null action received: " + action, null);
            }
        } catch (Exception e) {
            logError("Crash in onReceive", e);
        }
    }
}
