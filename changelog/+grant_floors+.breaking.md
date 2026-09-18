A `grant:<slug>` tag now raises the retain-until date of **any** class, instead of only
being read by the `grant` clock.

The rule it replaces asked a human to do arithmetic. A subcontractor invoice charged to a
project is both a Buchungsbeleg and a record supporting a declared cost, it may carry only
one class, and whoever filed it had to work out which of the two periods ran longer and
pick that class. That is hostile on its own, and it is worse than it looks: `receipts` is
the document's own year + N and grows every year while a grant's date is fixed, so which
one wins changes over the life of a grant. Getting it wrong under-retains, permanently,
in a store where a date can be extended and never shortened.

So the grant tag is now a floor rather than an alternative. A document says what it is —
`class:receipts` — and which grant it supports — `grant:futura` — and this computes the
later of the two bases. Both appear in the explanation carried into the sidecar, so the
reason a document outlives its own class is readable beside the date.

Two behaviour changes follow, and both are why this is `breaking` rather than a feature:

- A document on the `document_year` or `employment_end` clock that carries a grant tag may
  now get a **later** date than it did before. Nothing gets an earlier one — the floor only
  ever raises, which is the one combination that cannot be wrong.
- A grant tag naming a slug the registry does not have is now a refusal on **every** class,
  not just on `eu-grant`. Previously a typo on a receipt silently lost the grant basis.
  Now it costs a re-tag, which is the trade the grant clock has always made.

`class:eu-grant` is unchanged and still the right class for a document whose only basis is
the agreement — a deliverable, a consortium agreement, a periodic report.
