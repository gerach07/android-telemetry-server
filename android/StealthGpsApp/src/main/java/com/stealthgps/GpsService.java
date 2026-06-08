package com.stealthgps;

import android.annotation.SuppressLint;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Bundle;
import android.os.IBinder;
import android.util.Log;

import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;

import java.io.File;
import java.io.FileWriter;
import java.util.Date;

public class GpsService extends Service implements SensorEventListener {
    private static final String TAG = "StealthGps-Service";
    private LocationManager locationManager;
    private SensorManager sensorManager;
    private Sensor linearAccelSensor;

    private File coordsFile;
    private File errorFile;
    private long currentIntervalMs = 5000L; // Default 5 seconds if not provided

    private long lastMotionTime = 0;
    private Location lastValidLocation = null;

    private static final float MOTION_THRESHOLD = 1.5f; // m/s^2
    private static final long MOTION_TIMEOUT_MS = 60000; // 60 seconds
    private static final float TELEPORT_DISTANCE_THRESHOLD = 30.0f; // meters
    private static final float ACCURACY_THRESHOLD = 100.0f; // meters

    @Override
    public void onCreate() {
        super.onCreate();
        locationManager = (LocationManager) getSystemService(Context.LOCATION_SERVICE);
        sensorManager = (SensorManager) getSystemService(Context.SENSOR_SERVICE);
        if (sensorManager != null) {
            linearAccelSensor = sensorManager.getDefaultSensor(Sensor.TYPE_LINEAR_ACCELERATION);
            if (linearAccelSensor != null) {
                sensorManager.registerListener(this, linearAccelSensor, SensorManager.SENSOR_DELAY_NORMAL);
            } else {
                Log.w(TAG, "Linear acceleration sensor not found on this device.");
            }
        }
        coordsFile = new File("/data/local/tmp/coords.txt");
        errorFile = new File("/data/local/tmp/gps_errors.txt");
        Log.d(TAG, "GpsService created");
    }

    private void logError(String msg, Throwable e) {
        String fullMsg = new Date().toString() + " - " + msg + (e != null ? ": " + e.toString() : "");
        Log.e(TAG, fullMsg, e);
        try {
            FileWriter writer = new FileWriter(errorFile, true); // Append mode
            writer.write(fullMsg + "\n");
            writer.flush();
            writer.close();
        } catch (Exception ioException) {
            Log.e(TAG, "Could not write to error file", ioException);
        }
    }

    private boolean updatesStarted = false;

    @SuppressLint("MissingPermission")
    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        long requestedInterval = currentIntervalMs;
        if (intent != null && intent.hasExtra("interval")) {
            requestedInterval = intent.getLongExtra("interval", currentIntervalMs);
        }

        if (!updatesStarted || requestedInterval != currentIntervalMs) {
            currentIntervalMs = requestedInterval;
            updatesStarted = true;
            Log.d(TAG, "Configuring location updates with interval: " + currentIntervalMs + "ms");
            
            try {
                // Remove old updates before requesting new ones
                locationManager.removeUpdates(locationListener);

                boolean providerFound = false;
                
                if (locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER)) {
                    locationManager.requestLocationUpdates(LocationManager.GPS_PROVIDER, currentIntervalMs, 1.0f, locationListener);
                    Log.d(TAG, "Requested updates from GPS_PROVIDER (Forces chip refresh)");
                    providerFound = true;
                }
                
                if (locationManager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) {
                    locationManager.requestLocationUpdates(LocationManager.NETWORK_PROVIDER, currentIntervalMs, 1.0f, locationListener);
                    Log.d(TAG, "Requested updates from NETWORK_PROVIDER");
                    providerFound = true;
                }
                
                if (locationManager.isProviderEnabled(LocationManager.PASSIVE_PROVIDER)) {
                    locationManager.requestLocationUpdates(LocationManager.PASSIVE_PROVIDER, currentIntervalMs, 1.0f, locationListener);
                    Log.d(TAG, "Requested updates from PASSIVE_PROVIDER");
                    providerFound = true;
                }

                if (!providerFound) {
                    logError("No location providers available!", null);
                }

            } catch (SecurityException e) {
                logError("Location permissions not granted! (Should be granted via system app uid)", e);
            } catch (Exception e) {
                logError("Error requesting location updates", e);
            }
        }

        return START_STICKY; // Restart if killed
    }

    private final LocationListener locationListener = new LocationListener() {
        @Override
        public void onLocationChanged(Location location) {
            if (location == null) return;
            
            // 1. Strict accuracy filter
            if (location.hasAccuracy() && location.getAccuracy() > ACCURACY_THRESHOLD) {
                Log.d(TAG, "Discarding location due to poor accuracy: " + location.getAccuracy() + "m");
                return;
            }

            // 2. Teleportation filter using accelerometer motion confidence
            if (lastValidLocation != null) {
                float distance = lastValidLocation.distanceTo(location);
                if (distance > TELEPORT_DISTANCE_THRESHOLD) {
                    long timeSinceLastMotion = System.currentTimeMillis() - lastMotionTime;
                    if (timeSinceLastMotion > MOTION_TIMEOUT_MS) {
                        Log.w(TAG, "Discarding anomalous location jump (" + distance + "m) because no motion was detected.");
                        return;
                    }
                }
            }

            lastValidLocation = location;
            String coordsStr = location.getLatitude() + "," + location.getLongitude();
            Log.d(TAG, "Location updated: " + coordsStr);
            
            try {
                // Write to scratchpad file for C++ binary to read
                FileWriter writer = new FileWriter(coordsFile, false); // false = overwrite
                writer.write(coordsStr + "\n");
                writer.flush();
                writer.close();
            } catch (Exception e) {
                logError("Failed to write coordinates to " + coordsFile.getAbsolutePath(), e);
            }
        }

        @Override
        public void onStatusChanged(String provider, int status, Bundle extras) {}
        @Override
        public void onProviderEnabled(String provider) {}
        @Override
        public void onProviderDisabled(String provider) {}
    };

    @Override
    public void onDestroy() {
        super.onDestroy();
        try {
            if (locationManager != null && locationListener != null) {
                locationManager.removeUpdates(locationListener);
            }
            if (sensorManager != null) {
                sensorManager.unregisterListener(this);
            }
        } catch (Exception e) {
            logError("Error during onDestroy cleanup", e);
        }
        Log.d(TAG, "GpsService destroyed");
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onSensorChanged(SensorEvent event) {
        if (event.sensor.getType() == Sensor.TYPE_LINEAR_ACCELERATION) {
            float x = event.values[0];
            float y = event.values[1];
            float z = event.values[2];
            
            double magnitude = Math.sqrt(x*x + y*y + z*z);
            if (magnitude > MOTION_THRESHOLD) {
                lastMotionTime = System.currentTimeMillis();
            }
        }
    }

    @Override
    public void onAccuracyChanged(Sensor sensor, int accuracy) {}
}
