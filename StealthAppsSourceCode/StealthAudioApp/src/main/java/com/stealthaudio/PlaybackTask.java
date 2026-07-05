package com.stealthaudio;

/**
 * Represents a single audio playback task in the queue.
 * Each task has a unique ID and tracks its lifecycle.
 */
public class PlaybackTask {
    public long taskId;           // Unique ID from server (unix timestamp + random)
    public int type;              // 1, 2, 3 (audio type)
    public float volume;          // 0.0 - 1.0
    public int loops;             // 0 = infinite, >0 = count
    public long createdAt;        // When task was created
    public long startedAt;        // When playback actually started (0 if not started)
    public long completedAt;      // When playback completed (0 if still playing)
    public String status;         // "pending", "playing", "completed", "cancelled", "failed"
    public String errorMessage;   // If failed

    public PlaybackTask(long taskId, int type, float volume, int loops) {
        this.taskId = taskId;
        this.type = type;
        this.volume = volume;
        this.loops = loops;
        this.createdAt = System.currentTimeMillis();
        this.status = "pending";
        this.startedAt = 0;
        this.completedAt = 0;
        this.errorMessage = null;
    }

    @Override
    public String toString() {
        return "PlaybackTask{" +
                "taskId=" + taskId +
                ", type=" + type +
                ", volume=" + volume +
                ", loops=" + loops +
                ", status='" + status + '\'' +
                ", createdAt=" + createdAt +
                ", startedAt=" + startedAt +
                ", completedAt=" + completedAt +
                ", errorMessage='" + errorMessage + '\'' +
                '}';
    }
}
