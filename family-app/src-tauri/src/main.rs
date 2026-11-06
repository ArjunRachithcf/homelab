// The family's apps in one window (RUNBOOK.md "Family app"). Name and home page come from .env at build time (build.rs).
//
// One window, two webviews: on the left the sidebar (ui/, bundled, the only page with IPC), on the
// right the app itself (home.<domain> and the apps, remote, no IPC). Who sees which app comes from
// home.<domain>/apps.json, the same Caddy template the home page reads; Rust fetches it with the app
// webview's own Authentik cookies. Kids (layout "kids") get no sidebar: the home page's kids
// screen fills the window, and every app gets a Home capsule.
//
// Signing in: the webview can't do passkeys (no WebAuthn in WebKitGTK; a Mac app needs Apple's
// entitlement), and admins sign in with a passkey only. So the app signs in through the person's
// browser (authentik/family-app.yaml): authorization code + PKCE to a one-off listener on
// 127.0.0.1, then the 2-minute access token opens the flow app-signin in the app's window, which
// logs that user in once. "Sign in here" (password + authenticator code) stays for everyone else.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::Mutex;
use std::time::Instant;

use base64::engine::general_purpose::URL_SAFE_NO_PAD as B64;
use base64::Engine as _;
use sha2::{Digest, Sha256};
use std::time::Duration;

use serde_json::Value;
use tauri::image::Image;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{TrayIcon, TrayIconBuilder};
use tauri::webview::{NewWindowResponse, PageLoadEvent, WebviewBuilder};
use tauri::window::WindowBuilder;
use tauri::{
  AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, State, Webview, WebviewUrl, Window, WindowEvent,
};
use tauri_plugin_opener::OpenerExt;
use url::Url;

/// Built for this homelab: build.rs reads BRAND and the home page from ../../.env
/// (HOMELAB_BRAND / HOMELAB_HOME in the build environment win, e.g. for a test build).
const BRAND: &str = env!("HOMELAB_BRAND");
const HOME: &str = env!("HOMELAB_HOME");
const SIDEBAR_WIDTH: f64 = 240.0;
/// Collapsed: an icon rail
const RAIL_WIDTH: f64 = 60.0;
const REFRESH: Duration = Duration::from_secs(300);

const TRAY_UP: &[u8] = include_bytes!("../icons/tray.png");
const TRAY_DOWN: &[u8] = include_bytes!("../icons/tray-down.png");

/// Every page in the app webview fades and rises in (the home page's motion), unless the person
/// asked for less motion. Only the top frame; nothing else is touched.
const ENTER_JS: &str = r#"(function () {
  if (window.top !== window || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  document.documentElement.animate(
    [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "none" }],
    { duration: 450, easing: "cubic-bezier(.16, 1, .3, 1)" });
})();"#;

/// Kids: a floating Home capsule on every app page (plain link, no IPC).
fn home_capsule_js() -> String {
  format!(
    r#"(function () {{
  if (window.top !== window || document.getElementById("homelab-home")) return;
  var a = document.createElement("a");
  a.id = "homelab-home"; a.href = {home}; a.textContent = "Home";
  a.setAttribute("style", "position:fixed;z-index:2147483647;top:12px;left:12px;padding:10px 18px;border-radius:999px;" +
    "font:600 15px/1 -apple-system,BlinkMacSystemFont,Inter,system-ui,sans-serif;color:#1d1d1f;text-decoration:none;" +
    "background:rgba(250,250,252,.82);-webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px);" +
    "box-shadow:0 0 0 .5px rgba(0,0,0,.12),0 6px 20px rgba(0,0,0,.15);transition:transform .2s");
  a.addEventListener("pointerdown", function () {{ a.style.transform = "scale(.94)"; }});
  a.addEventListener("pointerup", function () {{ a.style.transform = ""; }});
  document.body.appendChild(a);
}})();"#,
    home = serde_json::to_string(HOME).unwrap()
  )
}

/// What the window shows: the welcome screen (signed out), Authentik in the app view (signing in
/// here, or the app-signin flow), or the apps.
#[derive(Clone, Copy, PartialEq, Default)]
enum Mode {
  #[default]
  Welcome,
  Here,
  In,
}

#[derive(Default)]
struct Shared {
  mode: Mutex<Mode>,
  /// The last apps.json, None while signed out
  apps: Mutex<Option<Value>>,
  kids: Mutex<bool>,
  /// Sidebar collapsed to the icon rail (the sidebar page remembers it and tells us at start)
  rail: Mutex<bool>,
}

fn home() -> Url {
  Url::parse(HOME).expect("HOMELAB_HOME is a URL")
}

/// Authentik: auth.<domain> on the home page's port
fn auth() -> Url {
  let mut u = home();
  let host = u.host_str().unwrap_or_default().split_once('.').map(|(_, rest)| rest).unwrap_or_default().to_string();
  let _ = u.set_host(Some(&format!("auth.{host}")));
  u.set_path("/");
  u
}

/// The registrable part of the home host (example.org): apps of this homelab open inside the
/// window, everything else in the browser.
fn ours(u: &Url) -> bool {
  let base = home().host_str().unwrap_or_default().split_once('.').map(|(_, rest)| rest).unwrap_or_default().to_string();
  match u.host_str() {
    Some(h) => u.scheme() == "https" && (h == base || h.ends_with(&format!(".{base}")) || h == "app.plex.tv"),
    None => false,
  }
}

/// What a page may hand to the computer: web pages go to the browser, mailto to the mail app.
/// Any other scheme (file:, smb:, an installed program's own) would start a program from a web page.
fn for_the_browser(u: &Url) -> bool {
  matches!(u.scheme(), "https" | "http" | "mailto")
}

/// What may load in the app view, in a frame too (WebKit asks for frames as well): the homelab's own
/// sites, and the pages they make themselves (about:blank, blob:, data:). An outside site in this
/// window could pass for the family's own sign-in.
fn in_the_window(u: &Url) -> bool {
  ours(u) || matches!(u.scheme(), "about" | "blob" | "data")
}

fn content(app: &AppHandle) -> Option<Webview> {
  app.get_webview("content")
}

/// Welcome: the sidebar page alone (its welcome screen). Here: the app view alone. In: sidebar
/// 240px + app, or the app alone for kids.
fn layout(window: &Window) {
  let app = window.app_handle();
  let (size, scale) = match (window.inner_size(), window.scale_factor()) {
    (Ok(z), Ok(f)) => (z, f),
    (z, f) => {
      eprintln!("layout: no size ({:?} / {:?})", z.err(), f.err());
      return;
    }
  };
  let size = size.to_logical::<f64>(scale);
  let s = app.state::<Shared>();
  let mode = *s.mode.lock().unwrap();
  let kids = *s.kids.lock().unwrap();
  let (side, app_x) = match mode {
    Mode::Welcome => (size.width, size.width),
    Mode::Here => (0.0, 0.0),
    Mode::In if kids => (0.0, 0.0),
    Mode::In => {
      let w = if *s.rail.lock().unwrap() { RAIL_WIDTH } else { SIDEBAR_WIDTH };
      (w, w)
    }
  };
  if cfg!(debug_assertions) {
    eprintln!("layout: mode {} window {:.0}x{:.0}", mode as u8, size.width, size.height);
  }
  if let Some(w) = app.get_webview("sidebar") {
    place(&w, side > 0.0, 0.0, side.max(1.0), size.height, mode == Mode::Welcome);
  }
  if let Some(c) = content(app) {
    place(&c, app_x < size.width, app_x.min(size.width - 1.0), (size.width - app_x).max(1.0), size.height, true);
  }
}

/// Shows/hides a child webview and puts it at x (logical px), full height. On Linux the split
/// view decides sizes (see linux_row), so there it only shows or hides.
fn place(w: &Webview, visible: bool, x: f64, width: f64, height: f64, fill: bool) {
  #[cfg(not(target_os = "linux"))]
  {
    let _ = fill;
    let _ = if visible { w.show() } else { w.hide() };
    let _ = w.set_position(LogicalPosition::new(x, 0.0));
    let _ = w.set_size(LogicalSize::new(width, height));
  }
  #[cfg(target_os = "linux")]
  {
    let _ = (x, height);
    let _ = (width, fill);
    let _ = w.with_webview(move |pw| {
      use gtk::prelude::*;
      pw.inner().set_visible(visible);
    });
  }
}

/// Linux (Wayland): Tauri packs child webviews into the window's vertical GtkBox (or a GtkFixed),
/// and set_position/set_size don't move them there. So once, at start, both webviews move into a
/// horizontal GtkPaned that GTK sizes itself: sidebar first, the app after it.
#[cfg(target_os = "linux")]
fn linux_row(content: &Webview) {
  let _ = content.with_webview(|pw| {
    use gtk::prelude::*;
    let app_view: gtk::Widget = pw.inner().upcast();
    let Some(holder) = app_view.parent().and_then(|p| p.downcast::<gtk::Container>().ok()) else {
      eprintln!("layout: the app view has no parent container");
      return;
    };
    // Where the row goes: the holder itself if it's a GtkBox, else the GtkBox around it
    let outer = match holder.clone().downcast::<gtk::Box>() {
      Ok(b) => b,
      Err(h) => match h.parent().and_then(|p| p.downcast::<gtk::Box>().ok()) {
        Some(b) => b,
        None => {
          eprintln!("layout: no GtkBox around the webviews; leaving Tauri's layout");
          return;
        }
      },
    };
    let mut views: Vec<gtk::Widget> =
      holder.children().into_iter().filter(|c| c.type_().name() == "WebKitWebView").collect();
    views.sort_by_key(|c| *c == app_view); // sidebar first
    // A split view: the sidebar keeps its width (240, draggable like a macOS sidebar), the app
    // takes the rest; a hidden side gives the other the whole window
    let row = gtk::Paned::new(gtk::Orientation::Horizontal);
    row.set_wide_handle(false);
    row.set_position(SIDEBAR_WIDTH as i32);
    for v in views {
      holder.remove(&v);
      if v == app_view {
        row.pack2(&v, true, false);
      } else {
        v.set_size_request(RAIL_WIDTH as i32, -1);
        row.pack1(&v, false, false);
      }
    }
    if holder.upcast_ref::<gtk::Widget>() != outer.upcast_ref::<gtk::Widget>() {
      outer.remove(&holder);
    }
    outer.pack_start(&row, true, true, 0);
    row.show_all();
  });
}

fn relayout(app: &AppHandle) {
  if let Some(w) = app.get_window("main") {
    layout(&w);
  }
}

fn set_mode(app: &AppHandle, mode: Mode) {
  *app.state::<Shared>().mode.lock().unwrap() = mode;
  relayout(app);
}

/// apps.json with the app webview's cookies. Signed out → Authentik's redirect → None.
async fn fetch_apps(app: &AppHandle) -> Option<Value> {
  let c = content(app)?;
  let url = home().join("apps.json").ok()?;
  // Off the main thread: reading cookies from a sync handler can deadlock (Tauri docs)
  let cookies = match tauri::async_runtime::spawn_blocking(move || c.cookies_for_url(home())).await {
    Ok(Ok(c)) => c,
    other => {
      eprintln!("apps.json: no cookies ({:?})", other.map(|r| r.err()));
      return None;
    }
  };
  eprintln!("apps.json: {} cookie(s): {:?}", cookies.len(), cookies.iter().map(|k| k.name().to_string()).collect::<Vec<_>>());
  let header = cookies.iter().map(|k| format!("{}={}", k.name(), k.value())).collect::<Vec<_>>().join("; ");
  let client = reqwest::Client::builder()
    .redirect(reqwest::redirect::Policy::none())
    .timeout(Duration::from_secs(15))
    .build()
    .ok()?;
  let res = match client.get(url).header(reqwest::header::COOKIE, header).send().await {
    Ok(r) => r,
    Err(e) => {
      eprintln!("apps.json: {e}");
      return None;
    }
  };
  if !res.status().is_success() {
    eprintln!("apps.json: HTTP {} {:?}", res.status(), res.headers().get("location"));
    return None;
  }
  match res.json::<Value>().await {
    Ok(v) => Some(v),
    Err(e) => {
      eprintln!("apps.json: not JSON: {e}");
      None
    }
  }
}

fn set_tray(app: &AppHandle, apps: Option<&Value>) {
  let Some(tray) = app.tray_by_id("status") else { return };
  let health = apps.map(|a| &a["health"]);
  let known = health.and_then(|h| h["known"].as_bool()).unwrap_or(false);
  let ok = health.and_then(|h| h["ok"].as_bool()).unwrap_or(true);
  let (icon, tip) = match (apps.is_some(), known, ok) {
    (false, _, _) => (TRAY_UP, "not signed in"),
    (true, false, _) => (TRAY_UP, "no outside check yet"),
    (true, true, true) => (TRAY_UP, "everything is up"),
    (true, true, false) => (TRAY_DOWN, "something isn't working"),
  };
  let tip = format!("{BRAND}: {tip}");
  if let Ok(img) = Image::from_bytes(icon) {
    let _ = tray.set_icon(Some(img));
  }
  let _ = tray.set_tooltip(Some(&tip));
}

async fn refresh(app: AppHandle) {
  let apps = fetch_apps(&app).await;
  let kids = apps.as_ref().map(|a| a["layout"] == "kids").unwrap_or(false);
  {
    let s = app.state::<Shared>();
    *s.apps.lock().unwrap() = apps.clone();
    *s.kids.lock().unwrap() = kids;
    let mut mode = s.mode.lock().unwrap();
    *mode = match (apps.is_some(), *mode) {
      (true, _) => Mode::In,
      // Session over (signed out elsewhere, expired): back to the welcome screen
      (false, Mode::In) => Mode::Welcome,
      (false, m) => m,
    };
  }
  set_tray(&app, apps.as_ref());
  let _ = app.emit_to("sidebar", "apps", apps);
  relayout(&app);
}

// ── Commands (sidebar only: the remote app webview has no capability) ──

#[tauri::command]
fn apps(state: State<'_, Shared>) -> Option<Value> {
  state.apps.lock().unwrap().clone()
}

#[tauri::command]
fn open_app(app: AppHandle, url: String) -> Result<(), String> {
  let u = Url::parse(&url).map_err(|e| e.to_string())?;
  if !ours(&u) {
    return Err("not one of the family's apps".into());
  }
  content(&app).ok_or("no app view")?.navigate(u).map_err(|e| e.to_string())
}

#[tauri::command]
fn go_home(app: AppHandle) -> Result<(), String> {
  content(&app).ok_or("no app view")?.navigate(home()).map_err(|e| e.to_string())
}

/// Full sidebar or icon rail. Linux: the split view's divider slides there (ease-out, ~250 ms).
#[tauri::command]
fn set_rail(app: AppHandle, rail: bool) {
  *app.state::<Shared>().rail.lock().unwrap() = rail;
  #[cfg(target_os = "linux")]
  if let Some(w) = app.get_webview("sidebar") {
    let to = if rail { RAIL_WIDTH } else { SIDEBAR_WIDTH } as i32;
    let _ = w.with_webview(move |pw| {
      use gtk::glib;
      use gtk::prelude::*;
      let Some(paned) = pw.inner().parent().and_then(|p| p.downcast::<gtk::Paned>().ok()) else { return };
      let from = paned.position();
      let start = std::time::Instant::now();
      glib::timeout_add_local(Duration::from_millis(16), move || {
        let t = (start.elapsed().as_secs_f64() / 0.25).min(1.0);
        let e = 1.0 - (1.0 - t).powi(3);
        paned.set_position(from + ((to - from) as f64 * e).round() as i32);
        if t < 1.0 { glib::ControlFlow::Continue } else { glib::ControlFlow::Break }
      });
    });
    return;
  }
  relayout(&app);
}

/// The toolbar: back, forward, reload in the app view (history lives in the page)
#[tauri::command]
fn nav(app: AppHandle, to: String) -> Result<(), String> {
  let js = match to.as_str() {
    "back" => "history.back()",
    "forward" => "history.forward()",
    "reload" => "location.reload()",
    _ => return Err("unknown".into()),
  };
  content(&app).ok_or("no app view")?.eval(js).map_err(|e| e.to_string())
}

#[tauri::command]
fn open_in_browser(app: AppHandle) -> Result<(), String> {
  let url = content(&app).ok_or("no app view")?.url().map_err(|e| e.to_string())?;
  if !for_the_browser(&url) {
    return Err("this page can't be opened in the browser".into());
  }
  app.opener().open_url(url.as_str(), None::<&str>).map_err(|e| e.to_string())
}

#[tauri::command]
fn sign_out(app: AppHandle) -> Result<(), String> {
  {
    let s = app.state::<Shared>();
    *s.apps.lock().unwrap() = None;
    *s.kids.lock().unwrap() = false;
  }
  let _ = app.emit_to("sidebar", "apps", None::<Value>);
  set_tray(&app, None);
  set_mode(&app, Mode::Welcome);
  let u = home().join("outpost.goauthentik.io/sign_out").map_err(|e| e.to_string())?;
  content(&app).ok_or("no app view")?.navigate(u).map_err(|e| e.to_string())
}

#[tauri::command]
async fn reload_apps(app: AppHandle) {
  refresh(app).await;
}

/// Password + authenticator code in the app's own window (no passkeys there)
#[tauri::command]
fn sign_in_here(app: AppHandle) -> Result<(), String> {
  set_mode(&app, Mode::Here);
  content(&app).ok_or("no app view")?.navigate(home()).map_err(|e| e.to_string())
}

fn random(n: usize) -> Result<String, String> {
  let mut b = vec![0u8; n];
  getrandom::fill(&mut b).map_err(|e| e.to_string())?;
  Ok(B64.encode(b))
}

/// Waits (up to 5 minutes) for the browser's redirect to http://127.0.0.1:<port>/callback and
/// answers it with a short page. Returns the query parameters.
fn wait_for_callback(listener: TcpListener) -> Result<Vec<(String, String)>, String> {
  let deadline = Instant::now() + Duration::from_secs(300);
  listener.set_nonblocking(true).map_err(|e| e.to_string())?;
  loop {
    match listener.accept() {
      Ok((mut stream, _)) => {
        stream.set_nonblocking(false).map_err(|e| e.to_string())?;
        stream.set_read_timeout(Some(Duration::from_secs(5))).ok();
        let mut buf = [0u8; 8192];
        let n = stream.read(&mut buf).unwrap_or(0);
        let req = String::from_utf8_lossy(&buf[..n]);
        // "GET /callback?code=…&state=… HTTP/1.1"
        let target = req.split_whitespace().nth(1).unwrap_or("");
        if !target.starts_with("/callback") {
          let _ = stream.write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
          continue;
        }
        let page = format!("<!doctype html><meta charset=utf-8><meta name=color-scheme content='light dark'><title>{BRAND}</title>\
          <body style=\"font:17px/1.4 -apple-system,BlinkMacSystemFont,system-ui,sans-serif;display:grid;place-items:center;height:90vh;margin:0\">\
          <div style=text-align:center><h1 style='font-size:28px;letter-spacing:-.02em;margin:0 0 8px'>You're signed in</h1>\
          <p style=opacity:.7;margin:0>Go back to the {BRAND} app. You can close this tab.</p></div>");
        let head = format!("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n", page.len());
        let _ = stream.write_all(head.as_bytes());
        let _ = stream.write_all(page.as_bytes());
        let url = Url::parse(&format!("http://127.0.0.1{target}")).map_err(|e| e.to_string())?;
        return Ok(url.query_pairs().map(|(k, v)| (k.into_owned(), v.into_owned())).collect());
      }
      Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
        if Instant::now() > deadline {
          return Err("no answer from the browser within 5 minutes".into());
        }
        std::thread::sleep(Duration::from_millis(150));
      }
      Err(e) => return Err(e.to_string()),
    }
  }
}

/// Sign in through the browser (passkeys work there), then carry the session into the app's
/// window via the one-time app-signin flow. authentik/family-app.yaml has the other half.
#[tauri::command]
async fn browser_sign_in(app: AppHandle) -> Result<(), String> {
  let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
  let redirect = format!("http://127.0.0.1:{}/callback", listener.local_addr().map_err(|e| e.to_string())?.port());
  let verifier = random(48)?;
  let challenge = B64.encode(Sha256::digest(verifier.as_bytes()));
  let state = random(24)?;
  let mut authorize = auth().join("application/o/authorize/").map_err(|e| e.to_string())?;
  authorize
    .query_pairs_mut()
    .append_pair("response_type", "code")
    .append_pair("client_id", "family-app")
    .append_pair("scope", "openid")
    .append_pair("redirect_uri", &redirect)
    .append_pair("state", &state)
    .append_pair("code_challenge", &challenge)
    .append_pair("code_challenge_method", "S256");
  app.opener().open_url(authorize.as_str(), None::<&str>).map_err(|e| e.to_string())?;

  let params = tauri::async_runtime::spawn_blocking(move || wait_for_callback(listener))
    .await
    .map_err(|e| e.to_string())??;
  let get = |k: &str| params.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone());
  if get("state").as_deref() != Some(state.as_str()) {
    return Err("the browser's answer didn't match (state)".into());
  }
  if let Some(err) = get("error") {
    return Err(format!("sign-in refused: {err}"));
  }
  let code = get("code").ok_or("no code from the browser")?;

  let token: Value = reqwest::Client::new()
    .post(auth().join("application/o/token/").map_err(|e| e.to_string())?)
    .form(&[
      ("grant_type", "authorization_code"),
      ("code", code.as_str()),
      ("redirect_uri", redirect.as_str()),
      ("client_id", "family-app"),
      ("code_verifier", verifier.as_str()),
    ])
    .send()
    .await
    .map_err(|e| e.to_string())?
    .error_for_status()
    .map_err(|e| e.to_string())?
    .json()
    .await
    .map_err(|e| e.to_string())?;
  let access = token["access_token"].as_str().ok_or("no access token")?;

  let mut flow = auth().join("if/flow/app-signin/").map_err(|e| e.to_string())?;
  flow.query_pairs_mut().append_pair("t", access);
  set_mode(&app, Mode::Here);
  content(&app).ok_or("no app view")?.navigate(flow).map_err(|e| e.to_string())?;
  if let Some(w) = app.get_window("main") {
    let _ = w.set_focus();
  }
  Ok(())
}

fn build_tray(app: &AppHandle) -> tauri::Result<TrayIcon> {
  let show = MenuItem::with_id(app, "show", format!("Show {BRAND}"), true, None::<&str>)?;
  let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
  let menu = Menu::with_items(app, &[&show, &quit])?;
  TrayIconBuilder::with_id("status")
    .icon(Image::from_bytes(TRAY_UP)?)
    .tooltip(BRAND)
    .menu(&menu)
    .on_menu_event(|app, e| match e.id.as_ref() {
      "show" => {
        if let Some(w) = app.get_window("main") {
          let _ = w.show();
          let _ = w.set_focus();
        }
      }
      "quit" => app.exit(0),
      _ => {}
    })
    .build(app)
}

fn main() {
  tauri::Builder::default()
    .plugin(tauri_plugin_opener::init())
    .manage(Shared::default())
    .invoke_handler(tauri::generate_handler![
      apps, open_app, go_home, nav, set_rail, open_in_browser, sign_out, reload_apps, sign_in_here, browser_sign_in
    ])
    .setup(|app| {
      let handle = app.handle().clone();
      #[cfg_attr(not(target_os = "macos"), allow(unused_mut))]
      let mut wb = WindowBuilder::new(app, "main")
        .title(BRAND)
        .inner_size(1240.0, 820.0)
        .min_inner_size(720.0, 480.0);
      // macOS: the sidebar sits on the window's own vibrancy (translucent, like Finder)
      #[cfg(target_os = "macos")]
      {
        use tauri::window::{Effect, EffectsBuilder};
        wb = wb.effects(EffectsBuilder::new().effect(Effect::Sidebar).build()).transparent(true);
      }
      let window = wb.build()?;
      let size = window.inner_size()?.to_logical::<f64>(window.scale_factor()?);

      let sidebar = WebviewBuilder::new("sidebar", WebviewUrl::App("index.html".into()));
      #[cfg(target_os = "macos")]
      let sidebar = sidebar.transparent(true);
      window.add_child(sidebar, LogicalPosition::new(0.0, 0.0), LogicalSize::new(SIDEBAR_WIDTH, size.height))?;

      let h_load = handle.clone();
      let h_new = handle.clone();
      let content = WebviewBuilder::new("content", WebviewUrl::External(home()))
        .initialization_script(ENTER_JS)
        .on_page_load(move |wv, p| {
          let url = p.url().clone();
          match p.event() {
            PageLoadEvent::Started => {
              let _ = h_load.emit_to("sidebar", "nav", url.as_str());
            }
            PageLoadEvent::Finished => {
              // Back on the home page (after a sign-in, too): read the apps again
              if url.origin() == home().origin() && url.path() == "/" {
                tauri::async_runtime::spawn(refresh(h_load.clone()));
              }
              if *h_load.state::<Shared>().kids.lock().unwrap() && url.origin() != home().origin() {
                let _ = wv.eval(home_capsule_js());
              }
            }
          }
        })
        // Links that open a new window: ours stay in this window, outside pages and mail go to the
        // computer's browser and mail app, nothing else anywhere
        .on_new_window(move |url, _| {
          if ours(&url) {
            if let Some(c) = content(&h_new) {
              let _ = c.navigate(url);
            }
          } else if for_the_browser(&url) {
            let _ = h_new.opener().open_url(url.as_str(), None::<&str>);
          }
          NewWindowResponse::Deny
        })
        // Any other navigation, a plain link too: only the homelab's sites load here
        .on_navigation(in_the_window);
      // Loads home in the background: a session kept from last time goes straight to the apps,
      // otherwise the welcome screen stays
      let content = window.add_child(content, LogicalPosition::new(0.0, 0.0), LogicalSize::new(size.width, size.height))?;
      #[cfg(target_os = "linux")]
      linux_row(&content);
      #[cfg(not(target_os = "linux"))]
      let _ = content;
      layout(&window);

      let h_resize = handle.clone();
      window.on_window_event(move |e| {
        if let WindowEvent::Resized(_) | WindowEvent::ScaleFactorChanged { .. } = e {
          relayout(&h_resize);
        }
      });

      build_tray(&handle)?;

      // The outside status in the tray, every 5 minutes
      let h_tick = handle.clone();
      tauri::async_runtime::spawn(async move {
        loop {
          tokio_sleep(REFRESH).await;
          refresh(h_tick.clone()).await;
        }
      });
      Ok(())
    })
    .run(tauri::generate_context!())
    .expect("error while running the family app");
}

async fn tokio_sleep(d: Duration) {
  let _ = tauri::async_runtime::spawn_blocking(move || std::thread::sleep(d)).await;
}

#[cfg(test)]
mod tests {
  use super::*;

  fn u(s: &str) -> Url {
    Url::parse(s).unwrap()
  }

  /// The home host's registrable part, whatever this build was made for (HOMELAB_HOME)
  fn base() -> String {
    home().host_str().unwrap().split_once('.').unwrap().1.to_string()
  }

  #[test]
  fn the_homelab_loads_in_the_window() {
    let b = base();
    for ok in [format!("https://photos.{b}/"), format!("https://auth.{b}:9443/if/flow/x/"), format!("https://{b}/"),
               "https://app.plex.tv/desktop".into(), "about:blank".into(), format!("blob:https://pdf.{b}/1-2")] {
      assert!(in_the_window(&u(&ok)), "{ok}");
    }
  }

  #[test]
  fn outside_sites_and_lookalikes_stay_out() {
    let b = base();
    for no in [format!("http://photos.{b}/"), format!("https://photos.{b}.evil.test/"), format!("https://evil{b}/"),
               "https://example.com/".into(), "file:///etc/passwd".into(), "smb://nas/share".into()] {
      assert!(!in_the_window(&u(&no)), "{no}");
    }
  }

  #[test]
  fn only_web_pages_and_mail_go_to_the_computer() {
    for ok in ["https://example.com/", "http://example.com/", "mailto:someone@example.com"] {
      assert!(for_the_browser(&u(ok)), "{ok}");
    }
    for no in ["file:///etc/passwd", "smb://nas/share", "ms-officecmd:x", "vscode://file/x", "steam://run/1", "javascript:alert(1)"] {
      assert!(!for_the_browser(&u(no)), "{no}");
    }
  }
}
