package com.stealthalert;

import android.app.Activity;
import android.os.Bundle;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.io.File;
import java.io.FileWriter;
import java.util.Date;

public class AlertActivity extends Activity {

    private static void logError(String msg, Throwable e) {
        String fullMsg = new Date().toString() + " [AlertActivity] " + msg + (e != null ? ": " + e.toString() : "");
        try {
            FileWriter writer = new FileWriter(new File("/data/local/tmp/alert_errors.txt"), true);
            writer.write(fullMsg + "\n");
            writer.flush();
            writer.close();
        } catch (Exception ignored) {}
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        try {
            super.onCreate(savedInstanceState);

            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                    | WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED
                    | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                    | WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD);
            getWindow().getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                            | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);

            String title = getIntent().getStringExtra("title");
            String text = getIntent().getStringExtra("text");
            if (title == null) title = "System Alert";
            if (text == null) text = "Received an alert from the server.";

            LinearLayout container = new LinearLayout(this);
            container.setOrientation(LinearLayout.VERTICAL);
            container.setGravity(Gravity.CENTER);
            container.setPadding(80, 80, 80, 80);
            container.setBackgroundColor(0xFF000000);
            container.setLayoutParams(new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.MATCH_PARENT));

            TextView titleView = new TextView(this);
            titleView.setText(title);
            titleView.setTextSize(32.0f);
            titleView.setTextColor(0xFFFFFFFF);
            titleView.setGravity(Gravity.CENTER);
            titleView.setPadding(0, 0, 0, 40);

            TextView textView = new TextView(this);
            textView.setText(text);
            textView.setTextSize(20.0f);
            textView.setTextColor(0xFFCCCCCC);
            textView.setGravity(Gravity.CENTER);
            textView.setLayoutParams(new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT));

            container.addView(titleView);
            container.addView(textView);

            setContentView(container);
        } catch (Exception e) {
            logError("Crash in onCreate", e);
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_VOLUME_UP ||
                keyCode == KeyEvent.KEYCODE_VOLUME_DOWN ||
                keyCode == KeyEvent.KEYCODE_VOLUME_MUTE) {
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    public boolean onKeyUp(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_VOLUME_UP ||
                keyCode == KeyEvent.KEYCODE_VOLUME_DOWN ||
                keyCode == KeyEvent.KEYCODE_VOLUME_MUTE) {
            return true;
        }
        return super.onKeyUp(keyCode, event);
    }
}
