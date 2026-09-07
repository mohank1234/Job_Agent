# Job Agent

Personal job **discovery + matching** agent. Pulls postings from company ATS
boards and remote-job aggregators, dedupes them, **stores every one**, classifies
the role, matches it against your real career profile, then writes a digest that
explains *why* each job fits and how hard to chase it.

It answers "which jobs available now are realistically worth me applying for?",
not "which jobs contain my keywords?".

```
sources -> normalize -> dedupe -> STORE ALL -> role classification ->
candidate detection -> profile matching (LLM) -> score -> rank -> digest
```

**By default, no job is ever deleted.** Postings that do not fit are stored
with match category D (low) or E (not relevant) and stay in `jobs.db`. This
is controlled by `retention.prune_non_matching` in `config.yaml` (off by
default) — turning it on does delete D/E rows, leaving a small tombstone so
they aren't re-fetched. Leave it off unless you deliberately want a lossy
database.

## Why it hits company ATS boards

Greenhouse, Lever, Ashby, SmartRecruiters and Workable expose public, documented
JSON endpoints for every company board. That means:

- **No scraping**, no auth, no ToS problem, no account-ban risk
- Postings appear the moment they go live — usually hours before any aggregator
  indexes them, which is when your application is at the top of the pile
- AI-first companies almost all use one of these five vendors

The aggregator feeds (RemoteOK, Remotive, Arbeitnow, Jobicy, optional Adzuna)
add breadth on top.

**LinkedIn and Naukri are deliberately not included.** Both prohibit automated
access and ban accounts for it. Use their own saved-search email alerts for
coverage there.

## Setup

```bash
pip install -r requirements.txt
cp config.yaml.example config.yaml     # then edit it — see below
cp profile.yaml.example profile.yaml   # then fill in your real profile
```

Matching runs through **Gemini's free tier** — no card, no local gateway to
run. `llm.provider: gemini` in `config.yaml` selects it, with `groq` and the
now-removed `omnirate` (a local OmniRoute gateway, used until 2026-08-20) as
the fallback chain's other free-tier options. The `anthropic` and
`openrouter` providers still exist in `jobagent/llm.py` but are paid and off
by default. Get a Gemini key in 60 seconds at
https://aistudio.google.com/apikey, then
`setx GEMINI_API_KEY "your-key"` (reopen the terminal afterward). See
`config.yaml.example`'s `llm:` block for the full provider/model rationale.

## Usage

```bash
python run.py fetch             # the daily command -> digest_YYYY-MM-DD.md
python run.py score             # resume LLM matching of deferred candidates
python run.py stats             # what the database holds, by category and role
python run.py top               # best matches currently stored (--all for D/E)
python run.py digest            # rebuild the digest from jobs.db, no fetching
python run.py classify-test     # role-classification regression suite
python run.py fetch --no-llm    # fetch + classify + store, zero LLM calls
python run.py verify            # check every ATS slug in companies.yaml is live
python run.py tailor <url> --title "..." --company "..." --description "..."
```

`fetch` LLM-scores the top `llm.max_jobs_per_run` candidates and defers the
rest; `score` picks up exactly where it stopped. Unchanged postings reuse their
cached verdict instead of costing another call.

## Choosing a provider and model

`python run.py models` queries OpenRouter's public catalogue directly, so the
slugs and prices are always current — never copy a model id out of a blog post.

**OpenRouter does not make Claude cheaper.** It bills Claude at Anthropic's own
rates (Opus 5 is $5/$25 per million tokens either way), plus a small fee when
you buy credits. The reason to use it is *access*: one key, 400+ models, and the
ability to switch models by editing one config line.

Where that matters here: scoring is a bulk classification task, and the
prefilter has already done the hard filtering. Verified options as of writing:

| Model | $/M in | $/M out | Notes |
|---|---|---|---|
| `anthropic/claude-opus-5` | 5.00 | 25.00 | Best judgement; overkill for this |
| `anthropic/claude-sonnet-5` | 2.00 | 10.00 | Good default |
| `anthropic/claude-haiku-4.5` | 1.00 | 5.00 | Fine for scoring |
| `openai/gpt-oss-20b:free` | free | free | Free tier, rate-limited |
| `google/gemma-4-26b-a4b-it:free` | free | free | Free tier, rate-limited |

Start on a free model to confirm the plumbing works, then move up if the scores
feel noisy — score quality is the whole point of this stage, so don't optimise
it to zero. Adding `:batch` to an Anthropic slug halves the price if you don't
mind waiting.

Models without native JSON-schema support still work; the provider falls back
to prompt-instructed JSON with tolerant parsing.

## How matching works

**Stage 1 — role classification** (`jobagent/roles.py`). Free, offline, runs on
every stored job. Titles are matched by *family*, not by string equality, so
"Senior QA Engineer", "Senior Quality Assurance Engineer" and "Sr. Software QA
Engineer" all land together.

| Tier | Families |
|---|---|
| 1 | core QA · test automation / SDET · AI-LLM QA · AI / model evaluation |
| 2 | RAG & AI agent testing · chatbot / conversational AI · computer vision · SWE-test / quality / tooling |
| 3 | SWE — AI applications · SWE — general (only with real quality overlap) |
| 4 | different job family · profile exclusions (stored, never deleted) |

Technologies are **signals, not roles**: Selenium, Playwright, Postman, Java,
RAG, LLM, agents and CV decide *how strongly* you match a role that has already
been identified. Tiers 1-3 become candidates — deliberately permissive.

**Stage 2 — profile matching** (`jobagent/matcher.py`). Candidates are compared
against the full profile by a free model through OmniRoute: role alignment,
responsibilities, seniority, skills, gaps, location. The final score blends the
LLM verdict with the deterministic score (`llm.llm_weight`), which stops a free
model inflating a generic SWE posting and stops the rule engine rewarding raw
keyword count.

| Score | Category | Meaning |
|---|---|---|
| 90-100 | **A** | strong match — apply today |
| 80-89 | **B** | very good match |
| 60-79 | **C** | good / adjacent — consider |
| 40-59 | **D** | low match (stored, not shown) |
| 0-39 | **E** | not relevant (stored, not shown) |

Experience is a **flexible factor, never a filter**. A role asking for somewhat
more years than you have still scores well when the responsibilities and skills
match; only genuinely senior scope — architecture ownership, people management,
research depth — pulls the score down hard.

## Tuning

Your profile lives in `profile.yaml`; run settings live in `config.yaml`.

| Symptom | Fix |
|---|---|
| Too few QA jobs found | Add QA-heavy companies to `companies.yaml`; enable Adzuna for India roles |
| Wrong *kind* of job ranked high | Adjust `target_roles` / `exclusions.off_domain_titles` in `profile.yaml` |
| Scores feel wrong | Rewrite `identity.summary` — that is what the model reasons over |
| Generic SWE roles creeping up | Lower `llm.llm_weight` so the deterministic score dominates |
| Matching too slow | Lower `llm.max_jobs_per_run`, then run `python run.py score` later |
| Free model rate-limited | Add more free ids to `llm.fallback_models` |

Run `python run.py classify-test` after any change to `profile.yaml` or
`roles.py` — it is the regression suite for the matching engine.

## Adding companies

Find the slug from any job posting URL on their careers page:

| URL pattern | Vendor | Slug |
|---|---|---|
| `boards.greenhouse.io/SLUG/jobs/123` | greenhouse | `SLUG` |
| `jobs.lever.co/SLUG/uuid` | lever | `SLUG` |
| `jobs.ashbyhq.com/SLUG/uuid` | ashby | `SLUG` |
| `careers.smartrecruiters.com/SLUG/123` | smartrecruiters | `SLUG` |
| `apply.workable.com/SLUG/j/ABC` | workable | `SLUG` |

Add it to `companies.yaml`, then run `python run.py verify` to confirm.

Aim for 100–200 target companies. That is where this stops being a toy and
starts beating manual searching — you see everything they post, the day they
post it.

## Run it daily

Windows Task Scheduler:

```
Program:   python
Arguments: D:\job-agent\run.py fetch
Start in:  D:\job-agent
Trigger:   Daily, 8:00 AM
```

## On auto-apply

Not implemented, on purpose. Programmatic submission violates every major
portal's ToS and gets accounts banned — and mass-generic applications are
exactly what gets filtered out of the 25 LPA band anyway.

`tailor` covers the useful half: a job-specific pitch you paste into the
application yourself. Applying to 10 roles with a tailored pitch beats 200
identical submissions, and it's what actually converts at this level.

## Layout

```
run.py                  CLI / pipeline driver
profile.yaml            YOUR CAREER PROFILE - roles, skills, seniority, exclusions
config.yaml             run settings: LLM, sources, digest
companies.yaml          target company ATS boards
jobs.db                 sqlite; every job ever fetched, with full match history
jobagent/
  profile.py            profile.yaml loader
  roles.py              role classification + deterministic scoring
  models.py             Job dataclass, fingerprinting, workplace inference
  store.py              retain-everything store + score cache
  llm.py                provider layer - omnirate | openrouter | anthropic
  matcher.py            profile matching, batching, retry, resume, tailoring
  report.py             markdown digest
  sources/ats.py        Greenhouse / Lever / Ashby / SmartRecruiters / Workable
  sources/feeds.py      RemoteOK / Remotive / Arbeitnow / Jobicy / Adzuna
tests/role_cases.py     role-classification regression cases
```

## Turning this into a portfolio piece

This is one addition away from being AI-QA portfolio material rather than just a
personal tool. Hand-label ~50 jobs as good-fit / bad-fit, then build an eval
suite that measures the matcher's precision and recall against your labels, with
a CI gate that fails when a prompt change drops the score.

At that point it stops being "I built a job scraper" and becomes "I built an
LLM system and the evaluation harness that proves it works" — which is the exact
claim the roles you're targeting are hiring for.
