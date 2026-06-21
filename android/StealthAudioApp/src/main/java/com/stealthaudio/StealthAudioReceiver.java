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
        String fullMsg = new Date() + " [StealthAudioReceiver] " + msg + (e != null ? ": " + e : "");
        Log.e(TAG, fullMsg, e);
        try (FileWriter writer = new FileWriter(new File("/data/local/tmp/audio_errors.txt"), true)) {
            writer.write(fullMsg + "\n");
        } catch (Exception ignored) {}
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        final PendingResult pendingResult = goAsync();
        try {
            if (intent == null) {
                pendingResult.finish();
                return;
            }

            String action = intent.getStringExtra("action");
            if (action == null) {
                logError("Missing 'action' extra in intent", null);
                pendingResult.finish();
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

                // Start the foreground service directly; this is now allowed because
                // the app is privileged and requests START_FOREGROUND_SERVICES_FROM_BACKGROUND.
                try {
                    appContext.startForegroundService(serviceIntent);
                    pendingResult.finish();
                } catch (Exception startException) {
                    logError("Service start failed, falling back to direct playback", startException);
                    final Context playbackContext = appContext;
                    final int playbackType = type;
                    final float playbackVolume = volume;
                    final int playbackLoops = loops;
                    new Thread(new Runnable() {
                        @Override
                        public void run() {
                            try {
                                StealthAudio.playSound(playbackContext, playbackType, playbackVolume, playbackLoops);
                            } catch (Exception playbackException) {
                                logError("Direct playback failed", playbackException);
                            } finally {
                                pendingResult.finish();
                            }
                        }
                    }, "sa-direct-playback").start();
                }

            } else if ("stop".equals(action)) {
                Log.i(TAG, "Stopping audio blast via ForegroundService");
                serviceIntent.putExtra("action", "stop");
                appContext.startService(serviceIntent);
                pendingResult.finish();

            } else {
                logError("Unknown action received: " + action, null);
                pendingResult.finish();
            }

        } catch (Exception e) {
            logError("Crash in onReceive", e);
            pendingResult.finish();
        }
    }
}
