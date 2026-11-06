use std::env;

fn main() {
  // The release the bootstrap clones (main.rs REPO / REF / COMMIT). CI sets all three from the tag it
  // builds; a local build points at the default branch and checks no commit.
  for k in ["HOMELAB_REPO", "HOMELAB_REF", "HOMELAB_COMMIT"] {
    println!("cargo:rerun-if-env-changed={k}");
  }
  // Its default is setup/project.toml's repo, the one place the project's address is written
  println!("cargo:rerun-if-changed=../../setup/project.toml");
  let project = std::fs::read_to_string("../../setup/project.toml").expect("setup/project.toml");
  let home = project.lines().find_map(|l| l.strip_prefix("repo = \"")?.strip_suffix('"')).expect("repo = \"…\" in setup/project.toml");
  let repo = env::var("HOMELAB_REPO").unwrap_or_else(|_| format!("{home}.git"));
  let rf = env::var("HOMELAB_REF").unwrap_or_else(|_| "main".into());
  let commit = env::var("HOMELAB_COMMIT").unwrap_or_default();
  assert!(repo.starts_with("https://"), "HOMELAB_REPO must be https");
  assert!(commit.is_empty() || (commit.len() == 40 && commit.bytes().all(|b| b.is_ascii_hexdigit())), "HOMELAB_COMMIT: a full SHA-1");
  println!("cargo:rustc-env=HOMELAB_REPO={repo}");
  println!("cargo:rustc-env=HOMELAB_REF={rf}");
  println!("cargo:rustc-env=HOMELAB_COMMIT={commit}");
  println!("cargo:rerun-if-changed=../../setup/bootstrap.sh");

  // Only these commands exist
  tauri_build::try_build(tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&[
    "info", "host_key", "connect_local", "connect_ssh", "disconnect", "set_sudo", "probe", "catalogue", "plan", "apply",
    "set_secret", "export_answers", "save_text", "bootstrap", "start_setup", "answer", "stop", "open_url",
    "assistant_status", "assistant_settings", "assistant_reset", "assistant_decide", "assistant_send",
  ])))
  .expect("tauri build");
}
