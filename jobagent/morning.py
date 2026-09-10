"""One resumable search-and-draft run per IST date, with a 06:00-11:00 gate."""
from __future__ import annotations

import concurrent.futures
import dataclasses
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

from jobagent.models import Job
from jobagent.output import QA_TITLE, partition, make_workbook
from jobagent.outreach.verification import board_ref, canonical, fetch_board, verify_row, csv_text, evidence_markdown, employer_posting, get_public
from jobagent.runtime import atomic_json, now_iso, process_lock

IST = timezone(timedelta(hours=5, minutes=30), "IST")
ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "research" / "morning-private"
BOARD_URLS = {"greenhouse": "https://job-boards.greenhouse.io/{}/jobs",
              "lever": "https://jobs.lever.co/{}", "ashby": "https://jobs.ashbyhq.com/{}",
              "phonepe": "https://www.phonepe.com/careers/job-openings/"}
BOARD_ALIASES = {('greenhouse','ocrolus'):('greenhouse','ocrolusinc'),
                 ('greenhouse','phonepe'):('phonepe','phonepe')}
EXPERIENCE = re.compile(r"(?<![\d.])(\d{1,2})(?:\s*(?:[-\u2013\u2014]|to)\s*(\d{1,2}))?\s*\+?\s*(?:years?|yrs?)\b", re.I)


def in_window(now):
    return 6 <= now.astimezone(IST).hour < 11


def day_key(now):
    return now.astimezone(IST).date().isoformat()


def read_json(path, default):
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else default


def experience_focus(text, low=4, high=6):
    """Minimum experience, not a coincidental years count in a company bio."""
    heading = re.search(r"(?:requirements|qualifications|what you.ll (?:need|bring)|what we.re looking for)", text, re.I)
    portion = text[heading.start():] if heading else text
    general, specialist = [], []
    for m in EXPERIENCE.finditer(portion):
        # Use a local sentence/line so '8 years QA; 4 years Python' is not
        # incorrectly promoted by the specialist number.
        left = max(portion.rfind("\n", 0, m.start()), portion.rfind(". ", 0, m.start()), portion.rfind(";", 0, m.start()))
        right = re.search(r"[\n;]|\.\s", portion[m.end():])
        stop = m.end() + right.start() if right else min(len(portion), m.end() + 170)
        excerpt = portion[max(left + 1, m.start() - 75):stop].strip()
        context = excerpt.lower()
        if not re.search(r"experience|\bqa\b|\bsdet\b|testing|quality assurance|quality engineering|test automation", context):
            continue
        record = (int(m.group(1)), excerpt[:260])
        if re.search(r"professional|overall|total|software (?:quality|testing|test|engineering)|quality (?:assurance|engineering)|automated testing|\bqa\b|\bsdet\b|test (?:engineering|automation)", context):
            general.append(record)
        elif re.search(r"python|java|typescript|playwright|selenium|aws|leadership|manag", context):
            specialist.append(record)
        elif "experience" in context:
            general.append(record)
    if not general:
        return "Not stated clearly", " | ".join(v for _, v in specialist)
    minimum = max(n for n, _ in general)
    evidence = " | ".join(dict.fromkeys(v for _, v in general))
    return ("4-6 year minimum" if low <= minimum <= high else "Outside 4-6 year focus"), evidence


def board_directory(companies, extra_urls=()):
    result = {}
    for vendor, slugs in companies.items():
        if vendor not in BOARD_URLS or not isinstance(slugs, list):
            continue
        for slug in slugs:
            if isinstance(slug, str) and re.fullmatch(r"[A-Za-z0-9_-]+", slug):
                actual = BOARD_ALIASES.get((vendor,slug),(vendor,slug))
                result[actual] = BOARD_URLS[actual[0]].format(actual[1])
    for url in extra_urls:
        ref = board_ref(url)
        if ref:
            ref = BOARD_ALIASES.get(ref,ref)
            result[ref] = BOARD_URLS[ref[0]].format(ref[1])
    return result


def phonepe_board(client):
    # This is the feed used by PhonePe's public careers frontend. Its status
    # filter is essential: never treat non-public/internal records as openings.
    source = 'https://www.phonepe.com/apollo/job-postings/latest.json'
    data = get_public(client,source).json()
    if not isinstance(data.get('results'),list):
        raise ValueError('PhonePe public feed has no results array')
    jobs = [Job(company='phonepe',source='phonepe',title=r.get('title',''),
                url=r['applyUrl'],location=r.get('location',''),description='',
                raw={'status':'PUBLIC','applyUrl':r['applyUrl']})
            for r in data['results'] if r.get('status')=='PUBLIC' and r.get('applyUrl')]
    # The feed contains discovery metadata, not full JDs. QA URLs, if present,
    # remain unverified leads until checked at their application source.
    return jobs, source


def _job_dict(job):
    d = dataclasses.asdict(job)
    if job.posted_at:
        d["posted_at"] = job.posted_at.isoformat()
    return d


def _load_job(data):
    d = dict(data)
    if d.get("posted_at"):
        d["posted_at"] = datetime.fromisoformat(d["posted_at"])
    return Job(**d)


def collect_boards(directory, cache_dir, *, workers=6, deadline=None, progress=None, fetch=fetch_board):
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    def one(item):
        ref, url = item
        path = cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:20] + ".json")
        if path.exists():
            return read_json(path, {})
        if deadline and datetime.now(timezone.utc) >= deadline:
            return {"Company": ref[1], "Vendor": ref[0], "Board URL": url, "Status": "Window ended; not attempted", "jobs": []}
        record = {"Company": ref[1], "Vendor": ref[0], "Board URL": url, "Checked At": now_iso()}
        try:
            with httpx.Client(headers={"User-Agent": "JobAgent/1.0 (personal job search)"}) as client:
                jobs, source = phonepe_board(client) if ref[0]=='phonepe' else fetch(url, client)
            qa = [j for j in jobs if QA_TITLE.search(j.title)]
            record.update({"Status": "Fetched", "Source URL": source, "Postings": len(jobs), "QA titles": len(qa), "jobs": [_job_dict(j) for j in qa]})
        except httpx.HTTPStatusError as exc:
            record.update({"Status": f"HTTP {exc.response.status_code}", "Postings": 0, "QA titles": 0, "jobs": []})
        except Exception as exc:
            record.update({"Status": type(exc).__name__, "Postings": 0, "QA titles": 0, "jobs": []})
        atomic_json(path, record)
        return record
    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, item) for item in directory.items()]
        for future in concurrent.futures.as_completed(futures):
            records.append(future.result())
            if progress and (len(records) % 20 == 0 or len(records) == len(directory)):
                progress(f"Company boards checked: {len(records)}/{len(directory)}")
    records.sort(key=lambda r: (r["Vendor"], r["Company"]))
    return records


def assess_boards(records, profile):
    rows, cache = [], {}
    for source in records:
        if source["Status"] != "Fetched":
            continue
        jobs = [employer_posting(_load_job(d), source['Vendor'], source['Company']) for d in source["jobs"]]
        ref = (source["Vendor"], source["Company"])
        cache[ref] = (jobs, source["Source URL"])
        for job in jobs:
            if not board_ref(job.url):
                continue
            row = verify_row({"Company": job.company, "Job Link": job.url}, profile, None, board_cache=cache)
            # The evidence timestamp is the actual fetch, including after resume.
            row["JD Verified At"] = source["Checked At"]
            if (job.workplace == 'remote' and re.search(r'\b(?:us|usa|united states)\b', job.location, re.I)
                    and not re.search(r'\bindia\b', job.location, re.I)):
                row['Fit Status'] = 'out_of_scope'
                row['Fit Notes'] += '; Employer location restricts this remote posting to the US; India eligibility not established'
            if re.search(r'(?:role|position) (?:is )?(?:open|available|based)[^.]{0,60}(?:across most of the US|United States only|US only)', job.description, re.I):
                row['Fit Status'] = 'out_of_scope'
                row['Fit Notes'] += '; JD explicitly restricts remote hiring to the US'
            focus, evidence = experience_focus(job.description)
            row.update({"Experience Focus": focus, "Experience Evidence": evidence})
            if focus != "4-6 year minimum":
                if focus.startswith("Outside"):
                    row["Fit Status"] = "out_of_scope"
                elif row["Fit Status"] == "in_scope":
                    row["Fit Status"] = "needs_review"
                row["Fit Notes"] += "; " + focus
            impact = re.findall(r"[^.\n]*(?:own|build|design|evaluation|quality strategy|release readiness)[^.\n]*", job.description, re.I)
            row["Impact Evidence"] = " | ".join(s.strip()[:350] for s in impact[:3])
            rows.append(row)
    seen, unique = set(), []
    for row in rows:
        key = canonical(row["Job Link"])
        if key not in seen:
            unique.append(row)
            seen.add(key)
    return unique


def augment_manifest(out, names):
    path = Path(out) / "deliverables.json"
    data = read_json(path, {"version": 1, "files": []})
    data["files"] = list(dict.fromkeys([*data["files"], *names]))
    atomic_json(path, data)


def prepare_report(out, rows, coverage, research, summary):
    from jobagent.startup_output import enrich_rows, write_outreach
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    shortlist = enrich_rows(rows, research)
    tabs = partition(rows)
    summary.update(checked=len(rows), counts={k: len(v) for k, v in tabs.items()}, startup_shortlist_count=len(shortlist))
    grid = write_outreach(out, shortlist)
    atomic_json(out / "startup-research.json", research)
    (out / "verified.csv").write_text(csv_text(rows), encoding="utf-8-sig")
    (out / "evidence.md").write_text(evidence_markdown(rows), encoding="utf-8")
    coverage_fields = ["Company", "Vendor", "Board URL", "Status", "Postings", "QA titles", "Checked At", "Source URL"]
    coverage_rows = [{k: r.get(k, "") for k in coverage_fields} for r in coverage]
    (out / "Source Coverage.csv").write_text(csv_text(coverage_rows, coverage_fields), encoding="utf-8-sig")
    atomic_json(out / "verification.json", summary)
    atomic_json(out / "Daily Run.json", summary)
    draft_state = read_json(out / 'Gmail Draft Status.json', {}).get('drafts', [])
    states = {canonical(d['Job Link']): d for d in draft_state if d.get('Job Link')}
    email_headers = ['Company', 'Job Title', 'To', 'Email Contact', 'Gmail Status', 'Gmail Drafts',
                     'Subject', 'Email Draft', 'LinkedIn Note', 'Manager LinkedIn', 'Email Source',
                     'Recipient Evidence', 'Approval', 'Job Link']
    email_rows = []
    from jobagent.outreach.report_drafts import draft_recipient
    for row in shortlist:
        state = states.get(canonical(row['Job Link']), {})
        recipient = draft_recipient(row)
        email_rows.append([row.get('Company Display Name') or row['Company'], row['Job Title'], recipient,
                           row.get('Email Contact Name') or row.get('Manager Name', ''),
                           state.get('Status') or ('Awaiting Gmail draft creation' if recipient else 'Pending contact research; Excel only'),
                           state.get('Gmail Draft URL', ''), row.get('Cold Email Subject', ''), row.get('Cold Email', ''),
                           row.get('LinkedIn Note', ''), row.get('Manager LinkedIn', ''), row.get('Email Source', ''),
                           row.get('Email Evidence', ''), 'Pending user approval; unsent', row['Job Link']])
    linkedin_headers = ['Name', 'Company', 'JD', 'Source', 'LinkedIn ID', 'LinkedIn Link',
                        'LinkedIn Note', 'LinkedIn Note Length', 'Job Link']
    linkedin_rows = []
    for row in shortlist:
        manager_li, contact_li = row.get('Manager LinkedIn', ''), row.get('Email Contact LinkedIn', '')
        if manager_li:
            name, linkedin_url, source = row.get('Manager Name', ''), manager_li, row.get('Manager Source', '')
        elif contact_li:
            name, linkedin_url, source = row.get('Email Contact Name', ''), contact_li, row.get('Email Source', '')
        else:
            continue
        slug = re.search(r'linkedin\.com/in/([A-Za-z0-9_%.-]+)', linkedin_url)
        note = row.get('LinkedIn Note', '')
        linkedin_rows.append([name, row.get('Company Display Name') or row['Company'], row['Job Title'],
                              source, slug.group(1) if slug else '', linkedin_url,
                              note, str(len(note)), row['Job Link']])
    workbook = make_workbook(tabs, summary, extra_grids={
        "Email drafts": [email_headers, *email_rows],
        "LinkedIn outreach": [linkedin_headers, *linkedin_rows],
        "Startup shortlist": grid,
        "Source coverage": [coverage_fields, *[[r.get(k, "") for k in coverage_fields] for r in coverage_rows]],
    })
    (out / "JobAgent Report.xlsx").write_bytes(workbook)
    augment_manifest(out, ["Startup Outreach.csv", "Startup Outreach.md", "startup-research.json", "Source Coverage.csv", "Daily Run.json"])


def daily_work(config, profile, out, run_dir, *, deadline=None, progress=print):
    from jobagent.enrich.exa import exa_search
    from jobagent.llm import make_provider
    from jobagent.outreach.daily_research import candidate_facts, research_company, cached_draft, remaining, META
    cfg = config.get('morning', {})
    out, run_dir = Path(out), Path(run_dir)
    progress('Morning step 1: search jobs, check full descriptions, and prepare the Excel report.')
    issues = []
    discovery_path = run_dir / 'discovery.json'
    if not discovery_path.exists():
        results, errors = [], []
        for query in cfg.get('discovery_queries', []):
            remaining(deadline)
            try:
                results.extend(exa_search(query, 10, max_characters=1500))
            except Exception as exc:
                errors.append(type(exc).__name__)
        atomic_json(discovery_path, {'results': results, 'errors': errors, 'checked_at': now_iso()})
    discovery = read_json(discovery_path, {})
    issues += [{'stage': 'discovery', 'error': e} for e in discovery.get('errors', [])]
    seeds = read_json(run_dir.parent / 'seed-research.json', {'roles': []})
    old = read_json(out / 'startup-research.json', {'roles': []})
    company_key = lambda name: re.sub(r'[^a-z0-9]', '', name.casefold())
    by_company = {company_key(r['Company']): r for r in [*seeds['roles'], *old['roles']]}
    urls = [r['url'] for r in discovery.get('results', [])]
    urls += [r['Job Link'] for r in old['roles']]
    urls += read_json(run_dir.parent / 'discovered-boards.json', [])
    directory = board_directory(yaml.safe_load((ROOT / 'companies.yaml').read_text(encoding='utf-8')), urls)
    # A resumed run uses its fixed source inventory, so a retry is not a second search.
    directory_path = run_dir / 'directory.json'
    if directory_path.exists():
        directory = {(r['vendor'], r['slug']): r['url'] for r in read_json(directory_path, [])}
    else:
        atomic_json(directory_path, [{'vendor': k[0], 'slug': k[1], 'url': v} for k, v in directory.items()])
    atomic_json(run_dir.parent / 'discovered-boards.json', sorted(set(directory.values())))
    coverage = collect_boards(directory, run_dir / 'boards', workers=cfg.get('workers', 6), deadline=deadline, progress=progress)
    remaining(deadline)
    all_rows = assess_boards(coverage, profile)
    from jobagent.report_quality import deduplicate_opportunities, resume_skill_text, review_frameworks
    resume_text = resume_skill_text(ROOT / 'resume' / 'resume_data.py')
    all_rows = [review_frameworks(r, resume_text) for r in all_rows]
    raw_assessed_count = len(all_rows)
    all_rows, duplicate_rows = deduplicate_opportunities(all_rows)
    relevant = [r for r in all_rows if r['Fit Status'] != 'out_of_scope']
    focus = [r for r in relevant if r['Experience Focus'] == '4-6 year minimum']
    progress(f'Assessed {len(all_rows)} QA-related postings; {len(focus)} match the experience and location focus; {len(relevant)-len(focus)} need experience clarification.')
    for r in coverage:
        if r['Status'] != 'Fetched':
            issues.append({'stage': 'board', 'company': r['Company'], 'error': r['Status']})
    # Keep all decision reasons visible, even when full descriptions exceed the
    # readable-report cap. Every source job is accounted for in this ledger.
    decision_fields = ['Company', 'Job Title', 'Location', 'Job Link', 'JD Status', 'Fit Status', 'Fit Notes', 'Experience Focus', 'Experience Evidence']
    out.mkdir(parents=True, exist_ok=True)
    (out / 'Duplicate Listings.csv').write_text(csv_text(duplicate_rows, ['Company','Job Title','Duplicate URL','Kept URL','Reason']), encoding='utf-8-sig')
    (out / 'All Job Decisions.csv').write_text(csv_text([{k: r.get(k, '') for k in decision_fields} for r in all_rows], decision_fields), encoding='utf-8-sig')
    unsupported = [{'Company': s['Company'], 'Job Title': j['title'], 'Job Link': j['url'],
                    'Status': 'External posting URL; not verified by supported ATS adapter'}
                   for s in coverage for j in s.get('jobs', []) if not board_ref(employer_posting(_load_job(j),s['Vendor'],s['Company']).url)]
    discovery_rows = [{'Company': '', 'Job Title': r['title'], 'Job Link': r['url'],
                       'Status': 'Search discovery only; check All Job Decisions for a verified match'} for r in discovery.get('results', [])]
    (out / 'Discovery Leads.csv').write_text(csv_text([*unsupported, *discovery_rows], ['Company', 'Job Title', 'Job Link', 'Status']), encoding='utf-8-sig')
    llm_config = {**config.get('llm', {}), 'timeout_seconds': 25, 'max_tokens': 5000}
    provider = None
    try:
        provider = make_provider(llm_config)
    except Exception as exc:
        issues.append({'stage': 'research_provider', 'error': type(exc).__name__})
    facts = candidate_facts(ROOT / 'resume' / 'resume_data.py')
    facts['phone'] = str(cfg.get('signature_phone') or '').strip()
    metadata = {}
    # Previously evidenced startups get first attention; no salary-based bonus.
    focus.sort(key=lambda r: (not bool(by_company.get(company_key(r['Company']), {}).get('Investment Source')),
                              r['Fit Status'] != 'in_scope', -float(r.get('Rule Score') or 0)))
    for row in focus:
        key = row['Company'].casefold()
        if key in metadata or len(metadata) >= cfg.get('max_company_research', 15):
            continue
        remaining(deadline)
        progress(f'Researching public leadership and investor evidence: {row["Company"]}')
        metadata[key] = research_company(row, run_dir / 'companies', provider, seed=by_company.get(company_key(key)), deadline=deadline)
        issues += [{'stage': 'company_research', 'company': row['Company'], 'error': e} for e in metadata[key].get('_errors', [])]
    def priority(row):
        meta = metadata.get(row['Company'].casefold(), {})
        backing = 0 if re.fullmatch(r'[WSFX]\d{2,4}', meta.get('YC Batch', '').strip(), re.I) or 'y combinator' in meta.get('Investor Backing', '').lower() else 1 if meta.get('Investment Source') else 2
        return (backing, row['Fit Status'] != 'in_scope', -float(row.get('Rule Score') or 0))
    focus.sort(key=priority)
    roles, drafted_companies, reused = [], set(), 0
    for row in focus:
        remaining(deadline)
        key = row['Company'].casefold()
        meta = metadata.get(key, {'Research Checked At': now_iso(), 'Contact Status': 'Research budget reached; no contact claim'})
        row.update({k: meta.get(k, '') for k in META})
        if key in drafted_companies or len(roles) >= cfg.get('max_drafts', 25):
            continue
        draft, cached = cached_draft(row, meta, facts, run_dir.parent / 'drafts')
        reused += int(cached)
        roles.append({'Company': row['Company'], 'Job Link': row['Job Link'],
                      **{k: meta.get(k, '') for k in META}, **draft,
                      'Draft JD SHA256': row['JD SHA256'], 'Requires Fit Review': row['Fit Status'] != 'in_scope'})
        drafted_companies.add(key)
    others = [r for r in relevant if r not in focus]
    excluded = [r for r in all_rows if r['Fit Status'] == 'out_of_scope']
    selected = [*focus, *others, *excluded][:cfg.get('max_report_rows', 150)]
    summary = {'schema_version': 1, 'checked_at': now_iso(), 'status': 'partial' if issues else 'ok',
               'scope': 'Separate employer careers boards across Greenhouse, Lever and Ashby; broader search results are discovery leads until verified. Full JDs preserved for report rows.',
               'issues': issues, 'leads_not_checked': len(unsupported),
               'applications': 'No applications submitted by this search-and-draft workflow',
               'replies': 'Not measured; drafts require explicit user approval before sending',
               'company_boards': len(coverage), 'boards_fetched': sum(s['Status'] == 'Fetched' for s in coverage),
               'postings_seen': sum(s.get('Postings', 0) for s in coverage),
               'qa_title_candidates': sum(s.get('QA titles', 0) for s in coverage),
               'qa_postings_assessed': len(all_rows), 'external_posting_urls_unverified': len(unsupported),
               'raw_postings_assessed': raw_assessed_count, 'duplicate_listings_grouped': len(duplicate_rows),
               'focus_roles': len(focus), 'experience_needs_clarification': len(others),
               'full_jds_omitted_from_display': max(0, len(all_rows)-len(selected)),
               'drafts': len(roles), 'reused_drafts': reused, 'companies_researched': len(metadata),
               'public_emails': sum(bool(r.get('Public Work Email')) for r in roles),
               'manager_profiles': sum(bool(r.get('Manager LinkedIn')) for r in roles),
               'investor_evidenced_companies': sum(bool(r.get('Investment Source')) for r in roles),
               'compensation_filter': False, 'experience_rule': 'Stated minimum of 4, 5 or 6 years; exact range remains visible',
               'schedule': '06:00 <= Asia/Kolkata < 11:00; at most one completed run per IST date',
               'workflow_order': ['Job search and Excel report', 'Gmail drafts' if cfg.get('gmail_drafts') else 'Gmail drafts disabled', 'Drive publication'],
               'approval_required': True, 'outreach_sent': 0}
    notes = ['# Daily job search and outreach', '', f'Checked: {summary["checked_at"]}', '',
             f'{len(coverage)} employer boards checked, {summary["boards_fetched"]} fetched. This is a count of employer boards, not distinct ATS vendors.',
             f'{summary["postings_seen"]} postings screened for QA titles; {len(focus)} current roles meet the stated minimum experience and geography focus.',
             'No compensation filter. Hyderabad onsite/hybrid and India-eligible remote remain the configured preferences. Singapore requires sponsorship review.',
             'Requirements starting at 4, 5 or 6 years are included; a 5-8-year range is shown exactly. Unstated requirements are a separate review queue.',
             'Start with the Startup shortlist. Every draft is unsent and pending your approval. One draft per company avoids contacting several leaders about the same hire.',
             'Only outreach with a sourced recipient email becomes a Gmail draft. Outreach with missing contacts stays in Excel for research; historical addresses remain labeled and are not delivery-verified.',
             'The job search and Excel report finish first, then Gmail drafts are saved, then outputs are published to the same Drive folder.',
             'Source Coverage.csv records failures and zero-result boards. All Job Decisions.csv keeps all assessed rejection reasons. Discovery Leads.csv contains unverified external links.',
             'The scheduled task checks at 06:00 and every 15 minutes until 11:00 IST, plus logon. A persistent ledger and process lock prevent duplicate completed runs; interrupted runs resume checkpoints.',
             'The computer must be awake, logged in and online within that window. Outside the window it waits for the next morning. Browserbase portal work remains pending separately.', '']
    (out / 'Research Notes.md').write_text('\n\n'.join(notes), encoding='utf-8')
    # Export public evidence, never candidate contacts, credentials or caches.
    evidence = {k: {'metadata': {f: v for f, v in m.items() if not f.startswith('_')},
                    'sources': m.get('_sources', []), 'errors': m.get('_errors', [])} for k, m in metadata.items()}
    atomic_json(out / 'Contact Research.json', evidence)
    prepare_report(out, selected, coverage, {'version': 1, 'roles': roles}, summary)
    augment_manifest(out, ['All Job Decisions.csv', 'Discovery Leads.csv', 'Research Notes.md', 'Contact Research.json', 'Duplicate Listings.csv'])
    if cfg.get('gmail_drafts'):
        remaining(deadline)
        progress('Morning step 2: Excel report prepared; now save Gmail drafts for approval.')
        from jobagent.outreach.report_drafts import sync_report_drafts
        try:
            drafts = sync_report_drafts(out,config.get('drive_output',{}).get('expected_account'),deadline=deadline)
            summary['gmail_drafts'] = {status:sum(d['Status']==status for d in drafts) for status in set(d['Status'] for d in drafts)}
            augment_manifest(out,['Gmail Draft Status.json'])
        except Exception as exc:
            summary['issues'].append({'stage':'gmail_drafts','error':type(exc).__name__})
            summary['status'] = 'partial'
            atomic_json(out/'Gmail Draft Status.json',{'checked_at':now_iso(),'status':'failed','error':type(exc).__name__,'outreach_sent':0})
            augment_manifest(out,['Gmail Draft Status.json'])
        prepare_report(out, selected, coverage, {'version': 1, 'roles': roles}, summary)
    return summary


def run_daily(config, profile, out, *, initial=False, now=None, state_dir=STATE, work=None, publish=None, preflight=None, progress=print):
    now = now or datetime.now(timezone.utc)
    if not initial and not in_window(now):
        return {"status": "skipped_outside_window", "date_ist": day_key(now), "window": "06:00 <= IST < 11:00"}
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with process_lock(state_dir / "daily.lock"):
        key = day_key(now)
        ledger_path = state_dir / "ledger.json"
        ledger = read_json(ledger_path, {"version": 1, "days": {}})
        entry = ledger["days"].get(key, {})
        if entry.get("status") == "completed":
            return {"status": "skipped_already_completed", "date_ist": key}
        if preflight:
            preflight()  # No job/model calls before a usable network/Drive connection.
        run_dir = state_dir / key
        run_dir.mkdir(parents=True, exist_ok=True)
        entry.update(status="running", initial=initial, started_at=entry.get("started_at") or now_iso())
        ledger["days"][key] = entry
        atomic_json(ledger_path, ledger)
        deadline = None if initial else datetime.combine(now.astimezone(IST).date(), datetime.min.time(), IST).replace(hour=11).astimezone(timezone.utc)
        try:
            if entry.get("stage") != "prepared":
                result = work(config, profile, out, run_dir, deadline=deadline, progress=progress)
                entry.update(stage="prepared", summary=result)
                atomic_json(ledger_path, ledger)
            if deadline and datetime.now(timezone.utc) >= deadline:
                entry.update(status="window_ended", finished_at=now_iso())
                atomic_json(ledger_path, ledger)
                return {"status": "window_ended", "date_ist": key}
            progress('Morning step 3: job report and draft stage finished; publish outputs to Drive.')
            publication = publish(out) if publish else None
            entry.update(status="completed", finished_at=now_iso(), publication=publication)
            atomic_json(ledger_path, ledger)
            return {"status": "completed", "date_ist": key, "summary": entry["summary"], "publication": publication}
        except Exception as exc:
            entry.update(status="interrupted", error=type(exc).__name__)
            atomic_json(ledger_path, ledger)
            raise
