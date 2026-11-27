---
description: Full review of an existing app (whole repo, not just recent changes)
---
Audit this entire repository, not just recent changes.

1. Run the code-reviewer, qa-tester, security-reviewer and release-ops subagents in parallel, telling each it is in audit mode for the whole repo.
   (qa-tester may run tests but must not add files during an audit.)
2. Combine everything into docs/audits/<today's date>.md, grouped by severity (BLOCKER, SHOULD-FIX, NIT, DRIFT), keeping each finding's ID.
3. Commit the audit file on a branch chore/audit-<date>, push it, and open a pull request so the report is saved.
4. Show me the top 10 issues and a suggested fix order (each can become a /fix or /feature).
Do not change any application code.
