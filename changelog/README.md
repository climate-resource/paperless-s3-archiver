# Changelog fragments

Each pull request adds a file here, named `<pr-number>.<type>.md`, holding one
line describing the change from the point of view of somebody operating an
archive. `towncrier` assembles them into `CHANGELOG.md` at release time.

Types: `breaking`, `deprecation`, `feature`, `improvement`, `fix`, `docs`,
`trivial`.

A change to how a retain-until date is computed is always `breaking`, even when
the code change is small: dates written under the old rule cannot be corrected
downwards afterwards.
