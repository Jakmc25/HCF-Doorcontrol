# Contracts

One file per thing that more than one app shares: an API, a data file, a database table, a message format.

Each contract states:
- Owner app (the only one allowed to change it)
- Consumer apps
- Exact format (fields, types, required/optional, example)
- Version and change history

Rule: changing a contract needs integration-architect review and tests in every consumer app.
