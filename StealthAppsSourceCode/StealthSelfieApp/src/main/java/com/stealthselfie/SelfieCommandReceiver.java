package com.stealthselfie;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

/**
 * FIX H-4: Receives broadcast commands from the reporter binary and starts
 * the selfie capture activity. Using a broadcast receiver instead of am-start
 * avoids Android 14+ background-activity-start restrictions that silently
 * prevent background native processes from launching activities directly.
 */
public class SelfieCommandReceiver extends BroadcastReceiver {
    private static final String TAG = "StealthSelfie";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null) return;
        String action = intent.getStringExtra("action");
        if (!"capture".equals(action)) {
            Log.w(TAG, "SelfieCommandReceiver: unknown action: " + action);
            return;
        }
        Log.i(TAG, "SelfieCommandReceiver: triggering stealth capture via activity");
        Intent activityIntent = new Intent(context, MainActivity.class);
        activityIntent.putExtra("mode", "verify");
        activityIntent.addFlags(
            Intent.FLAG_ACTIVITY_NEW_TASK |
            Intent.FLAG_ACTIVITY_SINGLE_TOP |
            Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
        );
        context.startActivity(activityIntent);
    }
}
