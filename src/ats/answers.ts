import type { Config } from '../config.js';
import type { CandidateAnswer, FormField } from '../core/types.js';

export function normalizeQuestion(value: string): string {
  // Trim before dropping the final "?" / ".": LinkedIn marks required questions "\u2026in English?*", and the "*" becomes a trailing space.
  return value.normalize('NFKC').toLowerCase().replace(/\s*\((?:required|optional)\)/g, '').replace(/[_*:\u200b]/g, ' ').replace(/\s+/g, ' ').trim().replace(/[?.]+$/, '').trim();
}

const aliases: Record<string, string[]> = {
  state: ['state', 'state province'], english_proficiency: ['english proficiency', 'what is your level of proficiency in english'], joining_availability: ['joining availability', 'how soon can you join'],
  full_name: ['full name', 'name', 'legal name', 'your name'],
  first_name: ['first name', 'given name', 'legal first name'],
  last_name: ['last name', 'surname', 'family name', 'legal last name'],
  email: ['email', 'email address', 'your email'], phone: ['phone', 'phone number', 'mobile', 'mobile number', 'mobile phone', 'mobile phone number', 'cell phone', 'cell phone number', 'contact number', 'contact phone', 'telephone number'],
  location: ['location', 'current location', 'where are you located', 'location (city)', 'current location (city)', 'what is your current location'],
  city: ['city', 'current city'], country: ['country', 'country of residence'],
  linkedin: ['linkedin', 'linkedin profile', 'linkedin url', 'linkedin profile url'],
  github: ['github', 'github url', 'github profile'], portfolio: ['portfolio', 'portfolio url', 'website', 'personal website'],
  years_experience: ['years of experience', 'total years of experience', 'total experience', 'professional experience in years'],
  current_company: ['current company', 'current employer', 'company'], current_title: ['current title', 'current job title'],
  notice_period: ['notice period', 'what is your notice period'], expected_salary: ['expected salary', 'salary expectation', 'salary expectations', 'expected ctc'],
  current_salary: ['current salary', 'current ctc'], preferred_locations: ['preferred locations', 'preferred location'],
  remote_preference: ['remote preference', 'preferred work arrangement'], education: ['education', 'educational qualifications'], degree: ['degree', 'highest degree', 'highest qualification'],
  skills: ['skills', 'technical skills'], sponsorship: ['sponsorship'], work_authorization: ['work authorization']
};

// Values come only from the configuration; a full name is never split into guessed legal names.
export function buildCandidateAnswers(config: Config): Record<string, CandidateAnswer> {
  const candidate = config.candidate as unknown as Record<string, unknown>;
  const answers: Record<string, CandidateAnswer> = {};
  for (const [key, labels] of Object.entries(aliases)) {
    const value = candidate[key];
    if (value === undefined || value === null || value === '' || (Array.isArray(value) && !value.length)) continue;
    const text = Array.isArray(value) ? value.join(', ') : String(value);
    for (const label of [key, ...labels]) answers[normalizeQuestion(label)] = { value: text, confidence: 'verified' };
  }
  if (!candidate.country && /,\s*India\s*$/i.test(String(candidate.location ?? ''))) answers.country = {value:'India',confidence:'verified'};
  if (config.candidate.phone_country_code) answers['fact:phone_dial_code'] = { value: config.candidate.phone_country_code, confidence: 'verified' };
  for (const [skill, years] of Object.entries(config.candidate.skill_years ?? {})) answers[`${SKILL_PREFIX}${normalizeQuestion(skill)}`] = { value: String(years), confidence: 'verified' };
  // Exact question answers are explicit user decisions, including country-specific authorization.
  const configured = (config.application as unknown as { answers?: Record<string, string> }).answers ?? {};
  for (const [question, value] of Object.entries(configured)) if (value.trim()) {
    const answer: CandidateAnswer = {value,confidence:'configured'};
    answers[normalizeQuestion(question)] = answer;
    const fact=canonicalFact(question);
    if(fact) answers[`fact:${fact}`]=answer;
  }
  return answers;
}

const SKILL_PREFIX = 'skill years: ';
const firstNumber = (text: string): number => Number(text.match(/\d+(?:\.\d+)?/)?.[0] ?? NaN);
const escapeRegExp = (text: string): string => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
// "years of X experience": words that still describe total experience rather than naming a skill.
const GENERAL_EXPERIENCE = /^(?:(?:work|working|professional|overall|total|relevant|industry|it|information technology|software|full[- ]time|paid|hands[- ]on)\s*)*$/;
const GENERAL_SUBJECT = /^(?:your )?(?:(?:chosen|current|own) )?(?:(?:engineering|professional|software|it) )?(?:field|industry|career)$|^(?:the )?(?:it|information technology|software)(?: industry)?$/;

/**
 * Employers word the same few questions many ways ("Notice Period in days", "What is your current notice period?", "Please enter your current
 * ctc in INR"). These map such wordings onto facts already in config/candidate.json, converting only units that are exact (days/months,
 * lakhs/rupees); anything else stays a question. Yes/no questions ("Do you have 5+ years…", "Are you OK with…") are never answered this way.
 */
export function inferAnswer(question: string, answers: Record<string, CandidateAnswer>): CandidateAnswer | undefined {
  const q = normalizeQuestion(question);
  if (/^(?:are|is|do|does|can|could|will|would|have|has|did|should)\b/.test(q)) return undefined;
  const value = (key: string): string | undefined => answers[key]?.value;
  const verified = (text: string): CandidateAnswer => ({ value: text, confidence: 'verified' });
  if (/\bnotice period\b/.test(q) && !/\b(?:buy ?out|negotiable|serving|last working)\b/.test(q)) {
    const notice = value('notice period');
    if (!notice) return undefined;
    const days = /\bday/.test(notice) ? firstNumber(notice) : /\bmonth/.test(notice) ? firstNumber(notice) * 30 : /\bweek/.test(notice) ? firstNumber(notice) * 7 : NaN;
    if (/\bdays?\b/.test(q)) return Number.isFinite(days) ? verified(String(days)) : undefined;
    if (/\bmonths?\b/.test(q)) return Number.isFinite(days) && days % 30 === 0 ? verified(String(days / 30)) : undefined;
    return verified(notice);
  }
  if (/\b(?:ctc|salary|compensation|package)\b/.test(q) && !/\b(?:monthly|per month|hourly|per hour|rate|variable|bonus|fixed|hike)\b/.test(q)) {
    const which = /\b(?:expected|desired|expectation|expecting)\b/.test(q) ? 'expected salary' : /\b(?:current|present|last drawn|existing)\b/.test(q) ? 'current salary' : undefined;
    const stated = which && value(which);
    if (!stated) return undefined;
    const lakhs = /\b(?:lpa|lakhs?|lacs?)\b/i.test(stated) ? firstNumber(stated) : NaN;
    if (/\b(?:lpa|lakhs?|lacs?)\b/.test(q)) return Number.isFinite(lakhs) ? verified(String(lakhs)) : undefined;
    if (/\b(?:inr|rupees|rs)\b/.test(q)) return Number.isFinite(lakhs) ? verified(String(Math.round(lakhs * 100000))) : undefined;
    return verified(stated);
  }
  if (/\blinkedin\b/.test(q) && /\b(?:url|profile|link)\b/.test(q)) return value('linkedin') ? verified(value('linkedin')!) : undefined;
  if (/\bpreferred (?:work(?:ing)? |job )?locations?\b/.test(q)) return value('preferred locations') ? verified(value('preferred locations')!) : undefined;
  if (/\byears?\b/.test(q) && !/\bmonths?\b/.test(q)) {
    // The subject after with/in/using ("…do you have with Selenium") or between "years of" and "experience" ("years of Selenium experience").
    const after = q.match(/\b(?:experience|do you have|have you worked|have you been working)\s+(?:(?:do|you|have|currently|got)\s+)*(?:with|in|using|on)\s+(.+)$/)?.[1]?.trim();
    const between = q.match(/\byears? of (.+?) experience\b/)?.[1]?.trim();
    const leading = q.match(/^(.+?) experience in years$/)?.[1] ?? q.match(/^years working with (.+)$/)?.[1];
    const subject = leading ?? (after && !GENERAL_SUBJECT.test(after) ? after : between && !GENERAL_EXPERIENCE.test(between) ? between : undefined);
    if (subject) {
      const skills = Object.keys(answers).filter((key) => key.startsWith(SKILL_PREFIX)).map((key) => key.slice(SKILL_PREFIX.length));
      const exact = skills.find((skill) => skill === subject);
      const contained = skills.filter((skill) => new RegExp(`(?:^|\\W)${escapeRegExp(skill)}(?:\\W|$)`).test(subject));
      const skill = exact ?? (contained.length === 1 ? contained[0] : undefined);
      return skill ? answers[`${SKILL_PREFIX}${skill}`] : undefined;
    }
    if (!subject && between && !GENERAL_EXPERIENCE.test(between)) return undefined;
    if (/\bexperience\b/.test(q) && /^(?:how many|what is your total|what is your overall|please (?:enter|select|mention|provide) your (?:total|overall)|total|overall|number of)\b/.test(q) && !/\b(?:as an?|designing|developing|managing|leading)\b/.test(q))
      return value('years experience') ? verified(value('years experience')!) : undefined;
  }
  return undefined;
}

export function classifyField(field: FormField, answers: Record<string, CandidateAnswer>): FormField {
  const question = normalizeQuestion(field.label);
  if (field.type === 'password' || /\b(captcha|verification code|one time (?:password|code)|otp|authenticator)\b/.test(question)) return { ...field, classification: 'SECURITY' };
  // A dropdown sharing its label with another ("From" month and "From" year) is answered by label plus its first option (see the manual pause).
  const qualified = field.options?.length ? answers[`${question} [${normalizeQuestion(field.options[0].label)}]`] : undefined;
  // Career sites often ask the plain profile fields as sentences ("What is your full name?", "Enter a phone number"; live 2026-09-30, Manatal).
  const plain = question.replace(/^(?:(?:what is|what's|whats) your|(?:please )?(?:enter|provide|share|type) (?:your|a|an)|your) /, '');
  const exact = qualified ?? answers[question] ?? answers[`fact:${canonicalFact(question)}`] ?? (plain !== question ? answers[plain] ?? answers[canonicalFact(plain) ?? ''] : undefined);
  const sensitive = /\b(gender|sex|race|ethnicity|ethnic|disability|disabled|veteran|religion|sexual|pronouns|criminal|convict|background check|consent|agree|privacy|certify|acknowledge|citizen|citizenship|sponsor|authorized|authorised|authorization|authorisation|visa|permit)\b/.test(question);
  if (sensitive && (!exact || exact.confidence !== 'configured')) return { ...field, classification: 'SENSITIVE' };
  // Never match a machine name in preference to a different human-readable question.
  const answer = exact ?? (question === 'unlabeled field' ? answers[normalizeQuestion(field.name ?? '')] : undefined)
    ?? (field.type === 'radio' || field.type === 'checkbox' || field.type === 'file' ? undefined : inferAnswer(question, answers));
  if (answer && ['verified', 'configured'].includes(answer.confidence)) {
    // The dialling code travels with a phone answer, for phone boxes that carry their own country picker (see fillFields).
    const dialCode = answers['fact:phone_dial_code']?.value;
    const phone = dialCode && field.type !== 'select' && [canonicalFact(question), canonicalFact(plain)].includes('phone');
    return { ...field, answer: answer.value, classification: 'KNOWN', ...(phone ? { dialCode } : {}) };
  }
  return { ...field, classification: 'MISSING' };
}

/** Conservative equivalence: legal declarations stay local; salary/notice units remain distinct. */
export function canonicalFact(question: string): string | undefined {
  const q = normalizeQuestion(question);
  if (/\b(?:authorized|authorised|visa|sponsor|citizen|agree|consent|certify|willing|relocat|shift|disability|gender|race|veteran)\w*\b/.test(q)) return undefined;
  for (const [key, labels] of Object.entries(aliases)) if ([key, ...labels].some(label => normalizeQuestion(label) === q)) return key;
  if (/^(?:do|are|have|can|would|will|is)\b/.test(q)) return undefined;
  if (/^(?:(?:how many|what is your|please enter your) )?(?:total|overall|professional|work)(?: years of| years)? experience$|^how many years of (?:professional|work|overall|total) experience(?: do you have)?$|^total years of work experience$/.test(q)) return 'years_experience';
  const skill = q.match(/^(?:how many )?years of (.+?) experience(?: do you (?:currently )?have)?$/)?.[1]
    ?? q.match(/^(.+?) experience in years$/)?.[1] ?? q.match(/^years working with (.+)$/)?.[1]
    ?? q.match(/^how many years of experience do you have with (.+)$/)?.[1];
  if (skill && !GENERAL_EXPERIENCE.test(skill) && /^[a-z][a-z0-9 +.#/-]{0,40}$/.test(skill) && !/\b(?:and|or|as|in|with)\b/.test(skill)) return `skill:${skill}`;
  if (/^(?:what is your |please enter your )?(?:current |expected )?(?:notice period)(?: in days)?$/.test(q)) return /in days$/.test(q) ? 'notice_period:days' : 'notice_period';
  const salary = q.match(/^(?:what is your |please enter your )?(current|expected|present|desired) (?:ctc|salary|compensation|package)(?: in (inr|rupees|lpa|lakhs))?$/);
  if (salary) return `${/current|present/.test(salary[1]) ? 'current' : 'expected'}_salary${salary[2] ? ':' + salary[2] : ''}`;
  return undefined;
}
