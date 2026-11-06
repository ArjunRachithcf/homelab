// ~/homelab/setup-app/ui/assistant.js: the assistant drawer (src-tauri/src/assistant.rs does the work).
// Its words, tool calls and questions arrive as "assistant" events; a tool that changes something
// waits for Allow here. Text only via textContent.
(function () {
  "use strict";
  var T = window.__TAURI__, invoke = T.core.invoke;
  var $ = function (id) { return document.getElementById(id); };
  var log = $("ai-log"), busy = false, status = null;

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function add(node) { log.appendChild(node); log.scrollTop = log.scrollHeight; return node; }
  function setBusy(b) { busy = b; $("ai-send").disabled = b; $("ai-send").textContent = b ? "Working…" : "Send"; }

  var NAMES = { list_components: "Looked at the components", plan: "Worked out a plan", apply: "Saved the choice", set_option: "Set an option",
                stages: "Looked at the setup steps", run_stage: "Ran a setup step", answer: "Answered the step", poll: "Waited for the step",
                stop: "Stopped the step", run_check: "Ran the checks", status: "Looked at the jobs", logs: "Read a log" };
  // The same, as what it's about to do (progress lines and the questions)
  var ACTS = { list_components: "Look at the components", plan: "Work out a plan", apply: "Save this choice (.env, then configure)",
               set_option: "Set an option", stages: "Look at the setup steps", run_stage: "Run a setup step", answer: "Answer the step",
               poll: "Wait for the step", stop: "Stop the step", run_check: "Run the checks", status: "Look at the jobs", logs: "Read a log" };

  function toggle(open) {
    $("ai").hidden = !open;
    $("ai-toggle").setAttribute("aria-expanded", String(open));
    if (open) { loadStatus(); $("ai-text").focus(); }
  }
  $("ai-toggle").addEventListener("click", function () { toggle($("ai").hidden); });
  $("ai-close").addEventListener("click", function () { toggle(false); });

  function showProvider() {
    var p = $("ai-provider").value;
    $("ai-key-row").hidden = p !== "anthropic";
    $("ai-forget").hidden = p !== "anthropic";
    $("ai-url-row").hidden = p !== "ollama";
    $("ai-model").placeholder = p === "anthropic" ? (status ? status.default_model : "") : "qwen3";
    $("ai-model-hint").textContent = p === "ollama" ? "A model with tool use, pulled on that Ollama (ollama pull qwen3)." : "Empty: the default.";
    $("ai-privacy").textContent = p === "anthropic"
      ? "What the assistant reads (components, plans, the setup's messages; secrets replaced by ‹NAME›) goes to Anthropic."
      : "Everything stays on your network.";
  }
  $("ai-provider").addEventListener("change", showProvider);

  function loadStatus() {
    return invoke("assistant_status").then(function (s) {
      status = s;
      $("ai-provider").value = s.provider; $("ai-model").value = s.model; $("ai-url").value = s.ollama_url;
      $("ai-key-state").textContent = s.has_key ? "A key is saved in this computer's keychain. Type a new one to replace it." : "Kept in this computer's keychain, never in a file.";
      if (s.provider === "anthropic" && !s.has_key) $("ai-settings").open = true;
      showProvider();
    });
  }

  $("ai-save").addEventListener("click", function () {
    var key = $("ai-key").value;
    invoke("assistant_settings", { provider: $("ai-provider").value, model: $("ai-model").value.trim(), ollamaUrl: $("ai-url").value.trim(), key: key || null, forget: false })
      .then(function () { $("ai-key").value = ""; $("ai-settings").open = false; return loadStatus(); })
      .catch(function (e) { add(el("p", "bad", String(e))); });
  });
  $("ai-forget").addEventListener("click", function () {
    invoke("assistant_settings", { provider: $("ai-provider").value, model: $("ai-model").value.trim(), ollamaUrl: $("ai-url").value.trim(), key: null, forget: true })
      .then(loadStatus).catch(function (e) { add(el("p", "bad", String(e))); });
  });
  $("ai-reset").addEventListener("click", function () { invoke("assistant_reset").then(function () { log.replaceChildren(); }); });

  $("ai-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var text = $("ai-text").value.trim();
    if (!text || busy) return;
    add(el("div", "me", text));
    $("ai-text").value = "";
    setBusy(true);
    invoke("assistant_send", { text: text }).catch(function (err) {
      add(el("p", "bad", String(err))); setBusy(false);
    });
  });
  $("ai-text").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("ai-form").requestSubmit(); }
  });

  T.event.listen("assistant", function (ev) {
    var m = ev.payload;
    if (m.kind === "text") add(el("div", "it", m.text));
    else if (m.kind === "tool") add(el("div", "tool", "⚙ " + (ACTS[m.tool] || m.tool) + "…"));
    else if (m.kind === "result") {
      var d = el("details"); d.appendChild(el("summary", m.error ? "bad" : null, (m.error ? "✗ " : "✓ ") + (NAMES[m.tool] || m.tool)));
      d.appendChild(el("pre", null, m.text)); add(d);
    } else if (m.kind === "confirm") {
      var c = el("div", "card confirm");
      c.appendChild(el("strong", null, "May it " + (ACTS[m.tool] || m.tool).replace(/^./, function (x) { return x.toLowerCase(); }) + "?"));
      // An answer: the wizard's own question, word for word (not the model's summary of it)
      var q = m.question;
      if (m.tool === "answer" && q) c.appendChild(el("p", "hint", "The wizard asks: " + (q.text || q.prompt || q.key || "?")));
      else if (m.tool === "answer") c.appendChild(el("p", "bad", "No open question from the wizard is known: check before allowing."));
      c.appendChild(el("pre", null, JSON.stringify(m.input, null, 2)));
      var row = el("div", "row");
      function decide(allow) {
        Array.prototype.forEach.call(row.querySelectorAll("button"), function (b) { b.disabled = true; });
        row.appendChild(el("span", "hint", allow ? "allowed" : "not allowed"));
        invoke("assistant_decide", { id: m.id, allow: allow });
      }
      var no = el("button", null, "Don't"), yes = el("button", "primary", "Allow");
      no.type = yes.type = "button";
      no.addEventListener("click", function () { decide(false); });
      yes.addEventListener("click", function () { decide(true); });
      row.append(no, yes); c.appendChild(row); add(c);  // no focus on Allow: Enter in the text box mustn't allow
    } else if (m.kind === "done") setBusy(false);
  });
})();
