package com.stealthgps;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.UserManager;
import android.util.Log;

public class BootReceiver extends BroadcastReceiver {
    private static final String TAG = "StealthGps-Boot";
    private static final String PREFS_NAME = "stealth_gps_settings";
    private static final String KEY_LOCATION_ENABLED = "location_enabled";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null) return;
        String action = intent.getAction();
        
        if (Intent.ACTION_BOOT_COMPLETED.equals(action) || 
            Intent.ACTION_LOCKED_BOOT_COMPLETED.equals(action)) {
            
            Log.d(TAG, "Boot event captured: " + action);

            Context storageContext = context;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                UserManager userManager = context.getSystemService(UserManager.class);
                boolean isUnlocked = userManager != null && userManager.isUserUnlocked();
                
                if (Intent.ACTION_LOCKED_BOOT_COMPLETED.equals(action) || !isUnlocked) {
                    storageContext = context.createDeviceProtectedStorageContext();
                }
            }

            SharedPreferences prefs = storageContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
            boolean serviceShouldRun = prefs.getBoolean(KEY_LOCATION_ENABLED, true);

            if (!serviceShouldRun) {
                Log.d(TAG, "Location control flag explicitly disabled.");
                return;
            }
            
            launchGpsService(storageContext);
        }
    }

    private void launchGpsService(Context context) {
        Intent serviceIntent = new Intent(context, GpsService.class);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(serviceIntent);
            } else {
                context.startService(serviceIntent);
            }
            Log.d(TAG, "GpsService triggered successfully.");
        } catch (Exception e) {
            Log.e(TAG, "Direct foreground launch restricted by OS.", e);
            // Non-AndroidX fallback can be implemented here via native JobScheduler if required
        }
    }
}