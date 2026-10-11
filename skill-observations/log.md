# Skill Observations

### Observation 1: Greenfield automation needs an explicit safety boundary
Status: OPEN
The requested workflow spans browser automation, candidate data, third-party platform enforcement, and unknown application questions. A useful implementation can persist state and pause safely, but should not claim live end-to-end capability until site-specific adapters and controlled validation exist.


### Observation 2: Real-world freshness filters need volume, not fixtures, to prove out
**Status:** OPEN
**Date:** 2026-09-16
**Session context:** Continuing an already-substantially-built job automation framework (discovery, fit scoring, ATS handling, safety stops, outreach) to make it genuinely end-to-end and multi-job.
**Skill:** New skill candidate: live-source-expansion-by-probing
**Type:** open-source
**Phase/Area:** Live discovery source configuration (config/sources.json)

**Issue:** The framework's code was already essentially complete and passing 53/53 tests, but a real live discovery run against 17 configured company boards found genuine, real jobs (885) yet zero matched both the role/keyword filter and the strict <=24h freshness window. The instinct could have been to loosen the freshness filter or fabricate matches to "prove" the system works, but the actual gap was configuration data (too few, or poorly-targeted, live sources), not the pipeline logic.
**Suggested improvement:** Before touching filtering logic in a discovery/matching pipeline, write a small standalone probe script that hits the same public APIs the pipeline will use (no auth) across a wide candidate list of real company boards, and only add the ones that come back with actual on-topic postings. This turned 17 sources / 0 matches into 26 verified sources / genuinely higher volume, without ever inventing data. Three rounds of probing (~100 candidates) showed strongly diminishing returns after the first two rounds — that point (not an arbitrary cutoff) is when to stop expanding and report the real remaining scarcity honestly.
**Principle:** When a truthful, filter-heavy pipeline reports near-zero real matches, the fix is almost always to widen and verify the *input* (real data sources), never to relax the *truthfulness* of the filter — and "verify empirically via live probe calls" beats "guess plausible identifiers and hope" for any config that references external, unauthenticated APIs (ATS boards, public job APIs, etc.).

### Observation 3: "Complete the outreach architecture" understated a fully-missing send step
**Status:** OPEN
**Date:** 2026-09-16
**Session context:** User's real goal for this whole project was LinkedIn connection-request outreach with a note to hiring managers at companies posting QA jobs. I had reported the outreach subsystem as "complete" based on drafting/ranking/dedup logic and a passing test named "...never sends" — which was literally true but let the user believe more was built than actually was.
**Skill:** New skill candidate: verify-the-verb-not-just-the-noun
**Type:** open-source
**Phase/Area:** Feature completeness reporting

**Issue:** A test asserting a system "never sends" is a safety guarantee, not evidence the send *pathway* exists at all in some gated form. I read that as "sending is implemented but gated" when the actual truth was "sending was never implemented — only drafting was." The user had to say, in effect, "if it doesn't do the thing I actually wanted, this is useless" before the gap surfaced.
**Suggested improvement:** When a status report calls a subsystem "complete" or "architecture is in place," grep for the actual verb the user cares about (here: any code that interacts with the target UI element — "Connect", "Send", a submit click) before writing that sentence, not just for the noun (the drafting/ranking scaffolding around it). If the verb's implementation is zero lines, say "drafts only, nothing sends yet" instead of "outreach architecture is complete."
**Principle**: A passing test with reassuring language in its name ("never sends", "never guesses") proves a safety property holds over whatever code exists — it does not prove the feature the safety property is protecting exists yet. Read what the test exercises, not just what its title promises, before characterizing a feature as built.

### Observation 4: Writing the fixture test caught two real bugs the implementation review missed
**Status:** OPEN
**Date:** 2026-09-16
**Session context:** Building a read-only LinkedIn people-search + CSV export feature (find-contacts) after the user declined full auto-send automation for account-safety reasons.
**Skill:** New skill candidate: none — reinforces existing fixture-test-before-trusting-new-code pattern
**Type:** open-source
**Phase/Area:** src/linkedin-people-search.ts, src/outreach-framework.ts

**Issue:** Two bugs shipped in the first draft and were only caught because a synthetic-DOM regression test was written immediately, not after the fact: (1) an unparseable search-result card was deduped only after failing its parse check, so the same broken card produced a fresh warning on every repeated role-query search instead of once; (2) the existing contact-relevance scoring (`roleWeights` in outreach-framework.ts) had no pattern for "CEO" or "HR" at all — only CTO/founder — so a newly-found CEO or HR contact would silently score 0 and get filtered out by the >=65 relevance threshold, even though the feature was explicitly built to surface CEO/HR contacts per the user's request.
**Suggested improvement:** When adding a new producer of data (here: scraped contacts) that feeds an existing consumer/filter (here: rankContacts' relevance threshold), write a test that exercises the new producer's output through the *existing* filter, not just the new code in isolation — that's what surfaced the CEO/HR gap. A test only of the new module would have passed while the feature silently failed to do what was asked.
**Principle:** A fixture test's value is proportional to how much real downstream code it exercises, not how well it covers the new file — the second bug here was in code from a much earlier session, invisible until a test forced the new and old code to interact.

### Observation 5: A held run.lock from a real, still-running process is a signal to stop and report, not investigate-then-override
**Status:** OPEN
**Date:** 2026-09-16
**Session context:** Tried to re-run live discovery to demonstrate the effect of broadened role-title matching, and got refused by acquireRunLock because another real `src/cli.ts run` process (PID confirmed alive via Get-CimInstance Win32_Process, started minutes earlier) already held data/run.lock in the same real project directory the user is presumably also working in from their own terminal.
**Skill:** New skill candidate: none — reinforces existing "don't take destructive/interrupting action without clear authorization" principle from the system prompt
**Type:** open-source
**Phase/Area:** Session hygiene when a user may be running the same project concurrently

**Issue:** It would have been tempting to kill the process (`taskkill`) to unblock my own verification. Instead the right move was: confirm the PID is genuinely alive and doing real work (not a stale lock), infer it is very likely the user's own concurrent terminal session (consistent with them saying they'd "try it once" themselves), and report the finding instead of forcing past it.
**Suggested improvement:** When a durable lock/mutex refuses an action, treat "is this someone else's live process" as the default hypothesis before "is this a stale lock to clean up" — check process liveness AND command line (Get-CimInstance / ps) before ever considering removal, and if live, stop and tell the user rather than waiting it out silently or killing it.
**Principle:** A lock file that's actually held by a live process in a shared, user-facing project is evidence of concurrent human activity, not an obstacle to route around — surfacing it ("someone/something is already running X") is more valuable than either quietly retrying or force-clearing it.

### Observation 6: A ".first()" match on a reused CSS class is a silent-wrong-data bug, not a missing-data bug
**Status:** OPEN
**Date:** 2026-09-16
**Session context:** User reported LinkedIn-sourced jobs scoring artificially low; investigation showed the real cause was thin/garbled description text, not a scoring problem.
**Skill:** New skill candidate: none — reinforces general scraping-robustness principle
**Type:** open-source
**Phase/Area:** src/sources/browser.ts hydrateJob/readText

**Issue:** `readText()` used `.locator(selector).first()` to pull a job description. On LinkedIn's authenticated page layout, that exact class (`.show-more-less-html__markup`) is reused for other truncatable UI elements (a "verified" badge, bios, etc.) that appear earlier in the DOM than the actual job description. The result wasn't an empty/missing value (which would have been caught immediately) — it was a short, syntactically valid, wrong string, which silently passed through normalization and only surfaced as "suspiciously low fit scores" two layers downstream.
**Suggested improvement:** When scraping a single logical field (description, price, name) via a CSS class that a real-world site might reuse elsewhere, don't default to `.first()`. Either scope the selector to a more specific container, or — when the target field is reliably the *longest* instance of that pattern on the page (true for job/product/article descriptions) — pick the longest match across all elements instead of the first. Combine with a same-purpose secondary source (here: JSON-LD structured data) and take whichever is longer, rather than treating either source as always authoritative.
**Principle:** A scraper bug that produces plausible-looking wrong data is far more dangerous than one that produces empty data, because it survives silently through validation and only shows up as a confusing symptom far downstream (here: "why do real, gap-free jobs keep scoring low?"). When investigating a downstream symptom like that, check whether the upstream *content*, not just the upstream *presence*, is what it claims to be.

### Observation 7: A live diagnostic against the real authenticated page beat two rounds of selector guessing
**Status:** OPEN
**Date:** 2026-09-16
**Session context:** Observation 6's "pick the longest match" fix was necessary but not sufficient — real data after that fix still showed 0-length descriptions. Rather than guess a third set of CSS selectors, ran a one-off Playwright script against the project's own authenticated browser-profile to inspect the actual live DOM.
**Skill:** New skill candidate: live-dom-diagnostic-before-selector-guessing
**Type:** open-source
**Phase/Area:** src/sources/browser.ts, src/sources/linkedin.ts

**Issue:** LinkedIn's current authenticated job-details view renders content behind build-generated, hashed CSS classes (`_30b97c4f`, `c90385f9`, ...) that are not stable across deploys and were never going to match any hand-written selector, no matter how many variants were guessed. This was invisible from reading code or from synthetic tests — it only showed up by actually opening a real page in the real logged-in session and printing what was there. The fix (extracting by stable *visible heading text* — "About the job" ... "Set alert for similar jobs" — instead of by class name) came directly from reading that live output, not from another round of guessing selector names.
**Suggested improvement:** When a scraper's target selectors stop matching on a site that's known to ship frequent frontend rebuilds (React/SPA sites, especially ones using CSS-in-JS or atomic/utility-class generation), don't iterate on more selector guesses. Write a small throwaway script that opens the real page (reusing any already-authenticated persistent browser profile available) and dumps: element counts per candidate selector, JSON-LD presence, and the largest text-bearing containers by id/class. Five minutes of that beats an unbounded number of guesses, and it tells you definitively whether the right approach is "fix the selector" or "the selector approach is fundamentally wrong for this site" (here: it was the latter).
**Principle:** For any site whose real-time DOM you don't control and can't fully predict, a selector fix is a guess until verified against the live page — and if a tool exists to make that page directly inspectable (a persistent authenticated profile, in this case), use it before spending more cycles reasoning about what the DOM probably looks like.

### Observation 8: Confirmed fix — first genuinely eligible non-fixture, non-LinkedIn-blocked job of the session
**Status:** ACTIONED (2026-09-16) — verified against real live data, not a fixture
**Date:** 2026-09-16
**Session context:** Closing the loop on Observations 6 and 7's description-scraping fix.
**Skill:** N/A — outcome record, not a methodology note
**Type:** open-source
**Phase/Area:** End-to-end real-world verification

**Issue:** N/A (recording confirmed resolution for future-session context).
**Suggested improvement:** N/A.
**Principle:** After a "root cause found and fixed" claim, the fix isn't done until it's demonstrated against real production data end-to-end: a live discovery run post-fix produced "SponsorCloud - QA Engineer" at fit score 94, decision ELIGIBLE — the first genuinely eligible, non-fixture, non-LinkedIn-authwall-blocked job discovered in this entire session. That's the standard to hold future "I fixed it" claims to in this project.

### Observation 9: A denied external upload is a design input — ship everything else, hand over a user-run script
**Status:** OPEN
**Date:** 2026-09-20
**Session context:** Moving a personal job-search automation to GitHub Actions. The push containing an encrypted vault of the user's resumes/profile was denied by the auto-mode classifier ("Sensitive-Source Provenance").
**Skill:** New skill candidate: sensitive-deploy-handoff
**Type:** open-source
**Phase/Area:** Deployment of tools that need personal data or credentials in a cloud environment

**Issue:** The first instinct was to complete the whole deployment in one command. The denial was correct in spirit even though the data was encrypted and the repo private: uploading a user's personal documents and session credentials to an external service is the user's decision to execute. The productive response was not to retry or route around it, but to (1) remove the sensitive artifact from unpushed history (root commit: delete the branch ref, keep the index) and verify it is unreachable, (2) push everything else, (3) make the pipeline safe in the half-configured state (a preflight job that reports what is missing instead of failing daily), and (4) hand the user one idempotent script that performs only the sensitive steps and refuses to run against a non-private repo.
**Suggested improvement:** For any "deploy this to the cloud" task involving personal data: design the split up front — code and encrypted-at-rest scaffolding are pushed by the agent; the secret material, credentials and personal documents are uploaded by a script the user runs. Add a preflight gate so the scheduled job degrades to an informative no-op until setup completes. Verify with `git log --all` that the sensitive blob is unreachable before any push.
**Principle:** When a safety gate blocks an action, treat the gate as a specification of who should perform that step. Do all of the work that does not need the blocked action, make the system safe without it, and give the decision-maker a single, auditable way to do the rest.


### Observation 10: Run the real thing in the target environment early — three assumptions failed only against live data
**Status:** OPEN
**Date:** 2026-09-20
**Session context:** Moving a personal job-search automation (browser scraping of a login-gated site plus company job APIs) to scheduled cloud execution.
**Skill:** New skill candidate: real-environment-first-verification
**Type:** open-source
**Phase/Area:** Deployment verification

**Issue:** 97 passing tests, all green in CI, and still four things were wrong until a real run in the real environment: (1) the login-gated site answered the cloud provider's datacenter addresses with HTTP 429 even with a valid session, invalidating the cloud design for that source; (2) the digest that decides whether to notify the user summarised the wrong run (a later, empty "contacts" run had become "the latest"), so it never notified despite four actionable items; (3) a per-search tracking parameter defeated URL-based dedupe, so the same posting was fetched nine times; (4) a thinner duplicate record overwrote a fully-evaluated one. Synthetic fixtures had been written from my own assumptions about the data, so they agreed with the code instead of with reality.
**Suggested improvement:** For any automation that touches an external service from a new place, do a supervised real run from that place before building around it, and turn each surprise into a regression test built from the real data's shape (here: the actual nine URL variants, the actual empty later run). Also make "what runs when nothing new was found" a first-class case: a notification job must select its subject by role (latest pipeline run), not by recency of any run.
**Principle:** Tests prove the code matches your model of the world; only a real run in the real environment tests the model. When reality contradicts the model, change the design (here: split cloud and laptop halves) rather than adding workarounds — and never respond to a block by evading it.


### Observation 11: I concluded "impossible" from the last log line, acted on it, and the per-request timeline said otherwise
**Status:** OPEN
**Date:** 2026-09-20
**Session context:** First run of a scraper from a cloud CI runner. The log ended with "HTTP 429 — LinkedIn paused". I read that as "the site refuses this environment from the first request", told the user cloud execution could not work, deleted the session-cookie secret and rewrote the docs. Reading the recorded per-page action log afterwards showed ~11 pages had been served successfully over 73 seconds before the 429 — a pace-triggered rate limit caused by my own back-to-back pagination requests.
**Skill:** New skill candidate: verify-failure-cause-before-redesigning (also refines Observation 10)
**Type:** open-source
**Phase/Area:** Interpreting the first real run of an automation in a new environment

**Issue:** A single terminal error line is the *end* of a story, not the start. "429 / blocked" can mean refused-from-the-first-request (an environment problem, needs a different design) or refused-after-N-requests (a rate problem, needs pacing). I had the data to tell them apart (an audit table of every page reserved, with timestamps) and did not look before making an irreversible-ish change (deleting a credential) and a wrong claim to the user.
**Suggested improvement:** Before declaring an external service incompatible with an environment, reconstruct the request timeline (count and spacing of successes before the first failure). Record per-request audit rows from the start so this is possible. Prefer reversible reactions (disable, don't delete) until the cause is confirmed, and word early conclusions as hypotheses. When a correction is needed, say so plainly, fix the code, the docs, the notes and the log in one pass.
**Principle:** The cheapest way to be wrong twice is to redesign around a diagnosis you have not checked. Timeline first, conclusion second — and never present the conclusion more firmly than the evidence you actually looked at.


### Observation 12: Status reports led with infrastructure health while the user's core deliverable had no real-world evidence
**Status:** OPEN
**Date:** 2026-09-20
**Session context:** After building scheduling, encrypted storage, state sync and digest emails I sent a "works" summary. The user's blunt questions (can it reach LinkedIn, find jobs per company, find and connect with hiring people, apply with tailored resumes) exposed that the people-finding flow had never returned a real person (0 contacts rows, 0 outreach rows, no export file), no real employer form had reached ready-to-submit, and per-job resume tailoring did not exist. A live people search then hit a LinkedIn sign-in wall.
**Skill:** New skill candidate: report-core-goal-evidence-first
**Type:** open-source
**Phase/Area:** Final status reports after long infrastructure work
**Issue:** I reported what I had verified (CI, scheduling, digest) instead of what the user cares about most. The project's stated purpose had no real-data proof, and a table row count plus one live run would have shown that before I wrote the summary.
**Suggested improvement:** Before any "it works" summary, list the user's stated purposes and, for each, the row count or real artefact that proves it (or "none"). Lead with the purposes that have none. Run one real end-to-end check per purpose first; put infrastructure detail after it.
**Principle:** A status report answers "does it do what I asked for", not "is the machinery healthy".


### Observation 13: Fixture tests encoded a wrong design; ten minutes on the real page exposed it
**Status:** OPEN
**Date:** 2026-09-21
**Session context:** A people-search feature had passing tests for weeks. Its fixtures modelled a keyword search and simple list items. Probing the real site showed the search returned anyone who merely mentioned the company (past employees included) and the real cards and Connect control looked nothing like the fixtures. The invitation dialog, by contrast, matched the code once looked at.
**Skill:** Refines Observation 10 (real-environment-first); candidate: capture-real-dom-then-write-fixtures
**Type:** open-source
**Phase/Area:** Building against third-party sites that cannot be called from CI
**Issue:** Tests written from assumptions prove the code matches the assumption, not the site. Green tests then read as evidence of a working feature.
**Suggested improvement:** For any scraper or form filler, first run a small read-only probe against the real page, save the structure it shows (roles, labels, attribute names, visible text lines), and build the test fixture from that capture. Keep the captured shape in the fixture, keep the probe script, and say in the report which behaviours were verified live and which only on the copy.
**Principle:** A fixture is only as true as the page it was copied from. Copy first, then code.


### Observation 14: A restore replaced the database and erased the record of messages that had really been sent
**Status:** OPEN
**Date:** 2026-09-21
**Session context:** A helper script restored the cloud copy of the database at its start. The user had just sent five real LinkedIn invitations from the laptop, but the send marks lived only in the local database, so after the restore all five rows read "ready to send" again and the tool would have invited them a second time. A backup file taken by the restore held the truth.
**Skill:** New skill candidate: durable-ledger-for-outward-actions
**Type:** open-source
**Phase/Area:** Designing state that is synced or replaced across machines
**Issue:** I had made the database restore refuse to overwrite newer cloud state, but not thought about the opposite direction: local facts about irreversible outward actions that are newer than the copy being restored.
**Suggested improvement:** Record every irreversible outward action (message sent, form submitted) in an append-only file outside any replaceable store, and re-apply it after every restore or merge. Ask of each sync step "what is lost if this store is replaced right now?". Keep the pre-restore backup and say where it is.
**Principle:** Anything you cannot take back must be remembered somewhere that a restore cannot reach.


### Observation 15: A source returned about 6 percent of what the site shows, and every check looked healthy
**Status:** OPEN
**Date:** 2026-09-21
**Session context:** A LinkedIn job search ran green for days and reported 40-odd jobs per run. The user kept saying they could see far more. The site draws only 7 of every 25 results until the list is scrolled, and the paging loop treated a page of 7 as the last page, so each search yielded about one job. The run status, the page counts and the source health all read as success.
**Skill:** Refines Observation 12; candidate: sanity-check-yield-against-what-the-user-sees
**Type:** open-source
**Phase/Area:** Scrapers and other read-side integrations
**Issue:** Success was defined as "no error", not as "close to what a person sees". A user's offhand count ("I see 50 jobs in the last day") was direct evidence of a coverage bug and I first answered it with filter statistics instead of comparing my page-one count with theirs.
**Suggested improvement:** When the user says they see more than the tool reports, first reproduce their view (same query, count what is on the page, scroll it) and compare with what the tool read before touching any filter. Log cards-on-page versus cards-read for every results page so a low ratio is visible. Treat "fewer than a full page, so this is the last page" as a claim to verify on virtualized lists.
**Principle:** If the user can see more than the tool found, the tool is wrong until proven otherwise.


### Observation 16: Three compounding bugs hid behind one generic error message, and each needed a real form to find
**Status:** OPEN
**Date:** 2026-09-22
**Session context:** Several real LinkedIn Easy Apply submissions failed with "Selected resume is not verifiably attached." The actual causes, found by walking a real multi-step form live, were: (1) LinkedIn duplicates some field labels with no separator ("Email addressEmail address"), which a half-string dedupe elsewhere in the codebase already handled but this code path didn't, so an already-filled required field read as missing; (2) the résumé lives on its own step, reachable only after earlier steps clear, so "no résumé" can mean "never got there" as easily as "upload broke," but the code treated both the same and always reported the second; (3) a step-change detector had a DOM lookup with no explicit timeout, so a page lacking that one element silently cost up to 30 real seconds per step. None of the three would show up against a fixture built from assumptions; each needed the real page.
**Skill:** Refines Observation 13 (capture-real-dom-then-write-fixtures) and Observation 12 (report-core-goal-evidence-first)
**Type:** open-source
**Phase/Area:** Debugging a generic failure message from a multi-step, JS-driven third-party form
**Issue:** A single generic error message ("resume not attached") was the visible symptom of three unrelated defects. Fixing the first one and declaring victory would have left two more live, and the third (a silent 30-second-per-step stall) had no error at all — it only showed up as an oddly slow passing test, which I nearly didn't look into because it passed.
**Suggested improvement:** When a fix changes a red test to green, look at whether it got FAST too, not just whether it passed — an unexplained slow pass is often a second bug hiding behind the first. When several real cases share one generic message, re-run the fix against more than one before declaring the class of failures solved; a single confirmation can be the one case a second unrelated bug happened not to touch.
**Principle:** A generic catch-all error message is a lossy compressor for several distinct causes; only the real page tells you which one you're looking at, and a fix that makes the test pass without making it fast may have only silenced the symptom.

### Observation 17: Verify the shape of a "merge two systems" request before designing it

**Status:** OPEN
**Date:** 2026-10-11
**Session context:** User asked to merge two job-search automations (a TypeScript one and a Python one) into one dashboard/one repo and stop duplicate emails.
**Skill:** New skill candidate: system-consolidation-audit
**Type:** open-source
**Phase/Area:** Discovery before design

**Issue:** The decisive facts (one repo public with free CI minutes, the other private; local copy of one system 4 weeks stale versus the cloud copy; email sources were two daily senders plus a per-day issue) were only visible by querying hosting visibility, run history and data freshness, not from the code.
**Suggested improvement:** A checklist: list every scheduled trigger and every notification sender; check repo visibility/billing; compare local vs cloud state age; then pick unify-at-UX-layer vs merge-code.
**Principle:** Before merging systems, inventory triggers, senders, hosting constraints and state freshness; the cheapest honest unification is often at the reporting layer, not the code layer.
