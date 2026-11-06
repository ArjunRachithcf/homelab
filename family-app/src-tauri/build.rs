use std::{env, fs, path::Path};

/// .env as the scripts read it: the last KEY= line wins, one pair of surrounding quotes goes.
fn env_file(key: &str) -> Option<String> {
  let text = fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.env")).ok()?;
  let line = text.lines().rfind(|l| l.starts_with(&format!("{key}=")))?;
  let v = &line[key.len() + 1..];
  let v = if v.len() >= 2 && ((v.starts_with('\'') && v.ends_with('\'')) || (v.starts_with('"') && v.ends_with('"'))) {
    &v[1..v.len() - 1]
  } else {
    v
  };
  Some(v.to_string()).filter(|v| !v.is_empty())
}

fn main() {
  // The app's name and home page (main.rs BRAND / HOME): the build environment first, then .env
  println!("cargo:rerun-if-changed=../../.env");
  println!("cargo:rerun-if-env-changed=HOMELAB_BRAND");
  println!("cargo:rerun-if-env-changed=HOMELAB_HOME");
  let brand = env::var("HOMELAB_BRAND").ok().or_else(|| env_file("BRAND")).unwrap_or_else(|| "Home".into());
  let home = env::var("HOMELAB_HOME").ok().unwrap_or_else(|| match env_file("DOMAIN") {
    Some(d) => format!("https://home.{d}:{}/", env_file("NETBIRD_PROXY_PORT").unwrap_or_else(|| "9443".into())),
    None => {
      println!("cargo:warning=no DOMAIN in ../../.env and no HOMELAB_HOME: the app points at home.example.org");
      "https://home.example.org/".into()
    }
  });
  println!("cargo:rustc-env=HOMELAB_BRAND={brand}");
  println!("cargo:rustc-env=HOMELAB_HOME={home}");

  // Only these commands exist, and only the sidebar's capability grants IPC at all
  tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
    tauri_build::AppManifest::new().commands(&["apps", "open_app", "go_home", "nav", "set_rail", "open_in_browser", "sign_out", "reload_apps", "sign_in_here", "browser_sign_in"]),
  ))
  .expect("tauri build");
}
