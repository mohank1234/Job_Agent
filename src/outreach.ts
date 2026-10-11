export function validateMessage(message: string, limit: number): void {
  const count = [...message].length;
  if (count > limit) throw new Error(`Message is ${count} characters; the UI limit is ${limit}.`);
  if (!message.trim()) throw new Error('Message cannot be empty.');
}

/** Who the note is written to: recruiters and HR, hiring/engineering/QA managers, or company leadership. */
export type Audience = 'HR' | 'MANAGER' | 'EXEC';
type MessageInput = { firstName: string; company: string; title: string; candidateName: string; specialization: string; submitted?: boolean; years?: number | null; audience?: Audience };
function clean(text: string): string { return text.replace(/\s+/g, ' ').trim(); }

export function audienceOf(contactTitle: string): Audience {
  // Chief People / Talent / HR officers lead the HR function; every other chief, founder or owner is company leadership.
  if (/\bchief\b[^,|]*\b(?:people|talent|human|hr)\b/i.test(contactTitle)) return 'HR';
  if (/\b(?:ceo|cto|coo|cfo|chief|founder|co-?founder|president|owner|managing director)\b/i.test(contactTitle)) return 'EXEC';
  if (/recruit|talent|human resources|\bhr\b|people (?:operations|partner|team)|staffing/i.test(contactTitle)) return 'HR';
  return 'MANAGER';
}

/** A connection note (at most `limit` characters) in the voice that suits who is reading it. Only facts passed in are stated. */
export function buildConnectionNote(input: MessageInput, limit: number): string {
  const name = clean(input.firstName); const company = clean(input.company); const title = clean(input.title); const sign = clean(input.candidateName);
  const greeting = name ? `Hi ${name},` : 'Hello,';
  const role = input.submitted ? `I've applied for the ${title} role at ${company}` : `I'm interested in the ${title} role at ${company}`;
  const specialization = clean(input.specialization);
  // Only facts from the candidate profile: the experience line appears only when the profile states the years.
  const who = specialization ? `I'm a QA engineer${input.years && input.years > 0 ? ` with ${input.years} years of experience` : ''} in ${specialization}.` : '';
  const byAudience: Record<Audience, string[]> = {
    HR: [
      `${greeting} ${who} ${role}, and I'd appreciate your guidance on the hiring process. Happy to share my profile. ${sign}`,
      `${greeting} ${role}. Could you point me to the right person or process? Thank you. ${sign}`
    ],
    MANAGER: [
      `${greeting} ${who} ${role} and would value a short chat about what your team is looking for. Happy to share my profile. ${sign}`,
      `${greeting} ${role}. I'd value a short chat about your team's needs. ${sign}`
    ],
    EXEC: [
      `${greeting} ${who} ${role}. I'd welcome the chance to connect and contribute to the quality of what ${company} builds. ${sign}`,
      `${greeting} ${role}. I'd welcome the chance to connect. ${sign}`
    ]
  };
  const candidates = [
    ...byAudience[input.audience ?? 'MANAGER'],
    `${greeting} ${role}. I'd appreciate connecting. ${sign}`,
    `${greeting} I'd appreciate connecting about QA opportunities. ${sign}`,
    `${greeting} I'd appreciate connecting.`
  ].map(clean);
  const note = candidates.find((message) => [...message].length <= limit);
  if (!note) throw new Error(`A natural connection note cannot fit the configured ${limit}-character limit.`);
  validateMessage(note, limit);
  return note;
}

export function buildInMail(input: MessageInput): string {
  const interest = input.submitted ? `I recently applied for the ${clean(input.title)} position at ${clean(input.company)}.` : `I'm interested in the ${clean(input.title)} position at ${clean(input.company)}.`;
  const background = input.specialization.trim() ? ` My QA background includes ${clean(input.specialization)}.` : '';
  return `Hi ${clean(input.firstName) || 'there'},\n\n${interest}${background} If you are involved in hiring for this role, I would appreciate the chance to discuss where I could contribute.\n\nRegards,\n${clean(input.candidateName)}`;
}

export type ExactNoteInput = { firstName: string; company: string; title: string; signature: string; skills: string[]; years?: number | null; audience?: Audience; submitted?: boolean };
const listSkills = (items: string[], and = 'and'): string => items.length <= 1 ? items.join('') : `${items.slice(0, -1).join(', ')} ${and} ${items[items.length - 1]}`;
// Titles such as "Senior QA Engineer (SDET) - Hyderabad | Remote" shrink to the part a person would say aloud.
const shortTitle = (title: string): string => clean(title.replace(/\([^)]*\)/g, ' ').split(/\s[-–|,]\s|\s[-–|]|[|]/)[0]);

/**
 * A connection note of exactly `length` characters: a short greeting, why the candidate fits THIS job (only skills passed in, which the caller took
 * from the job description), the ask, then one blank line and the signature. LinkedIn caps the note at 300, so the phrasing is chosen from
 * interchangeable wordings until the total is exactly `length`; nothing is padded with filler characters.
 */
export function buildExactConnectionNote(input: ExactNoteInput, length = 300): string {
  const tail = `\n\n${clean(input.signature)}`;
  const target = length - [...tail].length;
  const name = clean(input.firstName); const company = clean(input.company); const years = input.years && input.years > 0 ? input.years : 0;
  const greetings = name ? [`Hi ${name},`, `Hello ${name},`] : ['Hello,'];
  const titles = [...new Set([clean(input.title), shortTitle(input.title)].filter(Boolean))];
  const skillSets = (input.skills.length ? input.skills : ['']).map((_, index, all) => all.slice(0, all.length - index)).slice(0, 6);
  const numerals = years ? [String(years), ...(years <= 10 ? [['', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'][years]] : [])] : [''];
  const whos = (skills: string, y: string): string[] => skills ? (years
    ? [`I'm a QA Engineer with ${y} years of experience in ${skills}.`, `I have ${y} years of QA experience in ${skills}.`, `I'm a QA professional with ${y} years in ${skills}.`]
    : [`I'm a QA Engineer experienced in ${skills}.`, `My QA work covers ${skills}.`]) : [years ? `I'm a QA Engineer with ${years} years of experience.` : `I'm a QA Engineer.`];
  const roles = (title: string, noun: string): string[] => input.submitted
    ? [`I've applied for the ${title} ${noun} at ${company}.`, `I recently applied for the ${title} ${noun} at ${company}.`, `I've applied for the ${title} ${noun} at ${company} and am very keen on it.`, `I recently applied for the ${title} ${noun} at ${company} and am genuinely excited about it.`]
    : [`I'm very interested in the ${title} ${noun} at ${company}.`, `I'm interested in the ${title} ${noun} at ${company}.`, `I'm keen on the ${title} ${noun} at ${company}.`, `I'm genuinely excited about the ${title} ${noun} at ${company}.`];
  const asks: Record<Audience, string[]> = {
    HR: ['I believe my skills align well and would appreciate your guidance on the hiring process.', 'I believe my skills align well with it. Could you point me to the right person?', 'My skills align well, and I would appreciate your guidance on the process.', 'I would appreciate your guidance on the hiring process.'],
    MANAGER: ['I believe my skills align well and would value a short chat about what your team needs.', 'I believe my skills align well with what the team needs.', 'My skills align well, and I would value a short chat about your team.', 'I would value a short chat about your team.'],
    EXEC: ['I believe my skills align well and would welcome the chance to connect.', 'My skills align well, and I would welcome the chance to contribute to quality at ' + company + '.', 'I would welcome the chance to connect.']
  };
  const closings = ['', ' Thank you.', ' Thanks for your time.', ' Thank you for your time.', ' Happy to share my profile.', ' Happy to share my resume.', ' I would be glad to share my resume.',
    ' Happy to share my resume and profile.', ' Happy to share my resume if that helps. Thank you.', ' I would be glad to share my resume and any details you need. Thank you for your time.'];
  const nouns = ['role', 'position', 'opening'];
  // Each part is chosen independently, so the exact total is found by combining what lengths every part can reach (cheapest wording first).
  type Option = { text: string; cost: number };
  const greetingOptions: Option[] = greetings.map((text, cost) => ({ text, cost }));
  const whoOptions: Option[] = [];
  for (const [s, skills] of skillSets.entries()) for (const [j, and] of ['and', '&'].entries()) for (const [y, numeral] of numerals.entries()) for (const [w, text] of whos(listSkills(skills, and), numeral).entries()) whoOptions.push({ text, cost: s * 3 + j * 2 + y + w });
  const roleOptions: Option[] = [];
  for (const [t, title] of titles.entries()) for (const [n, noun] of nouns.entries()) for (const [r, text] of roles(title, noun).entries()) roleOptions.push({ text, cost: t * 4 + n * 2 + r });
  const askOptions: Option[] = asks[input.audience ?? 'MANAGER'].map((text, index) => ({ text, cost: index * 2 }));
  const closeOptions: Option[] = closings.map((text, cost) => ({ text, cost }));
  const joined = [greetingOptions, whoOptions, roleOptions, askOptions, closeOptions];
  type State = { cost: number; parts: string[] };
  let states = new Map<number, State>([[0, { cost: 0, parts: [] }]]);
  for (const options of joined) {
    const next = new Map<number, State>();
    for (const [used, state] of states) for (const option of options) {
      const size = used + [...option.text].length + (state.parts.length && option.text && !option.text.startsWith(' ') ? 1 : 0);
      if (size > target) continue;
      const cost = state.cost + option.cost; const known = next.get(size);
      if (!known || cost < known.cost) next.set(size, { cost, parts: [...state.parts, option.text] });
    }
    states = next;
  }
  const best = states.get(target);
  if (!best) throw new Error(`No natural ${length}-character connection note fits "${input.title}" at ${company}.`);
  const body = best.parts.reduce((text, part) => !text ? part : part.startsWith(' ') || !part ? text + part : `${text} ${part}`, '');
  if ([...body].length !== target) throw new Error(`Internal error: note is ${[...body].length} characters, expected ${target}.`);
  return body + tail;
}
