// Front-end checks for doorcontrol/static/app.js, run without a browser: node tests/test_ui.cjs
"use strict";
const fs = require("fs"),
  path = require("path"),
  vm = require("vm"),
  assert = require("assert");

const STATIC = path.join(__dirname, "..", "doorcontrol", "static");

// A tiny fake DOM: elements are created on first use and remembered by id.
function makeEl(id) {
  const listeners = {};
  return {
    id,
    textContent: "",
    innerHTML: "",
    className: "",
    value: "",
    hidden: false,
    disabled: false,
    dataset: {},
    listeners,
    addEventListener(type, fn) {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    async fire(type, ev) {
      for (const fn of listeners[type] || []) await fn(ev);
    },
    reset() {},
    focus() {},
    scrollIntoView() {},
  };
}

function load(file, responses) {
  const els = {},
    calls = [],
    checked = {};
  const doc = {
    getElementById: (id) => els[id] || (els[id] = makeEl(id)),
    querySelectorAll: (sel) => checked[sel] || [],
  };
  const ctx = {
    console,
    Promise,
    JSON,
    Date,
    URL,
    Set,
    Number,
    String,
    window: {},
    document: doc,
    location: {
      reloaded: 0,
      replaced: null,
      reload() {
        this.reloaded++;
      },
      replace(u) {
        this.replaced = u;
      },
      hash: "",
    },
    confirm: () => true,
    prompt: () => "30",
    setTimeout: () => 0,
    clearTimeout() {},
    setInterval: () => 0,
    async fetch(url, opts = {}) {
      const method = opts.method || "GET";
      calls.push({ url, method, headers: opts.headers || {}, body: opts.body });
      const key = method + " " + url;
      const r = key in responses ? responses[key] : { status: 404, body: {} };
      return {
        ok: r.status >= 200 && r.status < 300,
        status: r.status,
        json: async () => r.body,
      };
    },
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(STATIC, file), "utf8"), ctx);
  return { ctx, els, calls, checked, $: doc.getElementById };
}

const settle = async () => {
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
};

const DOORS = [
  {
    id: "d1",
    name: "Event <Center>",
    relay: "lock",
    rule: "schedule",
  },
  { id: "d2", name: "Office", relay: "unlock", rule: "keep_unlock" },
];
const SCHEDS = [
  {
    id: 7,
    name: "Office hours",
    doors: ["d2"],
    days: [0, 1, 2, 3, 4],
    date: null,
    start: "08:00",
    end: "17:00",
    end_action: "reset",
    enabled: true,
  },
];
const LOG = [
  {
    time: "Mon Sep 06 09:00:00 AM",
    door: "Office",
    ok: 1,
    detail: "unlocked for 60 min",
    source: "web",
    user: "jake@hardincountyfair.org",
  },
  {
    time: "Mon Sep 06 08:00:00 AM",
    door: "Office",
    ok: 1,
    detail: "kept unlocked",
    source: "scheduler: Office hours",
    user: "",
  },
  {
    time: "Mon Sep 06 07:00:00 AM",
    door: "Office",
    ok: 0,
    detail: "Cannot reach UniFi Access",
    source: "telegram",
  },
];
function dashboardResponses(extra = {}) {
  return {
    "GET /api/bootstrap": {
      status: 200,
      body: {
        csrf: "tok123",
        user: "jake@hardincountyfair.org",
        version: "1.0.0",
      },
    },
    "GET /api/doors": { status: 200, body: DOORS },
    "GET /api/schedules": { status: 200, body: SCHEDS },
    "GET /api/upcoming": { status: 200, body: [] },
    "GET /api/log": { status: 200, body: LOG },
    ...extra,
  };
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("renders doors, escaped, with action buttons", async () => {
  const { $ } = load("app.js", dashboardResponses());
  await settle();
  const html = $("doors").innerHTML;
  assert.match(html, /Event &lt;Center&gt;/);
  assert.doesNotMatch(html, /Event <Center>/);
  assert.match(html, /data-act="unlock_for"/);
  assert.match(html, /Unlocked/);
  assert.match($("f-doors").innerHTML, /All doors/);
});

test("door button posts the action", async () => {
  const { $, calls } = load("app.js", dashboardResponses());
  await settle();
  const btn = { dataset: { id: "d1", act: "unlock_for", min: "60" } };
  btn.closest = () => btn;
  await $("doors").fire("click", { target: btn });
  const post = calls.find((c) => c.method === "POST");
  assert.ok(post, "a POST was sent");
  assert.strictEqual(post.url, "/api/doors/d1/action");
  assert.deepStrictEqual(JSON.parse(post.body), {
    action: "unlock_for",
    minutes: 60,
  });
  assert.strictEqual(post.headers["X-CSRF-Token"], "tok123");
  // GETs never carry the token.
  assert.ok(
    calls
      .filter((c) => c.method === "GET")
      .every((c) => !("X-CSRF-Token" in c.headers)),
  );
});

test("header shows email and Sign out; sign out posts with CSRF token", async () => {
  const { $, calls, ctx } = load(
    "app.js",
    dashboardResponses({ "POST /api/logout": { status: 200, body: {} } }),
  );
  await settle();
  assert.strictEqual(calls[0].url, "/api/bootstrap", "bootstrap comes first");
  assert.strictEqual($("user").textContent, "jake@hardincountyfair.org");
  assert.strictEqual($("signout").hidden, false);
  await $("signout").fire("click", {});
  const out = calls.find((c) => c.url === "/api/logout");
  assert.strictEqual(out.method, "POST");
  assert.strictEqual(out.headers["X-CSRF-Token"], "tok123");
  assert.strictEqual(ctx.location.reloaded, 1, "reloads to the sign-in page");
});

test("any 401 reloads to the sign-in page", async () => {
  const { ctx } = load(
    "app.js",
    dashboardResponses({
      "GET /api/bootstrap": { status: 401, body: { error: "Sign in" } },
    }),
  );
  await settle();
  assert.ok(ctx.location.reloaded >= 1);
});

test("schedule list and form: add, edit shows Cancel, cancel hides it", async () => {
  const { $, calls, checked } = load(
    "app.js",
    dashboardResponses({ "POST /api/schedules": { status: 201, body: {} } }),
  );
  await settle();
  assert.match($("schedules").innerHTML, /Office hours/);
  assert.match($("schedules").innerHTML, /Mon, Tue, Wed, Thu, Fri/);
  $("f-name").value = "Fair week";
  $("f-start").value = "08:00";
  $("f-end").value = "22:00";
  $("f-endaction").value = "keep_lock";
  checked["#f-doors input:checked"] = [{ value: "*" }, { value: "d1" }];
  checked["#f-days input:checked"] = [{ value: "5" }];
  await $("sched-form").fire("submit", { preventDefault() {} });
  const post = calls.find(
    (c) => c.method === "POST" && c.url === "/api/schedules",
  );
  assert.ok(post);
  const body = JSON.parse(post.body);
  assert.deepStrictEqual(body.doors, ["*"]);
  assert.deepStrictEqual(body.days, [5]);
  assert.strictEqual(body.end_action, "keep_lock");
  // Edit, then cancel: the Cancel button uses the hidden attribute (no inline style).
  const edit = { dataset: { edit: "7" } };
  edit.closest = () => edit;
  await $("schedules").fire("click", { target: edit });
  assert.strictEqual($("f-cancel").hidden, false);
  assert.strictEqual($("f-name").value, "Office hours");
  await $("f-cancel").fire("click", {});
  assert.strictEqual($("f-cancel").hidden, true);
});

test("activity log renders and marks failures", async () => {
  const { $ } = load("app.js", dashboardResponses());
  await settle();
  const html = $("log").innerHTML;
  assert.match(html, /unlocked for 60 min/);
  assert.match(html, /FAILED: Cannot reach UniFi Access/);
});

test("By shows user, else source", async () => {
  const { $ } = load("app.js", dashboardResponses());
  await settle();
  const cells = [
    ...$("log").innerHTML.matchAll(/<td class="muted">([^<]*)<\/td>/g),
  ].map((m) => m[1]);
  assert.deepStrictEqual(cells, [
    "jake@hardincountyfair.org",
    "scheduler: Office hours",
    "telegram",
  ]);
});

function loginInfo(info) {
  return { "GET /api/login-info": { status: 200, body: info } };
}

test("login: Google button is the main way in; Admin sign-in link, not main button", async () => {
  const { $ } = load(
    "login.js",
    loginInfo({
      google: true,
      google_url: "/api/auth/google/start",
      remote: true,
      password: true,
    }),
  );
  await settle();
  assert.strictEqual($("google").hidden, false);
  assert.strictEqual($("admin").hidden, false);
  assert.notStrictEqual($("admin").open, true, "admin form starts closed");
  const html = fs.readFileSync(path.join(STATIC, "login.html"), "utf8");
  assert.match(html, /<details[^>]*id="admin"[^>]*hidden/);
  assert.doesNotMatch(html, /<details[^>]*\sopen/);
});

test("login: Admin sign-in link hidden when password is false", async () => {
  const { $ } = load(
    "login.js",
    loginInfo({
      google: true,
      google_url: "/api/auth/google/start",
      remote: true,
      password: false,
    }),
  );
  $("admin").hidden = true; // as in login.html
  await settle();
  assert.strictEqual($("admin").hidden, true);
});

test("login: on the fair network Google points at the public host, no password form", async () => {
  const { $ } = load(
    "login.js",
    loginInfo({
      google: true,
      google_url: "https://doors.jakemc.me/api/auth/google/start",
      remote: false,
      password: true,
    }),
  );
  $("admin").hidden = true;
  await settle();
  assert.strictEqual(
    $("google").href,
    "https://doors.jakemc.me/api/auth/google/start",
  );
  assert.match($("google-note").textContent, /doors\.jakemc\.me/);
  assert.strictEqual($("admin").hidden, true);
});

test("login: password form posts and goes to the dashboard", async () => {
  const { $, calls, ctx } = load("login.js", {
    ...loginInfo({ google: false, remote: true, password: true }),
    "POST /api/login": { status: 200, body: { ok: true } },
  });
  await settle();
  const f = $("login");
  const btn = { disabled: false, textContent: "" };
  f.querySelector = () => btn;
  f.name = { value: "admin" };
  f.password = { value: "a long random password", select() {} };
  await f.fire("submit", { preventDefault() {} });
  const post = calls.find((c) => c.url === "/api/login");
  assert.deepStrictEqual(JSON.parse(post.body), {
    name: "admin",
    password: "a long random password",
  });
  assert.strictEqual(ctx.location.replaced, "/");
});

test("no inline style or event handlers in built HTML", () => {
  for (const f of ["app.js", "index.html", "login.js", "login.html"]) {
    const src = fs.readFileSync(path.join(STATIC, f), "utf8");
    assert.doesNotMatch(src, /\sstyle\s*=/i, f + " has style=");
    assert.doesNotMatch(src, /\son[a-z]+\s*=\s*["']/i, f + " has on...=");
    assert.doesNotMatch(src, /\.style\./, f + " sets element.style");
  }
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log("ok   " + name);
    } catch (e) {
      failed++;
      console.log("FAIL " + name + "\n     " + e.message);
    }
  }
  console.log(`${tests.length - failed}/${tests.length} UI checks passed`);
  process.exit(failed ? 1 : 0);
})();
