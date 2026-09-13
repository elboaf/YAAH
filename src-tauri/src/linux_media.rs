//! Linux-only: WebKitGTK (Tauri's Linux webview) ships with media capture
//! disabled and a permission-request handler that silently denies everything,
//! so `navigator.mediaDevices.getUserMedia` always rejects with NotAllowedError
//! — no Ubuntu settings dialog involved. Enable media-stream and install a
//! handler that grants microphone requests inside this app's own webview and
//! denies all other permission types.

use tauri::{AppHandle, Manager};

pub fn enable_microphone_access(app: &AppHandle) {
    let Some(window) = app.get_webview_window("main") else {
        log::warn!("main webview window not found; mic capture stays disabled");
        return;
    };
    let result = window.with_webview(|webview| {
        use webkit2gtk::{SettingsExt as _, WebViewExt as _};
        let wv = webview.inner();
        if let Some(settings) = wv.settings() {
            settings.set_enable_media_stream(true);
        }
        wv.connect_permission_request(|_, request| {
            use webkit2gtk::glib::object::ObjectExt as _;
            use webkit2gtk::{PermissionRequestExt as _, UserMediaPermissionRequest};
            if request.is::<UserMediaPermissionRequest>() {
                request.allow();
            } else {
                request.deny();
            }
            true // handled; suppress WebKitGTK's deny-by-default handler
        });
    });
    if let Err(e) = result {
        log::warn!("failed to configure webview media permissions: {e}");
    }
}
