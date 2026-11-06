//! The assistant (plan Phase 3c): a model that sets the homelab up with the person, through the same
//! MCP server Claude Code uses (lib/setup/mcp.py), started on the machine over the app's transport.
//! Providers: Anthropic's Messages API (raw HTTP; the key lives in the OS keychain) or a local Ollama.
//! Every tool that changes something waits for the person's yes in the window (main.rs).

use std::{
  sync::atomic::{AtomicU64, Ordering},
  time::Duration,
};

use serde_json::{json, Value};
use tokio::sync::{mpsc, Mutex};

use crate::transport::{Conn, Cwd, Input};

type R<T> = Result<T, String>;

// ── The MCP server on the machine ────────────────────────────────────────

pub struct Mcp {
  input: mpsc::Sender<Input>,
  lines: Mutex<mpsc::Receiver<String>>,
  next: AtomicU64,
  pub tools: Vec<Value>,
}

impl Mcp {
  pub async fn start(conn: &Conn) -> R<Mcp> {
    // Through ./homelab, which puts a Python 3.11+ first (an activated conda env's older python3 is often first
    // on PATH, also for a command over SSH)
    let s = conn.stream(Cwd::Repo, &["./homelab".to_string(), "mcp".to_string()]).await?;
    let mut m = Mcp { input: s.input, lines: Mutex::new(s.lines), next: AtomicU64::new(1), tools: vec![] };
    m.request("initialize", json!({
      "protocolVersion": "2025-06-18", "capabilities": {},
      "clientInfo": { "name": "homelab-setup-app", "version": env!("CARGO_PKG_VERSION") }
    }))
    .await?;
    m.send(json!({ "jsonrpc": "2.0", "method": "notifications/initialized" })).await?;
    m.tools = m.request("tools/list", json!({})).await?["tools"].as_array().cloned().unwrap_or_default();
    Ok(m)
  }

  async fn send(&self, msg: Value) -> R<()> {
    self.input.send(Input::Data((msg.to_string() + "\n").into_bytes())).await.map_err(|_| "the MCP server ended".to_string())
  }

  /// One request at a time: the answer is the next line with its id (a stage can take minutes)
  pub async fn request(&self, method: &str, params: Value) -> R<Value> {
    let mut lines = self.lines.lock().await;
    let id = self.next.fetch_add(1, Ordering::SeqCst);
    self.send(json!({ "jsonrpc": "2.0", "id": id, "method": method, "params": params })).await?;
    loop {
      let line = tokio::time::timeout(Duration::from_secs(600), lines.recv())
        .await
        .map_err(|_| format!("{method}: no answer in 10 minutes"))?
        .ok_or("the MCP server ended")?;
      let Ok(msg) = serde_json::from_str::<Value>(&line) else { continue };
      if msg["id"] != json!(id) {
        continue;
      }
      if let Some(e) = msg.get("error") {
        return Err(format!("{method}: {}", e["message"].as_str().unwrap_or("error")));
      }
      return Ok(msg["result"].clone());
    }
  }

  /// The tool's text, and whether it failed
  pub async fn call(&self, name: &str, args: &Value) -> R<(String, bool)> {
    let r = self.request("tools/call", json!({ "name": name, "arguments": args })).await?;
    let text = r["content"].as_array().map(|c| c.iter().filter_map(|b| b["text"].as_str()).collect::<Vec<_>>().join("\n")).unwrap_or_default();
    Ok((text, r["isError"] == json!(true)))
  }

  /// Read-only tools run straight away; everything else asks the person first
  pub fn read_only(&self, name: &str) -> bool {
    self.tools.iter().any(|t| t["name"] == json!(name) && t["annotations"]["readOnlyHint"] == json!(true))
  }

  pub async fn stop(&self) {
    let _ = self.input.send(Input::Kill).await;
  }
}

// ── Providers ────────────────────────────────────────────────────────────

pub const ANTHROPIC_MODEL: &str = "claude-opus-5-5";
pub const ANTHROPIC_URL: &str = "https://api.anthropic.com/v1/messages";
pub const OLLAMA_URL: &str = "http://localhost:11434";

#[derive(Clone, Debug)]
pub enum Provider {
  Anthropic { key: String, model: String, url: String },
  Ollama { url: String, model: String },
}

#[derive(Clone, Debug, PartialEq)]
pub struct Call {
  pub id: String,
  pub name: String,
  pub input: Value,
}

#[derive(Debug)]
pub struct Turn {
  pub text: String,
  pub calls: Vec<Call>,
  /// The assistant message to append to the history, unchanged (thinking and fallback blocks included)
  pub assistant: Value,
}

impl Provider {
  /// The MCP tools in the provider's own shape
  pub fn tools(&self, mcp: &[Value]) -> Vec<Value> {
    mcp
      .iter()
      .map(|t| match self {
        Provider::Anthropic { .. } => json!({ "name": t["name"], "description": t["description"], "input_schema": t["inputSchema"] }),
        Provider::Ollama { .. } => json!({ "type": "function", "function": { "name": t["name"], "description": t["description"], "parameters": t["inputSchema"] } }),
      })
      .collect()
  }

  pub fn body(&self, system: &str, messages: &[Value], tools: &[Value]) -> Value {
    match self {
      Provider::Anthropic { model, .. } => json!({
        "model": model, "max_tokens": 16000, "system": system, "messages": messages, "tools": tools,
        // Opus 5.5 defaults to medium; said explicitly. A refusal reruns on the fallback model the API picks.
        "output_config": { "effort": "medium" },
        "fallbacks": "default",
      }),
      Provider::Ollama { model, .. } => {
        let mut all = vec![json!({ "role": "system", "content": system })];
        all.extend(messages.iter().cloned());
        json!({ "model": model, "messages": all, "tools": tools, "stream": false })
      }
    }
  }

  pub async fn turn(&self, http: &reqwest::Client, system: &str, messages: &[Value], tools: &[Value]) -> R<Turn> {
    let body = self.body(system, messages, tools);
    let req = match self {
      Provider::Anthropic { key, url, .. } => http
        .post(url)
        .header("x-api-key", key)
        .header("anthropic-version", "2026-10-21")
        .header("anthropic-beta", "server-side-fallback-2026-07-01"),
      Provider::Ollama { url, .. } => http.post(format!("{}/api/chat", url.trim_end_matches('/'))),
    };
    let resp = req.json(&body).timeout(Duration::from_secs(600)).send().await.map_err(|e| match self {
      Provider::Ollama { url, .. } => format!("Ollama at {url} isn't answering ({e})"),
      _ => format!("couldn't reach Anthropic ({e})"),
    })?;
    let status = resp.status().as_u16();
    let retry = resp.headers().get("retry-after").and_then(|v| v.to_str().ok()).map(str::to_string);
    let v: Value = resp.json().await.map_err(|e| format!("an answer that isn't JSON ({e})"))?;
    if status != 200 {
      return Err(http_error(self, status, retry.as_deref(), &v));
    }
    self.parse(v)
  }

  pub fn parse(&self, v: Value) -> R<Turn> {
    match self {
      Provider::Anthropic { .. } => {
        if v["stop_reason"] == json!("refusal") {
          return Err("the model declined this request; try saying it differently".into());
        }
        let content = v["content"].as_array().cloned().unwrap_or_default();
        let text = content.iter().filter(|b| b["type"] == json!("text")).filter_map(|b| b["text"].as_str()).collect::<Vec<_>>().join("\n");
        let calls = content
          .iter()
          .filter(|b| b["type"] == json!("tool_use"))
          .map(|b| Call { id: b["id"].as_str().unwrap_or_default().into(), name: b["name"].as_str().unwrap_or_default().into(), input: b["input"].clone() })
          .collect();
        let mut text = text;
        if v["stop_reason"] == json!("max_tokens") {
          text.push_str("\n(cut off: the answer was too long)");
        }
        Ok(Turn { text, calls, assistant: json!({ "role": "assistant", "content": content }) })
      }
      Provider::Ollama { .. } => {
        let m = v["message"].clone();
        let calls = m["tool_calls"]
          .as_array()
          .map(|cs| {
            cs.iter()
              .enumerate()
              .map(|(i, c)| {
                // Arguments come as an object (some models send a JSON string)
                let a = &c["function"]["arguments"];
                let input = a.as_str().and_then(|s| serde_json::from_str(s).ok()).unwrap_or_else(|| a.clone());
                Call { id: format!("call_{i}"), name: c["function"]["name"].as_str().unwrap_or_default().into(), input }
              })
              .collect()
          })
          .unwrap_or_default();
        Ok(Turn { text: m["content"].as_str().unwrap_or_default().into(), calls, assistant: m })
      }
    }
  }

  /// The history entries carrying the tools' results (Anthropic: one user message with every result)
  pub fn results(&self, results: &[(Call, String, bool)]) -> Vec<Value> {
    match self {
      Provider::Anthropic { .. } => vec![json!({
        "role": "user",
        "content": results.iter().map(|(c, text, err)| json!({ "type": "tool_result", "tool_use_id": c.id, "content": text, "is_error": err })).collect::<Vec<_>>()
      })],
      Provider::Ollama { .. } => results.iter().map(|(c, text, _)| json!({ "role": "tool", "content": text, "tool_name": c.name })).collect(),
    }
  }
}

fn http_error(p: &Provider, status: u16, retry: Option<&str>, v: &Value) -> String {
  let msg = v["error"]["message"].as_str().or_else(|| v["error"].as_str()).unwrap_or("").to_string();
  match (p, status) {
    (Provider::Anthropic { .. }, 401) => "Anthropic refused the API key: check it in the assistant's settings".into(),
    (Provider::Anthropic { .. }, 403) => format!("this API key may not do that ({msg})"),
    (Provider::Anthropic { .. }, 404) => format!("unknown model ({msg})"),
    (_, 429) => format!("too many requests just now; try again in {} s", retry.unwrap_or("a few")),
    (_, s) if s >= 500 => format!("the model's service has trouble right now (HTTP {s}); try again in a minute"),
    (Provider::Ollama { .. }, 404) => format!("Ollama doesn't have that model: ollama pull it first ({msg})"),
    (_, s) => format!("HTTP {s}: {msg}"),
  }
}

// ── The API key, in the OS keychain ───────────────────────────────────────

const SERVICE: &str = "homelab-setup";
const ACCOUNT: &str = "anthropic-api-key";

pub fn key_get() -> Option<String> {
  keyring::Entry::new(SERVICE, ACCOUNT).ok().and_then(|e| e.get_password().ok()).or_else(|| std::env::var("ANTHROPIC_API_KEY").ok()).filter(|k| !k.is_empty())
}

pub fn key_set(key: &str) -> R<()> {
  let e = keyring::Entry::new(SERVICE, ACCOUNT).map_err(|e| e.to_string())?;
  if key.is_empty() {
    return match e.delete_credential() {
      Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
      Err(err) => Err(err.to_string()),
    };
  }
  e.set_password(key).map_err(|e| format!("the keychain refused it ({e})"))
}

/// What the assistant is told (the same guide Claude Code reads), without the skill's front matter
pub fn system_prompt() -> String {
  let skill = include_str!("../../../.claude/skills/homelab-setup/SKILL.md");
  let body = skill.splitn(3, "---").nth(2).unwrap_or(skill).trim();
  format!(
    "You are the assistant inside the homelab's setup app. The person sees every tool you call; tools that \
change something wait for their yes. Answer briefly, in plain words, in the person's language.\n\n{body}"
  )
}

#[cfg(test)]
mod tests {
  use super::*;

  fn anthropic() -> Provider {
    Provider::Anthropic { key: "k".into(), model: ANTHROPIC_MODEL.into(), url: ANTHROPIC_URL.into() }
  }
  fn ollama() -> Provider {
    Provider::Ollama { url: OLLAMA_URL.into(), model: "qwen3".into() }
  }
  fn mcp_tools() -> Vec<Value> {
    vec![json!({ "name": "plan", "description": "What a choice would do", "inputSchema": { "type": "object", "properties": { "preset": { "type": "string" } } }, "annotations": { "readOnlyHint": true } })]
  }

  #[test]
  fn tool_shapes() {
    let a = anthropic().tools(&mcp_tools());
    assert_eq!(a[0]["input_schema"]["type"], "object");
    assert_eq!(a[0]["name"], "plan");
    let o = ollama().tools(&mcp_tools());
    assert_eq!(o[0]["function"]["parameters"]["properties"]["preset"]["type"], "string");
  }

  #[test]
  fn anthropic_request_and_turn() {
    let p = anthropic();
    let b = p.body("sys", &[json!({ "role": "user", "content": "hi" })], &p.tools(&mcp_tools()));
    assert_eq!(b["model"], "claude-opus-5-5");
    assert_eq!(b["output_config"]["effort"], "medium");
    assert!(b.get("temperature").is_none());
    // A recorded-shape answer: thinking, text and a tool call; the assistant message keeps every block
    let t = p
      .parse(json!({ "stop_reason": "tool_use", "content": [
        { "type": "thinking", "thinking": "", "signature": "sig" },
        { "type": "text", "text": "Let me plan that." },
        { "type": "tool_use", "id": "toolu_1", "name": "plan", "input": { "preset": "minimal" } } ] }))
      .unwrap();
    assert_eq!(t.text, "Let me plan that.");
    assert_eq!(t.calls, vec![Call { id: "toolu_1".into(), name: "plan".into(), input: json!({ "preset": "minimal" }) }]);
    assert_eq!(t.assistant["content"].as_array().unwrap().len(), 3);
    let r = p.results(&[(t.calls[0].clone(), "{}".into(), false)]);
    assert_eq!(r.len(), 1);
    assert_eq!(r[0]["content"][0]["tool_use_id"], "toolu_1");
    assert!(p.parse(json!({ "stop_reason": "refusal", "content": [] })).is_err());
  }

  #[test]
  fn ollama_request_and_turn() {
    let p = ollama();
    let b = p.body("sys", &[json!({ "role": "user", "content": "hi" })], &[]);
    assert_eq!(b["messages"][0]["role"], "system");
    assert_eq!(b["stream"], false);
    let t = p
      .parse(json!({ "message": { "role": "assistant", "content": "", "tool_calls": [
        { "function": { "name": "plan", "arguments": { "preset": "family" } } },
        { "function": { "name": "stages", "arguments": "{}" } } ] }, "done_reason": "stop" }))
      .unwrap();
    assert_eq!(t.calls.len(), 2);
    assert_eq!(t.calls[0].input["preset"], "family");
    assert_eq!(t.calls[1].input, json!({}));
    let r = p.results(&[(t.calls[0].clone(), "ok".into(), false)]);
    assert_eq!(r[0]["role"], "tool");
  }

  #[test]
  fn errors_in_words() {
    assert!(http_error(&anthropic(), 401, None, &json!({})).contains("API key"));
    assert!(http_error(&anthropic(), 429, Some("12"), &json!({})).contains("12 s"));
    assert!(http_error(&ollama(), 404, None, &json!({ "error": "model not found" })).contains("ollama pull"));
  }

  #[test]
  fn prompt_is_the_skill() {
    let s = system_prompt();
    assert!(s.contains("Never ask for a secret"));
    assert!(!s.contains("name: homelab-setup"));
  }

  /// The real MCP server through the local transport
  #[tokio::test]
  async fn mcp_round_trip() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize().unwrap();
    let conn = Conn::Local { dir: root };
    let m = Mcp::start(&conn).await.unwrap();
    assert!(m.tools.iter().any(|t| t["name"] == "run_stage"));
    assert!(m.read_only("plan"));
    assert!(!m.read_only("apply"));
    let (text, err) = m.call("stages", &json!({})).await.unwrap();
    assert!(!err, "{text}");
    assert!(text.contains("wizard:dns"));
    let (_, err) = m.call("plan", &json!({ "options": { "AK_SECRET_KEY": "x" } })).await.unwrap();
    assert!(err);
    m.stop().await;
  }
}
