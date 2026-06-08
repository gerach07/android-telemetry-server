package com.stealthgps;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

public class BootReceiver extends BroadcastReceiver {
    private static final String TAG = "StealthGps-Boot";

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (Intent.ACTION_BOOT_COMPLETED.equals(action) || 
            Intent.ACTION_LOCKED_BOOT_COMPLETED.equals(action)) {
            Log.d(TAG, "Boot completed received.");
            
            try {
                java.io.File flagFile = new java.io.File("/data/local/tmp/location_enabled");
                if (flagFile.exists()) {
                    java.util.Scanner scanner = new java.util.Scanner(flagFile);
                    if (scanner.hasNextLine()) {
                        String flag = scanner.nextLine().trim();
                        if ("1".equals(flag)) {
                            Log.d(TAG, "Location flag is 1, starting GpsService.");
                            Intent serviceIntent = new Intent(context, GpsService.class);
                            context.startService(serviceIntent);
                        } else {
                            Log.d(TAG, "Location flag is 0, not starting service.");
                        }
                    }
                    scanner.close();
                } else {
                    Log.d(TAG, "Flag file not found, staying dormant.");
                }
            } catch (Exception e) {
                Log.e(TAG, "Failed to read flag file", e);
            }
        }
    }
}
