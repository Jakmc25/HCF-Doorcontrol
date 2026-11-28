"use strict";
// Sign-in page (adapted from Awards). Google is the main way in. The admin password is a
// break-glass fallback, shown only at the internet address and only when an admin account exists.
const f = document.getElementById("login"),
  err = document.getElementById("err"),
  admin = document.getElementById("admin");
fetch("/api/login-info")
  .then((r) => r.json())
  .then((d) => {
    if (d.google) {
      const g = document.getElementById("google");
      g.href = d.google_url;
      g.hidden = false;
      // On the fair network Google sign-in finishes at the internet address, and the app carries on there.
      if (!d.remote) {
        const n = document.getElementById("google-note");
        n.textContent =
          "Google sign-in continues at " + new URL(d.google_url).host + ".";
        n.hidden = false;
      }
    }
    if (d.password && d.remote) admin.hidden = false;
    if (!d.google && !(d.password && d.remote))
      err.textContent =
        "Sign-in isn’t available here. Ask Jake, or use the UniFi Access app.";
  })
  .catch(() => {
    err.textContent = "Could not reach Door Control. Reload the page.";
  });
admin.addEventListener("toggle", () => {
  if (admin.open) f.name.focus();
});
f.addEventListener("submit", async (e) => {
  e.preventDefault();
  err.textContent = "";
  const b = f.querySelector("button");
  b.disabled = true;
  b.textContent = "Signing in…";
  try {
    const r = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: f.name.value, password: f.password.value }),
    });
    const d = await r
      .json()
      .catch(() => ({ error: "Sign-in failed. Try again." }));
    if (!r.ok) throw Error(d.error || "Sign-in failed.");
    location.replace("/");
  } catch (x) {
    err.textContent = x.message;
    b.disabled = false;
    b.textContent = "Sign in";
    f.password.select();
  }
});
