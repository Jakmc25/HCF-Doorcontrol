"use strict";
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let doors = [],
  editingId = null,
  csrf = "";
const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[c],
  );

function toast(msg, err) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toast.h);
  toast.h = setTimeout(() => (t.className = "toast"), 3500);
}
// Every change carries the CSRF token from /api/bootstrap. A 401 means the session ended:
// reloading shows the sign-in page.
async function api(method, url, body) {
  const headers = { "Content-Type": "application/json" };
  if (method !== "GET") headers["X-CSRF-Token"] = csrf;
  const r = await fetch(url, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) {
    location.reload();
    throw new Error("Sign in to continue.");
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || "Error " + r.status);
  return j;
}

function ruleText(d) {
  const map = {
    keep_unlock: "kept unlocked",
    keep_lock: "kept locked",
    custom: "temporarily unlocked",
    lock_early: "locked early",
    lock_now: "locked",
    schedule: "on its normal schedule",
  };
  return d.rule ? map[d.rule] || d.rule : "";
}
async function loadDoors() {
  try {
    doors = await api("GET", "/api/doors");
    $("doors").innerHTML =
      doors
        .map((d) => {
          const unlocked = d.relay === "unlock";
          return `<div class="card">
        <div><span class="door-name">${esc(d.name)}</span><span class="state ${unlocked ? "unlocked" : "locked"}">${unlocked ? "Unlocked" : "Locked"}</span></div>
        <div class="sub">${esc(ruleText(d))}</div>
        <button class="primary" data-id="${esc(d.id)}" data-act="unlock_for" data-min="60">Unlock 1 hr</button>
        <button data-id="${esc(d.id)}" data-act="unlock_for" data-ask="1">Unlock for…</button>
        <button data-id="${esc(d.id)}" data-act="keep_unlock">Keep unlocked</button>
        <button class="danger" data-id="${esc(d.id)}" data-act="lock">Lock now</button>
        <button data-id="${esc(d.id)}" data-act="reset">Back to normal</button>
      </div>`;
        })
        .join("") || '<div class="muted">No doors found.</div>';
    renderDoorChoices();
  } catch (e) {
    $("doors").innerHTML =
      `<div class="card fail">Can't reach the door controller: ${esc(e.message)}</div>`;
  }
}
$("doors").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button");
  if (!b) return;
  let minutes = b.dataset.min ? Number(b.dataset.min) : undefined;
  if (b.dataset.ask) {
    const v = prompt("Unlock for how many minutes? (max 480)", "120");
    if (!v) return;
    minutes = Number(v);
  }
  if (
    b.dataset.act === "keep_unlock" &&
    !confirm("Keep this door unlocked until you change it?")
  )
    return;
  b.disabled = true;
  try {
    const r = await api(
      "POST",
      `/api/doors/${encodeURIComponent(b.dataset.id)}/action`,
      {
        action: b.dataset.act,
        minutes,
      },
    );
    toast(r.message);
  } catch (e) {
    toast(e.message, true);
  }
  await refreshAll();
});

function renderDoorChoices(selected) {
  const sel = new Set(
    selected ||
      [...document.querySelectorAll("#f-doors input:checked")].map(
        (i) => i.value,
      ),
  );
  $("f-doors").innerHTML =
    `<label><input type="checkbox" value="*" ${sel.has("*") ? "checked" : ""}> All doors</label>` +
    doors
      .map(
        (d) =>
          `<label><input type="checkbox" value="${esc(d.id)}" ${sel.has(d.id) ? "checked" : ""}> ${esc(d.name)}</label>`,
      )
      .join("");
}
function renderDayChoices(selected) {
  $("f-days").innerHTML = DAYS.map(
    (n, i) =>
      `<label><input type="checkbox" value="${i}" ${(selected || []).includes(i) ? "checked" : ""}> ${n}</label>`,
  ).join("");
}
function doorNames(ids) {
  if (ids.includes("*")) return "All doors";
  return ids
    .map(
      (i) => (doors.find((d) => d.id === i) || { name: "(removed door)" }).name,
    )
    .join(", ");
}
async function loadSchedules() {
  const list = await api("GET", "/api/schedules");
  window._scheds = list;
  $("schedules").innerHTML = list.length
    ? `<table><tr><th>Name</th><th>When</th><th>Doors</th><th>At end</th><th></th></tr>` +
      list
        .map(
          (s) => `<tr>
      <td>${esc(s.name)} ${s.enabled ? "" : '<span class="pill">paused</span>'}</td>
      <td>${s.date ? esc(s.date) : s.days.map((d) => DAYS[d]).join(", ")}<br>${esc(s.start)} – ${esc(s.end)}</td>
      <td>${esc(doorNames(s.doors))}</td>
      <td>${s.end_action === "keep_lock" ? "Lock" : "Normal schedule"}</td>
      <td><button data-edit="${s.id}">Edit</button><button data-toggle="${s.id}">${s.enabled ? "Pause" : "Resume"}</button><button class="danger" data-del="${s.id}">Delete</button></td>
    </tr>`,
        )
        .join("") +
      "</table>"
    : '<div class="card muted">No schedules yet. Add one below.</div>';
}
$("schedules").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button");
  if (!b) return;
  try {
    if (b.dataset.del) {
      if (!confirm("Delete this schedule?")) return;
      await api("DELETE", "/api/schedules/" + b.dataset.del);
    } else if (b.dataset.toggle) {
      const s = window._scheds.find((x) => x.id == b.dataset.toggle);
      await api("PUT", "/api/schedules/" + s.id, {
        ...s,
        enabled: !s.enabled,
      });
    } else if (b.dataset.edit) {
      return startEdit(window._scheds.find((x) => x.id == b.dataset.edit));
    }
    await refreshAll();
  } catch (e) {
    toast(e.message, true);
  }
});
function startEdit(s) {
  editingId = s.id;
  $("form-title").textContent = "Edit schedule";
  $("f-cancel").hidden = false;
  $("f-name").value = s.name;
  $("f-start").value = s.start;
  $("f-end").value = s.end;
  $("f-endaction").value = s.end_action;
  $("f-date").value = s.date || "";
  renderDoorChoices(s.doors);
  renderDayChoices(s.days);
  $("sched-form").scrollIntoView({ behavior: "smooth" });
}
function resetForm() {
  editingId = null;
  $("sched-form").reset();
  $("form-title").textContent = "Add a schedule";
  $("f-cancel").hidden = true;
  renderDoorChoices([]);
  renderDayChoices([]);
}
$("f-cancel").addEventListener("click", resetForm);
$("sched-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = {
    name: $("f-name").value,
    start: $("f-start").value,
    end: $("f-end").value,
    end_action: $("f-endaction").value,
    doors: [...document.querySelectorAll("#f-doors input:checked")].map(
      (i) => i.value,
    ),
    days: [...document.querySelectorAll("#f-days input:checked")].map((i) =>
      Number(i.value),
    ),
    date: $("f-date").value || null,
    enabled: true,
  };
  if (body.doors.includes("*")) body.doors = ["*"];
  if (editingId) {
    const old = window._scheds.find((x) => x.id === editingId);
    body.enabled = old ? old.enabled : true;
  }
  try {
    if (editingId) await api("PUT", "/api/schedules/" + editingId, body);
    else await api("POST", "/api/schedules", body);
    toast("Schedule saved");
    resetForm();
    await refreshAll();
  } catch (e) {
    toast(e.message, true);
  }
});

async function loadUpcoming() {
  const rows = await api("GET", "/api/upcoming");
  const fmt = (iso) =>
    new Date(iso).toLocaleString([], {
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  $("upcoming").innerHTML = rows.length
    ? `<table><tr><th>When</th><th>What</th><th>Doors</th><th>From</th></tr>` +
      rows
        .map(
          (w) =>
            `<tr><td>${w.active ? '<span class="now">NOW · </span>' : ""}${esc(fmt(w.start))} → ${esc(fmt(w.end))}</td><td>${esc(w.title)}</td><td>${esc(w.doors.join(", "))}</td><td><span class="pill">${esc(w.source)}</span></td></tr>`,
        )
        .join("") +
      "</table>"
    : '<div class="card muted">Nothing scheduled in the next 7 days.</div>';
}
async function loadLog() {
  const rows = await api("GET", "/api/log");
  $("log").innerHTML = rows.length
    ? `<table><tr><th>Time</th><th>Door</th><th>What</th><th>By</th></tr>` +
      rows
        .map(
          (r) =>
            `<tr><td>${esc(r.time)}</td><td>${esc(r.door)}</td><td class="${r.ok ? "" : "fail"}">${esc(r.ok ? r.detail : "FAILED: " + r.detail)}</td><td class="muted">${esc(r.user || r.source)}</td></tr>`,
        )
        .join("") +
      "</table>"
    : '<div class="card muted">No activity yet.</div>';
}
// Calendar health notice. A failure here never shows a toast: the rest of the page works.
const CALENDAR_NOTICE = {
  stale:
    "Calendar stale: the calendar feed has not been read for over an hour. New calendar unlocks are paused; doors already open for an event close at its end.",
  unknown:
    "Calendar not read yet: calendar unlocks start after the first successful read.",
};
async function loadHealth() {
  const n = $("calnotice");
  try {
    const h = await api("GET", "/api/health");
    const text = CALENDAR_NOTICE[h.calendar_state] || "";
    n.textContent = text;
    n.hidden = !text;
  } catch (e) {
    n.hidden = true;
  }
}
async function refreshAll() {
  await loadDoors();
  await Promise.all(
    [
      loadSchedules(),
      loadUpcoming(),
      loadLog(),
      loadHealth(),
      loadPeople(),
    ].map((p) => p.catch((e) => toast(e.message, true))),
  );
}
// Settings › People: only admins get the section; it is never loaded for anyone else
// (the server refuses them too).
let isAdmin = false,
  removing = "";
async function loadPeople() {
  if (!isAdmin) return;
  const p = await api("GET", "/api/people");
  const admins = p.admins.map(
    (e) =>
      `<tr><td>${esc(e)}</td><td><span class="pill">admin</span></td><td class="muted">change in .env</td><td></td></tr>`,
  );
  const listed = p.people.map(
    (x) =>
      `<tr><td>${esc(x.email)}</td><td>person</td><td class="muted">${esc(x.added_by ? "added by " + x.added_by : "")}</td><td><button class="danger" data-remove="${esc(x.email)}">Remove</button></td></tr>`,
  );
  $("people").innerHTML =
    `<table><tr><th>Email</th><th>Role</th><th></th><th></th></tr>` +
    admins.concat(listed).join("") +
    "</table>" +
    (p.people.length
      ? ""
      : '<div class="card muted">Nobody else yet. Add people below.</div>');
}
function closeRemove() {
  removing = "";
  $("people-confirm").hidden = true;
  $("people-confirm-input").value = "";
  $("people-confirm-btn").disabled = true;
}
$("people").addEventListener("click", (ev) => {
  const b = ev.target.closest("button");
  if (!b || !b.dataset.remove) return;
  removing = b.dataset.remove;
  $("people-confirm-text").textContent =
    "To remove " +
    removing +
    " and sign them out now, type their email address again:";
  $("people-confirm-input").value = "";
  $("people-confirm-btn").disabled = true;
  $("people-confirm").hidden = false;
  $("people-confirm-input").focus();
});
$("people-confirm-input").addEventListener("input", () => {
  $("people-confirm-btn").disabled =
    !removing ||
    $("people-confirm-input").value.trim().toLowerCase() !== removing;
});
$("people-confirm-cancel").addEventListener("click", closeRemove);
$("people-confirm-btn").addEventListener("click", async () => {
  if (!removing) return;
  try {
    await api("POST", "/api/people/remove", {
      email: removing,
      confirm: $("people-confirm-input").value,
    });
    toast(removing + " removed and signed out");
    closeRemove();
    await Promise.all([loadPeople(), loadLog()]);
  } catch (e) {
    toast(e.message, true);
  }
});
$("people-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const r = await api("POST", "/api/people", {
      email: $("people-email").value,
    });
    toast(r.email + " added");
    $("people-email").value = "";
    await Promise.all([loadPeople(), loadLog()]);
  } catch (e) {
    toast(e.message, true);
  }
});

async function bootstrap() {
  const b = await api("GET", "/api/bootstrap");
  csrf = b.csrf;
  $("user").textContent = b.user || "";
  $("signout").hidden = false;
  isAdmin = b.admin === true;
  $("people-section").hidden = !isAdmin;
}
$("signout").addEventListener("click", async () => {
  try {
    await api("POST", "/api/logout");
    location.reload();
  } catch (e) {
    toast(e.message, true);
  }
});
async function start() {
  try {
    await bootstrap();
  } catch (e) {
    toast(e.message, true);
    return;
  }
  renderDayChoices([]);
  await refreshAll();
}
start();
setInterval(() => {
  loadDoors();
  loadUpcoming();
  loadLog();
  loadHealth();
}, 20000);
// Keep the CSRF token fresh on a dashboard left open for days.
setInterval(() => bootstrap().catch(() => {}), 3600000);
setInterval(
  () =>
    ($("clock").textContent = new Date().toLocaleString([], {
      weekday: "short",
      hour: "numeric",
      minute: "2-digit",
    })),
  1000,
);
