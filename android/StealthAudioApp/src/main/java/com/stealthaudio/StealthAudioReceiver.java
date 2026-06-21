package com.stealthaudio;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

import java.io.File;
import java.io.FileWriter;
import java.util.Date;

public class StealthAudioReceiver extends BroadcastReceiver {
    private static final String TAG = "StealthAudio";

    private static void logError(String msg, Throwable e) {
        String fullMsg = new Date() + " [StealthAudioReceiver] " + msg + (e != null ? ": " + e : "");
        Log.e(TAG, fullMsg, e);
        try (FileWriter writer = new FileWriter(new File("/data/local/tmp/audio_errors.txt"), true)) {
            writer.write(fullMsg + "\n");
        } catch (Exception ignored) {}
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        try {
            if (intent == null) return;

            String action = intent.getStringExtra("action");
            if (action == null) {
                logError("Missing 'action' extra in intent", null);
                return;
            }

            Context appContext = context.getApplicationContext();
            Intent serviceIntent = new Intent(appContext, StealthAudioService.class);

            if ("play".equals(action)) {
                float volume = 1.0f;
                int type = intent.getIntExtra("type", 1);
                int loops = intent.getIntExtra("loops", 0);
                String volumeString = intent.getStringExtra("volume");
                if (volumeString != null) {
                    try {
                        volume = Float.parseFloat(volumeString);
                    } catch (Exception e) {
                        logError("Invalid volume value: " + volumeString, e);
                    }
                }

                String deviceId = intent.getStringExtra("device_id");

                Log.i(TAG, "Starting audio blast via ForegroundService: type=" + type + " volume=" + volume + " loops=" + loops + " device=" + deviceId);

                serviceIntent.putExtra("action", "play");
                serviceIntent.putExtra("type", type);
                serviceIntent.putExtra("volume", volume);
                serviceIntent.putExtra("loops", loops);
                if (deviceId != null) serviceIntent.putExtra("device_id", deviceId);

                // startForegroundService() required on Android 8+ — the service MUST
                // call startForeground() within 5 seconds or the system throws ANR.
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    appContext.startForegroundService(serviceIntent);
                } else {
                    appContext.startService(serviceIntent);
                }

            } else if ("stop".equals(action)) {
                Log.i(TAG, "Stopping audio blast via ForegroundService");
                serviceIntent.putExtra("action", "stop");
                appContext.startService(serviceIntent);

            } else {
                logError("Unknown action received: " + action, null);
            }

        } catch (Exception e) {
            logError("Crash in onReceive", e);
        }
    }
}
