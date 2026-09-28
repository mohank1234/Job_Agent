"""Integrate public startup discovery and bounded leadership enrichment.

This module supplies the existing morning report; it never creates or sends
mail. Checkpoints and provider counters live alongside the daily state.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from jobagent.runtime import atomic_json, now_iso
from jobagent.outreach import founders
from jobagent.outreach.service import recent
from jobagent.outreach.verification import canonical

STARTUP_FIELDS = (
    'Domain', 'Website', 'Startup Source', 'Team Size', 'Region', 'Remote Status',
    'Has Relevant Opening', 'Opening Checked At', 'Opening Check Status',
    'Job Source', 'Date Discovered', 'Notes', 'Email Provider', 'Email Verification Status',
)


def discover(cfg, profile, run_dir, rows, coverage, *, remaining=lambda: None, progress=print, keep_domains=()):
    """Add directory identity to actual jobs and retain checked no-opening leads.

    `keep_domains` are startups with an unsent draft: they are checked again
    every day (free), so the draft stays current until it is sent."""
    from jobagent.morning import read_json
    from jobagent.sources import yc_directory
    from jobagent.startup_research import check_openings
    if not cfg.get('enabled', False):
        return [], []
    run_dir = Path(run_dir)
    path = run_dir / 'yc-startups.json'
    if path.exists():
        checks = read_json(path, [])
    else:
        remaining()
        inventory = yc_directory.fetch_hiring()
        ranked = yc_directory.candidates(inventory, min_size=cfg.get('min_employees', 10),
                                        max_size=cfg.get('max_employees', 200),
                                        primary=[profile.preferences.get('home_country', 'India')],
                                        places=profile.locations)
        history_path = run_dir.parent / 'startup-check-history.json'
        history = read_json(history_path, {})
        # New domains first, then least recently checked. Stable fit ordering
        # remains the tie-breaker and a run never researches an unbounded list.
        ranked.sort(key=lambda c: history.get(c['domain'], ''))
        keep = set(keep_domains)
        todays = [c for c in ranked if c['domain'] in keep]
        todays += [c for c in ranked if c['domain'] not in keep][:max(0, int(cfg.get('max_companies', 5)))]
        checks = []
        for company in todays:
            remaining()
            progress(f"Checking startup openings: {company['name']} ({company['domain']}).")
            result = check_openings(company, profile, existing_rows=rows, existing_coverage=coverage)
            checks.append(result)
            history[company['domain']] = now_iso()
            atomic_json(path, checks)
            atomic_json(history_path, history)
    startups, additions = [], []
    existing = {canonical(r['Job Link']): r for r in rows}
    seen_coverage = {(c['Vendor'], c['Company']) for c in coverage}
    for check in checks:
        company = check['company']
        checked = check['checked_at']
        has_opening = bool(check.get('has_relevant_opening'))
        common = {'Company': company['name'], 'Company Display Name': company['name'],
                  'Domain': company['domain'], 'Website': company['website'],
                  'Startup Source': 'YC public directory', 'Team Size': company['team_size'],
                  'Employee Count': str(company['team_size']), 'Employee Count Source': company['url'],
                  'Region': ', '.join(company.get('regions', [])),
                  'Has Relevant Opening': has_opening, 'Opening Checked At': checked,
                  'Opening Check Status': ('Checked: ' if check.get('complete') else 'Incomplete: ') + check['status'],
                  'Date Discovered': checked, 'Investor Backing': 'Y Combinator',
                  'YC Batch': company.get('batch', ''), 'Investment Source': company['url'],
                  'Startup Priority': 'YC-backed', 'Research Checked At': checked,
                  'Notes': 'Opening result covers the sources checked; it is not a claim about unpublished hiring needs.'}
        for row in check.get('rows', []):
            merged = {**row, **common, 'Has Relevant Opening': True,
                      'Remote Status': row.get('Workplace', ''),
                      'Job Source': row.get('JD Source URL', '')}
            key = canonical(row['Job Link'])
            if key in existing:
                existing[key].update(merged)
            else:
                additions.append(merged)
                existing[key] = merged
        for record in check.get('coverage', []):
            key = (record['Vendor'], record['Company'])
            if key not in seen_coverage:
                coverage.append(record)
                seen_coverage.add(key)
        if has_opening:
            continue
        text = company.get('one_liner', '')
        startups.append({**common, '_startup': company, '_opening_sources': check.get('sources', []),
                         'Job Title': 'Senior QA / SDET (proactive enquiry)',
                         'Job Link': company['url'] or company['website'],
                         'Location': company.get('location') or common['Region'],
                         'Remote Status': 'Not established; enquiry only', 'Job Source': company['url'],
                         'JD Text': text, 'JD Source URL': company['url'], 'JD Status': 'proactive',
                         'JD SHA256': hashlib.sha256(text.encode()).hexdigest(),
                         'Fit Status': 'needs_review', 'Fit Notes': 'No public matching role found; ask about QA/SDET needs.',
                         'Rule Score': 0, 'Experience Focus': 'Proactive enquiry'})
    rows.extend(additions)
    return startups, checks


def enrich_contacts(rows, metadata, sizes, lookups, cfg, state_dir, today, *,
                    search, allow_paid=False, contacted=lambda row: False,
                    remaining=lambda: None, progress=print):
    """Known verified contact -> Hunter -> Apollo -> opt-in existing verifier."""
    from jobagent.enrich import apollo, hunter
    from jobagent.enrich.email_finder import find_emails
    from jobagent import discovery
    state_dir = Path(state_dir)
    issues = []
    hunter_active = bool(hunter.api_key())
    apollo_active = bool(apollo.api_key())
    hunter_usage = state_dir / 'hunter-usage.json'
    hunter_finder = hunter.make_finder(hunter_usage, cfg.get('hunter_monthly_limit', 50)) if hunter_active else None
    paid_finder = (lambda people: find_emails(people, max_charge_per_run_usd=cfg.get('max_charge_per_run_usd', 0.5))) if allow_paid else None
    target = cfg.get('min_employees', 10), cfg.get('max_employees', 200)
    recheck = discovery.recent_cutoff(today, cfg.get('recheck_days', 30))
    empty_recheck = discovery.recent_cutoff(today, cfg.get('recheck_empty_days', 7))
    seen = set()
    for row in rows:
        key, ck = row['Company'].casefold(), founders.key(row['Company'])
        meta = metadata.get(key)
        if meta is None or ck in seen or contacted(row):
            continue
        seen.add(ck)
        known = sizes.get(ck, {})
        size = (known.get('low'), known.get('high'))
        team_size = size[1] if size[1] is not None else row.get('Team Size')
        is_startup = bool(row.get('Startup Source') or row.get('Listing Type', '').startswith('Y Combinator'))
        if size[0] is not None and not founders.in_target(size, *target):
            continue
        if size[0] is None and not (is_startup or hunter_active or apollo_active):
            continue
        # A careers-board name ("globalli") is a poor lookup key; the company's
        # own website is often linked in its research sources or job description.
        jd_links = re.findall(r'https?://[^\s<>"\')]+', row.get('JD Text', ''))[:40]
        domain = (founders.professional_domain(row.get('Domain') or row.get('Website') or meta.get('Domain') or '')
                  or founders.company_domain(row['Company'], [s.get('url', '') for s in meta.get('_sources', [])]
                                             + [row.get('Employer Job Link', ''), *jd_links]))
        if domain:
            meta['Domain'] = domain
        if (founders.verified_leadership_contact(meta, domain)
                and recent(meta.get('Email Ownership Checked At'), max_age_days=30)):
            continue
        earlier = lookups.get(domain) or lookups.get(ck, {})
        cache_valid = not earlier.get('domain') or earlier['domain'] == domain
        cached = earlier.get('contact') or {}
        if (cache_valid and earlier.get('checked', '') >= recheck
                and founders.verified_leadership_contact(cached, domain)
                and recent(cached.get('Email Ownership Checked At'), max_age_days=30)):
            meta.update(cached)
            continue
        remaining()
        people = earlier.get('people', []) if cache_valid and earlier.get('leaders_checked', '') >= recheck else []
        if not people:
            try:
                people = founders.leaders(row, meta, search, team_size=team_size)
            except Exception as exc:
                issues.append({'stage': 'leadership', 'company': row['Company'], 'error': type(exc).__name__})
                people = []
            if people:
                earlier.update(leaders_checked=today.isoformat(), people=people)
        if people and not meta.get('Manager Name'):
            lead = people[0]
            meta.update({'Manager Name': lead['name'], 'Manager Role': lead['title'],
                         'Manager Source': lead.get('source') or lead.get('url', ''),
                         'Manager LinkedIn': lead.get('url', '') if 'linkedin.com/in/' in lead.get('url', '') else ''})
        provider_signature = [bool(hunter.api_key()), bool(apollo.api_key()), bool(allow_paid)]
        if (cache_valid and not cached and earlier.get('checked', '') >= empty_recheck
                and earlier.get('providers') == provider_signature):
            continue
        contact, failed, attempted = {}, False, False
        if hunter_active:
            attempted = True
            try:
                executives = hunter.executives(founders.plain_name(row['Company']), domain, hunter_usage,
                                                cfg.get('hunter_monthly_limit', 50))
                contact = founders.contact_from_executives(row['Company'], executives, domain_given=bool(domain),
                                                           team_size=team_size, expected_domain=domain)
                listed = [e for e in executives if founders.rank(e.get('title', '')) < 4]
                progress(f"Hunter {row['Company']} ({domain or 'by name'}): {len(executives)} executives listed; "
                         f"leaders: {', '.join(e['title'] + (' (verified)' if e['verified'] else '') for e in listed[:3]) or 'none'}; "
                         f"{'verified email used' if contact else 'no verified leader email'}.")
                if contact and size[0] is None:
                    # Size unknown: Hunter's company lookup decides, 10-200 only.
                    email_domain = contact['Public Work Email'].rsplit('@', 1)[1]
                    found = hunter.company_size(email_domain, hunter_usage, cfg.get('hunter_monthly_limit', 50))
                    if found:
                        sizes[ck] = {'Employee Count': founders.size_label(found), 'low': found[0], 'high': found[1],
                                     'Employee Count Source': f'Hunter company enrichment for {email_domain}',
                                     'checked': today.isoformat()}
                        meta.update({'Employee Count': sizes[ck]['Employee Count'],
                                     'Employee Count Source': sizes[ck]['Employee Count Source']})
                        if not founders.in_target(found, *target):
                            progress(f"Hunter {row['Company']}: {founders.size_label(found)} employees, outside the target; not used.")
                            contact = {}
                if not contact and people and domain:
                    contact = founders.verified_contact(people, domain, hunter_finder)
            except Exception as exc:
                failed = True
                hunter_active = False  # no repeated billing/auth/refusal calls this run
                issues.append({'stage': 'hunter', 'company': row['Company'], 'error': getattr(exc, 'kind', type(exc).__name__)})
        if not contact and apollo_active and domain:
            attempted = True
            try:
                contact = apollo.best_contact(row['Company'], domain, state_dir / 'apollo-usage.json',
                                              monthly_limit=cfg.get('apollo_monthly_limit', 10), team_size=team_size)
            except Exception as exc:
                failed = True
                apollo_active = False
                issues.append({'stage': 'apollo', 'company': row['Company'], 'error': getattr(exc, 'kind', type(exc).__name__)})
        if not contact and paid_finder and people and domain:
            attempted = True
            try:
                contact = founders.verified_contact(people, domain, paid_finder)
            except Exception as exc:
                failed = True
                paid_finder = None
                issues.append({'stage': 'founder_email', 'company': row['Company'], 'error': getattr(exc, 'kind', type(exc).__name__)})
        if contact:
            meta.update(contact)
            progress(f"Verified professional email: {row['Company']} / {contact['Email Contact Role']}.")
        if contact or (attempted and not failed):
            earlier.update(checked=today.isoformat(), domain=domain, contact=contact, providers=provider_signature)
        lookups[domain or ck] = earlier
        atomic_json(state_dir / 'founder-emails.json', lookups)
    return issues
