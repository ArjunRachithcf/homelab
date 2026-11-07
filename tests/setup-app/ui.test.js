// tests/setup-app/ui.test.js: the setup app's window (setup-app/ui/) in Chromium, Tauri mocked.
// node ui.test.js ROOT ENVFILE SESSION.json [SCREENSHOT_DIR]: catalogue/plan/apply/set_secret run the
// real engine (lib/setup/engine.py) on ENVFILE; start_setup replays SESSION (a protocol run recorded
// by tests/unit/test_setup_app.py through lib/setup/run.py), pausing at every question until the
// window answers. Prints one JSON line: what the window sent, and what it showed.
const { chromium } = require('playwright');
const { spawnSync } = require('child_process');
const fs = require('fs'), path = require('path');
const [root, envFile, sessionFile, shots] = process.argv.slice(2);
const session = JSON.parse(fs.readFileSync(sessionFile, 'utf8'));
const ASKS = new Set(['pause', 'confirm', 'input', 'secret', 'lines', 'danger', 'terminal', 'sudo']);

function engine(args, input) {
  const r = spawnSync('python3', [path.join(root, 'lib/setup/engine.py'), ...args, '--env', envFile], { input: input || '', encoding: 'utf8' });
  return { code: r.status, out: r.stdout, err: r.stderr };
}
const lastJson = s => JSON.parse(s.trim().split('\n').filter(l => l.startsWith('{')).pop());

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1200, height: 900 } });
  const sent = { answers: [], sudo: [], secrets: [], calls: [] };
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('dialog', d => { errors.push('dialog: ' + d.message()); d.dismiss(); });

  await page.exposeFunction('__host', async (cmd, args) => {
    sent.calls.push(cmd);
    switch (cmd) {
      case 'info': return { local: true, repo: 'https://example.org/homelab.git', ref: 'v1', commit: '', version: '0.1.0' };
      case 'connect_local': return 'this computer (' + args.dir + ')';
      case 'probe': return { dir: '/home/you/homelab', repo: 'yes', root: 'no', os: 'Linux', distro: 'Fedora Linux 43', python: 'ok', git: 'yes', env: 'no', label: 'this computer' };
      case 'catalogue': return lastJson(engine(['components', '--json']).out);
      case 'plan': return lastJson(engine(['plan', '--answers', '-', '--json'], args.answers).out);
      case 'apply': { const r = lastJson(engine(['apply', '--answers', '-', '--json'], args.answers).out); r.configure = { code: 0, output: '✓ configure: nothing to change\n' }; r.console = { code: 0, output: "✓ the console's backend restarted: it offers the components now chosen\n" }; return r; }
      case 'set_secret': { sent.secrets.push(args.key); const r = engine(['secret', args.key], args.value + '\n'); if (r.code) throw r.err; return null; }
      case 'set_sudo': sent.sudo.push(args.password.length); return null;
      case 'start_setup': sent.start = args; return 'replay';
      case 'answer': sent.answers.push(args.text); return 'answered';
      case 'open_url': sent.opened = args.url; return null;
      case 'assistant_status': return { provider: 'anthropic', model: '', ollama_url: 'http://localhost:11434', has_key: !!sent.key, default_model: 'claude-opus-5-5' };
      case 'assistant_settings': if (args.key) sent.key = args.key; sent.settings = args; return null;
      case 'assistant_send': sent.asked = args.text; return 'assistant';
      case 'assistant_decide': sent.decided = args; return 'decided';
      default: return null;
    }
  });
  await page.addInitScript(() => {
    const listeners = {};
    window.__emit = (ev, payload) => (listeners[ev] || []).forEach(cb => cb({ payload }));
    window.__TAURI__ = {
      core: { invoke: (cmd, args) => window.__host(cmd, args || {}).then(r => { const step = ['replay', 'answered', 'assistant', 'decided'].includes(r); if (step) window.__step(cmd, args); return step ? null : r; }) },
      event: { listen: (ev, cb) => { (listeners[ev] = listeners[ev] || []).push(cb); return Promise.resolve(() => {}); } },
      dialog: { save: () => Promise.resolve(null) },
    };
  });
  // The replay: lines up to and including the next question, then wait for its answer(s)
  await page.addInitScript(session => {
    const ASKS = new Set(['pause', 'confirm', 'input', 'secret', 'lines', 'danger', 'terminal', 'sudo']);
    let i = 0, waitingLines = false;
    function run() {
      while (i < session.length) {
        const line = session[i++];
        window.__emit('stream-line', { kind: 'setup', line });
        const m = JSON.parse(line);
        if (ASKS.has(m.t)) { waitingLines = m.t === 'lines'; return; }
      }
      window.__emit('stream-end', { kind: 'setup', code: 0 });
    }
    // The assistant: a recorded-shape exchange (assistant.rs events): words, a change to confirm, its result
    const ai = p => window.__emit('assistant', p);
    window.__step = (cmd, args) => {
      if (cmd === 'assistant_send') return setTimeout(() => {
        ai({ kind: 'tool', tool: 'plan', input: { preset: 'minimal' } });
        ai({ kind: 'result', tool: 'plan', error: false, text: '{"enabled": ["core"]}' });
        ai({ kind: 'text', text: 'Minimal is documents and photos. Shall I save that?' });
        ai({ kind: 'confirm', id: 'apply-1', tool: 'apply', input: { preset: 'minimal' } });
      }, 0);
      if (cmd === 'assistant_decide') return setTimeout(() => {
        ai({ kind: 'tool', tool: 'apply', input: { preset: 'minimal' } });
        ai({ kind: 'result', tool: 'apply', error: false, text: '{"applied": true}' });
        ai({ kind: 'text', text: 'Saved.' });
        ai({ kind: 'done' });
      }, 0);
      if (cmd === 'start_setup') return setTimeout(run, 0);
      if (cmd === 'answer') { if (waitingLines && args.text !== '') return; waitingLines = false; setTimeout(run, 0); }
    };
  }, session.lines);

  const out = {};
  await page.goto('file://' + path.join(root, 'setup-app/ui/index.html'));
  // Connect: this computer
  await page.click('#pick-local');
  await page.click('#f-local button[type=submit]');
  await page.waitForSelector('#p-machine:not([hidden])');
  out.facts = await page.locator('#facts li').count();
  await page.click('#to-choose');
  await page.waitForSelector('#presets .pick');
  out.presets = await page.locator('#presets .pick').count();
  // Minimal, then what's still needed
  await page.locator('#presets .pick', { hasText: 'Minimal' }).click();
  await page.waitForFunction(() => window.__setup.state.plan && window.__setup.state.plan.enabled.length === 5);
  out.applyDisabledBefore = await page.isDisabled('#do-apply');
  const missing = await page.evaluate(() => window.__setup.state.plan.missing.map(x => x.key));
  out.missing = missing;
  const values = { DOMAIN: 'example.org', DOMAIN_ALIAS: 'example.net', NETBIRD_IP: '100.64.0.5', ADMIN_USER: 'sam', ADMIN_NAME: 'Sam Smith',
                   NOTIFY_TO: 'sam@example.org', AUTHENTIK_EMAIL__USERNAME: 'noreply@example.org', INFOMANIAK_API_TOKEN: 'tok-SECRET-1', BACKUP_AGE_RECIPIENT: 'age1example', IMMICH_ADMIN_EMAIL: 'sam@example.org' };
  for (const k of missing) {
    const input = page.locator('#catalogue label', { hasText: k }).locator('input, select').first();
    await input.fill(values[k] || 'x');
    await input.dispatchEvent('change');
  }
  await page.waitForFunction(() => !document.getElementById('do-apply').disabled, null, { timeout: 10000 }).catch(async e => {
    const st = await page.evaluate(() => ({ missing: window.__setup.state.plan.missing, options: window.__setup.state.options, secrets: Object.keys(window.__setup.state.secrets), plan: document.getElementById('plan').innerText }));
    throw new Error('Apply stayed disabled: ' + JSON.stringify(st));
  });
  if (shots) await page.screenshot({ path: path.join(shots, 'choose.png'), fullPage: true });
  out.answersFile = await page.evaluate(() => window.__setup.answers());
  await page.click('#do-apply');
  await page.waitForSelector('#p-run:not([hidden])');
  out.applied = await page.locator('#applied').innerText();
  // The wizards: answer each question as a person would
  await page.click('#run-start');
  const steps = [
    ['input', async () => { await page.fill('#ask input', 'example.org'); await page.click('#ask button.primary'); }],
    ['secret', async () => { await page.fill('#ask input', 'hunter2-SECRET'); await page.click('#ask button.primary'); }],
    ['confirm', async () => { await page.click('#ask button.primary'); }],
    ['lines', async () => { await page.fill('#ask textarea', 'ssh-ed25519 AAA one\nssh-ed25519 BBB two'); await page.click('#ask button.primary'); }],
    ['danger', async () => { await page.fill('#ask input', 'WIPE'); await page.click('#ask button.danger'); }],
    ['terminal', async () => { out.terminal = await page.locator('#ask code').textContent(); await page.click('#ask button.primary'); }],
    ['sudo', async () => { await page.fill('#ask input', 'sudo-PW-9'); await page.click('#ask button.primary'); }],
  ];
  for (const [what, act] of steps) {
    await page.waitForSelector('#ask:not([hidden])', { timeout: 10000 });
    await act();
  }
  await page.waitForSelector('#feed .good', { timeout: 10000 });
  if (shots) await page.screenshot({ path: path.join(shots, 'run.png'), fullPage: true });
  out.stages = await page.locator('#stages li').allTextContents();
  // The assistant: key into the keychain (never shown again), a question, a change that waits for Allow
  await page.click('#ai-toggle');
  await page.waitForSelector('#ai:not([hidden])');
  out.aiSettingsOpen = await page.evaluate(() => document.getElementById('ai-settings').open);
  await page.fill('#ai-key', 'sk-ant-TEST-KEY');
  await page.click('#ai-save');
  await page.waitForFunction(() => document.getElementById('ai-key').value === '');
  await page.fill('#ai-text', 'Set up documents and photos');
  await page.click('#ai-send');
  await page.waitForSelector('#ai-log .confirm');
  out.aiBeforeAllow = sent.decided || null;
  await page.click('#ai-log .confirm button.primary');
  await page.waitForFunction(() => document.getElementById('ai-send').textContent === 'Send');
  out.aiLog = await page.locator('#ai-log').innerText();
  out.bodyText += await page.locator('body').innerText();
  if (shots) await page.screenshot({ path: path.join(shots, 'assistant.png') });
  out.feedText = await page.locator('#feed').innerText();
  out.marked = await page.locator('#feed .m-b, #feed .m-y, #feed .m-g').allTextContents();
  out.bodyText = await page.locator('body').innerText();
  // The parts list (the plan's stages by wizard): one step of the first part, alone
  await page.click('#ai-close');
  out.startAll = sent.start;
  out.partTitles = await page.locator('#parts .part-toggle').allTextContents();
  await page.locator('#parts .part-toggle').first().click();
  out.firstStep = await page.locator('#parts .part-steps:not([hidden]) li span').first().textContent();
  await page.locator('#parts .part-steps:not([hidden]) li button').first().click();
  await page.waitForFunction(() => !document.getElementById('run-start').hidden);
  out.startOne = sent.start;
  if (shots) await page.screenshot({ path: path.join(shots, 'parts.png'), fullPage: true });
  out.toRunHidden = await page.evaluate(() => document.getElementById('to-run').hidden);
  // Switching off what the machine has (its .env holds the minimal choice now): the plan says so, Apply asks first
  await page.goto('file://' + path.join(root, 'setup-app/ui/index.html'));
  await page.click('#pick-local');
  await page.click('#f-local button[type=submit]');
  await page.waitForSelector('#p-machine:not([hidden])');
  await page.click('#to-choose');
  await page.waitForFunction(() => window.__setup.state.plan);
  // Apply at once, before the panel's own replan (250 ms): Apply plans what it sends, so it still asks
  await page.getByRole('checkbox', { name: 'Photos (Immich)' }).click();
  await page.click('#do-apply');
  await page.waitForSelector('#off-confirm:not([hidden])');
  out.planOff = await page.locator('#plan').innerText();
  out.offConfirm = await page.locator('#off-confirm').innerText();
  if (shots) await page.screenshot({ path: path.join(shots, 'switch-off.png'), fullPage: true });
  await page.locator('#off-confirm button', { hasText: 'Keep them' }).click();
  out.offConfirmHidden = await page.evaluate(() => document.getElementById('off-confirm').hidden);
  out.sent = sent;
  out.errors = errors;
  console.log(JSON.stringify(out));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
