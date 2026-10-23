package com.stolworthy.fireside;

import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
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

        /**
         * Open a sign-in page outside the webview.
         *
         * Google (and Apple, and Microsoft) refuse OAuth inside an embedded
         * webview, so the flow must happen in a real browser tab. It comes back
         * over fireside://auth?token=... and onNewIntent() finishes it here.
         */
        @JavascriptInterface
        public void openAuth(final String url) {
            final String u = normalize(url);
            if (u == null) return;
            runOnUiThread(new Runnable() {
                public void run() {
                    try {
                        new androidx.browser.customtabs.CustomTabsIntent.Builder()
                            .build()
                            .launchUrl(MainActivity.this, Uri.parse(u));
                    } catch (Throwable t) {
                        // no Custom Tabs provider on this device: any browser is fine
                        startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(u)));
                    }
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

    /**
     * Turn a fireside://auth?token=... return into the one URL the webview must
     * load: the server redeems that token once and sets the session cookie in
     * the webview's own jar. Anything else here is not ours to handle.
     */
    private void handleAuthReturn(Intent intent) {
        if (intent == null || intent.getData() == null) return;
        Uri d = intent.getData();
        if (!"fireside".equals(d.getScheme())) return;
        String token = d.getQueryParameter("token");
        String server = normalize(prefs().getString(KEY_URL, ""));
        if (token == null || token.isEmpty() || server == null) return;
        final String url = server.replaceAll("/+$", "") + "/auth/app/session?token="
                + Uri.encode(token);
        runOnUiThread(new Runnable() {
            public void run() {
                WebView wv = getBridge().getWebView();
                if (wv != null) wv.loadUrl(url);
            }
        });
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleAuthReturn(intent);
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
            // a signed-in return can also be the launch intent (app was cold)
            handleAuthReturn(getIntent());
        }
    }
}