import type { Config } from './config.js';

// Explicit tool boundaries prevent Java satisfying JavaScript or Selenium satisfying Cypress.
export const skillPatterns: Array<[string, RegExp]> = [
  ['Manual testing', /\bmanual (?:qa|testing)\b/i], ['Functional testing', /\bfunctional testing\b/i],
  ['Regression testing', /\bregression(?: automation| testing)?\b/i], ['Automation testing', /\b(?:test automation|automation testing|automated testing|ui automation|automation frameworks?|automation test engineer)\b/i],
  ['Selenium', /\bselenium(?: webdriver)?\b/i], ['Playwright', /\bplaywright\b/i], ['Cypress', /\bcypress\b/i],
  ['Appium', /\bappium\b/i], ['Java', /\bjava\b/i], ['JavaScript', /\bjavascript\b/i], ['TypeScript', /\btypescript\b/i],
  ['Python', /\bpython\b/i], ['C#', /(?:\bC#|\bc sharp\b)/i], ['API testing', /\b(?:api testing|api automation|rest api|api test(?:ing)? automation)\b/i],
  ['Postman', /\bpostman\b/i], ['REST Assured', /\brest[ -]?assured\b/i], ['SQL', /\bsql\b/i],
  ['Database testing', /\b(?:database testing|database validation|backend validation)\b/i],
  ['CI/CD', /\b(?:ci\s*\/\s*cd|continuous integration|continuous delivery)\b/i], ['Jenkins', /\bjenkins\b/i],
  ['Docker', /\bdocker\b/i], ['Kubernetes', /\bkubernetes\b/i], ['AWS', /\baws\b|amazon web services/i],
  ['Azure', /\bazure\b/i], ['Git', /\bgit\b/i], ['TestNG', /\btestng\b/i], ['JUnit', /\bjunit\b/i],
  ['JMeter', /\bjmeter\b/i], ['Performance testing', /\b(?:performance testing|load testing)\b/i],
  ['LLM testing', /\bllm(?:s)?(?: testing| evaluation)?\b|large language models?/i],
  ['RAG testing', /\brag(?: testing| evaluation)?\b|retrieval.augmented generation/i],
  ['AI agent testing', /\b(?:ai agent|agentic ai)(?: testing| evaluation)?\b/i],
  ['GenAI testing', /\b(?:genai|generative ai)(?: testing)?\b/i],
  ['AI application testing', /\bai (?:application )?testing\b/i], ['Prompt testing', /\bprompt (?:testing|evaluation)\b/i],
  ['Promptfoo', /\bpromptfoo\b/i], ['DeepEval', /\bdeepeval\b/i],
  ['Computer vision testing', /\b(?:computer vision|ai\/cv|cv testing)\b/i],
  ['ISTQB certification', /\bistqb\b/i], ['Salesforce', /\bsalesforce\b/i], ['SAP', /\bsap\b/i],
  ['German language', /\b(?:german (?:language|fluency)|fluent (?:in )?german)\b/i],
  ['French language', /\b(?:french (?:language|fluency)|fluent (?:in )?french)\b/i],
  ['Leadership', /\b(?:team leadership|leading (?:a |qa |testing )?teams?|mentoring|people management)\b/i],
];

export function extractSkills(text: string): string[] {
  return skillPatterns.filter(([, pattern]) => pattern.test(text)).map(([name]) => name);
}
export type FitResult = {
  score: number; reason: string; matched: string[]; missing: string[];
  mandatoryMissing: string[]; preferredMissing: string[]; decision: 'ELIGIBLE' | 'REVIEW' | 'NOT_ELIGIBLE';
};
export type FitContext = { title?: string; location?: string; workMode?: string };
type Clause = { text: string; mandatory: boolean; preferred: boolean };
function requirementClauses(description: string): Clause[] {
  let section: 'unknown' | 'required' | 'preferred' = 'unknown';
  return description.replace(/<\/(?:p|li|div|h\d)>/gi, '\n').replace(/<[^>]+>/g, ' ')
    .replace(/(?:,\s*|\s+(?:and|but|while|whereas)\s+)(?=(?:[^,;.]{0,80}\s)?(?:preferred|optional)\b)/gi, '; ')
    .replace(/,\s*(?=(?:preferred|nice.to.have|optional)\s*:)/gi, '; ')
    .split(/\n|;|(?<=[.!?])\s+/).flatMap((line) => {
    const text = line.trim();
    if (!text) return [];
    if (/^(?:preferred|nice to have|bonus|desirable|good to have)(?:\s+qualifications|\s+skills)?\s*:?$/i.test(text)) section = 'preferred';
    else if (/^(?:requirements|qualifications|minimum qualifications|what you(?:'|’)ll (?:need|bring)|required skills)\s*:?$/i.test(text)) section = 'required';
    else if (/^(?:responsibilities|benefits|about (?:us|you)|what (?:we offer|you(?:'|’)ll do))\s*:?$/i.test(text)) section = 'unknown';
    const explicitPreferred = /\b(?:preferred|nice.to.have|bonus|desirable|good.to.have|not required|optional)\b/i.test(text);
    const explicitMandatory = !explicitPreferred && /\b(?:must|required|requires|mandatory|minimum|at least|essential)\b/i.test(text);
    const preferred = explicitPreferred || (!explicitMandatory && section === 'preferred');
    const mandatory = !preferred && (explicitMandatory || section === 'required');
    return [{ text, mandatory, preferred }];
  });
}

const GENERAL_EXPERIENCE_SKILLS = ['Manual testing', 'Functional testing', 'Regression testing', 'Automation testing', 'API testing'];
/**
 * The experience level a posting asks for, in years, or undefined when it states none. A range counts by its top ("2-4 years" = 4: someone
 * with 4 years is who it is written for); a bare or open minimum counts as itself ("3+ years" = 3). Only overall experience counts: years
 * with one specific tool ("2 years with Cypress") or company age ("founded 20 years ago") say nothing about the role's level. With several
 * overall mentions the highest wins ("5+ years in QA, including 2 years of automation" = 5).
 */
export function requiredExperienceYears(title: string, description: string): number | undefined {
  const text = description.replace(/<\/(?:p|li|div|h\d)>/gi, '\n').replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ');
  const mention = /(?:(?:minimum|min\.?|at least|atleast)\s*(?:of\s*)?)?(\d{1,2}(?:\.\d)?)\s*(\+)?\s*(?:(?:-|–|—|to)\s*(\d{1,2}(?:\.\d)?)\s*\+?\s*)?(years?|yrs?|months?)\b/gi;
  let level: number | undefined;
  const consider = (source: string, needsExperienceWord: boolean): void => {
    for (const clause of source.split(/\n|;|(?<=[.!?])\s+/)) {
      // A bullet that opens with the years ("4+ years in Data Quality, …") states the level without saying "experience".
      const bullet = /^[\s•●▪*·\-–]*(?:minimum\s+|at least\s+)?\d{1,2}(?:\.\d)?\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)\s+(?:in|of|as|working)\b/i.test(clause);
      if (needsExperienceWord && !bullet && !/\bexp(?:erience|\.)?\b|\bexperienced\b/i.test(clause)) continue;
      if (/\byears? (?:ago|old|of age)\b|\b(?:founded|in business|serving|established|history|legacy|track record|education|qualification)\b/i.test(clause)) continue;
      for (const match of clause.matchAll(mention)) {
        const following = clause.slice((match.index ?? 0) + match[0].length).replace(/^\s*(?:of\s+)?(?:(?:relevant|professional|hands-on|overall|total|work|industry)\s+)*(?:experience\s+)?/i, '');
        const [, preposition, rest] = /^(in|with|using|on|of)\s+(.+)/i.exec(following) ?? [];
        const object = rest?.split(/[,.;]| and /)[0] ?? '';
        const skills = extractSkills(object);
        // Years "with"/"using" something name a tool (LinkedIn appends "N+ years of work experience with <skill>" lines); years "in" a field
        // are the role's level unless that field is itself one specific tool.
        if (/^(?:with|using)$/i.test(preposition ?? '') ? !skills.length || skills.some((skill) => !GENERAL_EXPERIENCE_SKILLS.includes(skill)) : skills.some((skill) => !GENERAL_EXPERIENCE_SKILLS.includes(skill))) continue;
        const low = Number(match[1]); const high = match[3] ? Number(match[3]) : undefined;
        const years = (high ?? low) / (/^month/i.test(match[4]) ? 12 : 1);
        if (years > 40) continue;
        level = Math.max(level ?? 0, years);
      }
    }
  };
  consider(title, false); // "Lead Test Engineer (5-7 Years)"
  consider(text, true);
  return level;
}

export function scoreJob(description: string, config: Config, context: FitContext = {}): FitResult {
  const profileText = [...config.candidate.skills, ...config.candidate.automation_skills, ...config.candidate.ai_testing_skills].join('\n');
  const verified = new Set(extractSkills(profileText));
  const clauses = requirementClauses(description);
  const matched: string[] = []; const missing: string[] = []; const mandatoryMissing: string[] = []; const preferredMissing: string[] = [];
  const addMissing = (name: string, mandatory: boolean, preferred = false) => {
    if (!missing.includes(name)) missing.push(name);
    if (mandatory && !mandatoryMissing.includes(name)) mandatoryMissing.push(name);
    if (preferred && !preferredMissing.includes(name)) preferredMissing.push(name);
  };
  const wanted = extractSkills(description);
  let matchedWeight = 0; let totalWeight = 0;
  for (const skill of wanted) {
    const pattern = skillPatterns.find(([name]) => name === skill)![1];
    const relevant = clauses.filter((clause) => pattern.test(clause.text));
    const mandatory = relevant.some((clause) => clause.mandatory);
    const preferred = relevant.length > 0 && relevant.every((clause) => clause.preferred);
    const alternativeSatisfied = relevant.length > 0 && relevant.every((clause) => {
      if (!/\bor\b/i.test(clause.text)) return false;
      // Require a small alternatives list; unrelated requirements in the same paragraph cannot satisfy it.
      const tokens = clause.text.match(/\b(?:JavaScript|TypeScript|Python|Java|Selenium|Playwright|Cypress|Appium)\b\s*(?:,\s*)?(?:or|\/)\s*\b(?:JavaScript|TypeScript|Python|Java|Selenium|Playwright|Cypress|Appium)\b/gi) ?? [];
      return tokens.some((token) => pattern.test(token) && extractSkills(token).some((name) => verified.has(name)));
    });
    const weight = mandatory ? 2 : preferred ? 0.4 : 1;
    if (alternativeSatisfied && !verified.has(skill)) continue;
    totalWeight += weight;
    if (verified.has(skill)) { matched.push(skill); matchedWeight += weight; }
    else addMissing(skill, mandatory, preferred);
  }
  const title = context.title ?? description.split(/[.\n]/)[0];
  const titleMatch = /\b(?:qa|quality assurance|sdet|test(?:ing)? (?:automation |quality )?engineer|software test|automation (?:qa|engineer)|llm testing|rag testing|ai agent testing)\b/i.test(title);
  let score = titleMatch ? 25 : 0;
  if (titleMatch) matched.push('QA/testing role'); else addMissing('QA/testing title match', false);
  score += totalWeight ? 50 * matchedWeight / totalWeight : 0;
  let foundExperience = false; let experienceSatisfied = true;
  for (const clause of clauses) {
    const years = clause.text.match(/(?:minimum of\s+|at least\s+)?(\d+)(?:\s*[-–]\s*\d+)?\+?\s*(?:years?|yrs?)(?:\s+of)?\s*(?:experience)?/i);
    if (!years || /\byears?\s+ago\b/i.test(clause.text) || (!clause.mandatory && /\b(?:company|business|founded|serving customers)\b/i.test(clause.text) && !/\bexperience\b/i.test(clause.text))) continue;
    foundExperience = true;
    const required = Number(years[1]);
    const following = clause.text.slice((years.index ?? 0) + years[0].length).trim();
    const specific = /^(?:in|with|using|of)\s+/i.test(following) && extractSkills(following.split(/[,.;]/)[0]).some((skill) => !['Manual testing', 'Functional testing', 'Regression testing', 'Automation testing', 'API testing'].includes(skill));
    if (specific) { experienceSatisfied = false; addMissing(`${required}+ years of specified technology experience (unverified)`, !clause.preferred, clause.preferred); }
    else if (config.candidate.years_experience !== null && config.candidate.years_experience >= required) matched.push(`${required}+ years overall experience`);
    else { experienceSatisfied = false; addMissing(`${required}+ years overall experience`, !clause.preferred, clause.preferred); }
  }
  score += foundExperience ? (experienceSatisfied ? 15 : 0) : (config.candidate.years_experience !== null ? 10 : 0);
  const locations = config.search.locations.length ? config.search.locations : config.candidate.preferred_locations;
  const jobLocation = `${context.location ?? ''} ${context.workMode ?? ''}`.trim() || description;
  const locationMatch = !locations.length || locations.some((location) => jobLocation.toLowerCase().includes(location.toLowerCase()));
  if (locationMatch) { score += 10; matched.push('Preferred location'); } else addMissing('Preferred location compatibility', false);
  for (const clause of clauses) {
    if (!clause.mandatory) continue;
    const authorization = clause.text.match(/(?:authori[sz]ed|eligib(?:le|ility)) to work in ([^,;.]+)/i);
    if (authorization) {
      const country = authorization[1].split(/\s+(?:without|and|or)\s+/i)[0].trim().replace(/^the\s+/i, '');
      const countryKey = (value: string): string => /^(?:us|usa|united states(?: of america)?)$/i.test(value) ? 'us' : /^(?:uk|united kingdom)$/i.test(value) ? 'uk' : value.toLowerCase();
      if (!config.candidate.authorized_countries?.some((value) => countryKey(value) === countryKey(country))) addMissing(`Work authorization in ${country} (unverified)`, true);
    }
    const postgraduate = /\b(?:master(?:'s|s)?\s+(?:degree|in|of)|ph\.?d|doctorate)\b/i.test(clause.text);
    const bachelorsAlternative = /bachelor.*\bor\b.*master|master.*\bor\b.*bachelor/i.test(clause.text);
    if (postgraduate && !bachelorsAlternative && !config.candidate.education.some((item) => /master|ph\.?d|doctorate/i.test(item))) addMissing('Required postgraduate qualification', true);
  }
  if (!titleMatch || wanted.length < 2 || description.trim().length < 80) score = Math.min(score, 64);
  if (mandatoryMissing.length) score = Math.min(64, score - Math.min(40, mandatoryMissing.length * 12));
  score = Math.round(Math.max(0, Math.min(100, score)));
  const decision = mandatoryMissing.length ? 'NOT_ELIGIBLE' : score >= config.application.auto_apply_min_score ? 'ELIGIBLE' : score >= config.application.review_min_score ? 'REVIEW' : 'NOT_ELIGIBLE';
  return { score, matched, missing, mandatoryMissing, preferredMissing, decision,
    reason: `Matched verified profile: ${matched.join(', ') || 'none'}. Missing or unverified: ${missing.join(', ') || 'none'}. Mandatory gaps: ${mandatoryMissing.join(', ') || 'none'}. Decision: ${decision}.` };
}
