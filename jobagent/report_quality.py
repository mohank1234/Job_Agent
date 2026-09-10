"""Conservative opportunity deduplication and resume/JD requirement review."""
import ast
import hashlib
import re
from pathlib import Path

from jobagent.startup_output import unique_notes


def deduplicate_opportunities(rows):
    groups = {}
    for row in rows:
        text = re.sub(r'\s+', ' ', row.get('JD Text', '')).strip().casefold()
        title = re.sub(r'\s+', ' ', row.get('Job Title', '')).strip().casefold()
        company = re.sub(r'[^a-z0-9]', '', row.get('Company', '').casefold())
        key = (company, title, hashlib.sha256(text.encode()).hexdigest()) if text else ('url',row['Job Link'])
        groups.setdefault(key, []).append(row)
    result, duplicates = [], []
    for group in groups.values():
        group.sort(key=lambda r: ({'in_scope':0,'needs_review':1,'out_of_scope':2}.get(r.get('Fit Status'),3),r['Job Link']))
        row = dict(group[0])
        row['Fit Notes'] = unique_notes(row.get('Fit Notes',''))
        if len(group)>1:
            row['Duplicate Listing URLs'] = ' | '.join(r['Job Link'] for r in group)
            row['Location Variants'] = ' | '.join(dict.fromkeys(r.get('Location','') for r in group))
            row['Fit Notes'] = unique_notes(row['Fit Notes'], 'Identical employer/title/JD grouped as one opportunity; location variants retained; headcount not inferred')
            duplicates += [{'Company':r['Company'],'Job Title':r['Job Title'],'Duplicate URL':r['Job Link'],
                            'Kept URL':row['Job Link'],'Reason':'Same employer, normalized title and full JD'} for r in group[1:]]
        result.append(row)
    return result, duplicates


def resume_skill_text(path):
    values = []
    for node in ast.parse(Path(path).read_text(encoding='utf-8')).body:
        if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name) and node.targets[0].id in ('SKILLS','EXPERIENCE'):
            values.append(str(ast.literal_eval(node.value)))
    return ' '.join(values).casefold()


def review_frameworks(row, resume_text):
    text = row.get('JD Text','')
    tools = {'TypeScript':r'\btypescript\b','Cypress':r'\bcypress\b','Kubernetes':r'\bkubernetes\b',
             'Terraform':r'\bterraform\b','Cucumber':r'\bcucumber\b','JUnit':r'\bjunit\b',
             'Pytest':r'\bpytest\b','GraphQL':r'\bgraphql\b','Databricks':r'\bdatabricks\b',
             'Playwright':r'\bplaywright\b','JavaScript':r'\bjavascript\b','AWS':r'\baws\b'}
    notes, gaps = [], []
    for name, pattern in tools.items():
        match = re.search(pattern,text,re.I)
        if not match:
            continue
        excerpt = text[max(0,match.start()-85):match.end()+125].strip()
        supported = bool(re.search(pattern,resume_text,re.I))
        if not supported:
            notes.append(f'{name}: not established in resume; JD context: {excerpt}')
            gaps.append(name)
        elif name == 'Playwright':
            notes.append('Playwright: listed on resume; ownership of a substantial production suite is not evidenced by the work bullets')
            gaps.append('Playwright depth')
        elif name == 'JavaScript' and 'javascript (basics)' in resume_text:
            notes.append('JavaScript: resume states basics; advanced proficiency is not established')
            gaps.append('JavaScript depth')
    row['Framework Review'] = ' | '.join(notes) or 'No additional unsupported tools detected in this bounded review; full requirements still need review'
    if gaps:
        row['Fit Notes'] = unique_notes(row.get('Fit Notes',''),'Validate required versus optional tools and practical depth: '+', '.join(gaps))
        if row.get('Fit Status') == 'in_scope':
            row['Fit Status'] = 'needs_review'
    else:
        row['Fit Notes'] = unique_notes(row.get('Fit Notes',''))
    return row
