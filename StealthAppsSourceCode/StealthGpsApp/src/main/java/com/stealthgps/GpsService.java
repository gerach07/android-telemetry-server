package com.stealthgps;

import android.annotation.SuppressLint;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.hardware.TriggerEvent;
import android.hardware.TriggerEventListener;
import android.hardware.Sensor;
import android.hardware.SensorManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Build;
import android.os.Bundle;
import android.os.IBinder;
import android.os.SystemClock;
import android.util.Log;

import java.io.File;
import java.io.FileWriter;
import java.io.BufferedWriter;
import java.util.Date;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;

public class GpsService extends Service {
    private static final String TAG = "StealthGps-Service";
    private static final int NOTIF_ID = 9982;
    private static final String CHANNEL_ID = "sg_location";

    private LocationManager locationManager;
    private SensorManager sensorManager;
    private Sensor sigMotionSensor;
    private TriggerEventListener triggerEventListener;
    
    private ExecutorService ioExecutor;

    private File coordsFile;
    private File tempCoordsFile;
    private File errorFile;
    
    private final AtomicBoolean isLowPowerMode = new AtomicBoolean(false);
    private final AtomicLong lastMotionTime = new AtomicLong(SystemClock.elapsedRealtime());
    private final AtomicReference<Location> lastValidLocation = new AtomicReference<>(null);
    
    private final AtomicLong lastWriteTimeMs = new AtomicLong(0L);
    private final AtomicReference<String> lastWrittenCoords = new AtomicReference<>("");

    private static final long MOTION_TIMEOUT_MS = 60000; 
    private static final long COOLDOWN_INTERVAL_MS = 300000; 
    private static final long ACTIVE_INTERVAL_MS = 5000L; 
    private static final float TELEPORT_DISTANCE_THRESHOLD = 500.0f;
    private static final float ACCURACY_THRESHOLD = 100.0f; 
    private static final long MIN_WRITE_INTERVAL_MS = 4000L;

    @Override
    public void onCreate() {
        super.onCreate();
        locationManager = (LocationManager) getSystemService(Context.LOCATION_SERVICE);
        sensorManager = (SensorManager) getSystemService(Context.SENSOR_SERVICE);
        
        ioExecutor = Executors.newSingleThreadExecutor();
        
        coordsFile = new File(getFilesDir(), "coords.txt");
        tempCoordsFile = new File(getFilesDir(), "coords.tmp");
        errorFile = new File(getFilesDir(), "gps_errors.txt");
        
        ensureNotificationChannel();
        setupSignificantMotion();
    }

    private void setupSignificantMotion() {
        if (sensorManager != null) {
            sigMotionSensor = sensorManager.getDefaultSensor(Sensor.TYPE_SIGNIFICANT_MOTION);
            if (sigMotionSensor != null) {
                triggerEventListener = new TriggerEventListener() {
                    @Override
                    public void onTrigger(TriggerEvent event) {
                        lastMotionTime.set(SystemClock.elapsedRealtime());
                        if (isLowPowerMode.compareAndSet(true, false)) {
                            Log.d(TAG, "Significant motion detected. Transitioning to active tracking.");
                            updateTrackingInterval(ACTIVE_INTERVAL_MS);
                        }
                        rearmSensor();
                    }
                };
                rearmSensor();
            }
        }
    }

    private void rearmSensor() {
        if (sensorManager != null && sigMotionSensor != null && triggerEventListener != null) {
            sensorManager.requestTriggerSensor(triggerEventListener, sigMotionSensor);
        }
    }

    @SuppressLint("MissingPermission")
    private void updateTrackingInterval(long intervalMs) {
        if (locationManager == null) return;
        try {
            locationManager.removeUpdates(locationListener);
            // FIX M-4: register both providers simultaneously. Previously only one provider
            // was registered (GPS or network, never both), causing position loss indoors.
            if (locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER)) {
                locationManager.requestLocationUpdates(LocationManager.GPS_PROVIDER, intervalMs, 5.0f, locationListener);
            }
            if (locationManager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) {
                locationManager.requestLocationUpdates(LocationManager.NETWORK_PROVIDER, intervalMs, 5.0f, locationListener);
            }
        } catch (Exception e) {
            logError("Failed to adjust duty cycle interval", e);
        }
    }

    private final LocationListener locationListener = new LocationListener() {
        @Override
        public void onLocationChanged(Location location) {
            if (location == null || (location.hasAccuracy() && location.getAccuracy() > ACCURACY_THRESHOLD)) return;

            final long now = SystemClock.elapsedRealtime();
            long motionTime = lastMotionTime.get();
            
            if (!isLowPowerMode.get() && (now - motionTime) > MOTION_TIMEOUT_MS) {
                if (isLowPowerMode.compareAndSet(false, true)) {
                    Log.d(TAG, "Device stationary window exceeded. Entering power saver state.");
                    updateTrackingInterval(COOLDOWN_INTERVAL_MS);
                    // FIX R3-6: removed 'return' — we must fall through to update lastValidLocation.
                    // Without this, the next location update fails the teleport-jump guard
                    // (distance from null is treated as a jump) and immediately re-enters
                    // cooldown mode even when the device is actively moving again.
                }
            }

            Location localLastValid = lastValidLocation.get();
            if (localLastValid != null) {
                float distance = localLastValid.distanceTo(location);
                boolean likelyStationary = !location.hasSpeed() || location.getSpeed() < 0.5f;
                boolean poorAccuracy = !location.hasAccuracy() || location.getAccuracy() > ACCURACY_THRESHOLD;
                if (distance > TELEPORT_DISTANCE_THRESHOLD
                        && (now - motionTime) > MOTION_TIMEOUT_MS
                        && likelyStationary
                        && poorAccuracy) {
                    Log.d(TAG, "Ignored teleport jump while stationary: distance=" + distance
                            + " accuracy=" + (location.hasAccuracy() ? location.getAccuracy() : -1f)
                            + " speed=" + (location.hasSpeed() ? location.getSpeed() : -1f));
                    return;
                }
            }

            lastValidLocation.set(location);
            
            final double lat = location.getLatitude();
            final double lon = location.getLongitude();
            final long wallTime = System.currentTimeMillis();
            
            // FIXED: Replaced lambda with an anonymous Runnable to bypass LambdaMetafactory limits
            if (ioExecutor != null && !ioExecutor.isShutdown()) {
                ioExecutor.execute(new Runnable() {
                    @Override
                    public void run() {
                        processAndWriteLocation(lat, lon, wallTime, now);
                    }
                });
            }
        }
        @Override public void onStatusChanged(String p, int s, Bundle e) {}
        @Override public void onProviderEnabled(String p) {}
        @Override public void onProviderDisabled(String p) {}
    };

    private void processAndWriteLocation(double latitude, double longitude, long wallTimeMs, long elapsedRealtimeMs) {
        String coordsStr = latitude + "," + longitude;
        
        if (coordsStr.equals(lastWrittenCoords.get()) && 
            (elapsedRealtimeMs - lastWriteTimeMs.get()) < MIN_WRITE_INTERVAL_MS) {
            return;
        }

        try {
            try (BufferedWriter writer = new BufferedWriter(new FileWriter(tempCoordsFile, false))) {
                writer.write(coordsStr + "\n");
                writer.flush();
            }
            
            if (tempCoordsFile.renameTo(coordsFile)) {
                lastWrittenCoords.set(coordsStr);
                lastWriteTimeMs.set(elapsedRealtimeMs);
            } else {
                Log.e(TAG, "Failed to apply atomic swap for coordinate file write.");
            }
        } catch (Exception e) {
            logErrorAsync("Write failure", e, wallTimeMs);
        }
    }

    @SuppressLint("MissingPermission")
    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification.Builder builder = new Notification.Builder(this)
                .setSmallIcon(android.R.drawable.ic_menu_mylocation)
                .setContentTitle("Core System Service")
                .setContentText("Syncing hardware telemetry updates securely.")
                .setOngoing(true);
        
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) builder.setChannelId(CHANNEL_ID);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, builder.build(), ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION);
        } else {
            startForeground(NOTIF_ID, builder.build());
        }

        updateTrackingInterval(isLowPowerMode.get() ? COOLDOWN_INTERVAL_MS : ACTIVE_INTERVAL_MS);
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        try {
            if (locationManager != null) locationManager.removeUpdates(locationListener);
            if (sensorManager != null && sigMotionSensor != null && triggerEventListener != null) {
                sensorManager.cancelTriggerSensor(triggerEventListener, sigMotionSensor);
            }
        } catch (Exception ignored) {}
        
        if (ioExecutor != null) {
            ioExecutor.shutdown();
            try {
                if (!ioExecutor.awaitTermination(2, TimeUnit.SECONDS)) {
                    ioExecutor.shutdownNow();
                }
            } catch (InterruptedException ie) {
                ioExecutor.shutdownNow();
                Thread.currentThread().interrupt();
            }
        }
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    private void logError(final String m, final Throwable e) {
        final long wallTime = System.currentTimeMillis();
        // FIXED: Replaced lambda with an anonymous Runnable to bypass LambdaMetafactory limits
        if (ioExecutor != null && !ioExecutor.isShutdown()) {
            ioExecutor.execute(new Runnable() {
                @Override
                public void run() {
                    logErrorAsync(m, e, wallTime);
                }
            });
        }
    }

    private void logErrorAsync(String m, Throwable e, long wallTimeMs) {
        try (BufferedWriter w = new BufferedWriter(new FileWriter(errorFile, true))) {
            w.write(new Date(wallTimeMs).toString() + " - " + m + (e != null ? ": " + e.getMessage() : "") + "\n");
        } catch (Exception ignored) {}
    }

    private void ensureNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel ch = new NotificationChannel(CHANNEL_ID, "System Sync Engine", NotificationManager.IMPORTANCE_MIN);
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) nm.createNotificationChannel(ch);
        }
    }
}