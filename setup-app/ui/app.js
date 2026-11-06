// ~/homelab/setup-app/ui/app.js: the setup app's window. It chooses; src-tauri/src/main.rs runs.
// Data: ./homelab components --json (catalogue), --plan/--apply --json (the engine), and the
// wizards' protocol (lib/setup/run.py) as "stream-line" events. Text only via textContent (rich() too).
(function () {
  "use strict";
  var T = window.__TAURI__;
  var invoke = T.core.invoke, listen = T.event.listen;
  var $ = function (id) { return document.getElementById(id); };
  var state = { info: null, where: "", cat: null, chosen: [], gpu: "", options: {}, secrets: {}, mode: "basic", plan: null, ssh: null, stages: {}, checked: {} };

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function btn(text, cls, fn) {
    var b = el("button", cls || null, text); b.type = "button";
    if (fn) b.addEventListener("click", fn);
    return b;
  }
  function msg(id, text, bad) { var m = $(id); m.textContent = text || ""; m.className = "msg" + (bad ? " bad" : ""); }
  // The wizards' emphasis (lib/wizard.sh, protocol): ⟦b⟧ bold, ⟦d⟧ dim, ⟦r⟧ ⟦y⟧ ⟦g⟧ ⟦u⟧ red, yellow, green,
  // blue; ⟦/⟧ ends them all. Spans with a class each, the text via textContent
  var MARK = { b: "m-b", d: "m-d", r: "m-r", y: "m-y", g: "m-g", u: "m-u" };
  function rich(tag, cls, text) {
    var e = el(tag, cls), on = [];
    String(text == null ? "" : text).split(/(⟦[bdrygu\/]⟧)/).forEach(function (part) {
      var m = /^⟦([bdrygu\/])⟧$/.exec(part);
      if (m) { if (m[1] === "/") on = []; else if (on.indexOf(MARK[m[1]]) < 0) on.push(MARK[m[1]]); return; }
      if (part) e.appendChild(on.length ? el("span", on.join(" "), part) : document.createTextNode(part));
    });
    return e;
  }
  function errText(e) { return typeof e === "string" ? e : (e && e.message) || String(e); }

  // ── Steps ──
  var ORDER = ["connect", "machine", "choose", "run"];
  function go(step) {
    ORDER.forEach(function (s, i) {
      $("p-" + s).hidden = s !== step;
      var li = document.querySelector('#steps [data-step="' + s + '"]');
      if (s === step) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
      li.classList.toggle("done", i < ORDER.indexOf(step));
    });
    $("main").scrollTop = 0;
  }

  // ── 1. Connect ──
  function pick(which) {
    $("pick-local").setAttribute("aria-pressed", String(which === "local"));
    $("pick-ssh").setAttribute("aria-pressed", String(which === "ssh"));
    $("f-local").hidden = which !== "local";
    $("f-ssh").hidden = which !== "ssh";
    msg("connect-msg", "");
  }
  $("pick-local").addEventListener("click", function () { pick("local"); });
  $("pick-ssh").addEventListener("click", function () { pick("ssh"); });

  $("f-local").addEventListener("submit", function (e) {
    e.preventDefault();
    invoke("connect_local", { dir: e.target.dir.value.trim() || "~/homelab" }).then(connected, function (err) { msg("connect-msg", errText(err), true); });
  });

  $("f-ssh").addEventListener("submit", function (e) {
    e.preventDefault();
    var f = e.target;
    var login = {
      host: f.host.value.trim(), port: parseInt(f.port.value, 10) || 22, user: f.user.value.trim(),
      password: f.password.value || null, keyPath: f.key_path.value.trim() || null, keyPassphrase: f.key_passphrase.value || null,
      dir: f.dir.value.trim() || "~/homelab"
    };
    msg("connect-msg", "Asking " + login.host + " for its key…");
    invoke("host_key", { host: login.host, port: login.port }).then(function (k) {
      if (k.known === k.fingerprint) return sshLogin(login, k.fingerprint);
      confirmHostKey(login, k);
    }, function (err) { msg("connect-msg", errText(err), true); });
  });

  // First connection (or a changed key): the person compares the fingerprint, as ssh would ask
  function confirmHostKey(login, k) {
    var box = el("div", "card form");
    if (k.known) {
      box.appendChild(el("p", "bad", "This computer's key changed since the last time. That happens after a reinstall; it also happens when something sits in between. Only go on if you know why."));
      box.appendChild(el("p", null, "Before: " + k.known));
    } else {
      box.appendChild(el("p", null, "First connection to " + login.host + ". To be sure it's really that computer, compare its key there (ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub):"));
    }
    box.appendChild(el("code", "cmd", k.fingerprint));
    var row = el("div", "row");
    row.appendChild(btn("Cancel", null, function () { box.remove(); msg("connect-msg", ""); }));
    row.appendChild(btn(k.known ? "Trust the new key" : "It matches, connect", k.known ? "danger" : "primary", function () { box.remove(); sshLogin(login, k.fingerprint); }));
    box.appendChild(row);
    $("connect-msg").before(box);
    msg("connect-msg", "");
  }

  function sshLogin(login, fp) {
    msg("connect-msg", "Signing in…");
    invoke("connect_ssh", Object.assign({}, login, { fingerprint: fp })).then(function (label) {
      state.ssh = login;
      // The login password is often the sudo password too: offer it there
      if (login.password && login.password.indexOf(";") < 0) $("sudo-pw").value = login.password;
      connected(label);
    }, function (err) { msg("connect-msg", errText(err), true); });
  }

  function connected(label) {
    state.where = label;
    $("where").textContent = "Connected to " + label;
    msg("connect-msg", "");
    probe();
  }

  // ── 2. This machine ──
  function probe() {
    return invoke("probe").then(function (p) {
      var facts = $("facts");
      facts.replaceChildren();
      function fact(name, value, good) {
        var li = el("li"); li.append(el("span", null, name), el("span", good === false ? "bad" : null, value)); facts.appendChild(li);
      }
      fact("System", p.distro || p.os || "unknown");
      fact("Python 3.11+", p.python === "ok" ? "yes" : p.python === "old" ? "too old (installed with the homelab)" : "no (installed with the homelab)", p.python === "ok" ? null : undefined);
      fact("git", p.git === "yes" ? "yes" : "no (installed with the homelab)");
      fact("The homelab", p.repo === "yes" ? "in " + p.dir : "not there yet");
      fact("Settings (.env)", p.env === "yes" ? "there" : "none yet");
      state.root = p.root === "yes";
      state.hasEnv = p.env === "yes";
      $("get").hidden = p.repo === "yes";
      $("sudo-box").hidden = state.root;
      $("to-choose").hidden = p.repo !== "yes";
      $("release").textContent = state.info ? state.info.ref + (state.info.commit ? ", commit " + state.info.commit.slice(0, 12) : "") : "";
      go("machine");
    }, function (err) { msg("connect-msg", errText(err), true); go("connect"); });
  }

  $("do-bootstrap").addEventListener("click", function () {
    var pw = $("sudo-pw").value;
    var b = $("do-bootstrap"); b.disabled = true;
    var log = $("boot-log"); log.hidden = false; log.textContent = "";
    (state.root || !pw ? Promise.resolve() : invoke("set_sudo", { password: pw })).then(function () {
      return invoke("bootstrap");
    }).catch(function (err) { log.textContent += errText(err) + "\n"; b.disabled = false; });
  });
  $("to-choose").addEventListener("click", function () { loadCatalogue().then(function () { go("choose"); }); });

  // ── 3. Choose ──
  function loadCatalogue() {
    return invoke("catalogue").then(function (c) {
      state.cat = c;
      state.chosen = c.chosen.slice();
      state.gpu = c.gpu;
      renderPresets();
      renderCatalogue();
      replan();
    }, function (err) { alert(errText(err)); });
  }

  function comp(id) { return state.cat.components.filter(function (c) { return c.id === id; })[0]; }
  function titleOf(id) { var c = state.cat && comp(id); return c ? c.title : id; }

  function renderPresets() {
    var box = $("presets"); box.replaceChildren();
    state.cat.presets.forEach(function (p) {
      var b = el("button", "card pick"); b.type = "button";
      b.append(el("strong", null, p.title), el("span", null, p.description), el("small", null, p.components.length + " parts"));
      b.addEventListener("click", function () {
        state.chosen = p.components.slice();
        Array.prototype.forEach.call(box.children, function (x) { x.setAttribute("aria-pressed", String(x === b)); });
        renderCatalogue(); replan();
      });
      box.appendChild(b);
    });
  }

  var CATS = { core: "Always there", access: "Getting in", apps: "Apps", media: "Watch, listen, read", storage: "Disks", backup: "Backups", home: "Home" };

  // The options shown for a component: Basic = what's still needed; Advanced = every non-secret one
  // Secrets: only the ones nothing else fills in (no generator, no wizard); kept in this window's
  // memory until Apply, then sent one by one over stdin (set_secret), never in the answers file
  function shownOptions(c) {
    return c.options.filter(function (o) {
      var needed = o.required && !o.set && o.default == null && !o.set_by && !o.generate;
      if (o.type === "secret") return needed;
      return state.mode === "advanced" || needed;
    });
  }

  function optionInput(o) {
    var lab = el("label", null, o.key);
    var input;
    if (o.type === "secret") {
      input = el("input"); input.type = "password"; input.autocomplete = "off"; input.value = state.secrets[o.key] || "";
      input.addEventListener("change", function () {
        if (input.value) state.secrets[o.key] = input.value; else delete state.secrets[o.key];
        renderPlan();
      });
      lab.append(input, el("small", null, o.help + " (stays secret: written straight into the settings there)"));
      return lab;
    }
    if (o.type === "bool") {
      input = el("select");
      ["", "true", "false"].forEach(function (v) { var opt = el("option", null, v || "(default)"); opt.value = v; input.appendChild(opt); });
    } else {
      input = el("input");
      input.type = o.type === "int" || o.type === "port" ? "number" : o.type === "email" ? "email" : "text";
      input.spellcheck = false;
      input.placeholder = o.default != null ? String(o.default) : o.required ? "needed" : "";
    }
    input.value = state.options[o.key] != null ? state.options[o.key] : (o.value || "");
    input.addEventListener("change", function () {
      var v = input.value.trim();
      if (v === "" || v === (o.value || "")) delete state.options[o.key]; else state.options[o.key] = v;
      replan();
    });
    lab.append(input, el("small", null, o.help));
    return lab;
  }

  function renderCatalogue() {
    var box = $("catalogue"); box.replaceChildren();
    var enabled = state.plan ? state.plan.enabled : state.chosen;
    var groups = {};
    state.cat.components.forEach(function (c) { (groups[c.category] = groups[c.category] || []).push(c); });
    Object.keys(groups).forEach(function (g) {
      box.appendChild(el("h3", null, CATS[g] || g));
      var wrap = el("div", "group");
      groups[g].forEach(function (c) {
        var row = el("div", "comp");
        var t = el("div"); t.append(el("div", "t", c.title), el("div", "d", c.description));
        var on = enabled.indexOf(c.id) >= 0, mine = state.chosen.indexOf(c.id) >= 0;
        if (on && !mine && !c.core) {
          var by = state.cat.components.filter(function (x) { return enabled.indexOf(x.id) >= 0 && x.requires.indexOf(c.id) >= 0; }).map(function (x) { return x.title; });
          t.appendChild(el("div", "why", "Comes with " + (by.join(", ") || "what you chose")));
        }
        if (c.gpu === "nvidia") t.appendChild(el("div", "why", state.gpu === "nvidia" ? "Uses the NVIDIA card" : "Needs an NVIDIA card (none found)"));
        var away = state.cat.platform && c.platforms && c.platforms.indexOf(state.cat.platform) < 0;
        if (away) t.appendChild(el("div", "why", "Not available on " + state.cat.platform + " (only " + c.platforms.join(", ") + ")"));
        var sw = el("input", "switch"); sw.type = "checkbox"; sw.checked = on || c.core; sw.disabled = !!c.core || (away && !on);
        sw.setAttribute("aria-label", c.title);
        sw.addEventListener("change", function () {
          if (sw.checked) { if (state.chosen.indexOf(c.id) < 0) state.chosen.push(c.id); }
          else state.chosen = state.chosen.filter(function (x) { return x !== c.id; });
          replan();
        });
        row.append(t, sw);
        var opts = on ? shownOptions(c) : [];
        if (opts.length) {
          var o = el("div", "opts");
          opts.forEach(function (x) { o.appendChild(optionInput(x)); });
          row.appendChild(o);
        }
        wrap.appendChild(row);
      });
      box.appendChild(wrap);
    });
    if (state.mode === "advanced") {
      var g = el("label", null, "Graphics card");
      var s = el("select");
      [["none", "None (or not NVIDIA)"], ["nvidia", "NVIDIA"]].forEach(function (p) { var o = el("option", null, p[1]); o.value = p[0]; s.appendChild(o); });
      s.value = state.gpu || "none";
      s.addEventListener("change", function () { state.gpu = s.value; replan(); });
      g.appendChild(s);
      box.prepend(g);
    }
  }

  Array.prototype.forEach.call(document.querySelectorAll(".seg button"), function (b) {
    b.addEventListener("click", function () {
      state.mode = b.dataset.mode;
      Array.prototype.forEach.call(document.querySelectorAll(".seg button"), function (x) { x.setAttribute("aria-pressed", String(x === b)); });
      if (state.cat) renderCatalogue();
    });
  });

  // An answers file for ./homelab setup --plan/--apply (lib/setup/engine.py); secrets never go in it
  function tomlStr(v) {
    return '"' + String(v).replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/[\u0000-\u001f]/g, "") + '"';
  }
  function answers() {
    var out = ["# Answers for ./homelab setup --apply (written by the setup app; no secrets)",
               "components = [" + state.chosen.map(tomlStr).join(", ") + "]"];
    if (state.gpu) out.push("gpu = " + tomlStr(state.gpu));
    out.push("", "[options]");
    Object.keys(state.options).sort().forEach(function (k) { out.push(k + " = " + tomlStr(state.options[k])); });
    return out.join("\n") + "\n";
  }

  var planTimer = null, planSeq = 0;
  function replan() {
    clearTimeout(planTimer);
    planTimer = setTimeout(function () {
      var seq = ++planSeq;
      invoke("plan", { answers: answers() }).then(function (p) {
        if (seq !== planSeq) return;
        state.plan = p; renderPlan(); renderCatalogue();
      }, function (err) { if (seq === planSeq) $("plan").replaceChildren(el("p", "bad", errText(err))); });
    }, 250);
  }

  function renderPlan() {
    var p = state.plan, box = $("plan");
    $("off-confirm").hidden = true; // it asked about the plan before this one
    box.replaceChildren(el("h2", null, "What happens"));
    box.appendChild(el("p", null, p.enabled.length + " parts, " + p.stages.length + " setup steps."));
    function list(title, items, cls) {
      if (!items.length) return;
      box.appendChild(el("strong", cls || null, title));
      var ul = el("ul");
      items.forEach(function (t) { ul.appendChild(el("li", cls || null, t)); });
      box.appendChild(ul);
    }
    // Against what the machine's .env has now (nothing on a new machine)
    list("Switched on", (p.switch_on || []).map(titleOf), "good");
    list("Switched off", (p.switch_off || []).map(titleOf), "bad");
    if (p.switch_off_means) box.appendChild(el("p", "hint", "Switched off: " + p.switch_off_means + "."));
    list("Can't be done", p.errors, "bad");
    list("Not valid", p.invalid.map(function (x) { return x.key + ": " + x.why; }), "bad");
    var missing = p.missing.filter(function (x) { return !state.secrets[x.key]; });
    list("Still needed", missing.map(function (x) { return x.key + ": " + x.help; }), "warn");
    list("Good to know", p.notes);
    if (p.generate.length) list("Made up for you (never shown)", [p.generate.length + " secrets"]);
    if (p.later.length) list("Asked during setup", p.later.map(function (x) { return x.key; }));
    $("do-apply").disabled = !!(p.errors.length || p.invalid.length || missing.length);
    $("to-run").hidden = !(state.hasEnv && p.same_components && $("do-apply").disabled === false && unchanged());
  }

  // Nothing chosen differently from what the machine's .env says: its steps can run as they are
  function unchanged() {
    var a = state.chosen, b = state.cat.chosen;
    return a.length === b.length && a.every(function (x) { return b.indexOf(x) >= 0; }) && !Object.keys(state.options).length
      && !Object.keys(state.secrets).length && (state.gpu || "") === (state.cat.gpu || "");
  }
  $("to-run").addEventListener("click", function () { toRun(null); });

  $("save-answers").addEventListener("click", function () {
    T.dialog.save({ defaultPath: "homelab-answers.toml", filters: [{ name: "Answers", extensions: ["toml"] }] }).then(function (path) {
      if (path) return invoke("save_text", { path: path, text: answers() });
    }).catch(function (err) { alert(errText(err)); });
  });

  // Apply: plan exactly what goes out (the panel's plan trails the switches by the replan's delay), then ask
  // first when it switches off something the machine has (a preset replaces the whole choice)
  $("do-apply").addEventListener("click", function () {
    var a = answers(), b = $("do-apply"), seq = ++planSeq;
    clearTimeout(planTimer);
    b.disabled = true;
    invoke("plan", { answers: a }).then(function (p) {
      if (seq !== planSeq) { b.disabled = false; return; } // the choice changed meanwhile: its own plan shows
      state.plan = p; renderPlan(); renderCatalogue();
      if (b.disabled) return; // can't be done or still needs something: the panel says what
      if (p.switch_off.length) confirmOff(p.switch_off, a); else apply(a);
    }, function (err) { b.disabled = false; alert(errText(err)); });
  });
  function confirmOff(off, a) {
    var box = $("off-confirm");
    box.replaceChildren(el("strong", "bad", "Switch off " + off.length + (off.length === 1 ? " part" : " parts") + " this machine has?"));
    var ul = el("ul"); off.forEach(function (id) { ul.appendChild(el("li", null, titleOf(id))); });
    box.append(ul, el("p", null, "Apply saves this right away: " + state.plan.switch_off_means + "."));
    var row = el("div", "row");
    row.append(btn("Keep them", "primary", function () { box.hidden = true; }),
               btn("Switch them off and apply", "danger", function () {
                 box.hidden = true;
                 if (answers() === a) apply(a); else $("do-apply").click(); // changed since: plan and ask again
               }));
    box.appendChild(row);
    box.hidden = false; // in the sticky row, above Apply
  }

  function apply(a) {
    var b = $("do-apply"); b.disabled = true;
    invoke("apply", { answers: a }).then(function (r) {
      b.disabled = false;
      if (!r.applied) { state.plan = r; renderPlan(); return; }
      var keys = Object.keys(state.secrets);
      return keys.reduce(function (pr, k) {
        return pr.then(function () { return invoke("set_secret", { key: k, value: state.secrets[k] }); });
      }, Promise.resolve()).then(function () {
        state.secrets = {}; state.options = {};
        if (r.configure && r.configure.code !== 0) { alert("Saved, but ./homelab configure said:\n" + r.configure.output); return; }
        // The steps' titles carry the domains, which this Apply may have written just now
        return invoke("catalogue").then(function (c) { state.cat.stage_titles = c.stage_titles; }, function () {})
          .then(function () { toRun(r); });
      });
    }).catch(function (err) { b.disabled = false; alert(errText(err)); });
  }

  // ── 4. Set up: the wizards' protocol ──
  var feed = $("feed"), ask = $("ask");
  function add(node) { feed.appendChild(node); node.scrollIntoView({ block: "nearest" }); return node; }
  var WIZ = { wizard: "The machine", public: "Public access", hardening: "Sign-in and backups", immich: "Photos", apps: "Apps", music: "Music", files: "Files and reading", family: "Family life" };

  function toRun(applied) { go("run"); renderApplied(applied); renderParts(); }

  // What Apply just did: configure's own words (e.g. Caddy to recreate) and what was switched off but still runs
  function renderApplied(r) {
    var box = $("applied");
    box.replaceChildren();
    box.hidden = !r;
    if (!r) return;
    box.appendChild(el("strong", "good", "Saved."));
    var out = r.configure && r.configure.output ? r.configure.output.trim() : "";
    if (out) box.appendChild(el("pre", "log", out));
    if (r.still_running && r.still_running.length) {
      box.appendChild(el("p", "warn", "Switched off, but their containers still run: " + r.still_running.map(titleOf).join(", ")
        + ". To stop and remove them (their data volumes stay), in the homelab's folder:"));
      box.appendChild(el("code", "cmd", r.stop_command));
      box.appendChild(btn("Copy", null, function () { navigator.clipboard && navigator.clipboard.writeText(r.stop_command); }));
    }
  }

  // The plan's stages by wizard ("part"), in order: each part can be checked or started, each step run alone
  function renderParts() {
    var box = $("parts"), by = {}, order = [], titles = (state.cat && state.cat.stage_titles) || {};
    box.replaceChildren();
    ((state.plan && state.plan.stages) || []).forEach(function (s) {
      var w = s.split(":")[0];
      if (!by[w]) { by[w] = []; order.push(w); }
      by[w].push(s.slice(w.length + 1));
    });
    order.forEach(function (w) {
      var part = el("div", "part"), head = el("div", "part-head"), list = el("ol", "part-steps");
      list.hidden = true;
      var tog = btn(WIZ[w] || w, "part-toggle", function () {
        list.hidden = !list.hidden; tog.setAttribute("aria-expanded", String(!list.hidden));
      });
      tog.setAttribute("aria-expanded", "false");
      tog.appendChild(el("small", null, by[w].length + (by[w].length === 1 ? " step" : " steps")));
      var badge = el("span", "badge"); badge.dataset.wizard = w;
      head.append(tog, badge, btn("Check", "run-btn", function () { startRun({ wizards: [w], check: true }); }),
                  btn("Start", "run-btn", function () { startRun({ wizards: [w] }); }));
      by[w].forEach(function (id) {
        var li = el("li");
        li.append(el("span", null, titles[w + ":" + id] || id.replace(/_/g, " ")),
                  btn("Run", "run-btn", function () { startRun({ wizards: [w], stages: [id] }); }));
        list.appendChild(li);
      });
      part.append(head, list);
      box.appendChild(part);
    });
    showBadges();
  }

  // What the last check of each part found (only check runs: a stage's own checks are a part of them)
  function showBadges() {
    Array.prototype.forEach.call(document.querySelectorAll("#parts .badge"), function (b) {
      var c = state.checked[b.dataset.wizard];
      if (!c || !c.done || c.stopped) { b.textContent = c ? (c.stopped ? "check stopped" : "checking…") : ""; b.className = "badge"; return; }
      var good = !c.no && !c.code;
      b.textContent = good ? "✓ checks pass" : c.no ? "✗ " + c.no + (c.no === 1 ? " check fails" : " checks fail") : "✗ stopped (exit " + c.code + ")";
      b.className = "badge " + (good ? "good" : "bad");
    });
  }

  function startRun(opts) {
    feed.replaceChildren(); $("stages").replaceChildren(); state.stages = {};
    state.checking = !!opts.check;
    running(true);
    invoke("start_setup", { wizards: opts.wizards || [], stages: opts.stages || null, check: !!opts.check })
      .catch(function (err) { add(el("p", "bad", errText(err))); ended(); });
  }
  function running(on) {
    $("run-start").hidden = on; $("run-check").hidden = on; $("run-stop").hidden = !on;
    Array.prototype.forEach.call(document.querySelectorAll("#parts .run-btn"), function (b) { b.disabled = on; });
  }
  function ended() {
    running(false); ask.hidden = true;
    // A check stopped before its wizard said "exit"
    Object.keys(state.checked).forEach(function (w) { var c = state.checked[w]; if (!c.done) { c.done = true; c.stopped = true; } });
    showBadges();
  }
  $("run-start").addEventListener("click", function () { startRun({}); });
  $("run-check").addEventListener("click", function () { startRun({ check: true }); });
  $("run-stop").addEventListener("click", function () { invoke("stop"); });

  function reply(text, shown) {
    ask.hidden = true; ask.replaceChildren();
    add(el("div", "answered", "→ " + shown));
    return invoke("answer", { text: text }).catch(function (err) { add(el("p", "bad", errText(err))); });
  }

  function askBox(title) {
    ask.replaceChildren(); ask.hidden = false;
    if (title) ask.appendChild(rich("strong", null, title));
    return ask;
  }

  function field(type, value, placeholder) {
    var i = el("input"); i.type = type; i.value = value || ""; i.placeholder = placeholder || ""; i.spellcheck = false;
    return i;
  }

  function onMessage(m) {
    var a, i, row;
    switch (m.t) {
      case "start":
        add(el("div", "wiz", WIZ[m.wizard] || m.wizard));
        if (state.checking) { state.checked[m.wizard] = { no: 0, done: false }; showBadges(); }
        break;
      case "banner": break; // the wizard's title: "start" already said it
      case "stage": {
        var key = m.wizard + ":" + m.id;
        Array.prototype.forEach.call(document.querySelectorAll("#stages li.now"), function (x) { x.classList.remove("now"); x.classList.add("done"); });
        var li = el("li", "now", m.title); $("stages").appendChild(li); state.stages[key] = li;
        add(el("div", "stage", m.n + "/" + m.of + "  " + m.title));
        break;
      }
      case "say": add(rich("div", "say " + (m.style || "say"), m.text)); break;
      case "note": add(rich("div", "say note", m.text)); break;
      case "output": add(rich("div", "out", m.text)); break;
      case "link": {
        var p = el("div", "say"), l = el("a", null, m.url); l.href = "#";
        l.addEventListener("click", function (e) { e.preventDefault(); invoke("open_url", { url: m.url }); });
        p.appendChild(l); add(p); break;
      }
      case "check":
        add(rich("div", "chk " + (m.ok ? "ok" : "no"), m.label));
        if (state.checking && !m.ok && state.checked[m.wizard]) state.checked[m.wizard].no++;
        break;
      case "wrote": add(el("div", "answered", "saved " + m.key)); break;
      case "pause":
        a = askBox(m.text);
        a.appendChild(btn("Continue", "primary", function () { reply("", "continued"); }));
        break;
      case "confirm":
        a = askBox(m.text); row = el("div", "row");
        row.append(btn("No", null, function () { reply("n", "no"); }), btn("Yes", "primary", function () { reply("y", "yes"); }));
        a.appendChild(row); break;
      case "input":
        a = askBox(m.prompt); i = field("text", m.current);
        a.append(i, btn("OK", "primary", function () { reply(i.value, (m.key ? m.key + " = " : "") + (i.value || "(kept)")); }));
        i.addEventListener("keydown", function (e) { if (e.key === "Enter") a.querySelector("button").click(); });
        i.focus(); break;
      case "secret":
        a = askBox(m.prompt); i = field("password", "", m.current_set ? "Empty keeps the saved one" : "");
        a.appendChild(i);
        a.appendChild(el("small", "hint", "Goes straight into the settings on that machine; never shown again."));
        a.appendChild(btn("OK", "primary", function () { reply(i.value, (m.key || "secret") + " = ••••"); }));
        i.focus(); break;
      case "lines": {
        a = askBox(m.prompt); var ta = el("textarea"); a.appendChild(ta);
        a.appendChild(btn("OK", "primary", function () {
          var ls = ta.value.split("\n").map(function (x) { return x.trim(); }).filter(Boolean);
          ask.hidden = true;
          add(el("div", "answered", "→ " + ls.length + " line(s)"));
          ls.concat([""]).reduce(function (pr, x) { return pr.then(function () { return invoke("answer", { text: x }); }); }, Promise.resolve());
        }));
        ta.focus(); break;
      }
      case "danger": {
        a = askBox("Careful"); a.appendChild(rich("p", "bad", m.text));
        a.appendChild(el("p", null, "Type " + m.word + " to go on; anything else stops."));
        i = field("text", ""); a.appendChild(i);
        a.appendChild(btn("Go on", "danger", function () { reply(i.value, i.value === m.word ? m.word : "stopped"); }));
        i.focus(); break;
      }
      case "terminal": {
        a = askBox(m.text);
        a.appendChild(el("p", null, state.ssh ? "Open a terminal, sign in there (ssh " + state.ssh.user + "@" + state.ssh.host + ") and run:" : "Open a terminal on this computer and run:"));
        var c = el("code", "cmd", m.command); a.appendChild(c);
        row = el("div", "row");
        row.append(btn("Copy", null, function () { navigator.clipboard && navigator.clipboard.writeText(m.command); }),
                   btn("Done", "primary", function () { reply("", "done in a terminal"); }));
        a.appendChild(row); break;
      }
      case "sudo": {
        a = askBox("Password for sudo" + (m.wizard ? " (" + (WIZ[m.wizard] || m.wizard) + ")" : ""));
        i = field("password", ""); a.appendChild(i);
        var keep = el("label", "hint"); var cb = el("input"); cb.type = "checkbox"; cb.checked = true;
        keep.style.flexDirection = "row"; keep.append(cb, document.createTextNode(" Remember until the app closes"));
        a.appendChild(keep);
        a.appendChild(btn("OK", "primary", function () {
          var pw = i.value;
          (cb.checked ? invoke("set_sudo", { password: pw }) : Promise.resolve()).then(function () { reply(pw, "sudo password given"); });
        }));
        i.focus(); break;
      }
      case "finish": {
        var f = el("div", "card");
        f.appendChild(el("strong", "good", "✓ " + (WIZ[m.wizard] || m.wizard) + " done"));
        if (m.wrote && m.wrote.length) f.appendChild(el("p", "hint", "Saved: " + m.wrote.join(", ")));
        if (m.todo && m.todo.length) {
          f.appendChild(el("p", "warn", "Left for later:"));
          var ul = el("ul"); m.todo.forEach(function (x) { ul.appendChild(rich("li", null, x)); }); f.appendChild(ul);
        }
        add(f); break;
      }
      case "exit":
        if (state.checking) {
          if (state.checked[m.wizard]) { state.checked[m.wizard].done = true; state.checked[m.wizard].code = m.code; }
          showBadges();
        } else if (m.code) add(el("p", "bad", (WIZ[m.wizard] || m.wizard) + " stopped (exit " + m.code + "). Fix what it says, then start it again: finished steps offer to be skipped."));
        break;
      case "done":
        Array.prototype.forEach.call(document.querySelectorAll("#stages li.now"), function (x) { x.classList.remove("now"); x.classList.add("done"); });
        add(el("p", m.failed.length ? "bad" : "good", m.failed.length ? "Not finished: " + m.failed.join(", ") : "All done."));
        break;
      default: break;
    }
  }

  listen("stream-line", function (ev) {
    var p = ev.payload;
    if (p.kind === "bootstrap") {
      var log = $("boot-log"); log.textContent += p.line + "\n"; log.scrollTop = log.scrollHeight;
      return;
    }
    var m;
    try { m = JSON.parse(p.line); } catch (e) { m = { t: "output", text: p.line }; }
    if (!m || typeof m !== "object" || typeof m.t !== "string") m = { t: "output", text: p.line };
    onMessage(m);
  });
  listen("stream-end", function (ev) {
    var p = ev.payload;
    if (p.kind === "bootstrap") {
      $("do-bootstrap").disabled = false;
      $("boot-log").textContent += p.code === 0 ? "✓ done\n" : "✗ stopped (exit " + p.code + ")\n";
      if (p.code === 0) probe();
      return;
    }
    ended();
  });

  invoke("info").then(function (i) {
    state.info = i;
    if (!i.local) { $("pick-local").disabled = true; $("pick-local").querySelector("span").textContent = "Only on Linux or macOS: use another computer from here."; }
  });
  // For tests (tests/setup-app/): the state, read-only
  window.__setup = { state: state, answers: answers };
})();
