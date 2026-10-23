package com.stolworthy.fireside;

import android.content.SharedPreferences;
import android.os.Bundle;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import com.getcapacitor.BridgeActivity;

/**
 * Fireside Android shell.
 *
 * The app is a thin remote for a Fireside server the user picks (their own, or one
 * shared to them). On first run there is no server, so the bundled www/ page asks for
 * a URL; window.NativeMedia.connect() saves it and loads it in the same WebView.
 * Because the load is programmatic, Capacitor's external-link redirect does not fire.
 *
 * window.NativeMedia also raises a foreground media service on play/pause so audio
 * survives screen lock. Absent in a plain browser, so the same web app is a no-op there.
 */
public class MainActivity extends BridgeActivity {

    private static final String PREFS = "fireside";
    private static final String KEY_URL = "server_url";

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, MODE_PRIVATE);
    }

    private static String normalize(String url) {
        if (url == null) return null;
        String u = url.trim();
        if (u.isEmpty()) return null;
        if (!u.matches("(?i)^https?://.*")) u = "https://" + u;
        return u;
    }

    public class NativeMediaBridge {
        @JavascriptInterface
        public void start() {
            MediaPlaybackService.start(MainActivity.this);
        }

        @JavascriptInterface
        public void stop() {
            MediaPlaybackService.stop(MainActivity.this);
        }

        /** Saved server URL, or "" if none yet. */
        @JavascriptInterface
        public String getServer() {
            return prefs().getString(KEY_URL, "");
        }

        /** Save a server URL and load it in the WebView. */
        @JavascriptInterface
        public void connect(final String url) {
            final String u = normalize(url);
            if (u == null) return;
            prefs().edit().putString(KEY_URL, u).apply();
            runOnUiThread(new Runnable() {
                public void run() {
                    WebView wv = getBridge().getWebView();
                    if (wv != null) wv.loadUrl(u);
                }
            });
        }

        /** Forget the server and return to the first-run screen. */
        @JavascriptInterface
        public void reset() {
            prefs().edit().remove(KEY_URL).apply();
            runOnUiThread(new Runnable() {
                public void run() {
                    recreate();
                }
            });
        }
    }

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        WebView webView = getBridge().getWebView();
        if (webView != null) {
            webView.addJavascriptInterface(new NativeMediaBridge(), "NativeMedia");
            String saved = prefs().getString(KEY_URL, "");
            if (!saved.isEmpty()) {
                webView.loadUrl(saved);
            }
        }
    }
}