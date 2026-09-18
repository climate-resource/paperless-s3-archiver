The tap no longer changes permissions in paperless. It used to give every document of a
`restricted` class to the `archive-tap` user and grant it to the group named by
`hr_group`. Now it reads a document and writes nothing back.

Who may see a document is decided inside paperless, by the instance's own permission
model. The grant the tap made did the same job twice, and only on the documents the tap
happened to process. It also failed without a word when the group was missing: the
default, `hr`, was not a group any deployment actually had.

`hr_group` is gone from the config. A config that still sets it loads unchanged, because
unknown top-level keys are ignored. `restricted` stays, and now means only one thing: the
class is left out of the auditor export. The paperless token the tap uses no longer needs
write access to documents or read access to groups. `metrics` still reads users, to find
the break-glass account.
