package com.stolworthy.fireside;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;

/**
 * Holds the app process in the foreground while audio is playing, so the WebView
 * keeps running past screen lock. It does not touch the audio itself: the WebView
 * owns that. Started from window.NativeMedia.start(), stopped on pause.
 */
public class MediaPlaybackService extends Service {

    private static final String CHANNEL = "playback";
    private static final int ID = 1;

    public static void start(Context c) {
        Intent i = new Intent(c, MediaPlaybackService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            c.startForegroundService(i);
        } else {
            c.startService(i);
        }
    }

    public static void stop(Context c) {
        c.stopService(new Intent(c, MediaPlaybackService.class));
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startForeground(ID, buildNotification(this));
        // Not sticky: if the process dies, the WebView is gone and the service is pointless.
        return START_NOT_STICKY;
    }

    private static Notification buildNotification(Context c) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationManager nm = c.getSystemService(NotificationManager.class);
            if (nm != null && nm.getNotificationChannel(CHANNEL) == null) {
                NotificationChannel ch = new NotificationChannel(
                        CHANNEL, "Playback", NotificationManager.IMPORTANCE_LOW);
                ch.setShowBadge(false);
                nm.createNotificationChannel(ch);
            }
        }
        PendingIntent open = PendingIntent.getActivity(
                c, 0, new Intent(c, MainActivity.class),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        Notification.Builder b = (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                ? new Notification.Builder(c, CHANNEL)
                : new Notification.Builder(c);
        return b.setContentTitle("Fireside")
                .setContentText("Playing")
                .setSmallIcon(android.R.drawable.ic_media_play)
                .setOngoing(true)
                .setContentIntent(open)
                .build();
    }
}