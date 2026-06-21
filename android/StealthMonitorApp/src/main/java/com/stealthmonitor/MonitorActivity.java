package com.stealthmonitor;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.Layout;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.RandomAccessFile;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.LinkedList;
import java.util.Locale;

public class MonitorActivity extends Activity {

    private static final String LOG_FILE = "/data/local/tmp/reporter.log";
    private static final String ALT_LOG_FILE = "/data/system/reporter.log";
    private static final String C2_URL_FILE = "/data/local/tmp/c2_url.txt";
    private static final String PING_INTERVAL_FILE = "/data/system/ping_interval.txt";
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
    // Log line colors — each level gets its own distinct color
    private static final int LOG_SYS   = 0xFF56D4DD; // Cyan — system startup/shutdown
    private static final int LOG_TASK  = 0xFFFF9E64; // Orange — C2 task execution
    private static final int LOG_CMD   = 0xFF79C0FF; // Blue — shell command execution
    private static final int LOG_WS    = 0xFFD2A8FF; // Purple — WebSocket traffic
    private static final int LOG_ERR   = 0xFFF85149; // Red — errors & failures
    private static final int LOG_INFO  = 0xFF7EE787; // Green — heartbeat & report cycles
    private static final int LOG_DIM   = 0xFF6E7681; // Dim gray — fallback

    private Handler handler;
    private Runnable refreshRunnable;
    private long lastLogSize = 0;
    private long lastLogModified = 0;
    private static final int MAX_LOG_LINES = 500;
    private boolean isUserScrolling = false;

    // Status views
    private TextView serverUrlView;
    private TextView pingIntervalView;
    private TextView reporterStatusView;
    private TextView locationFlagView;
    private TextView coordsView;
    private TextView disableFlagView;
    private TextView lastUpdateView;
    private TextView logTitle;
    private ScrollView logScrollView;
    private LinearLayout logEntriesContainer;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().setStatusBarColor(0xFF050508);
        getWindow().setNavigationBarColor(0xFF050508);

        // Root container - Deep Cyberpunk Black/Blue Gradient
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        GradientDrawable rootBg = new GradientDrawable(GradientDrawable.Orientation.TL_BR, new int[]{0xFF020204, 0xFF0A0F1A});
        root.setBackground(rootBg);
        root.setPadding(dp(16), dp(24), dp(16), dp(16));

        // ── Title Bar ──
        LinearLayout titleBar = new LinearLayout(this);
        titleBar.setOrientation(LinearLayout.HORIZONTAL);
        titleBar.setGravity(Gravity.CENTER_VERTICAL);
        titleBar.setPadding(0, 0, 0, dp(20));

        // Pulsating glowing dot
        View titleDot = new View(this);
        GradientDrawable dotBg = new GradientDrawable();
        dotBg.setShape(GradientDrawable.OVAL);
        dotBg.setColor(0xFF00FF88);
        titleDot.setBackground(dotBg);
        titleDot.setLayoutParams(new LinearLayout.LayoutParams(dp(10), dp(10)));
        
        android.animation.ObjectAnimator pulse = android.animation.ObjectAnimator.ofFloat(titleDot, "alpha", 1f, 0.3f);
        pulse.setDuration(800);
        pulse.setRepeatCount(android.animation.ValueAnimator.INFINITE);
        pulse.setRepeatMode(android.animation.ValueAnimator.REVERSE);
        pulse.start();

        titleBar.addView(titleDot);

        TextView titleText = new TextView(this);
        titleText.setText(" SYSTEM_MONITOR");
        titleText.setTextColor(0xFFFFFFFF);
        titleText.setTextSize(TypedValue.COMPLEX_UNIT_SP, 22);
        titleText.setTypeface(Typeface.create("sans-serif-black", Typeface.BOLD));
        titleText.setLetterSpacing(0.05f);
        titleText.setPadding(dp(8), 0, 0, 0);
        titleText.setShadowLayer(10f, 0f, 0f, 0xAA00FF88);
        titleBar.addView(titleText);

        TextView titleSpacer = new TextView(this);
        titleSpacer.setLayoutParams(new LinearLayout.LayoutParams(0, 0, 1.0f));
        titleBar.addView(titleSpacer);

        lastUpdateView = new TextView(this);
        lastUpdateView.setTextColor(0xFF00FFFF);
        lastUpdateView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        lastUpdateView.setTypeface(Typeface.create("monospace", Typeface.NORMAL));
        lastUpdateView.setShadowLayer(5f, 0f, 0f, 0x8800FFFF);
        titleBar.addView(lastUpdateView);

        root.addView(titleBar);

        // ── Status Cards Row 1 ──
        LinearLayout row1 = new LinearLayout(this);
        row1.setOrientation(LinearLayout.HORIZONTAL);
        row1.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        // Reporter Status Card
        LinearLayout reporterCard = createPremiumCard("DAEMON", 0xFF00FF88);
        reporterStatusView = getCardValueView(reporterCard);
        row1.addView(reporterCard, createCardParams());

        addSpacer(row1, dp(12));

        // Server Card
        LinearLayout serverCard = createPremiumCard("UPSTREAM", 0xFFB300FF);
        serverUrlView = getCardValueView(serverCard);
        row1.addView(serverCard, createCardParams());

        addSpacer(row1, dp(12));

        // Ping Interval Card
        LinearLayout intervalCard = createPremiumCard("BEACON", 0xFFFF007F);
        pingIntervalView = getCardValueView(intervalCard);
        row1.addView(intervalCard, createCardParams());

        root.addView(row1);
        addVerticalSpacer(root, dp(12));

        // ── Status Cards Row 2 ──
        LinearLayout row2 = new LinearLayout(this);
        row2.setOrientation(LinearLayout.HORIZONTAL);
        row2.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        // Location Flag Card
        LinearLayout locCard = createPremiumCard("GPS_LINK", 0xFF00FFFF);
        locationFlagView = getCardValueView(locCard);
        row2.addView(locCard, createCardParams());

        addSpacer(row2, dp(12));

        // GPS Coords Card
        LinearLayout coordsCard = createPremiumCard("COORDINATES", 0xFF00FFFF);
        coordsView = getCardValueView(coordsCard);
        row2.addView(coordsCard, createCardParams());

        root.addView(row2);
        addVerticalSpacer(root, dp(12));

        // ── Disable Flag Card (full width) ──
        LinearLayout disableCard = createPremiumCard("OVERRIDE_LOCK", 0xFFFF2222);
        disableFlagView = getCardValueView(disableCard);
        root.addView(disableCard);
        addVerticalSpacer(root, dp(20));

        // ── Log Header ──
        LinearLayout logHeader = new LinearLayout(this);
        logHeader.setOrientation(LinearLayout.HORIZONTAL);
        logHeader.setGravity(Gravity.CENTER_VERTICAL);
        logHeader.setPadding(dp(4), 0, dp(4), dp(8));

        logTitle = new TextView(this);
        logTitle.setText("> /var/log/syslog --tail");
        logTitle.setTextColor(0xFF8899AA);
        logTitle.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        logTitle.setTypeface(Typeface.create("monospace", Typeface.BOLD));
        logHeader.addView(logTitle);

        root.addView(logHeader);

        // ── Log Container (Glassmorphism) ──
        GradientDrawable logBorder = new GradientDrawable();
        logBorder.setColor(0x880A0F16); // Semi-transparent black
        logBorder.setStroke(dp(1), 0x5500FFFF); // Subtle neon cyan border
        logBorder.setCornerRadius(dp(12));

        logScrollView = new ScrollView(this);
        logScrollView.setBackground(logBorder);
        logScrollView.setPadding(dp(16), dp(16), dp(16), dp(16));
        logScrollView.setFillViewport(true);
        LinearLayout.LayoutParams logScrollParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1.0f);
        logScrollView.setLayoutParams(logScrollParams);
        logScrollView.setOnTouchListener(new View.OnTouchListener() {
            @Override
            public boolean onTouch(View v, MotionEvent event) {
                isUserScrolling = true;
                if (event.getAction() == MotionEvent.ACTION_UP || event.getAction() == MotionEvent.ACTION_CANCEL) {
                    handler.postDelayed(new Runnable() {
                        @Override
                        public void run() {
                            isUserScrolling = false;
                        }
                    }, 1000);
                }
                return false;
            }
        });

        logEntriesContainer = new LinearLayout(this);
        logEntriesContainer.setOrientation(LinearLayout.VERTICAL);
        logEntriesContainer.setLayoutParams(new ScrollView.LayoutParams(
            ScrollView.LayoutParams.MATCH_PARENT, ScrollView.LayoutParams.WRAP_CONTENT));
        logScrollView.addView(logEntriesContainer);
        root.addView(logScrollView);

        setContentView(root);

        // ── Periodic Refresh ──
        handler = new Handler(Looper.getMainLooper());
        refreshRunnable = new Runnable() {
            @Override
            public void run() {
                refreshAll();
                handler.postDelayed(this, 1500); // Faster UI refresh (1.5s)
            }
        };
    }

    @Override
    protected void onResume() {
        super.onResume();
        lastLogSize = 0;
        logEntriesContainer.removeAllViews();
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
        refreshPingInterval();
        refreshLog();

        String ts = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date());
        lastUpdateView.setText("⟳ " + ts);
    }

    private void refreshReporterStatus() {
        String foundPid = null;
        try {
            Process process = Runtime.getRuntime().exec("getprop init.svc.system_telemetry_service");
            BufferedReader reader = new BufferedReader(new java.io.InputStreamReader(process.getInputStream()));
            String status = reader.readLine();
            reader.close();
            process.waitFor();
            
            if ("running".equals(status)) {
                foundPid = "system_telemetry_service"; // Android doesn't expose the PID easily, but we know it's running via init
            }
        } catch (Exception ignored) {}

        if (foundPid != null) {
            reporterStatusView.setText("RUNNING (PID " + foundPid + ")");
            reporterStatusView.setTextColor(ACCENT_GREEN);
        } else {
            File logFile = new File(ALT_LOG_FILE);
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

    private void refreshPingInterval() {
        String intervalValue = readFileOneLine(PING_INTERVAL_FILE);
        if (intervalValue == null) {
            pingIntervalView.setText("NO ping_interval.txt");
            pingIntervalView.setTextColor(TEXT_SECONDARY);
            return;
        }

        if (!intervalValue.isEmpty()) {
            try {
                int interval = Integer.parseInt(intervalValue.trim());
                if (interval >= 1) {
                    pingIntervalView.setText("Every " + interval + " sec");
                    pingIntervalView.setTextColor(TEXT_PRIMARY);
                    return;
                }
            } catch (NumberFormatException ignored) {
            }
        }

        pingIntervalView.setText("UNKNOWN interval");
        pingIntervalView.setTextColor(TEXT_SECONDARY);
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

    private LinkedList<String> tailLogLines(File logFile, int maxLines) {
        LinkedList<String> lines = new LinkedList<>();
        try (RandomAccessFile raf = new RandomAccessFile(logFile, "r")) {
            long fileLength = raf.length();
            if (fileLength == 0) return lines;

            long pointer = fileLength - 1;
            StringBuilder line = new StringBuilder();
            while (pointer >= 0 && lines.size() < maxLines) {
                raf.seek(pointer);
                int c = raf.read();
                if (c == '\n') {
                    if (line.length() > 0) {
                        line.reverse();
                        lines.addFirst(line.toString());
                        line.setLength(0);
                    }
                } else if (c != '\r') {
                    line.append((char) c);
                }
                pointer--;
            }
            if (line.length() > 0 && lines.size() < maxLines) {
                line.reverse();
                lines.addFirst(line.toString());
            }
        } catch (Exception e) {
            // Fall back to full file read if tailing fails.
        }
        return lines;
    }

    private void refreshLog() {
        if (isUserScrolling) return;

        File logFile = new File(ALT_LOG_FILE);
        if (!logFile.exists()) {
            logTitle.setText("reporter.log — live tail");
            showLogPlaceholder("(no reporter log found)");
            return;
        }

        logTitle.setText(logFile.getName() + " — live tail");
        long currentSize = logFile.length();
        long currentModified = logFile.lastModified();

        if (currentSize == lastLogSize && currentModified == lastLogModified) return;

        if (currentSize < lastLogSize || lastLogSize == 0) {
            // File truncated or first run: read last 500 lines using tailing
            LinkedList<String> lines = tailLogLines(logFile, MAX_LOG_LINES);
            if (lines.isEmpty()) {
                showLogPlaceholder("(no reporter log found)");
                lastLogSize = currentSize;
                lastLogModified = currentModified;
                return;
            }

            logEntriesContainer.removeAllViews();
            for (String line : lines) {
                addLogEntry(line);
            }
            logScrollView.post(new Runnable() {
                @Override
                public void run() {
                    logScrollView.fullScroll(View.FOCUS_DOWN);
                }
            });
        } else {
            // Incremental append
            try {
                RandomAccessFile raf = new RandomAccessFile(logFile, "r");
                raf.seek(lastLogSize);
                long bytesToRead = currentSize - lastLogSize;
                if (bytesToRead > 1024 * 1024) bytesToRead = 1024 * 1024; // Limit to 1MB per read
                byte[] bytes = new byte[(int) bytesToRead];
                raf.readFully(bytes);
                raf.close();

                String newContent = new String(bytes);
                String[] lines = newContent.split("\n");

                boolean wasAtBottom = isAtBottom();
                for (String line : lines) {
                    addLogEntry(line);
                }

                if (wasAtBottom) {
                    logScrollView.post(new Runnable() {
                        @Override
                        public void run() {
                            logScrollView.fullScroll(View.FOCUS_DOWN);
                        }
                    });
                }
            } catch (Exception e) {
                // Ignore parsing errors on incremental
            }
        }

        lastLogSize = currentSize;
        lastLogModified = currentModified;
    }

    private int pickLineColor(String line) {
        String lowerLine = line.toLowerCase();
        
        // Critical Errors (Red)
        if (lowerLine.contains("error") || lowerLine.contains("failed") || lowerLine.contains("denied") || lowerLine.contains("exception")) {
            return LOG_ERR;
        }
        
        // WebSocket Activity (Purple)
        if (lowerLine.contains("ws connection") || lowerLine.contains("ws command") || lowerLine.contains("report sent successfully via ws")) {
            return LOG_WS;
        }
        
        // C2 Tasks & Payloads (Orange)
        if (lowerLine.contains("queued c2 tasks") || lowerLine.contains("json payload:") || lowerLine.contains("sending report (")) {
            return LOG_TASK;
        }
        
        // Shell Commands (Blue)
        if (lowerLine.contains("executing command:")) {
            return LOG_CMD;
        }
        
        // Info/Heartbeat (Green)
        if (lowerLine.contains("starting report") || lowerLine.contains("report finished") || lowerLine.contains("ping interval updated") || lowerLine.contains("location tracking set")) {
            return LOG_INFO;
        }
        
        return LOG_DIM;
    }

    private void showLogPlaceholder(String text) {
        logEntriesContainer.removeAllViews();
        TextView placeholder = new TextView(this);
        placeholder.setText(text);
        placeholder.setTextColor(TEXT_SECONDARY);
        placeholder.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        placeholder.setTypeface(Typeface.create("monospace", Typeface.NORMAL));
        placeholder.setPadding(dp(8), dp(8), dp(8), dp(8));
        logEntriesContainer.addView(placeholder);
    }

    private void addLogEntry(String rawLine) {
        if (rawLine == null || rawLine.isEmpty()) {
            return;
        }

        if (rawLine.contains("── Report cycle start ──")) {
            addLogDivider();
            return;
        }

        String timestamp = null;
        String message = rawLine;
        if (rawLine.startsWith("[") && rawLine.contains("] ")) {
            int timestampEnd = rawLine.indexOf("] ");
            if (timestampEnd > 1) {
                timestamp = rawLine.substring(1, timestampEnd);
                message = rawLine.substring(timestampEnd + 2);
            }
        }

        int messageColor = pickLineColor(message);
        LinearLayout entryCard = new LinearLayout(this);
        entryCard.setOrientation(LinearLayout.VERTICAL);
        entryCard.setPadding(dp(12), dp(10), dp(12), dp(10));

        GradientDrawable entryBg = new GradientDrawable(GradientDrawable.Orientation.TL_BR, new int[]{0xAA121722, 0xAA0B1018});
        entryBg.setCornerRadius(dp(12));
        entryBg.setStroke(dp(1), colorWithAlpha(messageColor, 110));
        entryCard.setBackground(entryBg);

        LinearLayout.LayoutParams entryParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        entryParams.setMargins(0, 0, 0, dp(8));
        entryCard.setLayoutParams(entryParams);

        LinearLayout headerRow = new LinearLayout(this);
        headerRow.setOrientation(LinearLayout.HORIZONTAL);
        headerRow.setGravity(Gravity.CENTER_VERTICAL);

        View accentBar = new View(this);
        GradientDrawable accentBg = new GradientDrawable();
        accentBg.setShape(GradientDrawable.RECTANGLE);
        accentBg.setCornerRadius(dp(2));
        accentBg.setColor(messageColor);
        accentBar.setBackground(accentBg);
        accentBar.setLayoutParams(new LinearLayout.LayoutParams(dp(6), dp(28)));
        headerRow.addView(accentBar);

        LinearLayout textColumn = new LinearLayout(this);
        textColumn.setOrientation(LinearLayout.VERTICAL);
        textColumn.setLayoutParams(new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1.0f));
        textColumn.setPadding(dp(10), 0, 0, 0);

        if (timestamp != null) {
            TextView timestampView = new TextView(this);
            timestampView.setText(timestamp);
            timestampView.setTextColor(TEXT_SECONDARY);
            timestampView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10);
            timestampView.setTypeface(Typeface.create("monospace", Typeface.BOLD));
            timestampView.setLetterSpacing(0.04f);
            textColumn.addView(timestampView);
        }

        TextView messageView = new TextView(this);
        messageView.setText(message);
        messageView.setTextColor(messageColor);
        messageView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        messageView.setTypeface(Typeface.create("monospace", Typeface.NORMAL));
        messageView.setLineSpacing(dp(3), 1.05f);
        messageView.setIncludeFontPadding(false);
        messageView.setBreakStrategy(Layout.BREAK_STRATEGY_HIGH_QUALITY);
        messageView.setHyphenationFrequency(Layout.HYPHENATION_FREQUENCY_NORMAL);
        messageView.setPadding(0, dp(2), 0, 0);
        textColumn.addView(messageView);

        headerRow.addView(textColumn);
        entryCard.addView(headerRow);
        logEntriesContainer.addView(entryCard);
    }

    private void addLogDivider() {
        TextView divider = new TextView(this);
        divider.setText("REPORT CYCLE");
        divider.setTextColor(ACCENT_BLUE);
        divider.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10);
        divider.setTypeface(Typeface.create("sans-serif-medium", Typeface.BOLD));
        divider.setLetterSpacing(0.12f);
        divider.setGravity(Gravity.CENTER_HORIZONTAL);
        divider.setPadding(dp(0), dp(10), dp(0), dp(6));
        logEntriesContainer.addView(divider);

        View dividerLine = new View(this);
        GradientDrawable lineBg = new GradientDrawable();
        lineBg.setShape(GradientDrawable.RECTANGLE);
        lineBg.setColor(0x3358A6FF);
        dividerLine.setBackground(lineBg);
        LinearLayout.LayoutParams lineParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(1));
        lineParams.setMargins(dp(18), 0, dp(18), dp(10));
        dividerLine.setLayoutParams(lineParams);
        logEntriesContainer.addView(dividerLine);
    }

    private int colorWithAlpha(int color, int alpha) {
        return (color & 0x00FFFFFF) | ((alpha & 0xFF) << 24);
    }

    private boolean isAtBottom() {
        if (logScrollView == null || logScrollView.getChildCount() == 0) {
            return true;
        }
        int childHeight = logScrollView.getChildAt(0).getHeight();
        int scrollY = logScrollView.getScrollY();
        int height = logScrollView.getHeight();
        return (childHeight - (scrollY + height)) <= dp(10);
    }

    // ── UI Helpers ──

    private LinearLayout createPremiumCard(String label, int accentColor) {
        GradientDrawable cardBg = new GradientDrawable(GradientDrawable.Orientation.TL_BR, new int[]{0x99111822, 0x99050A10});
        cardBg.setStroke(dp(1), accentColor & 0x66FFFFFF); // 40% opacity accent border
        cardBg.setCornerRadius(dp(12));

        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setBackground(cardBg);
        card.setPadding(dp(14), dp(12), dp(14), dp(12));

        // Label with glowing accent dot
        LinearLayout labelRow = new LinearLayout(this);
        labelRow.setOrientation(LinearLayout.HORIZONTAL);
        labelRow.setGravity(Gravity.CENTER_VERTICAL);

        GradientDrawable dotBg = new GradientDrawable();
        dotBg.setShape(GradientDrawable.OVAL);
        dotBg.setColor(accentColor);
        
        View dot = new View(this);
        dot.setBackground(dotBg);
        dot.setLayoutParams(new LinearLayout.LayoutParams(dp(6), dp(6)));
        
        // Slight glow on the dot
        android.animation.ObjectAnimator dotPulse = android.animation.ObjectAnimator.ofFloat(dot, "alpha", 1f, 0.5f);
        dotPulse.setDuration(1500);
        dotPulse.setRepeatCount(android.animation.ValueAnimator.INFINITE);
        dotPulse.setRepeatMode(android.animation.ValueAnimator.REVERSE);
        dotPulse.start();

        labelRow.addView(dot);

        TextView labelView = new TextView(this);
        labelView.setText("  " + label);
        labelView.setTextColor(0xFF8899AA);
        labelView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10);
        labelView.setTypeface(Typeface.create("sans-serif-medium", Typeface.BOLD));
        labelView.setLetterSpacing(0.05f);
        labelRow.addView(labelView);

        card.addView(labelRow);

        // Value
        TextView valueView = new TextView(this);
        valueView.setText("...");
        valueView.setTextColor(0xFFFFFFFF);
        valueView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        valueView.setTypeface(Typeface.create("monospace", Typeface.BOLD));
        valueView.setPadding(0, dp(8), 0, 0);
        valueView.setTag("card_value");
        valueView.setShadowLayer(4f, 0f, 0f, accentColor & 0xAAFFFFFF);
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
