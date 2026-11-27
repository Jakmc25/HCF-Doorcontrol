# <App name>

## What this app does

## Stack
Language/runtime:
Key libraries:

## Commands
Run locally:
Run tests:
Lint:

## Conventions to keep
- Code style: Python formatted with black; JS/CSS/HTML with prettier (see .claude/standards/STANDARDS.md)
- Version bump = (every file that holds the version)

## Where it runs
- fair-server (Ubuntu, LAN 192.168.1.160), folder /srv/fair-apps/<app>, port <port>, public address
- Deploy (run by the person deploying, see People in STANDARDS.md), as one copy-paste block:
  ```bash
  ```
- Never remove the app's data volume.

## Connections to other apps
(See .claude/standards/APPS.md and .claude/standards/contracts/.)

## App-specific rules
-

## Team workflow
- Standards: .claude/standards/ (synced from the dev-standards repo; don't edit here)
- New work: /feature. Small bugs: /fix. Full review: /audit-app.
- Never push to main or merge. Changes go through pull requests that Jake or Kolt approves.
  Deploys: see People in .claude/standards/STANDARDS.md.
