package com.stealthmonitor;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

/**
 * Starts {@link ScreenTimeService} after the device finishes booting.
 *
 * <p>Only ACTION_BOOT_COMPLETED is handled — ACTION_LOCKED_BOOT_COMPLETED is
 * intentionally ignored because credential-encrypted (CE) storage (including
 * /data/data/<pkg>) is unavailable until the user completes their first unlock.
 * Starting the service before CE storage is ready would cause silent I/O failures.
 *
 * <p>Declared in AndroidManifest.xml with:
 * <pre>
 *   &lt;uses-permission android:name="android.permission.RECEIVE_BOOT_COMPLETED"/&gt;
 *   &lt;receiver android:name=".BootReceiver" android:exported="true"
 *             android:directBootAware="false"&gt;
 *     &lt;intent-filter&gt;
 *       &lt;action android:name="android.intent.action.BOOT_COMPLETED"/&gt;
 *     &lt;/intent-filter&gt;
 *   &lt;/receiver&gt;
 * </pre>
 */
public final class BootReceiver extends BroadcastReceiver {

    private static final String TAG = "StealthMonitor/Boot";

    @Override
    public void onReceive(final Context context, final Intent intent) {
        if (context == null || intent == null) return;

        // Guard: only handle full boot — not locked-boot (CE storage unavailable).
        if (!Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) return;

        Log.i(TAG, "BOOT_COMPLETED received — launching ScreenTimeService");

        try {
            final Intent serviceIntent = new Intent(context, ScreenTimeService.class);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                // API 26+: background service start restrictions require startForegroundService().
                // ScreenTimeService MUST call startForeground() within 5 seconds or the OS will ANR.
                context.startForegroundService(serviceIntent);
            } else {
                context.startService(serviceIntent);
            }
        } catch (final Exception e) {
            Log.e(TAG, "Failed to start ScreenTimeService", e);
        }
    }
}
