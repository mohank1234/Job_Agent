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
    'Domain', 'Website', 'Startup Source', 'Team Size', 'Industry', 'Region', 'Remote Status',
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
                  'Region': ', '.join(company.get('regions', [])), 'Industry': company.get('industry', ''),
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


PROVIDER_CODES = ('COMPANY_WEBSITE', 'PROSPEO', 'HUNTER', 'TOMBA', 'NO_VERIFIED_EMAIL')


def _person_contact(person, email, provider, detail, domain):
    """Contact fields for a provider-verified address of a named decision-maker."""
    linkedin = person.get('url', '') if 'linkedin.com/in/' in person.get('url', '') else ''
    source = next((u for u in (person.get('source'), linkedin) if (u or '').startswith(('http://', 'https://'))),
                  f'https://{domain}')
    label = provider.title()
    return {'Public Work Email': email, 'Email Source': source, 'Email Contact Name': person['name'],
            'Email Contact Role': person['title'], 'Email Contact LinkedIn': linkedin,
            'Email Provider': provider, 'Email Verification Status': 'verified', 'Company Domain': domain,
            'Email Evidence': f'{label}: {detail}; name and role from {source}',
            'Email Ownership Status': f'Verified deliverable mailbox; found by {label}',
            'Email Ownership Checked At': now_iso()}


def enrich_contacts(rows, metadata, sizes, lookups, cfg, state_dir, today, *,
                    search, allow_paid=False, contacted=lambda row: False,
                    remaining=lambda: None, progress=print, read_site=None):
    """One decision-maker per relevant company, then the email waterfall:
    company website -> Prospeo -> Hunter -> Tomba -> NO_VERIFIED_EMAIL,
    stopping at the first published or verified address. Free lookups
    (website, known leaders) always come before any provider credit."""
    from jobagent.enrich import hunter, prospeo, tomba
    from jobagent.enrich.credits import Credits, LimitReached
    from jobagent.outreach import website_contacts
    from jobagent import discovery
    read_site = read_site or website_contacts.read_site
    state_dir = Path(state_dir)
    issues = []
    prospeo_credits = Credits(state_dir / 'provider-usage' / 'prospeo.json', cfg.get('prospeo_monthly_limit', 100), 'Prospeo',
                              account=prospeo.account_status if prospeo.api_key() else None)
    tomba_credits = Credits(state_dir / 'provider-usage' / 'tomba.json', cfg.get('tomba_monthly_limit', 25), 'Tomba')
    hunter_usage = state_dir / 'hunter-usage.json'
    live = {'PROSPEO': bool(prospeo.api_key()), 'HUNTER': bool(hunter.api_key()), 'TOMBA': tomba.active()}
    hunter_finder = hunter.make_finder(hunter_usage, cfg.get('hunter_monthly_limit', 50)) if live['HUNTER'] else None
    target = cfg.get('min_employees', 10), cfg.get('max_employees', 200)
    recheck = discovery.recent_cutoff(today, cfg.get('recheck_days', 30))
    empty_recheck = discovery.recent_cutoff(today, cfg.get('recheck_empty_days', 7))
    signature = [live['PROSPEO'], live['HUNTER'], live['TOMBA']]
    tally = dict.fromkeys(PROVIDER_CODES, 0)
    seen, seen_domains, used_emails = set(), set(), set()
    for row in rows:
        key, ck = row['Company'].casefold(), founders.key(row['Company'])
        meta = metadata.get(key)
        if meta is None or ck in seen or contacted(row):
            continue
        seen.add(ck)
        known = sizes.get(ck, {})
        size = (known.get('low'), known.get('high'))
        team_size = size[1] if size[1] is not None else row.get('Team Size')
        # Relevance before any credit: outside the startup size range, skip.
        if size[0] is not None and not founders.in_target(size, *target):
            continue
        # A careers-board name ("globalli") is a poor lookup key; the company's
        # own website is often linked in its research sources or job description.
        jd_links = re.findall(r'https?://[^\s<>"\')]+', row.get('JD Text', ''))[:40]
        domain = (founders.professional_domain(row.get('Domain') or row.get('Website') or meta.get('Domain') or '')
                  or founders.company_domain(row['Company'], [s.get('url', '') for s in meta.get('_sources', [])]
                                             + [row.get('Employer Job Link', ''), *jd_links]))
        if domain:
            meta['Domain'] = domain
            if domain in seen_domains:
                meta.pop('Public Work Email', None)
                meta['Email Provider'] = 'NO_VERIFIED_EMAIL'
                meta['Contact Status'] = 'Duplicate company domain; retained the first company lookup'
                progress(f'{row["Company"]}: duplicate domain already used for another company; no additional lookup.')
                continue
            seen_domains.add(domain)
        if (founders.verified_leadership_contact(meta, domain)
                and recent(meta.get('Email Ownership Checked At'), max_age_days=30)):
            used_emails.add(meta['Public Work Email'].casefold())
            continue
        earlier = lookups.get(domain) or lookups.get(ck, {})
        cache_valid = not earlier.get('domain') or earlier['domain'] == domain
        cached = earlier.get('contact') or {}
        if (cache_valid and earlier.get('checked', '') >= recheck
                and founders.verified_leadership_contact(cached, domain)
                and recent(cached.get('Email Ownership Checked At'), max_age_days=30)
                and cached['Public Work Email'].casefold() not in used_emails):
            meta.update(cached)
            used_emails.add(cached['Public Work Email'].casefold())
            continue
        if (cache_valid and not cached and earlier.get('checked', '') >= empty_recheck
                and earlier.get('providers') == signature and not earlier.get('retry_needed')):
            meta.setdefault('Email Provider', 'NO_VERIFIED_EMAIL')
            continue  # no verified email last week; retried after recheck_empty_days
        remaining()
        steps = []
        retry_needed = False
        # Decision-maker first, from free sources: leaders already known
        # (job posting, research, search) and the company's own website.
        site = {'pages': [], 'leaders': [], 'emails': []}
        if domain:
            site_url = row.get('Website') or f'https://{domain}'
            try:
                site = read_site(site_url)
            except Exception as exc:
                retry_needed = True
                steps.append(f'website unreadable ({type(exc).__name__})')
        try:
            known_people = founders.leaders(row, meta, search, team_size=team_size)
        except Exception as exc:
            retry_needed = True
            issues.append({'stage': 'leadership', 'company': row['Company'], 'error': type(exc).__name__})
            known_people = []
        people = sorted({founders.key(p['name']): p for p in [*site.get('leaders', []), *known_people]}.values(),
                        key=lambda p: founders.rank(p.get('title')))
        best = next((p for p in people if founders.split_name(p['name'])), None)
        if best and not meta.get('Manager Name'):
            meta.update({'Manager Name': best['name'], 'Manager Role': best['title'],
                         'Manager Source': best.get('source') or best.get('url', ''),
                         'Manager LinkedIn': best.get('url', '') if 'linkedin.com/in/' in best.get('url', '') else ''})
        # 1. Company website: an address the company itself publishes.
        contact = website_contacts.published_contact(site, people, domain) if domain else {}
        steps.append(f"website: published {contact['Public Work Email']}" if contact else
                     f"website: no published leadership email ({len(site.get('pages', []))} pages read)"
                     if domain else 'website: skipped (company website not known)')
        name_parts = founders.split_name(best['name']) if best else None
        # 2. Prospeo (needs the decision-maker's name).
        if not contact:
            if not live['PROSPEO']:
                retry_needed |= signature[0]
                steps.append('Prospeo: skipped (no PROSPEO_API_KEY)')
            elif not (name_parts and domain):
                steps.append('Prospeo: skipped (no decision-maker name or website identified)')
            else:
                try:
                    email, detail = prospeo.find_email(name_parts[0], name_parts[1], domain, prospeo_credits)
                    retry_needed |= detail.startswith('unusable response')
                    steps.append(f'Prospeo: {detail}')
                    if email:
                        contact = _person_contact(best, email, 'PROSPEO', detail, domain)
                except LimitReached as exc:
                    retry_needed = True
                    live['PROSPEO'] = False
                    steps.append(f'Prospeo: stopped for this run ({exc})')
                except Exception as exc:
                    retry_needed = True
                    steps.append(f'Prospeo: error {type(exc).__name__}')
                    issues.append({'stage': 'prospeo', 'company': row['Company'], 'error': type(exc).__name__})
        # 3. Hunter: the named decision-maker if known, else one Domain Search
        # that lists the company's leaders (one credit either way, never both).
        if not contact:
            if not live['HUNTER']:
                retry_needed |= signature[1]
                steps.append('Hunter: skipped (no HUNTER_API_KEY or allowance used)')
            else:
                try:
                    if name_parts and domain:
                        contact = founders.verified_contact([best], domain, hunter_finder)
                        steps.append(f"Hunter Email Finder for {best['name']}: "
                                     + ('verified' if contact else 'no verified address'))
                    else:
                        executives = hunter.executives(founders.plain_name(row['Company']), domain, hunter_usage,
                                                        cfg.get('hunter_monthly_limit', 50))
                        contact = founders.contact_from_executives(row['Company'], executives, domain_given=bool(domain),
                                                                   team_size=team_size, expected_domain=domain,
                                                                   provider='HUNTER')
                        listed = [e for e in executives if founders.is_decision_maker(e.get('title', ''))]
                        steps.append(f"Hunter Domain Search: {len(executives)} executives, "
                                     f"{len(listed)} decision-makers, " + ('verified email' if contact else 'none verified'))
                        if contact and size[0] is None:
                            # Size unknown: Hunter's company lookup decides.
                            email_domain = contact['Public Work Email'].rsplit('@', 1)[1]
                            found = hunter.company_size(email_domain, hunter_usage, cfg.get('hunter_monthly_limit', 50))
                            if found:
                                sizes[ck] = {'Employee Count': founders.size_label(found), 'low': found[0],
                                             'high': found[1], 'checked': today.isoformat(),
                                             'Employee Count Source': f'Hunter company enrichment for {email_domain}'}
                                meta.update({'Employee Count': sizes[ck]['Employee Count'],
                                             'Employee Count Source': sizes[ck]['Employee Count Source']})
                                if not founders.in_target(found, *target):
                                    steps.append(f'Hunter: {founders.size_label(found)} employees, outside the target')
                                    contact = {}
                    if contact:
                        contact['Email Provider'] = 'HUNTER'
                except Exception as exc:
                    retry_needed = True
                    live['HUNTER'] = False
                    steps.append(f"Hunter: stopped for this run ({getattr(exc, 'kind', type(exc).__name__)})")
                    issues.append({'stage': 'hunter', 'company': row['Company'],
                                   'error': getattr(exc, 'kind', type(exc).__name__)})
        # 4. Tomba (needs the decision-maker's name), the last resort.
        if not contact:
            if not live['TOMBA']:
                retry_needed |= signature[2]
                steps.append('Tomba: skipped (no TOMBA keys or allowance used)')
            elif not (name_parts and domain):
                steps.append('Tomba: skipped (no decision-maker name or website identified)')
            else:
                try:
                    email, detail = tomba.find_email(name_parts[0], name_parts[1], domain, tomba_credits)
                    retry_needed |= detail.startswith('unusable response')
                    steps.append(f'Tomba: {detail}')
                    if email:
                        contact = _person_contact(best, email, 'TOMBA', detail, domain)
                except LimitReached as exc:
                    retry_needed = True
                    live['TOMBA'] = False
                    steps.append(f'Tomba: stopped for this run ({exc})')
                except Exception as exc:
                    retry_needed = True
                    steps.append(f'Tomba: error {type(exc).__name__}')
                    issues.append({'stage': 'tomba', 'company': row['Company'], 'error': type(exc).__name__})
        # Duplicate protection: one address is never used for two companies.
        if contact and contact['Public Work Email'].casefold() in used_emails:
            steps.append(f"duplicate: {contact['Public Work Email']} already used for another company")
            contact = {}
        if contact:
            meta.update(contact)
            used_emails.add(contact['Public Work Email'].casefold())
            tally[contact['Email Provider']] = tally.get(contact['Email Provider'], 0) + 1
        else:
            meta['Email Provider'] = 'NO_VERIFIED_EMAIL'
            tally['NO_VERIFIED_EMAIL'] += 1
        who = f"{best['name']} ({best['title']})" if best else 'not identified'
        progress(f"{row['Company']} [{domain or 'no website'}] decision-maker {who}: "
                 + ' -> '.join(steps) + f" => {contact.get('Email Provider', 'NO_VERIFIED_EMAIL')}")
        earlier.update(checked=today.isoformat(), domain=domain, contact=contact, providers=signature,
                       people=people[:3], leaders_checked=today.isoformat(), retry_needed=retry_needed and not bool(contact))
        lookups[domain or ck] = earlier
        atomic_json(state_dir / 'founder-emails.json', lookups)
    progress('Leadership emails this run: ' + ', '.join(f'{k} {v}' for k, v in tally.items())
             + f'. {prospeo_credits.summary()}; {tomba_credits.summary()}.')
    return issues
