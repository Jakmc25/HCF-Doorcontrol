---
description: Run the dev-team workflow for a new feature, sized to the change
argument-hint: <feature or bug description>
---
Run our team workflow for: $ARGUMENTS

## 0. Size it first
Read just enough of the app to judge, then tell me in one line which size you suggest and why, and wait for my OK (I may change it):
- **Small**: a bug, wording, or one small screen tweak; no new data. → Use the /fix workflow instead.
- **Medium**: a new field, page section or report; small schema addition; nothing in the "risky" list.
- **Large**: anything in the risky list, a new page or area with its own data, or more than about 5 files.
- **Risky** (always Large): sign-in or permissions, money or payments, deleting data or schema changes to existing tables, files uploaded or downloaded, secrets, outside services (QuickBooks, Google, Stripe, email), shared code or contracts between apps.

"Security check needed" means the change touches anything in the risky list, user input that is saved or exported, or a new route.

Pick a short slug. Create and switch to branch feature/<slug> from an up-to-date main. Ask all open questions in one go, not a few at a time.

## Medium
1. Use the architect subagent with **model sonnet** to write one short docs/features/<slug>/PLAN.md that starts with the acceptance criteria (no separate SPEC.md). Show me the summary and open questions. STOP and wait for my approval.
2. Use the developer subagent to implement it.
3. Run the code-reviewer subagent (it also runs the tests). Add the security-reviewer only if a security check is needed. Put findings in docs/features/<slug>/REVIEW.md.
4. Fix rules below.
5. Use the release-ops subagent for the go/no-go, then step 7 of Large.

## Large
1. Use the product-owner subagent to write docs/features/<slug>/SPEC.md. Show me the summary and open questions. STOP and wait for my approval.
2. Use the architect subagent to write PLAN.md. Show me the summary.
   If the plan says INTEGRATION REVIEW REQUIRED, run the integration-architect subagent (Mode A) on it and show its findings.
   STOP and wait for my approval.
3. Use the developer subagent to implement the plan. For sign-in, money or outside-service work, run the developer and the security-reviewer with **model opus**.
4. Run the code-reviewer and qa-tester subagents in parallel, plus security-reviewer if a security check is needed (plus integration-architect Mode A if integration review was required).
   Combine all findings into docs/features/<slug>/REVIEW.md (DRIFT findings are listed but not fixed in this feature) using .claude/standards/templates/REVIEW.md.
5. Fix rules below.
6. Use the release-ops subagent for the go/no-go.
7. If GO: commit, push the feature/<slug> branch, and open a pull request to main.
   PR description: the release notes, test results, link to SPEC/PLAN/REVIEW, open NITs, and the deploy block.
   If NO-GO: don't open the PR; tell me why.
8. Give me a short summary and the PR link.

## Fix rules (both sizes)
- Send every BLOCKER and SHOULD-FIX to the developer in one batch. Cheap NITs can go in the same batch; the rest stay open in REVIEW.md.
- Re-check only with the reviewers that raised a BLOCKER or SHOULD-FIX, and tell them to look only at the fix commit and those IDs. NIT fixes are not re-checked.
- Another round only if a re-check finds a new BLOCKER or SHOULD-FIX. Stop after 3 rounds and ask me what to do.
- Don't ask agents for full reports when a short one will do; the documents hold the detail.

Never push to main and never merge. Merging the PR is my decision, and it deploys.
