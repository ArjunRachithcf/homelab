"""tests/authentik/client.py: drives Authentik's flow executor the way the browser does (stdlib only). Runs in the
test's "proxy" container (Caddy's place: Authentik trusts its X-Forwarded-For) or its "neighbour" (any other
container), signs in with a password and one-time codes, and prints one JSON line per step.

  python3 client.py SCENARIO 'JSON args'   (args: user, password, codes, xff, and the scenario's own)
"""
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://authentik-server:9000"


class Session:
    def __init__(self, xff=None):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.xff = xff

    def csrf(self):
        return next((c.value for c in self.jar if c.name == "authentik_csrf"), None)

    def req(self, method, path, body=None, csrf=True, raw=False):
        h = {"Accept": "application/json"}
        if self.xff:
            h["X-Forwarded-For"] = self.xff
        if body is not None:
            h["Content-Type"] = "application/json"
        if csrf and self.csrf():
            h["X-authentik-CSRF"] = self.csrf()
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
        try:
            with self.op.open(r, timeout=120) as resp:
                text, status = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            text, status = e.read(), e.code
        if raw:
            return status, text.decode(errors="replace")
        try:
            return status, json.loads(text or b"null")
        except ValueError:
            return status, text.decode(errors="replace")[:300]

    def start(self, slug, query=""):
        """As the SSH keys sheet's links: a plan left over cancelled, then the flow with ?next=/ssh-keys/done"""
        self.req("GET", "/flows/-/cancel/", raw=True)
        query = "&".join(x for x in ("next=%2Fssh-keys%2Fdone", query) if x)
        path = f"/api/v3/flows/executor/{slug}/?query=" + urllib.parse.quote(query, safe="")
        self.req("GET", f"/if/flow/{slug}/", raw=True)   # the flow's page: the CSRF cookie, as for a browser
        st, ch = self.req("GET", path)
        return path, ch

    def post(self, path, body, csrf=True):
        return self.req("POST", path, body, csrf=csrf)


def comp(ch):
    return ch.get("component") if isinstance(ch, dict) else None


def login(s, user, password, codes, trace):
    path, ch = s.start("default-authentication-flow")
    for _ in range(8):
        c = comp(ch)
        trace.append(summary(ch))
        if c == "ak-stage-identification":
            body = {"component": c, "uid_field": user}
            if ch.get("password_fields"):
                body["password"] = password
            ch = s.post(path, body)[1]
        elif c == "ak-stage-password":
            ch = s.post(path, {"component": c, "password": password})[1]
        elif c == "ak-stage-authenticator-validate":
            ch = s.post(path, {"component": c, "code": codes.pop(0)})[1]
        elif c == "ak-stage-user-login":   # "Stay signed in?" (hardening.yaml)
            ch = s.post(path, {"component": c, "remember_me": False})[1]
        elif c == "xak-flow-redirect":
            return True
        else:
            return False
    return False


def step_up(s, path, ch, codes):
    """The passkey step (one-time codes in this test) → the next challenge"""
    if comp(ch) != "ak-stage-authenticator-validate":
        return ch
    return s.post(path, {"component": "ak-stage-authenticator-validate", "code": codes.pop(0)})[1]


def summary(ch):
    if not isinstance(ch, dict):
        return {"component": None, "raw": str(ch)[:200]}
    out = {"component": comp(ch)}
    for k in ("to", "error_message"):
        if k in ch:
            out[k] = ch[k]
    if "response_errors" in ch:
        out["errors"] = [e.get("string") for v in ch["response_errors"].values() for e in v]
    if "fields" in ch:
        out["fields"] = [{f: x.get(f) for f in ("field_key", "type", "initial_value", "choices")} for x in ch["fields"]]
    return out


def emit(case, **kw):
    print(json.dumps({"case": case, **kw}), flush=True)


def add(s, codes, title, key, switch_xff=None, csrf=True):
    path, ch = s.start("ssh-key-add")
    first = comp(ch)
    ch = step_up(s, path, ch, codes)
    if comp(ch) != "ak-stage-prompt":
        return {"first": first, **summary(ch)}
    if switch_xff:
        s.xff = switch_xff
    st, ch = s.post(path, {"component": "ak-stage-prompt", "ssh_key_title": title, "ssh_key": key}, csrf=csrf)
    return {"first": first, "status": st, **summary(ch)}


def main():
    scenario = sys.argv[1]
    a = json.loads(sys.argv[2])
    codes = a["codes"]
    s = Session(a.get("xff"))
    trace = []
    if not login(s, a["user"], a["password"], codes, trace):
        emit("login", ok=False, trace=trace)
        return
    emit("login", ok=True)
    if scenario == "add":
        for i, (title, key) in enumerate(a["keys"]):
            emit("add", i=i, **add(s, codes, title, key, csrf=not a.get("no_csrf")))
    elif scenario == "remove":
        path, ch = s.start("ssh-key-remove", a.get("query", ""))
        first = comp(ch)
        ch = step_up(s, path, ch, codes)
        form = summary(ch)
        result = None
        if comp(ch) == "ak-stage-prompt" and a.get("fp"):
            result = summary(s.post(path, {"component": "ak-stage-prompt", "ssh_key_fp": a["fp"]})[1])
        emit("remove", first=first, form=form, result=result)
    elif scenario == "cache-window":
        # passed over NetBird, then the same session from a public address straight away
        s.start("ssh-key-add")
        s.xff = a["public"]
        path, ch = s.start("ssh-key-add")
        first = comp(ch)
        ch = step_up(s, path, ch, codes)
        title, key = a["keys"][0]
        result = summary(s.post(path, {"component": "ak-stage-prompt", "ssh_key_title": title, "ssh_key": key})[1]
                         if comp(ch) == "ak-stage-prompt" else ch)
        emit("cache-window", first=first, result=result)
    elif scenario == "two-tabs":
        t = Session(a.get("xff"))
        ok2 = login(t, a["user"], a["password"], codes, [])
        at_form = []
        for x in (s, t):
            path, ch = x.start("ssh-key-add")
            ch = step_up(x, path, ch, codes)
            at_form.append((x, path, comp(ch)))
        results = []
        for (x, path, _), (title, key) in zip(at_form, a["keys"]):
            results.append(summary(x.post(path, {"component": "ak-stage-prompt", "ssh_key_title": title,
                                                   "ssh_key": key})[1]))
        emit("two-tabs", second_login=ok2, at_form=[c for _, _, c in at_form], results=results)
    elif scenario == "start":
        path, ch = s.start(a["slug"])
        emit("start", **summary(ch))


if __name__ == "__main__":
    main()
