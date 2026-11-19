package com.agastsya.xteinkhub;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

public class MainActivity extends Activity {
    static final String URL = "http://127.0.0.1:8080/";
    static final int FILE_REQ = 1;
    static final String LOADING =
        "<html><body style='font-family:sans-serif;padding:40px 24px;color:#444'>"
        + "<h2>Starting Xteink Hub…</h2><p>The first start takes up to a minute.</p>"
        + "<p style='font-size:13px;color:#888'>If this never finishes, make sure the Termux "
        + "version is stopped (run <b>xhub-stop</b> in Termux) and reopen the app.</p></body></html>";

    private WebView web;
    private ValueCallback<Uri[]> fileCb;
    private final Handler handler = new Handler(Looper.getMainLooper());

    static void startHub(Context c) {
        Intent i = new Intent(c, HubService.class);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i); else c.startService(i);
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 2);
        }
        startHub(this);

        web = new WebView(this);
        setContentView(web);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        web.addJavascriptInterface(new Bridge(), "Android");

        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) {
                    v.loadData(LOADING, "text/html", "utf-8");
                    handler.postDelayed(() -> v.loadUrl(URL), 1500);
                }
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams p) {
                if (fileCb != null) fileCb.onReceiveValue(null);
                fileCb = cb;
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("*/*");
                i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                try {
                    startActivityForResult(Intent.createChooser(i, "Choose EPUB files"), FILE_REQ);
                    return true;
                } catch (Exception e) {
                    fileCb = null;
                    return false;
                }
            }
        });

        web.loadData(LOADING, "text/html", "utf-8");
        handler.postDelayed(() -> web.loadUrl(URL), 1200);
    }

    @Override
    protected void onActivityResult(int req, int code, Intent data) {
        super.onActivityResult(req, code, data);
        if (req != FILE_REQ || fileCb == null) return;
        Uri[] result = null;
        if (code == RESULT_OK && data != null) {
            if (data.getClipData() != null) {
                int n = data.getClipData().getItemCount();
                result = new Uri[n];
                for (int k = 0; k < n; k++) result[k] = data.getClipData().getItemAt(k).getUri();
            } else if (data.getData() != null) {
                result = new Uri[]{data.getData()};
            }
        }
        fileCb.onReceiveValue(result);
        fileCb = null;
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else moveTaskToBack(true);   // keep the server running
    }

    class Bridge {
        @JavascriptInterface
        public void stop() {
            handler.post(() -> {
                stopService(new Intent(MainActivity.this, HubService.class));
                finishAndRemoveTask();
            });
        }

        @JavascriptInterface
        public void hotspot() {
            handler.post(() -> {
                try {
                    Intent i = new Intent(Intent.ACTION_MAIN);
                    i.setClassName("com.android.settings", "com.android.settings.TetherSettings");
                    i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(i);
                } catch (Exception e) {
                    startActivity(new Intent(Settings.ACTION_WIRELESS_SETTINGS));
                }
            });
        }
    }
}
