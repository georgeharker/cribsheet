
* Crib storage for code indexes, notes and additional features below should optionally be
  allowed to be in a specified subdir in a project.  How this works with the consistent project
  store between versions needs to be determined st. it's root relative.  It should be
  configurable in .crib but by default in the global project storage.  Discuss.
* Plan and design dependencies
    - a full documented pair of hierarchies which store design decisions with dependencies and allows re-checking other design decisions if a dependent design is changed.  Standard delete add, remove semantics, but deletion of a design decision which others depend on errors - warns that this may have impacts.  Individual dependencies can be added, removed etc.  Tooling to pull a dep tree which allows reconsideration.  Links to notes, docs.  Goal: reduce unexpected consequences.
    - Plans similar, but with a status (not started, in progress, done, verified or similar).  Maintain a persistent plan list for the project, allow resume.  standard ops, modify status.  Allow rendering and producing a plan which is ordered.  Dependencies also tracked.  Order between items specifiable, but needs to be easy to maintain the ordering without worrying about removal or permuting when dependencies are added etc.  So some thought into ordering which could be priority /rank ordering rather than explicit
    - both semantically searchable as well as by id

Discuss designs with me.

---

Execution plans (detailed, self-contained — written 2026-08-05 after design
discussion + a 4-agent review sweep; execute in any order, suggested first):

1. `docs/plans/robustness-fixes.md` — prioritized defects from the review
   (Tier 0 security/data-loss first; item 1.4 is the live "Collection does
   not exist" daemon bug).
2. `docs/plans/surface-parity-fixes.md` — CLI⇄MCP pairing audit fixes.
3. `docs/plans/repo-local-storage.md` — item 1 above, decisions settled.
4. `docs/plans/design-plan-tracking.md` — item 2 above, decisions settled.
