package com.stealthaudio;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import java.io.File;
import java.io.FileWriter;
import java.util.Date;

/**
 * ForegroundService that plays audio blasts.
 *
 * Running as a foreground service is critical on Android 8+ with strict ROM
 * audio-hardening (e.g. MIUI, HyperOS).  A plain BroadcastReceiver loses its
 * process priority the moment onReceive() returns, and AudioHardening mutes &
 * kills the background process within 1-2 seconds.  A foreground service is
 * treated as a visible, user-interacting component and is exempt from that
 * restriction.
 */
public class StealthAudioService extends Service {
    private static final String TAG = "StealthAudio";
    private static final int NOTIF_ID = 9981;
    private static final String CHANNEL_ID = "sa_emergency";

    private Thread playbackThread;

    // ── Logging ──────────────────────────────────────────────────────────────

    private static void log(String msg) {
        Log.i(TAG, msg);
    }

    private static void logError(String msg, Throwable t) {
        String full = new Date() + " [StealthAudioService] " + msg + (t != null ? ": " + t : "");
        Log.e(TAG, full, t);
        try (FileWriter w = new FileWriter(new File("/data/local/tmp/audio_errors.txt"), true)) {
            w.write(full + "\n");
        } catch (Exception ignored) {}
    }

    // ── Service lifecycle ─────────────────────────────────────────────────────

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) {
            stopSelf();
            return START_NOT_STICKY;
        }

        String action = intent.getStringExtra("action");
        if ("play".equals(action)) {
            final int type   = intent.getIntExtra("type", 1);
            final float vol  = intent.getFloatExtra("volume", 1.0f);
            final int loops  = intent.getIntExtra("loops", 0);
            final String deviceId = intent.getStringExtra("device_id");
            startAudio(type, vol, loops, deviceId);
        } else {
            // "stop" or unknown — just die
            stopSelf();
        }

        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        log("StealthAudioService destroyed, interrupting playback thread.");
        if (playbackThread != null && playbackThread.isAlive()) {
            playbackThread.interrupt();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ── Core logic ────────────────────────────────────────────────────────────

    private void startAudio(int type, float volume, int loops, final String deviceId) {
        // Must call startForeground() quickly (within 5 s of startForegroundService)
        ensureNotificationChannel();
        Notification notif = buildNotification();
        startForeground(NOTIF_ID, notif);

        log("Starting foreground audio blast: type=" + type + " vol=" + volume + " loops=" + loops);

        // Kill any ongoing playback thread before starting a new one
        if (playbackThread != null && playbackThread.isAlive()) {
            playbackThread.interrupt();
        }

        final int finalType   = type;
        final float finalVol  = volume;
        final int finalLoops  = loops;

        playbackThread = new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    StealthAudio.playSound(getApplicationContext(), finalType, finalVol, finalLoops);
                    log("Audio blast finished: type=" + finalType);
                } catch (Exception e) {
                    logError("Playback exception type=" + finalType, e);
                } finally {
                    reportAudioDone(deviceId);
                    stopSelf();
                }
            }
        });
        playbackThread.setName("sa-playback-" + type);
        playbackThread.start();
    }

    private void reportAudioDone(String deviceId) {
        if (deviceId == null || deviceId.isEmpty()) return;
        try {
            java.net.URL url = new java.net.URL("http://127.0.0.1:8000/audio_done");
            java.net.HttpURLConnection conn = (java.net.HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setDoOutput(true);
            conn.setConnectTimeout(5000);
            conn.setReadTimeout(5000);
            String params = "device_id=" + java.net.URLEncoder.encode(deviceId, "UTF-8");
            conn.getOutputStream().write(params.getBytes("UTF-8"));
            int responseCode = conn.getResponseCode();
            log("Reported audio done to server, response=" + responseCode);
            conn.disconnect();
        } catch (Exception e) {
            logError("Failed to report audio done", e);
        }
    }

    // ── Notification helpers ──────────────────────────────────────────────────

    private void ensureNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL_ID,
                    "System Emergency Alert",
                    NotificationManager.IMPORTANCE_HIGH);
            ch.setDescription("Emergency system notifications");
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) nm.createNotificationChannel(ch);
        }
    }

    private Notification buildNotification() {
        // Use the oldest-compatible Notification.Builder (no compat lib needed —
        // minSdk=24 so Notification.Builder is always available).
        Notification.Builder builder = new Notification.Builder(this)
                .setSmallIcon(android.R.drawable.ic_dialog_alert)
                .setContentTitle("System Emergency Alert")
                .setContentText("Emergency notification in progress")
                .setOngoing(true);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            builder.setChannelId(CHANNEL_ID);
        }

        return builder.build();
    }
}
