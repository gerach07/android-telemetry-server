package com.stealthaudio;

import android.content.Context;
import android.util.Log;

import java.util.ArrayList;
import java.util.LinkedList;
import java.util.List;
import java.util.Queue;

/**
 * Manages playback task queue with a consumer thread.
 * Ensures sequential playback of audio tasks instead of cancelling previous ones.
 */
public class PlaybackQueue {
    private static final String TAG = "PlaybackQueue";
    private static final int MAX_QUEUE_SIZE = 50;
    private static final Object QUEUE_LOCK = new Object();
    
    private static final Queue<PlaybackTask> taskQueue = new LinkedList<>();
    private static volatile PlaybackTask currentTask = null;
    private static volatile Thread consumerThread = null;
    private static volatile boolean isRunning = false;

    /**
     * Enqueue a new audio playback task.
     * Returns the task or null if queue is full.
     */
    /**
     * Enqueue a new audio playback task.
     * If caller provides a non-zero taskId it will be used; otherwise one is generated.
     */
    public static PlaybackTask enqueueTask(long externalTaskId, int type, float volume, int loops) {
        synchronized (QUEUE_LOCK) {
            if (taskQueue.size() >= MAX_QUEUE_SIZE) {
                Log.w(TAG, "Queue full (" + MAX_QUEUE_SIZE + " tasks), rejecting new task");
                return null;
            }

            long taskId = externalTaskId != 0 ? externalTaskId : (System.currentTimeMillis() * 1000 + (int)(Math.random() * 1000));
            PlaybackTask task = new PlaybackTask(taskId, type, volume, loops);
            taskQueue.offer(task);

            Log.d(TAG, "Task " + taskId + " enqueued (type=" + type + ", volume=" + volume + ", loops=" + loops + "). Queue size: " + taskQueue.size());

            // Wake consumer if waiting
            QUEUE_LOCK.notifyAll();

            return task;
        }
    }

    /**
     * Get the next task from queue (removes from queue).
     */
    private static PlaybackTask dequeueTask() {
        synchronized (QUEUE_LOCK) {
            return taskQueue.poll();
        }
    }

    /**
     * Get the currently playing task.
     */
    public static PlaybackTask getCurrentTask() {
        synchronized (QUEUE_LOCK) {
            return currentTask;
        }
    }

    /**
     * Get queue size (pending tasks, not including current).
     */
    public static int getQueueSize() {
        synchronized (QUEUE_LOCK) {
            return taskQueue.size();
        }
    }

    /**
     * Get queue status snapshot.
     */
    public static QueueStatus getQueueStatus() {
        synchronized (QUEUE_LOCK) {
            List<PlaybackTask> pending = new ArrayList<>(taskQueue);
            return new QueueStatus(currentTask, pending, taskQueue.size(), isRunning);
        }
    }

    /**
     * Start the queue consumer thread if not already running.
     */
    public static void startConsumer(Context context) {
        synchronized (QUEUE_LOCK) {
            if (consumerThread != null && consumerThread.isAlive()) {
                Log.d(TAG, "Consumer already running");
                return;
            }
            
            isRunning = true;
            consumerThread = new Thread(new Runnable() {
                @Override
                public void run() {
                    runConsumer(context);
                }
            });
            consumerThread.setName("PlaybackQueueConsumer");
            consumerThread.setDaemon(false);
            consumerThread.start();
            
            Log.d(TAG, "Consumer thread started");
        }
    }

    /**
     * Stop the queue consumer thread.
     */
    public static void stopConsumer() {
        synchronized (QUEUE_LOCK) {
            isRunning = false;
            
            if (currentTask != null) {
                Log.d(TAG, "Stopping current playback (task " + currentTask.taskId + ")");
                StealthAudio.stopPlayback();
            }
            
            QUEUE_LOCK.notifyAll();
        }
    }

    /**
     * Main consumer loop - processes tasks sequentially.
     */
    private static void runConsumer(Context context) {
        Log.d(TAG, "Consumer thread running");
        
        while (isRunning) {
            synchronized (QUEUE_LOCK) {
                // Wait if queue empty or current task not done
                while ((taskQueue.isEmpty() || currentTask != null) && isRunning) {
                    try {
                        QUEUE_LOCK.wait(1000);  // Check every second
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        Log.d(TAG, "Consumer interrupted");
                        return;
                    }
                }
                
                if (!isRunning) break;
                
                currentTask = dequeueTask();
            }
            
            if (currentTask != null) {
                playTask(context, currentTask);
            }
        }
        
        Log.d(TAG, "Consumer thread exiting");
    }

    /**
     * Play a single task to completion.
     */
    private static void playTask(Context context, PlaybackTask task) {
        try {
            task.status = "playing";
            task.startedAt = System.currentTimeMillis();
            reportTaskStatus(context, task, "audio_task_started");
            
            Log.i(TAG, "Playing task " + task.taskId + " (type=" + task.type + 
                  ", volume=" + task.volume + ", loops=" + task.loops + ")");
            
            // Block until playback completes or interrupted
            StealthAudio.playSound(context, task.type, task.volume, task.loops);
            
            task.status = "completed";
            task.completedAt = System.currentTimeMillis();
            long duration = task.completedAt - task.startedAt;
            
            Log.i(TAG, "Task " + task.taskId + " completed in " + duration + "ms");
            reportTaskStatus(context, task, "audio_task_completed");
            
        } catch (Exception e) {
            task.status = "failed";
            task.errorMessage = e.getMessage();
            task.completedAt = System.currentTimeMillis();
            
            Log.e(TAG, "Task " + task.taskId + " failed: " + e.getMessage(), e);
            reportTaskStatus(context, task, "audio_task_failed");
            
        } finally {
            synchronized (QUEUE_LOCK) {
                currentTask = null;
                QUEUE_LOCK.notifyAll();  // Wake consumer for next task
            }
        }
    }

    /**
     * Cancel a specific task by ID.
     * If it's currently playing, stops playback.
     * If it's pending, removes from queue.
     */
    public static boolean cancelTask(long taskId) {
        synchronized (QUEUE_LOCK) {
            // If it's current task, stop playback
            if (currentTask != null && currentTask.taskId == taskId) {
                currentTask.status = "cancelled";
                currentTask.completedAt = System.currentTimeMillis();
                
                Log.d(TAG, "Cancelling current task " + taskId);
                reportTaskStatus(null, currentTask, "audio_task_cancelled");
                StealthAudio.stopPlayback();
                
                currentTask = null;
                QUEUE_LOCK.notifyAll();
                return true;
            }
            
            // Otherwise try to remove from queue
            for (PlaybackTask task : taskQueue) {
                if (task.taskId == taskId) {
                    task.status = "cancelled";
                    task.completedAt = System.currentTimeMillis();
                    taskQueue.remove(task);
                    
                    Log.d(TAG, "Cancelled pending task " + taskId);
                    reportTaskStatus(null, task, "audio_task_cancelled");
                    return true;
                }
            }
            
            Log.w(TAG, "Task " + taskId + " not found");
            return false;
        }
    }

    /**
     * Clear all pending tasks (but doesn't stop current playback).
     */
    public static void clearQueue() {
        synchronized (QUEUE_LOCK) {
            int cleared = taskQueue.size();
            taskQueue.clear();
            Log.d(TAG, "Cleared " + cleared + " pending tasks");
        }
    }

    /**
     * Clear all tasks including current playback.
     */
    public static void clearAll() {
        synchronized (QUEUE_LOCK) {
            if (currentTask != null) {
                currentTask.status = "cancelled";
                currentTask.completedAt = System.currentTimeMillis();
                reportTaskStatus(null, currentTask, "audio_task_cancelled");
                StealthAudio.stopPlayback();
                currentTask = null;
            }
            
            int cleared = taskQueue.size();
            for (PlaybackTask t : taskQueue) {
                t.status = "cancelled";
                reportTaskStatus(null, t, "audio_task_cancelled");
            }
            taskQueue.clear();
            
            QUEUE_LOCK.notifyAll();
            Log.d(TAG, "Cleared current + " + cleared + " pending tasks");
        }
    }

    /**
     * Report task status update via IPC to reporter.cpp
     */
    private static void reportTaskStatus(Context context, PlaybackTask task, String eventType) {
        try {
            String json = "{\"event\":\"" + eventType + "\"," +
                         "\"task_id\":" + task.taskId + "," +
                         "\"type\":" + task.type + "," +
                         "\"status\":\"" + task.status + "\"}";
            
            LocalSocketReporter.send(json);
            Log.d(TAG, "Reported: " + eventType + " for task " + task.taskId);
        } catch (Exception e) {
            Log.e(TAG, "Failed to report task status", e);
        }
    }

    /**
     * Public class to represent queue status.
     */
    public static class QueueStatus {
        public PlaybackTask currentTask;
        public List<PlaybackTask> pendingTasks;
        public int queueSize;
        public boolean isRunning;

        public QueueStatus(PlaybackTask current, List<PlaybackTask> pending, int size, boolean running) {
            this.currentTask = current;
            this.pendingTasks = pending;
            this.queueSize = size;
            this.isRunning = running;
        }

        @Override
        public String toString() {
            return "QueueStatus{" +
                    "currentTask=" + (currentTask != null ? currentTask.taskId : "none") +
                    ", pendingTasks=" + queueSize +
                    ", isRunning=" + isRunning +
                    '}';
        }
    }
}
