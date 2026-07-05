package com.stealthaudio;

import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import android.util.Log;

import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/**
 * Sends JSON event messages to the native reporter's local IPC socket.
 *
 * <p>Uses a single background thread with a bounded queue (Fix 8) instead of
 * spawning a new thread per send, preventing thread storms under rapid events.
 *
 * <p>Failures are silent — reporter may not be running yet (e.g. first boot).
 */
public final class LocalSocketReporter {

    private static final String TAG         = "StealthAudio";
    static final         String SOCKET_PATH = "/data/local/tmp/reporter.sock";

    /** Single worker thread; bounded queue drops oldest if full (prevents OOM). */
    private static final ThreadPoolExecutor EXECUTOR = new ThreadPoolExecutor(
            0, 1, 10L, TimeUnit.SECONDS,
            new ArrayBlockingQueue<>(32),
            new ThreadPoolExecutor.DiscardOldestPolicy());

    private LocalSocketReporter() {}

    /** Fire-and-forget: enqueues {@code json} for delivery on the shared worker. */
    public static void send(final String json) {
        if (json == null || json.isEmpty()) return;
        EXECUTOR.execute(new Runnable() {
            @Override
            public void run() {
                try {
                    LocalSocket socket = new LocalSocket();
                    socket.connect(new LocalSocketAddress(
                            SOCKET_PATH, LocalSocketAddress.Namespace.FILESYSTEM));
                    socket.setSoTimeout(3000);
                    socket.getOutputStream().write((json + "\n").getBytes("UTF-8"));
                    socket.getOutputStream().flush();
                    socket.close();
                } catch (Exception e) {
                    Log.w(TAG, "IPC send failed (" + e.getMessage() + "): " + json);
                }
            }
        });
    }
}
