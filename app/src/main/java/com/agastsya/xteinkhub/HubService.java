package com.agastsya.xteinkhub;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.Process;
import android.util.Log;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/** Runs the Python hub (OPDS + KOReader sync + dashboard) in its own process. */
public class HubService extends Service {
    private static boolean running = false;
    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        goForeground();
        if (!running) {
            running = true;
            acquireLocks();
            if (!Python.isStarted()) Python.start(new AndroidPlatform(this));
            final String home = getFilesDir().getAbsolutePath() + "/hub";
            new Thread(() -> {
                try {
                    Python.getInstance().getModule("hub").callAttr("serve", home);
                } catch (Throwable t) {
                    Log.e("XteinkHub", "server stopped", t);
                }
            }, "xteink-hub").start();
        }
        return START_STICKY;
    }

    private void goForeground() {
        Notification.Builder nb;
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm = getSystemService(NotificationManager.class);
            nm.createNotificationChannel(new NotificationChannel("hub", "Xteink Hub server",
                    NotificationManager.IMPORTANCE_LOW));
            nb = new Notification.Builder(this, "hub");
        } else {
            nb = new Notification.Builder(this);
        }
        PendingIntent pi = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification n = nb.setContentTitle("Xteink Hub is running")
                .setContentText("OPDS + sync server on port 8080 · tap to open")
                .setSmallIcon(R.drawable.ic_stat)
                .setContentIntent(pi)
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(1, n);
        }
    }

    @SuppressWarnings("deprecation")
    private void acquireLocks() {
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "XteinkHub::server");
            wakeLock.acquire();
            WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            int mode = Build.VERSION.SDK_INT >= 29 ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY
                                                   : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
            wifiLock = wm.createWifiLock(mode, "XteinkHub::wifi");
            wifiLock.acquire();
        } catch (Exception e) {
            Log.w("XteinkHub", "locks", e);
        }
    }

    @Override
    public void onDestroy() {
        try { if (wakeLock != null && wakeLock.isHeld()) wakeLock.release(); } catch (Exception ignored) {}
        try { if (wifiLock != null && wifiLock.isHeld()) wifiLock.release(); } catch (Exception ignored) {}
        super.onDestroy();
        Process.killProcess(Process.myPid());   // stops the Python server cleanly
    }
}
