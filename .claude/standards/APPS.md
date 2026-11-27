# App map

All apps run on **fair-server** (Ubuntu 26.04, LAN 192.168.1.160) under `/srv/fair-apps/`, in Docker
Compose, published through the Cloudflare tunnel. Owner: Jake; maintained with Kolt (see People in STANDARDS.md). Each app's CLAUDE.md has the details
and the exact deploy command. Update this file (in dev-standards) when an app or connection changes.

| App | GitHub repo | What it does | Server folder | Port | Public address | Server copy is | Version (2026-11-09) |
|-----|-------------|--------------|---------------|------|----------------|----------------|---------|
| Board Docs | Jakmc25/HCF-Board-Portal | Board/committee meetings, agendas, minutes, attendance kiosk, rosters | /srv/fair-apps/boarddocs/hcf-board-portal | 8088 | board.jakemc.me | copy of the `hcf-board-portal/` subfolder; app's own update.sh | 2.8.1 |
| SoldIII | Jakmc25/HCF-SOLDIII | Consignment auction: clerking, cashier, Stripe, settlements | /srv/fair-apps/soldiii | 8090 | (LAN; admin via tunnel) | not a git checkout; server/ and web/ copied in | 1.0.4 |
| Vendor Map | Jakmc25/HCF-Vendor-Map | Vendor booth placement on fairground maps | /srv/fair-apps/vendor-map (runs from vendor-map/ subfolder) | 8089 | vendors.jakemc.me | git checkout, but files copied into untracked subfolder | 1.9.0 |
| Incident Report | Jakmc25/HCF-Incident_Report | Password-free incident reports with PDFs, email, Drive | /srv/fair-apps/incidents/hcf-incidents | 8091 | incident.jakemc.me | not a git checkout; files copied, compose.yaml kept | 1.3.2 |
| Budget | Jakmc25/Financial_Summary | Budget vs spent from QuickBooks exports | /srv/fair-apps/budget/hcf-budget | 8092 | budget.jakemc.me | git checkout (git pull) | 1.4.0 |
| Awards | Jakmc25/HCF_Awards | Awards, sponsors, invoices, ribbon inventory; public sponsor form | /srv/fair-apps/awards/hcf-awards | 8093 | awards.jakemc.me | git checkout (git pull) | 1.5.0 |
| Buyer Thank You | Jakmc25/HCF-Livestock_Thankyou | Emails exhibitors their livestock buyers' details | /srv/fair-apps/thankyou/hcf-thankyou | 8094 | thankyou.jakemc.me | git checkout (git pull) | 1.2.0 |

## Shared code (copies, not a library)
- `google_signin.py` + `auth.py`: same modules in Budget, Awards and Thank You (Vendor Map and
  Board Docs have their own `google_signin.py`). A sign-in fix in one usually belongs in all of them.
- App frame (shell, header, modal, toast): Budget, Awards, Thank You, Incident Report.

## Connections
- No app reads another app's data.
- Deploy dependency: the SoldIII, Incident Report and Board Docs deploy commands take the SSH remote
  URL from the Budget checkout (`/srv/fair-apps/budget/hcf-budget`). Moving or renaming that checkout
  breaks those deploys.
- All public addresses go through the same Cloudflare tunnel.
