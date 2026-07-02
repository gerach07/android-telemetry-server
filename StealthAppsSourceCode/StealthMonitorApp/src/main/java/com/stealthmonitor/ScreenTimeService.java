package com.stealthmonitor;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.app.usage.UsageStats;
import android.app.usage.UsageStatsManager;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.util.Log;

import androidx.annotation.NonNull;
import androidx.annotation.Nullable;
import androidx.annotation.VisibleForTesting;
import androidx.core.app.NotificationCompat;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileWriter;
import java.io.IOException;
import java.io.InputStreamReader;
import java.util.Calendar;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Persistent foreground service that records today's total screen-on time (in minutes)
 * to a file every {@link #UPDATE_INTERVAL_MS} milliseconds.
 *
 * <p><b>Threading model:</b> all UsageStats queries, shell execution, and file I/O
 * run on a single dedicated {@link HandlerThread} ("ScreenTimeWorker"). The main thread
 * is never blocked.
 *
 * <p><b>Data source priority:</b>
 * <ol>
 *   <li>{@link UsageStatsManager#queryAndAggregateUsageStats} — accurate, no root needed,
 *       requires {@code android.permission.PACKAGE_USAGE_STATS}.</li>
 *   <li>{@code dumpsys batterystats} fallback — coarse estimate, root/system UID only.</li>
 * </ol>
 *
 * <p><b>Output:</b> written atomically via tmp-then-rename to prevent partial reads.
 * Falls back to {@link Context#getFilesDir()} if the preferred system path is not writable
 * (i.e. on non-rooted consumer devices).
 */
public final class ScreenTimeService extends Service {

    private static final String TAG = "StealthMonitor/ScreenTime";

    /** Preferred output path — writable only with root / system UID. */
    @VisibleForTesting
    static final String PREFERRED_OUT_FILE = "/data/local/tmp/screen_time_minutes.txt";

    /** Fallback filename under {@link Context#getFilesDir()} on non-rooted devices. */
    private static final String FALLBACK_OUT_FILENAME = "screen_time_minutes.txt";

    /** How often the worker fires. 5 minutes balances freshness against battery drain. */
    private static final long UPDATE_INTERVAL_MS = 5 * 60_000L;

    private static final String NOTIF_CHANNEL_ID = "screen_time_channel";
    private static final int    NOTIF_ID          = 1001;

    /**
     * Pre-compiled at class-load time — Pattern.compile() is expensive; compiling
     * inside a method called every 5 minutes would allocate needlessly.
     */
    private static final Pattern H_PATTERN = Pattern.compile("(\\d+)h");
    private static final Pattern M_PATTERN = Pattern.compile("(\\d+)m");
    private static final Pattern S_PATTERN = Pattern.compile("(\\d+)s");

    private HandlerThread workerThread;
    private Handler       workerHandler;

    /**
     * The periodic task. Captured as a field so it can be reliably cancelled
     * via {@link Handler#removeCallbacks(Runnable)} in {@link #onDestroy()}.
     */
    private final Runnable updateTask = new Runnable() {
        @Override
        public void run() {
            final int minutes = computeTodayScreenOnMinutes(ScreenTimeService.this);
            writeScreenTimeMinutes(ScreenTimeService.this, minutes);
            scheduleNext();
        }
    };

    // -------------------------------------------------------------------------
    // Service lifecycle
    // -------------------------------------------------------------------------

    @Override
    public void onCreate() {
        super.onCreate();
        // FIX R3-7: create the notification channel once here, not inside buildNotification().
        // If the channel is created on every onStartCommand() call and NotificationManager is
        // temporarily null during a START_STICKY restart, the channel never registers and the
        // foreground notification silently drops — risking an ANR on API 26+.
        ensureNotificationChannel();
        workerThread = new HandlerThread("ScreenTimeWorker");
        workerThread.start();
        workerHandler = new Handler(workerThread.getLooper());
        Log.d(TAG, "Service created");
    }

    @Override
    public int onStartCommand(@Nullable final Intent intent, final int flags, final int startId) {
        // startForeground() must be called within 5 s on API 26+ or the OS raises an ANR.
        startForeground(NOTIF_ID, buildNotification());

        // Remove any already-queued task, then run one immediately.
        workerHandler.removeCallbacks(updateTask);
        workerHandler.post(updateTask);

        // START_STICKY: if killed, the OS restarts us with a null intent — safe because
        // onStartCommand handles null intent gracefully and re-arms the worker.
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        workerHandler.removeCallbacks(updateTask);
        // quitSafely() drains pending messages before stopping — avoids data loss on shutdown.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.JELLY_BEAN_MR2) {
            workerThread.quitSafely();
        } else {
            workerThread.quit();
        }
        super.onDestroy();
    }

    @Nullable
    @Override
    public IBinder onBind(@NonNull final Intent intent) {
        return null; // Not a bound service.
    }

    // -------------------------------------------------------------------------
    // Scheduling
    // -------------------------------------------------------------------------

    private void scheduleNext() {
        workerHandler.postDelayed(updateTask, UPDATE_INTERVAL_MS);
    }

    // -------------------------------------------------------------------------
    // Screen-time computation
    // -------------------------------------------------------------------------

    /**
     * Returns today's total screen-on time in whole minutes.
     * Tries {@link UsageStatsManager} first; falls back to {@code dumpsys batterystats}.
     */
    @VisibleForTesting
    static int computeTodayScreenOnMinutes(@NonNull final Context context) {
        final int fromUsageStats = queryUsageStatsMinutes(context);
        return fromUsageStats > 0 ? fromUsageStats : parseBatterystatsMinutes();
    }

    /**
     * Queries {@link UsageStatsManager} for aggregated foreground / visible time today.
     *
     * <p>API behaviour differences:
     * <ul>
     *   <li>API 21–28: {@link UsageStats#getTotalTimeInForeground()} — counts time the app
     *       window is in the foreground.</li>
     *   <li>API 29+: {@link UsageStats#getTotalTimeVisible()} — additionally counts
     *       picture-in-picture and other visible-but-not-focused windows.</li>
     * </ul>
     */
    private static int queryUsageStatsMinutes(@NonNull final Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.LOLLIPOP) {
            return 0; // UsageStatsManager unavailable below API 21.
        }
        try {
            final UsageStatsManager usm =
                    (UsageStatsManager) context.getSystemService(Context.USAGE_STATS_SERVICE);
            if (usm == null) return 0;

            final long start = todayMidnightMillis();
            final long end   = System.currentTimeMillis();

            final Map<String, UsageStats> stats = usm.queryAndAggregateUsageStats(start, end);
            if (stats == null || stats.isEmpty()) return 0;

            long totalMs = 0L;
            for (final UsageStats s : stats.values()) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    totalMs += s.getTotalTimeVisible();
                } else {
                    totalMs += s.getTotalTimeInForeground();
                }
            }
            return (int) (totalMs / 60_000L);

        } catch (final Exception e) {
            Log.w(TAG, "UsageStats query failed: " + e.getMessage());
        }
        return 0;
    }

    /** Epoch-ms for 00:00:00.000 today in the device's local timezone. */
    private static long todayMidnightMillis() {
        final Calendar cal = Calendar.getInstance();
        cal.set(Calendar.HOUR_OF_DAY, 0);
        cal.set(Calendar.MINUTE,      0);
        cal.set(Calendar.SECOND,      0);
        cal.set(Calendar.MILLISECOND, 0);
        return cal.getTimeInMillis();
    }

    /**
     * Fallback: executes {@code dumpsys batterystats} and parses the
     * "Estimated screen on time" line for a coarse screen-on estimate.
     *
     * <p>The reader loop exits as soon as the target line is found — this avoids
     * buffering the full (often multi-MB) batterystats output into memory.
     *
     * <p>{@code process.waitFor()} is called before {@code process.destroy()} so that
     * the subprocess can finish writing its output naturally and all descriptors are
     * flushed before the handle is released.
     */
    private static int parseBatterystatsMinutes() {
        Process process = null;
        try {
            process = Runtime.getRuntime().exec(new String[]{"dumpsys", "batterystats"});
            // FIX M-2: drain stderr on a separate thread to prevent the subprocess from
            // blocking if its stderr pipe buffer fills (~64 KB). Without this, the reader
            // loop below would hang forever waiting for stdout that never arrives because
            // the process is blocked waiting for stderr to be consumed.
            final Process proc = process;
            Thread stderrDrainer = new Thread(new Runnable() {
                @Override public void run() {
                    try {
                        byte[] buf = new byte[4096];
                        //noinspection StatementWithEmptyBody
                        while (proc.getErrorStream().read(buf) != -1) {}
                    } catch (Exception ignored) {}
                }
            }, "BattStatsStderr");
            stderrDrainer.setDaemon(true);
            stderrDrainer.start();

            try (final BufferedReader reader =
                         new BufferedReader(new InputStreamReader(process.getInputStream()))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    if (line.contains("Estimated screen on time:")) {
                        return parseDurationToMinutes(line);
                    }
                }
            }
        } catch (final IOException e) {
            Log.w(TAG, "batterystats parse failed: " + e.getMessage());
        } finally {
            if (process != null) {
                try {
                    process.waitFor(); // drain output and flush descriptors first
                } catch (final InterruptedException e) {
                    Thread.currentThread().interrupt(); // restore interrupt status
                } finally {
                    process.destroy(); // release OS handle regardless
                }
            }
        }
        return 0;
    }

    /**
     * Parses a duration string of the form {@code "... Xh Ym Zs ..."} into total minutes.
     * Seconds >= 30 are rounded up to the nearest minute.
     */
    @VisibleForTesting
    static int parseDurationToMinutes(@Nullable final String line) {
        if (line == null || line.isEmpty()) return 0;
        final int hours = extractGroup(line, H_PATTERN);
        final int mins  = extractGroup(line, M_PATTERN);
        final int secs  = extractGroup(line, S_PATTERN);
        return hours * 60 + mins + (secs >= 30 ? 1 : 0);
    }

    private static int extractGroup(@NonNull final String text, @NonNull final Pattern pattern) {
        final Matcher m = pattern.matcher(text);
        return m.find() ? Integer.parseInt(m.group(1)) : 0;
    }

    // -------------------------------------------------------------------------
    // File output
    // -------------------------------------------------------------------------

    /**
     * Writes {@code minutes} to disk atomically using a tmp-then-rename strategy.
     *
     * <p>A reader of the output file will either see the previous complete value or
     * the new complete value — never a partial write.
     *
     * <p>Path selection:
     * <ol>
     *   <li>Preferred: {@link #PREFERRED_OUT_FILE} — accessible to native daemons,
     *       requires root / system UID.</li>
     *   <li>Fallback: {@link Context#getFilesDir()}/{@link #FALLBACK_OUT_FILENAME} —
     *       always writable by this app, CE-storage protected.</li>
     * </ol>
     *
     * <p>Note: {@link File#setReadable(boolean, boolean)} has no effect on SELinux
     * labels on API 23+. World-readability on non-rooted devices depends on the
     * app's SELinux domain — this call is a best-effort hint only.
     */
    @VisibleForTesting
    static void writeScreenTimeMinutes(@NonNull final Context context, int minutes) {
        if (minutes < 0) minutes = 0;

        final File preferredOut = new File(PREFERRED_OUT_FILE);
        final File parentDir    = preferredOut.getParentFile();
        final File out = (parentDir != null && parentDir.canWrite())
                ? preferredOut
                : new File(context.getFilesDir(), FALLBACK_OUT_FILENAME);

        final File tmp = new File(out.getAbsolutePath() + ".tmp");

        try (final FileWriter writer = new FileWriter(tmp, false /* overwrite */)) {
            writer.write(String.valueOf(minutes));
            writer.write('\n');
        } catch (final IOException e) {
            Log.e(TAG, "Failed to write tmp file: " + tmp.getAbsolutePath(), e);
            return;
        }

        if (!tmp.renameTo(out)) {
            Log.e(TAG, "Atomic rename failed: " + tmp.getAbsolutePath() + " → " + out.getAbsolutePath());
            //noinspection ResultOfMethodCallIgnored
            tmp.delete(); // clean up orphaned tmp
            return;
        }

        //noinspection ResultOfMethodCallIgnored
        out.setReadable(true, false); // best-effort; SELinux may override on API 23+

        Log.d(TAG, "Screen time written: " + minutes + " min → " + out.getAbsolutePath());
    }

    // -------------------------------------------------------------------------
    // Foreground notification
    // -------------------------------------------------------------------------

    /** Creates the notification channel exactly once — safe to call multiple times. */
    private void ensureNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            final NotificationChannel channel = new NotificationChannel(
                    NOTIF_CHANNEL_ID,
                    "System Services",
                    NotificationManager.IMPORTANCE_MIN
            );
            channel.setShowBadge(false);
            final NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) nm.createNotificationChannel(channel);
        }
    }

    @NonNull
    private Notification buildNotification() {
        // FIX R3-7: channel is guaranteed to exist because ensureNotificationChannel() ran
        // in onCreate() — no need to (re-)create it here on every startForeground() call.
        return new NotificationCompat.Builder(this, NOTIF_CHANNEL_ID)
                .setContentTitle("System Services")
                .setContentText("Running background maintenance")
                .setSmallIcon(android.R.drawable.ic_menu_info_details)
                .setPriority(NotificationCompat.PRIORITY_MIN)
                .setOngoing(true)
                .build();
    }
}
