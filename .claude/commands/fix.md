---
description: Shorter workflow for a small, well-understood bug fix
argument-hint: <bug description>
---
Small-fix workflow for: $ARGUMENTS

1. Find the cause and tell me in 2-3 lines what you think it is and how you'd fix it. STOP for my OK.
2. Create branch fix/<slug> from an up-to-date main. Use the developer subagent to fix it and add a test that fails without the fix.
3. Run the code-reviewer subagent (it also runs the tests). Add qa-tester only if the fix is in money, totals or saving logic, and security-reviewer only if it touches sign-in, user input that is saved or exported, files, or secrets.
   Fix any BLOCKER or SHOULD-FIX; re-check only those IDs.
4. Push the branch and open a pull request to main with the cause, the fix, and test results. Give me the link.
If the fix turns out to touch a contract, shared library, sign-in or money, stop and tell me to use /feature instead.
Never push to main and never merge.
