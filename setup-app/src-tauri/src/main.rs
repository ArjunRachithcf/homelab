// ~/homelab/setup-app/src-tauri/src/main.rs: the guided setup app (plan Phase 3b, README.md here).
//
// The window (ui/) chooses; this side runs. It only runs fixed argvs in the homelab repository:
// ./homelab components --json, ./homelab setup --plan|--apply --answers - --json, ./homelab configure,
// ./homelab refresh-console, ./homelab setup --set-secret KEY (value on stdin), ./homelab setup --export-answers, ./homelab setup --protocol [wizards] [--stages=…] [--check], and the embedded
// setup/bootstrap.sh when there's no repository yet. Over SSH the same argvs are quoted word by word.
//
// Secrets: answers to `secret` steps go to the wizard's stdin (lib/setup/run.py); the sudo password,
// once given, stays in this process and answers the protocol's `sudo` messages (askpass) and the
// bootstrap's `sudo -S`; it is never sent back to the window, logged or written anywhere.
//
// The assistant (assistant.rs, optional): a model with the MCP server's tools (lib/setup/mcp.py, run on
// the machine like everything else). Tools that change something wait for the person's yes here; the
// server itself refuses secrets, sudo and destructive steps. The API key sits in the OS keychain.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod assistant;
mod transport;

use std::{
  collections::{BTreeMap, HashMap},
  path::PathBuf,
  sync::{Arc, Mutex},
};

use serde::Serialize;
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager, State};
use tokio::sync::{mpsc, oneshot};
use transport::{Conn, Cwd, Input};

/// The repository the bootstrap clones, and the commit its release tag must be (build.rs)
const REPO: &str = env!("HOMELAB_REPO");
const REF: &str = env!("HOMELAB_REF");
const COMMIT: &str = env!("HOMELAB_COMMIT");
const BOOTSTRAP: &str = include_str!("../../../setup/bootstrap.sh");
const WIZARDS: &[&str] = &["wizard", "public", "hardening", "immich", "apps", "music", "files", "family", "downloads"];

#[derive(Default)]
struct App {
  conn: Mutex<Option<Arc<Conn>>>,
  running: Mutex<Option<mpsc::Sender<Input>>>,
  sudo: Arc<Mutex<Option<String>>>,
  ai: tokio::sync::Mutex<Ai>,
  pending: Mutex<HashMap<String, oneshot::Sender<bool>>>,
}

/// The assistant's state: its settings (not the key: keychain), the MCP server, the conversation
#[derive(Default)]
struct Ai {
  provider: String,
  model: String,
  ollama_url: String,
  mcp: Option<assistant::Mcp>,
  history: Vec<Value>,
  /// The wizard's open question from the last tool result: the Allow card for `answer` shows it
  /// verbatim, so the person doesn't depend on the model's own account of what it answers
  asking: Option<Value>,
}

type R<T> = Result<T, String>;

fn conn(s: &App) -> R<Arc<Conn>> {
  s.conn.lock().expect("lock").clone().ok_or_else(|| "not connected".into())
}

fn argv(words: &[&str]) -> Vec<String> {
  words.iter().map(|w| w.to_string()).collect()
}

fn known_hosts(app: &AppHandle) -> R<PathBuf> {
  let dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
  std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
  Ok(dir.join("known_hosts.json"))
}

fn read_known(app: &AppHandle) -> BTreeMap<String, String> {
  known_hosts(app)
    .ok()
    .and_then(|p| std::fs::read_to_string(p).ok())
    .and_then(|t| serde_json::from_str(&t).ok())
    .unwrap_or_default()
}

#[derive(Serialize)]
struct Info {
  local: bool,
  repo: &'static str,
  r#ref: &'static str,
  commit: &'static str,
  version: &'static str,
}

#[tauri::command]
fn info() -> Info {
  Info { local: cfg!(unix), repo: REPO, r#ref: REF, commit: COMMIT, version: env!("CARGO_PKG_VERSION") }
}

/// The host key, and the one remembered for this host:port (if any), for the person to compare
#[tauri::command]
async fn host_key(app: AppHandle, host: String, port: u16) -> R<Value> {
  let fp = transport::host_key(&host, port).await?;
  let known = read_known(&app).get(&format!("{host}:{port}")).cloned();
  Ok(json!({ "fingerprint": fp, "known": known }))
}

#[tauri::command]
async fn connect_local(state: State<'_, App>, dir: String) -> R<String> {
  if !cfg!(unix) {
    return Err("the homelab runs on Linux or macOS: use another computer over SSH".into());
  }
  let dir = match dir.strip_prefix("~/") {
    Some(rest) => dirs::home_dir().ok_or("no home directory")?.join(rest),
    None => PathBuf::from(dir),
  };
  let c = Conn::Local { dir };
  let label = c.label();
  *state.conn.lock().expect("lock") = Some(Arc::new(c));
  Ok(label)
}

#[allow(clippy::too_many_arguments)]
#[tauri::command]
async fn connect_ssh(
  app: AppHandle,
  state: State<'_, App>,
  host: String,
  port: u16,
  user: String,
  password: Option<String>,
  key_path: Option<String>,
  key_passphrase: Option<String>,
  dir: String,
  fingerprint: String,
) -> R<String> {
  let handle = transport::connect(transport::Login {
    host: &host,
    port,
    user: &user,
    password: password.as_deref(),
    key_path: key_path.as_deref(),
    key_passphrase: key_passphrase.as_deref(),
    fingerprint: &fingerprint,
  })
  .await?;
  // Confirmed and it worked: remember it (a later change is refused until confirmed again)
  let mut known = read_known(&app);
  known.insert(format!("{host}:{port}"), fingerprint);
  if let Ok(p) = known_hosts(&app) {
    let _ = std::fs::write(p, serde_json::to_string_pretty(&known).unwrap_or_default());
  }
  let c = Conn::Ssh { handle, dir, label: format!("{user}@{host}") };
  let label = c.label();
  *state.conn.lock().expect("lock") = Some(Arc::new(c));
  Ok(label)
}

#[tauri::command]
async fn disconnect(state: State<'_, App>) -> R<()> {
  let mut ai = state.ai.lock().await;
  if let Some(m) = ai.mcp.take() {
    m.stop().await;
  }
  ai.history.clear();
  drop(ai);
  if let Some(tx) = state.running.lock().expect("lock").take() {
    let _ = tx.try_send(Input::Kill);
  }
  *state.conn.lock().expect("lock") = None;
  *state.sudo.lock().expect("lock") = None;
  Ok(())
}

/// Keep the sudo password for this session (the window forgets it right after)
#[tauri::command]
fn set_sudo(state: State<'_, App>, password: String) {
  *state.sudo.lock().expect("lock") = Some(password).filter(|p| !p.is_empty());
}

const PROBE: &str = r#"d=$1; case $d in "~/"*) d="$HOME/${d#\~/}" ;; "~") d=$HOME ;; esac
echo "dir=$d"
[ -x "$d/homelab" ] && echo repo=yes || echo repo=no
[ "$(id -u)" = 0 ] && echo root=yes || echo root=no
echo "os=$(uname -s)"
[ -r /etc/os-release ] && . /etc/os-release && echo "distro=${PRETTY_NAME:-$ID}"
command -v python3 >/dev/null && python3 -c 'import sys; print("python=" + ("ok" if sys.version_info >= (3, 11) else "old"))' || echo python=none
command -v git >/dev/null && echo git=yes || echo git=no
[ -f "$d/.env" ] && echo env=yes || echo env=no"#;

#[tauri::command]
async fn probe(state: State<'_, App>) -> R<Value> {
  let c = conn(&state)?;
  let dir = match &*c {
    Conn::Local { dir } => dir.display().to_string(),
    Conn::Ssh { dir, .. } => dir.clone(),
  };
  let out = c.run(Cwd::Home, &argv(&["sh", "-c", PROBE, "probe", &dir]), None).await?;
  let mut m = serde_json::Map::new();
  for l in out.stdout.lines() {
    if let Some((k, v)) = l.split_once('=') {
      m.insert(k.into(), Value::String(v.into()));
    }
  }
  m.insert("label".into(), Value::String(c.label()));
  Ok(Value::Object(m))
}

fn json_out(o: &transport::Output) -> R<Value> {
  let line = o.stdout.lines().rev().find(|l| l.starts_with('{'));
  match line {
    Some(l) => serde_json::from_str(l).map_err(|e| e.to_string()),
    None => Err(format!("no answer (exit {}): {}", o.code, o.stderr.lines().rev().take(5).collect::<Vec<_>>().join(" · "))),
  }
}

#[tauri::command]
async fn catalogue(state: State<'_, App>) -> R<Value> {
  let o = conn(&state)?.run(Cwd::Repo, &argv(&["./homelab", "components", "--json"]), None).await?;
  json_out(&o)
}

/// What an answers file (TOML, built by the window) would do; changes nothing
#[tauri::command]
async fn plan(state: State<'_, App>, answers: String) -> R<Value> {
  let o = conn(&state)?.run(Cwd::Repo, &argv(&["./homelab", "setup", "--plan", "--answers", "-", "--json"]), Some(answers.as_bytes())).await?;
  json_out(&o)
}

/// Writes the choice into .env (secrets generated there, never shown), runs ./homelab configure, and gives a
/// running console's backend the new choice (./homelab refresh-console: it reads the components when it starts)
#[tauri::command]
async fn apply(state: State<'_, App>, answers: String) -> R<Value> {
  let c = conn(&state)?;
  let o = c.run(Cwd::Repo, &argv(&["./homelab", "setup", "--apply", "--answers", "-", "--json"]), Some(answers.as_bytes())).await?;
  let mut v = json_out(&o)?;
  if v["applied"] == json!(true) {
    let cf = c.run(Cwd::Repo, &argv(&["./homelab", "configure"]), None).await?;
    v["configure"] = json!({ "code": cf.code, "output": format!("{}{}", cf.stdout, cf.stderr) });
    let rc = c.run(Cwd::Repo, &argv(&["./homelab", "refresh-console"]), None).await?;
    v["console"] = json!({ "code": rc.code, "output": format!("{}{}", rc.stdout, rc.stderr) });
  }
  Ok(v)
}

/// A secret option (e.g. a DNS token) the person typed in: over stdin into .env there, never argv
#[tauri::command]
async fn set_secret(state: State<'_, App>, key: String, value: String) -> R<()> {
  if key.is_empty() || !key.bytes().all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_') {
    return Err("odd key".into());
  }
  let o = conn(&state)?.run(Cwd::Repo, &argv(&["./homelab", "setup", "--set-secret", &key]), Some(format!("{value}\n").as_bytes())).await?;
  if o.code != 0 {
    return Err(format!("{key}: {}", o.stderr.trim()));
  }
  Ok(())
}

#[tauri::command]
async fn export_answers(state: State<'_, App>) -> R<String> {
  let o = conn(&state)?.run(Cwd::Repo, &argv(&["./homelab", "setup", "--export-answers"]), None).await?;
  if o.code != 0 {
    return Err(o.stderr);
  }
  Ok(o.stdout)
}

#[tauri::command]
async fn save_text(path: String, text: String) -> R<()> {
  tokio::fs::write(&path, text).await.map_err(|e| format!("{path}: {e}"))
}

#[derive(Clone, Serialize)]
struct Line {
  kind: &'static str,
  line: String,
}

/// Forward a stream's lines to the window as events; protocol `sudo` messages are answered here
/// when the password is known (once: a second ask in a row means it was wrong, so the window asks)
fn forward(app: AppHandle, kind: &'static str, mut s: transport::Stream, sudo: Arc<Mutex<Option<String>>>) {
  let input = s.input.clone();
  tauri::async_runtime::spawn(async move {
    let mut answered_last = false;
    while let Some(line) = s.lines.recv().await {
      if kind == "setup" {
        let is_sudo = serde_json::from_str::<Value>(&line).map(|v| v["t"] == "sudo").unwrap_or(false);
        if is_sudo {
          let pw = sudo.lock().expect("lock").clone();
          match pw {
            Some(pw) if !answered_last => {
              answered_last = true;
              let msg = json!({ "answer": pw }).to_string() + "\n";
              let _ = input.send(Input::Data(msg.into_bytes())).await;
              let _ = app.emit("stream-line", Line { kind, line: json!({"t": "note", "text": "sudo: answered with the saved password"}).to_string() });
              continue;
            }
            Some(_) => {
              *sudo.lock().expect("lock") = None; // it was wrong
            }
            None => {}
          }
        } else if line.contains("Sorry, try again") || line.contains("incorrect password attempt") {
          // sudo's retry notice comes in as output between two asks (no tty): the saved password is
          // wrong, so the window asks instead of burning the next tries (pam_faillock locks accounts)
          *sudo.lock().expect("lock") = None;
        }
        answered_last = false;
      }
      let _ = app.emit("stream-line", Line { kind, line });
    }
    let code = s.done.await.unwrap_or(-1);
    let _ = app.emit("stream-end", json!({ "kind": kind, "code": code }));
    if let Some(st) = app.try_state::<App>() {
      st.running.lock().expect("lock").take();
    }
  });
}

fn start(app: &AppHandle, state: &App, kind: &'static str, s: transport::Stream) -> R<()> {
  let mut slot = state.running.lock().expect("lock");
  if slot.is_some() {
    return Err("something is already running".into());
  }
  *slot = Some(s.input.clone());
  drop(slot);
  forward(app.clone(), kind, s, state.sudo.clone());
  Ok(())
}

/// No homelab there yet: copy setup/bootstrap.sh (embedded) over and run it
#[tauri::command]
async fn bootstrap(app: AppHandle, state: State<'_, App>) -> R<()> {
  let c = conn(&state)?;
  let up = c
    .run(Cwd::Home, &argv(&["sh", "-c", "umask 077; mkdir -p \"$HOME/.cache\" && cat > \"$HOME/.cache/homelab-bootstrap.sh\""]), Some(BOOTSTRAP.as_bytes()))
    .await?;
  if up.code != 0 {
    return Err(format!("couldn't copy the bootstrap script: {}", up.stderr));
  }
  let dir = match &*c {
    Conn::Local { dir } => dir.display().to_string(),
    Conn::Ssh { dir, .. } => dir.replacen("~/", "$HOME/", 1),
  };
  let pw = state.sudo.lock().expect("lock").clone();
  let mut words = vec!["sh", "-c", "d=$1; shift; case $d in '$HOME/'*) d=\"$HOME/${d#\\$HOME/}\" ;; esac; exec bash \"$HOME/.cache/homelab-bootstrap.sh\" --dir \"$d\" \"$@\"", "bootstrap", &dir, "--repo", REPO, "--ref", REF];
  if !COMMIT.is_empty() {
    words.extend(["--commit", COMMIT]);
  }
  if pw.is_some() {
    words.push("--sudo-stdin");
  }
  let s = c.stream(Cwd::Home, &argv(&words)).await?;
  if let Some(pw) = pw {
    let _ = s.input.send(Input::Data(format!("{pw}\n").into_bytes())).await;
  }
  start(&app, &state, "bootstrap", s)
}

/// The wizards' stages (`wizards` empty: all the chosen ones); `stages`: only these of one wizard
#[tauri::command]
async fn start_setup(app: AppHandle, state: State<'_, App>, wizards: Vec<String>, stages: Option<Vec<String>>, check: bool) -> R<()> {
  let mut words = vec!["./homelab".to_string(), "setup".into(), "--protocol".into()];
  for w in &wizards {
    if !WIZARDS.contains(&w.as_str()) {
      return Err(format!("unknown wizard {w}"));
    }
    words.push(w.clone());
  }
  let stages = stages.unwrap_or_default();
  if !stages.is_empty() {
    if wizards.len() != 1 {
      return Err("steps of one wizard at a time".into());
    }
    if !stages.iter().all(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')) {
      return Err("odd step".into());
    }
    words.push(format!("--stages={}", stages.join(",")));
  }
  if check {
    words.push("--check".into());
  }
  let s = conn(&state)?.stream(Cwd::Repo, &words).await?;
  start(&app, &state, "setup", s)
}

/// The answer to the protocol's current question (lib/setup/run.py reads {"answer": …} lines)
#[tauri::command]
async fn answer(state: State<'_, App>, text: String) -> R<()> {
  let tx = state.running.lock().expect("lock").clone().ok_or("nothing is running")?;
  let msg = json!({ "answer": text }).to_string() + "\n";
  tx.send(Input::Data(msg.into_bytes())).await.map_err(|_| "it already ended".to_string())
}

#[tauri::command]
fn stop(state: State<'_, App>) {
  if let Some(tx) = state.running.lock().expect("lock").as_ref() {
    let _ = tx.try_send(Input::Kill);
  }
}

#[tauri::command]
fn open_url(app: AppHandle, url: String) -> R<()> {
  use tauri_plugin_opener::OpenerExt;
  if !(url.starts_with("https://") || url.starts_with("http://")) {
    return Err("only web links".into());
  }
  app.opener().open_url(url, None::<&str>).map_err(|e| e.to_string())
}

// ── The assistant ──

fn settings_file(app: &AppHandle) -> R<PathBuf> {
  Ok(known_hosts(app)?.with_file_name("assistant.json"))
}

#[tauri::command]
async fn assistant_status(app: AppHandle, state: State<'_, App>) -> R<Value> {
  let mut ai = state.ai.lock().await;
  if ai.provider.is_empty() {
    let saved: Value = settings_file(&app).ok().and_then(|p| std::fs::read_to_string(p).ok()).and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
    ai.provider = saved["provider"].as_str().unwrap_or("anthropic").into();
    ai.model = saved["model"].as_str().unwrap_or("").into();
    ai.ollama_url = saved["ollama_url"].as_str().unwrap_or(assistant::OLLAMA_URL).into();
  }
  let has_key = tokio::task::spawn_blocking(assistant::key_get).await.map_err(|e| e.to_string())?.is_some();
  Ok(json!({
    "provider": ai.provider, "model": ai.model, "ollama_url": ai.ollama_url, "has_key": has_key,
    "default_model": assistant::ANTHROPIC_MODEL,
  }))
}

/// Settings; a non-empty key goes to the keychain, an empty `key` with `forget` removes it
#[tauri::command]
async fn assistant_settings(app: AppHandle, state: State<'_, App>, provider: String, model: String, ollama_url: String, key: Option<String>, forget: bool) -> R<()> {
  if provider != "anthropic" && provider != "ollama" {
    return Err("unknown provider".into());
  }
  if !(ollama_url.starts_with("http://") || ollama_url.starts_with("https://")) {
    return Err("the Ollama address starts with http:// or https://".into());
  }
  if let Some(k) = key.filter(|k| !k.is_empty()) {
    tokio::task::spawn_blocking(move || assistant::key_set(&k)).await.map_err(|e| e.to_string())??;
  } else if forget {
    tokio::task::spawn_blocking(|| assistant::key_set("")).await.map_err(|e| e.to_string())??;
  }
  let mut ai = state.ai.lock().await;
  if ai.provider != provider {
    ai.history.clear(); // the two keep different histories
  }
  ai.provider = provider;
  ai.model = model;
  ai.ollama_url = ollama_url;
  let saved = json!({ "provider": ai.provider, "model": ai.model, "ollama_url": ai.ollama_url });
  std::fs::write(settings_file(&app)?, serde_json::to_string_pretty(&saved).unwrap_or_default()).map_err(|e| e.to_string())
}

#[tauri::command]
async fn assistant_reset(state: State<'_, App>) -> R<()> {
  let mut ai = state.ai.lock().await;
  ai.history.clear();
  ai.asking = None;
  Ok(())
}

/// The person's yes or no to a tool that changes something
#[tauri::command]
fn assistant_decide(state: State<'_, App>, id: String, allow: bool) {
  if let Some(tx) = state.pending.lock().expect("lock").remove(&id) {
    let _ = tx.send(allow);
  }
}

/// One message from the person; the model works (tools included) until it answers. Its words, its tool
/// calls and the questions for the person arrive as "assistant" events.
#[tauri::command]
async fn assistant_send(app: AppHandle, state: State<'_, App>, text: String) -> R<()> {
  let c = conn(&state)?;
  let mut ai = state.ai.lock().await;
  let provider = match ai.provider.as_str() {
    "ollama" => assistant::Provider::Ollama {
      url: ai.ollama_url.clone(),
      model: if ai.model.is_empty() { "qwen3".into() } else { ai.model.clone() },
    },
    _ => assistant::Provider::Anthropic {
      key: tokio::task::spawn_blocking(assistant::key_get).await.map_err(|e| e.to_string())?.ok_or("no Anthropic API key yet: add it in the assistant's settings")?,
      model: if ai.model.is_empty() { assistant::ANTHROPIC_MODEL.into() } else { ai.model.clone() },
      url: assistant::ANTHROPIC_URL.into(),
    },
  };
  if ai.mcp.is_none() {
    ai.mcp = Some(assistant::Mcp::start(&c).await?);
  }
  let emit = |v: Value| {
    let _ = app.emit("assistant", v);
  };
  let http = reqwest::Client::new();
  let system = assistant::system_prompt();
  ai.history.push(json!({ "role": "user", "content": text }));
  for _ in 0..24 {
    let mcp = ai.mcp.as_ref().expect("started");
    let tools = provider.tools(&mcp.tools);
    let turn = match provider.turn(&http, &system, &ai.history, &tools).await {
      Ok(t) => t,
      Err(e) => {
        ai.history.pop_if(|m| m["role"] == "user" && m["content"].is_string()); // so the person can send it again
        return Err(e);
      }
    };
    ai.history.push(turn.assistant.clone());
    if !turn.text.trim().is_empty() {
      emit(json!({ "kind": "text", "text": turn.text }));
    }
    if turn.calls.is_empty() {
      emit(json!({ "kind": "done" }));
      return Ok(());
    }
    let mut results = Vec::new();
    for call in turn.calls {
      let mcp = ai.mcp.as_ref().expect("started");
      let allowed = if mcp.read_only(&call.name) {
        true
      } else {
        let id = format!("{}-{}", call.name, std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0));
        let (tx, rx) = oneshot::channel();
        state.pending.lock().expect("lock").insert(id.clone(), tx);
        let question = if call.name == "answer" { ai.asking.clone() } else { None };
        emit(json!({ "kind": "confirm", "id": id, "tool": call.name, "input": call.input, "question": question }));
        rx.await.unwrap_or(false)
      };
      let (out, err) = if allowed {
        emit(json!({ "kind": "tool", "tool": call.name, "input": call.input }));
        match mcp.call(&call.name, &call.input).await {
          Ok(r) => r,
          Err(e) => (e, true),
        }
      } else {
        ("The person said no to this. Ask what they'd like instead.".to_string(), true)
      };
      if !err {
        // Only the stage tools say where the wizard stands (run_stage, poll, answer: "state")
        if let Ok(v) = serde_json::from_str::<Value>(&out) {
          if v.get("state").is_some() {
            ai.asking = v.get("question").filter(|q| q.is_object()).cloned();
          }
        }
      }
      emit(json!({ "kind": "result", "tool": call.name, "error": err, "text": out.chars().take(4000).collect::<String>() }));
      results.push((call, out, err));
    }
    let more = provider.results(&results);
    ai.history.extend(more);
  }
  emit(json!({ "kind": "text", "text": "(stopped after many steps; say how to go on)" }));
  emit(json!({ "kind": "done" }));
  Ok(())
}

fn main() {
  tauri::Builder::default()
    .plugin(tauri_plugin_opener::init())
    .plugin(tauri_plugin_dialog::init())
    .manage(App::default())
    .invoke_handler(tauri::generate_handler![
      info, host_key, connect_local, connect_ssh, disconnect, set_sudo, probe, catalogue, plan, apply,
      set_secret, export_answers, save_text, bootstrap, start_setup, answer, stop, open_url, assistant_status,
      assistant_settings, assistant_reset, assistant_decide, assistant_send
    ])
    .run(tauri::generate_context!())
    .expect("the setup app failed to start");
}
