# Release checklist

- [ ] All tests pass (commands in CLAUDE.md)
- [ ] Every acceptance criterion has a passing test
- [ ] No BLOCKER or SHOULD-FIX findings open
- [ ] No secrets or real fair data in code or git history
- [ ] Version bumped in every place CLAUDE.md lists (VERSION / core.py / compose image tag / README / CHANGELOG / manifest)
- [ ] New files added to the Dockerfile and any file lists or manifests CLAUDE.md mentions
- [ ] Schema changes are additive migrations with a backup first
- [ ] New .env / compose settings written out as exact lines for the person deploying
- [ ] Deploy command in CLAUDE.md still correct; any extra server steps written as one copy-paste block
- [ ] CLAUDE.md History updated
- [ ] APPS.md updated in dev-standards if ports, addresses, folders or connections changed
