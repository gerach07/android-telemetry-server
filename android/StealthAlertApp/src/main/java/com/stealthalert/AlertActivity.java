package com.stealthalert;

import android.animation.ArgbEvaluator;
import android.animation.ObjectAnimator;
import android.animation.ValueAnimator;
import android.app.Activity;
import android.graphics.Color;
import android.graphics.Typeface;
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

    private static final Object logMutex = new Object();

    private static void logError(String msg, Throwable e) {
        String fullMsg = new Date().toString() + " [AlertActivity] " + msg + (e != null ? ": " + e.toString() : "");
        synchronized (logMutex) {
            try (FileWriter writer = new FileWriter(new File("/data/local/tmp/alert_errors.txt"), true)) {
                writer.write(fullMsg + "\n");
            } catch (Exception ignored) {}
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        try {
            super.onCreate(savedInstanceState);

            if (android.os.Build.VERSION.SDK_INT >= 27) {
                setShowWhenLocked(true);
                setTurnScreenOn(true);
            }
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
            if (title == null || title.isEmpty() || title.equals("1")) {
                title = "⚠ SYSTEM ALERT ⚠";
            }
            if (text == null || text.isEmpty()) {
                text = "CRITICAL WARNING RECEIVED";
            }

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
            titleView.setTextSize(48.0f);
            titleView.setTextColor(0xFFFFFFFF);
            titleView.setTypeface(Typeface.DEFAULT_BOLD);
            titleView.setGravity(Gravity.CENTER);
            titleView.setPadding(0, 0, 0, 80);

            TextView textView = new TextView(this);
            textView.setText(text);
            textView.setTextSize(28.0f);
            textView.setTextColor(0xFFEEEEEE);
            textView.setGravity(Gravity.CENTER);
            textView.setLayoutParams(new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT));

            container.addView(titleView);
            container.addView(textView);

            setContentView(container);

            // Flashing Red Background Animation
            ObjectAnimator colorAnim = ObjectAnimator.ofInt(container, "backgroundColor", 0xFFAA0000, 0xFF110000);
            colorAnim.setDuration(400);
            colorAnim.setEvaluator(new ArgbEvaluator());
            colorAnim.setRepeatCount(ValueAnimator.INFINITE);
            colorAnim.setRepeatMode(ValueAnimator.REVERSE);
            colorAnim.start();

            // Pulsing scale animation for the title to make it look alarming
            ObjectAnimator scaleX = ObjectAnimator.ofFloat(titleView, "scaleX", 1.0f, 1.15f);
            ObjectAnimator scaleY = ObjectAnimator.ofFloat(titleView, "scaleY", 1.0f, 1.15f);
            scaleX.setDuration(500);
            scaleY.setDuration(500);
            scaleX.setRepeatCount(ValueAnimator.INFINITE);
            scaleY.setRepeatCount(ValueAnimator.INFINITE);
            scaleX.setRepeatMode(ValueAnimator.REVERSE);
            scaleY.setRepeatMode(ValueAnimator.REVERSE);
            scaleX.start();
            scaleY.start();

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
