-- Support the most common real-world case: the user knows their submission is
-- wrong because an online judge told them so, but does not have the failing
-- test. Local samples pass, so compile-and-judge alone would return AC and
-- stop. Recording the external verdict lets the pipeline skip straight to
-- authoring a reference implementation and hunting for a counterexample.

ALTER TABLE submissions
    ADD COLUMN external_verdict TEXT NOT NULL DEFAULT '';
