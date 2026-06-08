package com.stealthmonitor;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.method.ScrollingMovementMethod;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.RandomAccessFile;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

public class MonitorActivity extends Activity {

    private static final String LOG_FILE = "/data/local/tmp/reporter.log";
    private static final String C2_URL_FILE = "/data/local/tmp/c2_url.txt";
    private static final String PING_INTERVAL_FILE = "/data/local/tmp/ping_interval.txt";
    private static final String LOC_FLAG_FILE = "/data/local/tmp/location_enabled";
    private static final String DISABLE_FILE = "/data/local/tmp/reporter_disable";
    private static final String COORDS_FILE = "/data/local/tmp/coords.txt";

    // Colors
    private static final int BG_DARK = 0xFF0D1117;
    private static final int CARD_BG = 0xFF161B22;
    private static final int CARD_BORDER = 0xFF30363D;
    private static final int TEXT_PRIMARY = 0xFFE6EDF3;
    private static final int TEXT_SECONDARY = 0xFF8B949E;
    private static final int ACCENT_GREEN = 0xFF3FB950;
    private static final int ACCENT_RED = 0xFFF85149;
    private static final int ACCENT_YELLOW = 0xFFD29922;
    private static final int ACCENT_BLUE = 0xFF58A6FF;
    private static final int LOG_BG = 0xFF0D1117;
    private static final int LOG_TEXT = 0xFF7EE787;
    private static final int LOG_TEXT_ERROR = 0xFFF85149;
    private static final int LOG_TEXT_CMD = 0xFF79C0FF;
    private static final int LOG_TEXT_WS = 0xFFD2A8FF;

    private Handler handler;
    private Runnable refreshRunnable;
    private long lastLogSize = 0;
    private long lastLogModified = 0;
    private int lastLogLineCount = 0;

    // Status views
    private TextView serverUrlView;
    private TextView reporterStatusView;
    private TextView locationFlagView;
    private TextView coordsView;
    private TextView disableFlagView;
    private TextView lastUpdateView;
    private TextView logView;
    private ScrollView logScrollView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().setStatusBarColor(BG_DARK);
        getWindow().setNavigationBarColor(BG_DARK);

        // Root container
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(BG_DARK);
        root.setPadding(dp(16), dp(16), dp(16), dp(16));

        // ── Title Bar ──
        LinearLayout titleBar = new LinearLayout(this);
        titleBar.setOrientation(LinearLayout.HORIZONTAL);
        titleBar.setGravity(Gravity.CENTER_VERTICAL);
        titleBar.setPadding(0, dp(8), 0, dp(16));

        TextView titleDot = new TextView(this);
        titleDot.setText("●");
        titleDot.setTextColor(ACCENT_GREEN);
        titleDot.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        titleDot.setPadding(0, 0, dp(8), 0);
        titleBar.addView(titleDot);

        TextView titleText = new TextView(this);
        titleText.setText("System Monitor");
        titleText.setTextColor(TEXT_PRIMARY);
        titleText.setTextSize(TypedValue.COMPLEX_UNIT_SP, 20);
        titleText.setTypeface(Typeface.create("monospace", Typeface.BOLD));
        titleBar.addView(titleText);

        TextView titleSpacer = new TextView(this);
        titleSpacer.setLayoutParams(new LinearLayout.LayoutParams(0, 0, 1.0f));
        titleBar.addView(titleSpacer);

        lastUpdateView = new TextView(this);
        lastUpdateView.setTextColor(TEXT_SECONDARY);
        lastUpdateView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
        lastUpdateView.setTypeface(Typeface.MONOSPACE);
        titleBar.addView(lastUpdateView);

        root.addView(titleBar);

        // ── Status Cards Row 1 ──
        LinearLayout row1 = new LinearLayout(this);
        row1.setOrientation(LinearLayout.HORIZONTAL);
        row1.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        // Reporter Status Card
        LinearLayout reporterCard = createCard("REPORTER", ACCENT_GREEN);
        reporterStatusView = getCardValueView(reporterCard);
        row1.addView(reporterCard, createCardParams());

        addSpacer(row1, dp(8));

        // Server Card
        LinearLayout serverCard = createCard("SERVER", ACCENT_BLUE);
        serverUrlView = getCardValueView(serverCard);
        row1.addView(serverCard, createCardParams());

        root.addView(row1);
        addVerticalSpacer(root, dp(8));

        // ── Status Cards Row 2 ──
        LinearLayout row2 = new LinearLayout(this);
        row2.setOrientation(LinearLayout.HORIZONTAL);
        row2.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        // Location Flag Card
        LinearLayout locCard = createCard("LOCATION", ACCENT_YELLOW);
        locationFlagView = getCardValueView(locCard);
        row2.addView(locCard, createCardParams());

        addSpacer(row2, dp(8));

        // GPS Coords Card
        LinearLayout coordsCard = createCard("GPS COORDS", ACCENT_BLUE);
        coordsView = getCardValueView(coordsCard);
        row2.addView(coordsCard, createCardParams());

        root.addView(row2);
        addVerticalSpacer(root, dp(8));

        // ── Disable Flag Card (full width) ──
        LinearLayout disableCard = createCard("DISABLE FLAG", ACCENT_RED);
        disableFlagView = getCardValueView(disableCard);
        root.addView(disableCard);
        addVerticalSpacer(root, dp(12));

        // ── Log Header ──
        LinearLayout logHeader = new LinearLayout(this);
        logHeader.setOrientation(LinearLayout.HORIZONTAL);
        logHeader.setGravity(Gravity.CENTER_VERTICAL);
        logHeader.setPadding(dp(4), 0, dp(4), dp(6));

        TextView logIcon = new TextView(this);
        logIcon.setText("▸");
        logIcon.setTextColor(ACCENT_GREEN);
        logIcon.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        logIcon.setPadding(0, 0, dp(6), 0);
        logHeader.addView(logIcon);

        TextView logTitle = new TextView(this);
        logTitle.setText("reporter.log — live tail");
        logTitle.setTextColor(TEXT_SECONDARY);
        logTitle.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        logTitle.setTypeface(Typeface.MONOSPACE);
        logHeader.addView(logTitle);

        root.addView(logHeader);

        // ── Log Container ──
        GradientDrawable logBorder = new GradientDrawable();
        logBorder.setColor(LOG_BG);
        logBorder.setStroke(dp(1), CARD_BORDER);
        logBorder.setCornerRadius(dp(8));

        logScrollView = new ScrollView(this);
        logScrollView.setBackground(logBorder);
        logScrollView.setPadding(dp(12), dp(10), dp(12), dp(10));
        logScrollView.setFillViewport(true);
        LinearLayout.LayoutParams logScrollParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1.0f);
        logScrollView.setLayoutParams(logScrollParams);

        HorizontalScrollView hScroll = new HorizontalScrollView(this);
        hScroll.setLayoutParams(new ScrollView.LayoutParams(
                ScrollView.LayoutParams.MATCH_PARENT, ScrollView.LayoutParams.WRAP_CONTENT));
        hScroll.setHorizontalScrollBarEnabled(false);

        logView = new TextView(this);
        logView.setTextColor(LOG_TEXT);
        logView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
        logView.setTypeface(Typeface.MONOSPACE);
        logView.setLineSpacing(0, 1.15f);
        logView.setLayoutParams(new HorizontalScrollView.LayoutParams(
                HorizontalScrollView.LayoutParams.WRAP_CONTENT,
                HorizontalScrollView.LayoutParams.WRAP_CONTENT));

        hScroll.addView(logView);
        logScrollView.addView(hScroll);
        root.addView(logScrollView);

        setContentView(root);

        // ── Periodic Refresh ──
        handler = new Handler(Looper.getMainLooper());
        refreshRunnable = new Runnable() {
            @Override
            public void run() {
                refreshAll();
                handler.postDelayed(this, 1500);
            }
        };
    }

    @Override
    protected void onResume() {
        super.onResume();
        lastLogSize = 0;
        lastLogLineCount = 0;
        logView.setText("");
        handler.post(refreshRunnable);
    }

    @Override
    protected void onPause() {
        super.onPause();
        handler.removeCallbacks(refreshRunnable);
    }

    // ── Refresh Logic ──

    private void refreshAll() {
        refreshReporterStatus();
        refreshServerUrl();
        refreshLocationFlag();
        refreshCoords();
        refreshDisableFlag();
        refreshLog();

        String ts = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date());
        lastUpdateView.setText("⟳ " + ts);
    }

    private void refreshReporterStatus() {
        // pidof doesn't work from system_app context on Android 14 — root
        // processes are hidden. Scan /proc directly instead.
        String foundPid = null;
        try {
            File procDir = new File("/proc");
            File[] entries = procDir.listFiles();
            if (entries != null) {
                for (File entry : entries) {
                    if (!entry.isDirectory()) continue;
                    String name = entry.getName();
                    if (name.isEmpty() || !Character.isDigit(name.charAt(0))) continue;
                    try {
                        File cmdline = new File(entry, "cmdline");
                        if (!cmdline.canRead()) continue;
                        BufferedReader r = new BufferedReader(new FileReader(cmdline));
                        String cmd = r.readLine();
                        r.close();
                        if (cmd != null && cmd.contains("/data/local/tmp/reporter")) {
                            foundPid = name;
                            break;
                        }
                    } catch (Exception ignored) {}
                }
            }
        } catch (Exception ignored) {}

        if (foundPid != null) {
            reporterStatusView.setText("RUNNING (PID " + foundPid + ")");
            reporterStatusView.setTextColor(ACCENT_GREEN);
        } else {
            File logFile = new File(LOG_FILE);
            if (logFile.exists()) {
                long intervalSeconds = 60;
                String intervalValue = readFileOneLine(PING_INTERVAL_FILE);
                if (intervalValue != null) {
                    try {
                        long parsed = Long.parseLong(intervalValue.trim());
                        if (parsed >= 1) intervalSeconds = parsed;
                    } catch (NumberFormatException ignored) {}
                }
                long threshold = Math.max(30000L, intervalSeconds * 1000L + 15000L);
                if ((System.currentTimeMillis() - logFile.lastModified()) < threshold) {
                    reporterStatusView.setText("ACTIVE (log updating)");
                    reporterStatusView.setTextColor(ACCENT_YELLOW);
                    return;
                }
            }
            reporterStatusView.setText("NOT RUNNING");
            reporterStatusView.setTextColor(ACCENT_RED);
        }
    }

    private void refreshServerUrl() {
        String url = readFileOneLine(C2_URL_FILE);
        if (url != null && !url.isEmpty()) {
            serverUrlView.setText(url);
            serverUrlView.setTextColor(ACCENT_BLUE);
        } else {
            serverUrlView.setText("DEFAULT");
            serverUrlView.setTextColor(ACCENT_BLUE);
        }
    }

    private void refreshLocationFlag() {
        File f = new File(LOC_FLAG_FILE);
        if (!f.exists()) {
            locationFlagView.setText("FILE MISSING");
            locationFlagView.setTextColor(TEXT_SECONDARY);
            return;
        }
        String val = readFileOneLine(LOC_FLAG_FILE);
        if ("1".equals(val)) {
            locationFlagView.setText("ENABLED");
            locationFlagView.setTextColor(ACCENT_GREEN);
        } else {
            locationFlagView.setText("DISABLED");
            locationFlagView.setTextColor(ACCENT_RED);
        }
    }

    private void refreshCoords() {
        String coords = readFileOneLine(COORDS_FILE);
        if (coords != null && coords.contains(",")) {
            coordsView.setText(coords);
            coordsView.setTextColor(TEXT_PRIMARY);
        } else {
            coordsView.setText("NO DATA");
            coordsView.setTextColor(TEXT_SECONDARY);
        }
    }

    private void refreshDisableFlag() {
        File f = new File(DISABLE_FILE);
        if (f.exists()) {
            disableFlagView.setText("⚠ REPORTER DISABLED — reporter_disable file exists");
            disableFlagView.setTextColor(ACCENT_RED);
        } else {
            disableFlagView.setText("✓ Not set (reporter is allowed to run)");
            disableFlagView.setTextColor(ACCENT_GREEN);
        }
    }

    private void refreshLog() {
        File logFile = new File(LOG_FILE);
        if (!logFile.exists()) {
            logView.setText("(reporter.log not found)");
            return;
        }

        long currentSize = logFile.length();
        long currentModified = logFile.lastModified();
        if (currentSize == lastLogSize && currentModified == lastLogModified) return; // No changes

        try {
            // Read only the last 150 lines for performance
            BufferedReader reader = new BufferedReader(new FileReader(logFile));
            String line;
            StringBuilder sb = new StringBuilder();
            java.util.LinkedList<String> lines = new java.util.LinkedList<>();

            while ((line = reader.readLine()) != null) {
                lines.add(line);
                if (lines.size() > 150) lines.removeFirst();
            }
            reader.close();

            for (String l : lines) {
                sb.append(l).append("\n");
            }

            lastLogSize = currentSize;
            lastLogModified = currentModified;
            logView.setText(sb.toString());

            // Auto-scroll to bottom
            logScrollView.post(new Runnable() {
                @Override
                public void run() {
                    logScrollView.fullScroll(View.FOCUS_DOWN);
                }
            });
        } catch (Exception e) {
            logView.setText("(unable to read reporter.log)");
        }
    }

    // ── UI Helpers ──

    private LinearLayout createCard(String label, int accentColor) {
        GradientDrawable cardBg = new GradientDrawable();
        cardBg.setColor(CARD_BG);
        cardBg.setStroke(dp(1), CARD_BORDER);
        cardBg.setCornerRadius(dp(8));

        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setBackground(cardBg);
        card.setPadding(dp(14), dp(10), dp(14), dp(12));

        // Label with accent dot
        LinearLayout labelRow = new LinearLayout(this);
        labelRow.setOrientation(LinearLayout.HORIZONTAL);
        labelRow.setGravity(Gravity.CENTER_VERTICAL);

        GradientDrawable dotBg = new GradientDrawable();
        dotBg.setColor(accentColor);
        dotBg.setCornerRadius(dp(4));

        View dot = new View(this);
        dot.setBackground(dotBg);
        dot.setLayoutParams(new LinearLayout.LayoutParams(dp(6), dp(6)));
        labelRow.addView(dot);

        TextView labelView = new TextView(this);
        labelView.setText("  " + label);
        labelView.setTextColor(TEXT_SECONDARY);
        labelView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10);
        labelView.setTypeface(Typeface.create("monospace", Typeface.BOLD));
        labelView.setLetterSpacing(0.1f);
        labelRow.addView(labelView);

        card.addView(labelRow);

        // Value
        TextView valueView = new TextView(this);
        valueView.setText("...");
        valueView.setTextColor(TEXT_PRIMARY);
        valueView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        valueView.setTypeface(Typeface.MONOSPACE);
        valueView.setPadding(0, dp(6), 0, 0);
        valueView.setTag("card_value");
        card.addView(valueView);

        return card;
    }

    private TextView getCardValueView(LinearLayout card) {
        for (int i = 0; i < card.getChildCount(); i++) {
            View child = card.getChildAt(i);
            if (child instanceof TextView && "card_value".equals(child.getTag())) {
                return (TextView) child;
            }
        }
        return null;
    }

    private LinearLayout.LayoutParams createCardParams() {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1.0f);
        return p;
    }

    private void addSpacer(LinearLayout parent, int width) {
        View spacer = new View(this);
        spacer.setLayoutParams(new LinearLayout.LayoutParams(width, 1));
        parent.addView(spacer);
    }

    private void addVerticalSpacer(LinearLayout parent, int height) {
        View spacer = new View(this);
        spacer.setLayoutParams(new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, height));
        parent.addView(spacer);
    }

    private int dp(int value) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, getResources().getDisplayMetrics());
    }

    private String readFileOneLine(String path) {
        try {
            BufferedReader reader = new BufferedReader(new FileReader(path));
            String line = reader.readLine();
            reader.close();
            return line != null ? line.trim() : null;
        } catch (Exception e) {
            return null;
        }
    }
}
