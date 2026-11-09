//! Where the homelab runs: this computer (a child process) or another one (an SSH channel, russh).
//! Both run an argv, never a shell string built from the UI: over SSH each word is quoted.

use std::{path::PathBuf, sync::Arc, time::Duration};

use russh::{
  client,
  keys::{self, HashAlg, PrivateKeyWithHashAlg, PublicKeyOrCertificate},
  ChannelMsg, Sig,
};
use tokio::{
  io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
  process::Command,
  sync::{mpsc, oneshot},
};
use zeroize::Zeroizing;

pub struct Output {
  pub code: i32,
  pub stdout: String,
  pub stderr: String,
}

/// Where a command runs: the repository, or the home directory (bootstrap, before there is one)
#[derive(Clone, Copy)]
pub enum Cwd {
  Repo,
  Home,
}

pub enum Conn {
  Local { dir: PathBuf },
  Ssh { handle: client::Handle<Client>, dir: String, label: String },
}

/// What a running stream accepts. Data can be an answer with a secret or the sudo password in it: wiped once
/// written
pub enum Input {
  Data(Zeroizing<Vec<u8>>),
  Kill,
}

pub struct Stream {
  pub lines: mpsc::Receiver<String>,
  pub input: mpsc::Sender<Input>,
  pub done: oneshot::Receiver<i32>,
}

/// One shell word: 'it'\''s'
pub fn quote(s: &str) -> String {
  format!("'{}'", s.replace('\'', "'\\''"))
}

/// `cd` into the repository on the far side: a leading ~/ is the remote user's home
fn remote_cd(dir: &str) -> String {
  match dir.strip_prefix("~/") {
    Some(rest) => format!("cd \"$HOME\"/{}", quote(rest)),
    None if dir == "~" => "cd \"$HOME\"".into(),
    None => format!("cd {}", quote(dir)),
  }
}

impl Conn {
  pub fn label(&self) -> String {
    match self {
      Conn::Local { dir } => format!("this computer ({})", dir.display()),
      Conn::Ssh { label, dir, .. } => format!("{label} ({dir})"),
    }
  }

  fn remote_command(dir: &str, cwd: Cwd, argv: &[String]) -> String {
    let words: Vec<String> = argv.iter().map(|a| quote(a)).collect();
    let cd = match cwd {
      Cwd::Repo => remote_cd(dir),
      Cwd::Home => "cd \"$HOME\"".into(),
    };
    format!("{cd} && exec {}", words.join(" "))
  }

  fn local_command(dir: &PathBuf, cwd: Cwd, argv: &[String]) -> Command {
    let mut c = Command::new(&argv[0]);
    c.args(&argv[1..]);
    match cwd {
      Cwd::Repo => {
        c.current_dir(dir);
      }
      Cwd::Home => {
        if let Some(h) = dirs::home_dir() {
          c.current_dir(h);
        }
      }
    }
    c.kill_on_drop(true);
    c
  }

  /// Run to the end; stdin (if any) is written and closed first
  pub async fn run(&self, cwd: Cwd, argv: &[String], stdin: Option<&[u8]>) -> Result<Output, String> {
    match self {
      Conn::Local { dir } => {
        let mut c = Self::local_command(dir, cwd, argv);
        c.stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::piped());
        let mut child = c.spawn().map_err(|e| format!("{}: {e}", argv[0]))?;
        let mut sin = child.stdin.take().expect("piped");
        if let Some(data) = stdin {
          sin.write_all(data).await.map_err(|e| e.to_string())?;
        }
        drop(sin);
        let out = child.wait_with_output().await.map_err(|e| e.to_string())?;
        Ok(Output {
          code: out.status.code().unwrap_or(-1),
          stdout: String::from_utf8_lossy(&out.stdout).into(),
          stderr: String::from_utf8_lossy(&out.stderr).into(),
        })
      }
      Conn::Ssh { handle, dir, .. } => {
        let mut ch = handle.channel_open_session().await.map_err(|e| e.to_string())?;
        ch.exec(true, Self::remote_command(dir, cwd, argv)).await.map_err(|e| e.to_string())?;
        if let Some(data) = stdin {
          ch.data(data).await.map_err(|e| e.to_string())?;
        }
        ch.eof().await.map_err(|e| e.to_string())?;
        let (mut out, mut err, mut code) = (Vec::new(), Vec::new(), -1);
        while let Some(msg) = ch.wait().await {
          match msg {
            ChannelMsg::Data { data } => out.extend_from_slice(&data),
            ChannelMsg::ExtendedData { data, ext: 1 } => err.extend_from_slice(&data),
            ChannelMsg::ExitStatus { exit_status } => code = exit_status as i32,
            _ => {}
          }
        }
        Ok(Output { code, stdout: String::from_utf8_lossy(&out).into(), stderr: String::from_utf8_lossy(&err).into() })
      }
    }
  }

  /// Run with stdout and stderr as lines, as they come, and stdin open for more (the protocol)
  pub async fn stream(&self, cwd: Cwd, argv: &[String]) -> Result<Stream, String> {
    let (line_tx, lines) = mpsc::channel::<String>(256);
    let (input, mut in_rx) = mpsc::channel::<Input>(16);
    let (done_tx, done) = oneshot::channel::<i32>();
    match self {
      Conn::Local { dir } => {
        let mut c = Self::local_command(dir, cwd, argv);
        c.stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::piped());
        let mut child = c.spawn().map_err(|e| format!("{}: {e}", argv[0]))?;
        let mut sin = child.stdin.take().expect("piped");
        for r in [Box::new(child.stdout.take().expect("piped")) as Box<dyn tokio::io::AsyncRead + Unpin + Send>,
                  Box::new(child.stderr.take().expect("piped"))] {
          let tx = line_tx.clone();
          tokio::spawn(async move {
            let mut rd = BufReader::new(r).lines();
            while let Ok(Some(l)) = rd.next_line().await {
              if tx.send(l).await.is_err() {
                break;
              }
            }
          });
        }
        drop(line_tx);
        let (kill_tx, kill_rx) = oneshot::channel::<()>();
        tokio::spawn(async move {
          let mut kill_tx = Some(kill_tx);
          while let Some(i) = in_rx.recv().await {
            match i {
              Input::Data(d) => {
                if sin.write_all(&d).await.is_err() || sin.flush().await.is_err() {
                  break;
                }
              }
              Input::Kill => {
                if let Some(k) = kill_tx.take() {
                  let _ = k.send(());
                }
                break;
              }
            }
          }
        });
        tokio::spawn(async move {
          let code = tokio::select! {
            s = child.wait() => s.ok().and_then(|s| s.code()).unwrap_or(-1),
            _ = kill_rx => { let _ = child.kill().await; -1 }
          };
          let _ = done_tx.send(code);
        });
      }
      Conn::Ssh { handle, dir, .. } => {
        let ch = handle.channel_open_session().await.map_err(|e| e.to_string())?;
        ch.exec(true, Self::remote_command(dir, cwd, argv)).await.map_err(|e| e.to_string())?;
        let (mut rd, wr) = ch.split();
        tokio::spawn(async move {
          while let Some(i) = in_rx.recv().await {
            match i {
              Input::Data(d) => {
                if wr.data(&d[..]).await.is_err() {
                  break;
                }
              }
              Input::Kill => {
                let _ = wr.signal(Sig::TERM).await;
                let _ = wr.close().await;
                break;
              }
            }
          }
        });
        tokio::spawn(async move {
          let (mut out, mut err, mut code) = (Vec::new(), Vec::new(), -1);
          async fn flush(buf: &mut Vec<u8>, tx: &mpsc::Sender<String>, all: bool) {
            while let Some(i) = buf.iter().position(|&b| b == b'\n') {
              let line: Vec<u8> = buf.drain(..=i).collect();
              let _ = tx.send(String::from_utf8_lossy(&line[..line.len() - 1]).into()).await;
            }
            if all && !buf.is_empty() {
              let _ = tx.send(String::from_utf8_lossy(buf).into()).await;
              buf.clear();
            }
          }
          while let Some(msg) = rd.wait().await {
            match msg {
              ChannelMsg::Data { data } => {
                out.extend_from_slice(&data);
                flush(&mut out, &line_tx, false).await;
              }
              ChannelMsg::ExtendedData { data, .. } => {
                err.extend_from_slice(&data);
                flush(&mut err, &line_tx, false).await;
              }
              ChannelMsg::ExitStatus { exit_status } => code = exit_status as i32,
              _ => {}
            }
          }
          flush(&mut out, &line_tx, true).await;
          flush(&mut err, &line_tx, true).await;
          let _ = done_tx.send(code);
        });
      }
    }
    Ok(Stream { lines, input, done })
  }
}

/// Reads a whole file (a key) without holding it longer than needed
pub async fn read_small(path: &str) -> Result<String, String> {
  let mut f = tokio::fs::File::open(path).await.map_err(|e| format!("{path}: {e}"))?;
  let mut s = String::new();
  f.read_to_string(&mut s).await.map_err(|e| format!("{path}: {e}"))?;
  Ok(s)
}

// ── SSH ──────────────────────────────────────────────────────────────────

/// Accepts exactly the host key the person confirmed (or none, to learn it)
pub struct Client {
  expect: Option<String>,
  seen: Arc<std::sync::Mutex<Option<String>>>,
}

pub fn fingerprint(key: &PublicKeyOrCertificate) -> String {
  match key {
    PublicKeyOrCertificate::PublicKey { key, .. } => key.fingerprint(HashAlg::Sha256).to_string(),
    PublicKeyOrCertificate::Certificate(c) => c.public_key().fingerprint(HashAlg::Sha256).to_string(),
  }
}

impl client::Handler for Client {
  type Error = russh::Error;

  async fn check_server_key(&mut self, key: &PublicKeyOrCertificate) -> Result<bool, Self::Error> {
    let fp = fingerprint(key);
    *self.seen.lock().expect("lock") = Some(fp.clone());
    Ok(self.expect.as_deref() == Some(fp.as_str()))
  }
}

fn config() -> Arc<client::Config> {
  Arc::new(client::Config {
    inactivity_timeout: None,
    keepalive_interval: Some(Duration::from_secs(30)),
    ..Default::default()
  })
}

/// The server's host key fingerprint (SHA256:…), without logging in
pub async fn host_key(host: &str, port: u16) -> Result<String, String> {
  let seen = Arc::new(std::sync::Mutex::new(None));
  let c = Client { expect: None, seen: seen.clone() };
  let r = tokio::time::timeout(Duration::from_secs(15), client::connect(config(), (host, port), c)).await;
  if let Some(fp) = seen.lock().expect("lock").clone() {
    return Ok(fp);
  }
  match r {
    Err(_) => Err(format!("{host}:{port} didn't answer in 15 s")),
    Ok(Err(e)) => Err(format!("{host}:{port}: {e}")),
    Ok(Ok(_)) => Err("no host key seen".into()),
  }
}

pub struct Login<'a> {
  pub host: &'a str,
  pub port: u16,
  pub user: &'a str,
  pub password: Option<&'a str>,
  pub key_path: Option<&'a str>,
  pub key_passphrase: Option<&'a str>,
  pub fingerprint: &'a str,
}

/// Log in: the key first (if any), then the password, then keyboard-interactive with the password
/// for every prompt (servers like this homelab's own sshd take "password;TOTP" there)
pub async fn connect(l: Login<'_>) -> Result<client::Handle<Client>, String> {
  let seen = Arc::new(std::sync::Mutex::new(None));
  let c = Client { expect: Some(l.fingerprint.to_string()), seen: seen.clone() };
  let mut h = tokio::time::timeout(Duration::from_secs(15), client::connect(config(), (l.host, l.port), c))
    .await
    .map_err(|_| format!("{}:{} didn't answer in 15 s", l.host, l.port))?
    .map_err(|e| {
      match seen.lock().expect("lock").as_deref() {
        Some(fp) if fp != l.fingerprint => format!("the host key changed: now {fp}. Not connecting."),
        _ => format!("{}:{}: {e}", l.host, l.port),
      }
    })?;
  let mut ok = false;
  if let Some(path) = l.key_path.filter(|p| !p.is_empty()) {
    let text = read_small(path).await?;
    let key = keys::decode_secret_key(&text, l.key_passphrase.filter(|p| !p.is_empty())).map_err(|e| format!("the key: {e}"))?;
    let hash = h.best_supported_rsa_hash().await.map_err(|e| e.to_string())?.flatten();
    let r = h.authenticate_publickey(l.user, PrivateKeyWithHashAlg::new(Arc::new(key), hash)).await.map_err(|e| e.to_string())?;
    ok = r.success();
  }
  if let (false, Some(pw)) = (ok, l.password.filter(|p| !p.is_empty())) {
    ok = h.authenticate_password(l.user, pw).await.map_err(|e| e.to_string())?.success();
    if !ok {
      let mut r = h.authenticate_keyboard_interactive_start(l.user, None).await.map_err(|e| e.to_string())?;
      for _ in 0..4 {
        match r {
          client::KeyboardInteractiveAuthResponse::Success => {
            ok = true;
            break;
          }
          client::KeyboardInteractiveAuthResponse::Failure { .. } => break,
          client::KeyboardInteractiveAuthResponse::InfoRequest { ref prompts, .. } => {
            let answers = prompts.iter().map(|_| pw.to_string()).collect();
            r = h.authenticate_keyboard_interactive_respond(answers).await.map_err(|e| e.to_string())?;
          }
        }
      }
    }
  }
  if !ok {
    return Err("sign-in refused: check the user, the password (with ;code if your server asks for one) or the key".into());
  }
  Ok(h)
}

#[cfg(test)]
mod tests {
  use super::*;

  fn words(w: &[&str]) -> Vec<String> {
    w.iter().map(|s| s.to_string()).collect()
  }

  #[test]
  fn quoting() {
    assert_eq!(quote("it's"), r"'it'\''s'");
    assert_eq!(remote_cd("~/homelab"), "cd \"$HOME\"/'homelab'");
    assert_eq!(remote_cd("/srv/a b"), "cd '/srv/a b'");
    assert_eq!(
      Conn::remote_command("~/h", Cwd::Repo, &words(&["./homelab", "setup", "--answers", "-", "x; rm -rf /"])),
      "cd \"$HOME\"/'h' && exec './homelab' 'setup' '--answers' '-' 'x; rm -rf /'"
    );
  }

  #[tokio::test]
  async fn local_run_and_stream() {
    let c = Conn::Local { dir: std::env::temp_dir() };
    let o = c.run(Cwd::Repo, &words(&["cat"]), Some(b"hello\n")).await.unwrap();
    assert_eq!((o.code, o.stdout.as_str()), (0, "hello\n"));
    let o = c.run(Cwd::Repo, &words(&["sh", "-c", "echo oops >&2; exit 3"]), None).await.unwrap();
    assert_eq!((o.code, o.stderr.as_str()), (3, "oops\n"));

    let mut s = c.stream(Cwd::Repo, &words(&["sh", "-c", "read a; echo \"got $a\"; read b; echo \"got $b\""])).await.unwrap();
    s.input.send(Input::Data(b"one\n".to_vec().into())).await.unwrap();
    assert_eq!(s.lines.recv().await.unwrap(), "got one");
    s.input.send(Input::Data(b"two\n".to_vec().into())).await.unwrap();
    assert_eq!(s.lines.recv().await.unwrap(), "got two");
    assert_eq!(s.done.await.unwrap(), 0);

    let s = c.stream(Cwd::Repo, &words(&["sleep", "30"])).await.unwrap();
    s.input.send(Input::Kill).await.unwrap();
    assert_eq!(tokio::time::timeout(Duration::from_secs(5), s.done).await.unwrap().unwrap(), -1);
  }

  /// Against a real sshd: SETUP_APP_SSH="host port user keyfile fingerprint" (tests/setup-app/ssh.sh)
  #[tokio::test]
  async fn ssh_round_trip() {
    let Ok(spec) = std::env::var("SETUP_APP_SSH") else {
      eprintln!("SETUP_APP_SSH not set: skipped");
      return;
    };
    let p: Vec<&str> = spec.split(' ').collect();
    let (host, port, user, key, fp) = (p[0], p[1].parse::<u16>().unwrap(), p[2], p[3], p[4]);
    assert_eq!(host_key(host, port).await.unwrap(), fp);
    let login = |fingerprint| Login { host, port, user, password: None, key_path: Some(key), key_passphrase: None, fingerprint };
    let err = connect(login("SHA256:not-this-one")).await.err().unwrap();
    assert!(err.contains("host key changed"), "{err}");
    let handle = connect(login(fp)).await.unwrap();
    let c = Conn::Ssh { handle, dir: "/".into(), label: "test".into() };
    let o = c.run(Cwd::Repo, &words(&["printf", "%s|", "a'b", "$HOME", "x; echo no"]), None).await.unwrap();
    assert_eq!((o.code, o.stdout.as_str()), (0, "a'b|$HOME|x; echo no|"));
    let o = c.run(Cwd::Repo, &words(&["cat"]), Some(b"in")).await.unwrap();
    assert_eq!(o.stdout, "in");
    let o = c.run(Cwd::Repo, &words(&["sh", "-c", "pwd; exit 4"]), None).await.unwrap();
    assert_eq!((o.code, o.stdout.as_str()), (4, "/\n"));
    let mut s = c.stream(Cwd::Home, &words(&["sh", "-c", "read a; echo \"got $a\"; echo err >&2"])).await.unwrap();
    s.input.send(Input::Data(b"one\n".to_vec().into())).await.unwrap();
    let mut got = vec![s.lines.recv().await.unwrap(), s.lines.recv().await.unwrap()];
    got.sort();
    assert_eq!(got, ["err", "got one"]);
    assert_eq!(s.done.await.unwrap(), 0);
  }
}
