import { access } from 'node:fs/promises';
import { basename } from 'node:path';
import type { Frame, Locator, Page } from 'playwright';
import type { ApplicationContext, ApplicationInspection, FormField } from '../core/types.js';
import type { AtsAdapter } from './adapter.js';
import { classifyField, normalizeQuestion } from './answers.js';

export type FormProfile = {
  name: string;
  hosts: RegExp;
  formSelector: string;
  resumeSelector: string;
  submitSelector: string;
  successSelector?: string;
  applySelector?: string;
};

function normalized(value: string): string { return value.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim(); }
function meaningfulTokens(value: string): string[] { return normalized(value).split(' ').filter((word) => word.length > 2 && !['the', 'and', 'for', 'with', 'inc', 'ltd', 'limited', 'llc', 'private'].includes(word)); }

/** Match only a single option justified by a verified answer. */
export function matchingSelectOption(field: FormField): {label:string;value:string}|undefined {
  const answer=field.answer?.trim();
  if(!answer)return undefined;
  const options=field.options??[];
  const exact=options.filter(item=>normalizeQuestion(item.label)===normalizeQuestion(answer)||normalizeQuestion(item.value)===normalizeQuestion(answer));
  if(exact.length===1)return exact[0];
  if(exact.length>1)return undefined;
  if(/\b(?:years?|experience)\b/i.test(field.label) && /^\d+(?:\.\d+)?$/.test(answer)) {
    const years=Number(answer);
    const ranges=options.filter(item=>{
      const label=item.label.trim();
      const between=label.match(/^(\d+(?:\.\d+)?)\s*(?:,|-|to)\s*(\d+(?:\.\d+)?)$/i);
      const more=label.match(/^(\d+(?:\.\d+)?)\s*\+$/);
      const less=label.match(/^less than\s+(\d+(?:\.\d+)?)$/i);
      return between ? years>=Number(between[1])&&years<=Number(between[2]) : more ? years>=Number(more[1]) : less ? years<Number(less[1]) : false;
    });
    if(ranges.length===1)return ranges[0];
  }
  if(/\b(?:preferred|current) (?:working )?location\b|\blocation \(city\)/i.test(field.label)) {
    const parts=answer.split(',').map(part=>normalizeQuestion(part)).filter(Boolean);
    const candidates=options.filter(item=>parts.some(part=>normalizeQuestion(item.label)===part));
    if(candidates.length===1)return candidates[0];
  }
  return undefined;
}

export class HostedFormAdapter implements AtsAdapter {
  readonly name: string;
  private formFrame?: Frame;
  constructor(protected readonly profile: FormProfile) { this.name = profile.name; }
  matches(url: string): boolean { try { return this.profile.hosts.test(new URL(url).hostname); } catch { return false; } }
  protected scope(page: Page): Page | Frame { if(this.formFrame && !this.formFrame.isDetached())return this.formFrame; if(this.name==='generic')return page; return page.frames().find((frame) => frame !== page.mainFrame() && this.matches(frame.url()) && /^https?:/.test(frame.url())) ?? page; }

  async prepare(page: Page, _context: ApplicationContext): Promise<void> {
    const interactive = async (form: Locator): Promise<void> => {
      await form.locator('input:not([type="hidden"]), select, textarea, button').first().waitFor({state:'attached',timeout:10000});
    };
    for (let hop = 0; hop < 3; hop++) {
      for (const frame of page.frames()) if(frame!==page.mainFrame() && /^https?:/.test(frame.url()) && this.matches(frame.url()) && await frame.locator(this.profile.formSelector).count()) { this.formFrame=frame;break; }
      const scope = this.scope(page);
      const form = scope.locator(this.profile.formSelector).first();
      if (await form.isVisible().catch(() => false)) {await interactive(form);return;}
      const apply = this.profile.applySelector ? scope.locator(this.profile.applySelector).first()
        : scope.getByRole('button', {name:/^(?:apply|apply now|apply for (?:this job|this role)|start application|apply manually|continue application)$/i})
          .or(scope.getByRole('link', {name:/^(?:apply|apply now|apply for (?:this job|this role)|start application|apply manually|continue application)$/i})).first();
      await form.or(apply).first().waitFor({state:'visible',timeout:8000}).catch(()=>undefined);
      if (await form.isVisible().catch(()=>false)) {await interactive(form);return;}
      if (!await apply.isVisible().catch(()=>false)) return;
      // Keep ordinary navigation on this application tab so state/evidence follow the destination.
      await apply.evaluate(el => { if (el.tagName === 'A') el.removeAttribute('target'); });
      await apply.click({timeout:8000});
      await page.waitForLoadState('domcontentloaded').catch(()=>undefined);
    }
  }

  protected form(page: Page): Locator { return this.scope(page).locator(this.profile.formSelector).first(); }

  protected async resumeControl(page: Page): Promise<Locator> {
    const form = this.form(page);
    const configured = form.locator(this.profile.resumeSelector);
    if (await configured.count() === 1) return configured;
    // Custom ATS fields often use UUID names. Only a semantic resume label can identify them safely.
    await form.locator('input[type="file"]').evaluateAll((elements) => {
      for (const element of elements) {
        const input = element as HTMLInputElement;
        let label = [input.getAttribute('aria-label'), ...Array.from(input.labels ?? []).map((item) => item.textContent ?? '')].join(' ').trim();
        // A hidden input inside an attachment widget titled "Resume" (Manatal, live 2026-09-30) has no label of its own; the widget's short
        // text names it, as long as that widget holds no other file input.
        for (let node = input.parentElement, depth = 0; !label && node && depth < 3 && node.querySelectorAll('input[type="file"]').length === 1; node = node.parentElement, depth++) {
          const text = (node as HTMLElement).innerText?.replace(/\s+/g, ' ').trim() ?? '';
          if (text && text.length <= 200) label = text;
        }
        const isResume = /\b(?:resume|résumé|curriculum vitae|cv)\b/i.test(label) && !/cover letter/i.test(label);
        if (isResume) input.setAttribute('data-job-link-resume', 'true');
        else input.removeAttribute('data-job-link-resume');
      }
    });
    const labeled = form.locator('input[type="file"][data-job-link-resume="true"]');
    // Ambiguous controls intentionally yield no supported upload target.
    return await labeled.count() === 1 ? labeled : form.locator('input[data-job-link-no-resume="true"]');
  }

  async inspect(page: Page, context: ApplicationContext): Promise<ApplicationInspection> {
    const fields = (await this.readFields(page)).map((field) => classifyField(field, context.candidateAnswers));
    return { fields, finalUrl: page.url() };
  }

  /** Fills every field the candidate profile answers with confidence; anything else is left for the caller to report. */
  protected async fillFields(page: Page, inspection: ApplicationInspection): Promise<{ errors: string[]; fieldsFilled: number }> {
    const form = this.form(page);
    const errors: string[] = [];
    let fieldsFilled = 0;
    for (const field of inspection.fields) {
      if (field.classification !== 'KNOWN' || field.answer === undefined || field.type === 'file') continue;
      const control = form.locator(`[data-job-link-field="${field.key}"]`);
      try {
        if (field.type === 'radio') {
          const matches=field.options?.flatMap((item,index)=>normalizeQuestion(item.label)===normalizeQuestion(field.answer!)||normalizeQuestion(item.value)===normalizeQuestion(field.answer!)?[index]:[])??[];
          if(matches.length!==1) throw new Error('Configured answer does not match a unique available choice');
          await control.locator('input[type="radio"]').nth(matches[0]).check({ timeout: 5000 });
        } else if (field.type === 'checkbox') {
          if (!/^(yes|no|true|false)$/i.test(field.answer)) throw new Error('Checkbox requires an explicit yes/no answer');
          await control.setChecked(/^(yes|true)$/i.test(field.answer), { timeout: 5000 });
        } else if (field.type === 'select') {
          const option = matchingSelectOption(field);
          if (!option) throw new Error('Configured answer does not match an available choice');
          await control.selectOption(option.value, { timeout: 5000 });
        } else if (field.type === 'combobox' || field.type === 'custom-select') {
          // A location typeahead rarely offers the configured text verbatim ("Hyderabad, India" vs "Hyderabad, Telangana, India"; seen live on
          // Greenhouse and LinkedIn), and typing all of it can return nothing. So only the first part is typed, and an option is taken when it
          // is the answer itself or the single option containing every comma-separated part of it. Otherwise it stays a question.
          const parts = field.answer.split(',').map((part) => normalizeQuestion(part)).filter(Boolean);
          if (field.type === 'combobox') { await control.fill('', { timeout: 5000 }); await control.pressSequentially(field.answer.split(',')[0].trim(), { delay: 40, timeout: 5000 }); }
          else await control.click({ timeout: 5000 });
          // Explicit role="option" only: a native <select>'s own <option>s elsewhere on the page (a phone country code list) are not choices here.
          const options = this.scope(page).locator('[role="option"]:visible');
          const deadline = Date.now() + 6000;
          let names: string[] = []; let pick = -1;
          while (pick < 0 && Date.now() < deadline) {
            names = (await options.allInnerTexts().catch(() => [] as string[])).map((text) => text.replace(/\s+/g, ' ').trim());
            // LinkedIn can list the very same wording several times (live 2026-09-29: "Hyderabad, Telangana, India" seven times). Identical
            // text is one choice to anyone reading it, so only the first of each wording counts; two different places that match stay ambiguous.
            const first = (index: number): boolean => names.findIndex((name) => normalizeQuestion(name) === normalizeQuestion(names[index])) === index;
            const exact = names.flatMap((name, index) => normalizeQuestion(name) === normalizeQuestion(field.answer!) && first(index) ? [index] : []);
            const containing = names.flatMap((name, index) => first(index) && parts.every((part) => ` ${normalizeQuestion(name).replace(/[^a-z0-9]+/g, ' ')} `.includes(` ${part.replace(/[^a-z0-9]+/g, ' ').trim()} `)) ? [index] : []);
            pick = exact.length === 1 ? exact[0] : containing.length === 1 ? containing[0] : -1;
            if (pick < 0) await page.waitForTimeout(300);
          }
          if (pick < 0) {
            // Leave no half-typed search text behind for the person taking over: it can look like an answer but is not a selection.
            if (field.type === 'combobox') await control.fill('', { timeout: 2000 }).catch(() => undefined);
            throw new Error(`Autocomplete requires an unambiguous matching option for "${field.answer}"${names.length ? ` (offered: ${names.slice(0, 5).join(' | ')})` : ' (no options appeared)'}`);
          }
          await options.nth(pick).click({ timeout: 5000 });
        } else if (field.type?.startsWith('unsupported')) {
          throw new Error('This custom control requires manual completion');
        } else if (field.dialCode && !field.answer.trim().startsWith('+') && await control.evaluate((element) => !!element.closest('.iti, .PhoneInput, .react-tel-input, .intl-tel-input, [class*="phone-input" i]')).catch(() => false)) {
          // A phone box with its own country picker formats the digits for whatever country it defaults to (live 2026-09-30: Manatal showed
          // a 10-digit Indian number as the US "(889) 740-4807"). Typed with its dialling code, the picker switches to that country.
          const dial = field.dialCode.replace(/\D/g, '');
          let digits = field.answer.replace(/\D/g, '');
          if (digits.startsWith(dial) && digits.length > 10) digits = digits.slice(dial.length);
          await control.fill('', { timeout: 5000 });
          await control.pressSequentially(`+${dial}${digits}`, { delay: 20, timeout: 10000 });
        } else {
          await control.fill(field.answer, { timeout: 5000 });
        }
        fieldsFilled++;
      } catch (error) { errors.push(`${field.label}: ${error instanceof Error ? error.message : String(error)}`); }
    }
    return { errors, fieldsFilled };
  }

  async fillKnownFields(page: Page, context: ApplicationContext): Promise<ApplicationInspection> {
    const inspection = await this.inspect(page, context);
    const { errors, fieldsFilled } = await this.fillFields(page, inspection);
    let resumeUploaded = inspection.fields.some(field=>field.type==='file' && field.value===basename(context.resumePath)) || await this.form(page).getByText(basename(context.resumePath), {exact:true}).first().isVisible().catch(()=>false);
    const resume = await this.resumeControl(page);
    if (!resumeUploaded && await resume.count()) {
      try {
        await access(context.resumePath);
        const accept = (await resume.getAttribute('accept') ?? '').toLowerCase();
        const extension = `.${context.resumePath.split('.').pop()?.toLowerCase()}`;
        if (accept && !accept.split(',').some((item) => item.trim() === extension || (extension === '.pdf' && item.trim() === 'application/pdf') || item.trim() === '*/*')) throw new Error(`Resume format ${extension} is not accepted by the upload control (${accept})`);
        await resume.setInputFiles(context.resumePath, { timeout: 15000 });
        const expected = basename(context.resumePath);
        // Some ATSs (Greenhouse's current job boards, seen live 2026-09-29) remove the file input the moment a file is chosen and show a
        // "<filename> ×" chip instead. Waiting on the vanished input only times out, so its absence falls through to the chip (as validate() does).
        const actual = await resume.count() ? await resume.evaluate((element) => Array.from((element as HTMLInputElement).files ?? []).map((file) => file.name), undefined, { timeout: 2000 }).catch(() => [] as string[]) : [];
        if (!actual.includes(expected)) {
          const shown = await this.form(page).getByText(expected, { exact: true }).first().waitFor({ state: 'visible', timeout: 10000 }).then(() => true, () => false);
          if (!shown) throw new Error('Upload control did not retain the selected resume filename');
        }
        resumeUploaded = true;
      } catch (error) { errors.push(`Resume upload failed: ${error instanceof Error ? error.message : String(error)}`); }
    } else if (!resumeUploaded && inspection.fields.length) errors.push('Resume upload failed: no supported resume upload control was found');
    return { ...await this.inspect(page, context), fieldsFilled, resumeUploaded, errors };
  }

  async validate(page: Page, context: ApplicationContext): Promise<ApplicationInspection> {
    const inspection = await this.inspect(page, context);
    const errors: string[] = [];
    if (!inspection.fields.length) errors.push('No supported application form found');
    const identityVerified = await this.verifyIdentity(page, context);
    if (!identityVerified) errors.push('Application identity could not be verified against the expected role, company and job URL');
    for (const field of inspection.fields) {
      if (field.required && !field.value?.trim()) errors.push(`Required question has no answer: ${field.label}`);
      else if (field.valid === false && field.value) errors.push(`${field.label}: ${field.validationMessage || 'Browser validation rejected the value'}`);
      if (field.classification === 'KNOWN' && field.answer !== undefined && field.value) {
        const expected = normalizeQuestion(field.answer);
        const selected = field.options?.find((option) => option.value === field.value);
        const actual = normalizeQuestion(selected?.label ?? field.value);
        const phoneMatch = /\b(?:mobile|cell|contact|phone|telephone)\b/i.test(field.label) &&
          field.type !== 'select' && field.type !== 'combobox' && field.type !== 'custom-select' &&
          field.answer.replace(/\D/g,'').length >= 7 && actual.replace(/\D/g,'').endsWith(field.answer.replace(/\D/g,''));
        const booleanMatch = field.type === 'checkbox' && /^(yes|true)$/i.test(field.answer) && field.value === 'true';
        // A typeahead holds the site's fuller wording of the chosen option ("Hyderabad, Telangana, India" for "Hyderabad, India"; see fillFields).
        const words = (text: string): string => ` ${normalizeQuestion(text).replace(/[^a-z0-9]+/g, ' ').trim()} `;
        const typeaheadMatch = (field.type === 'combobox' || field.type === 'custom-select') && field.answer.split(',').every((part) => words(actual).includes(words(part)));
        if (!phoneMatch && !booleanMatch && !typeaheadMatch && actual !== expected && normalizeQuestion(field.value) !== expected) errors.push(`${field.label}: Current field value does not match the configured answer`);
      }
    }
    const file = await this.resumeControl(page);
    let resumeUploaded = false;
    if (await file.count()) {
      resumeUploaded = await file.evaluate((element, expected) => Array.from((element as HTMLInputElement).files ?? []).some((item) => item.name === expected), basename(context.resumePath));
    }
    // Some ATSs replace the upload input after upload. Accept a visible filename in its resume section.
    if (!resumeUploaded) {
      const filename = this.form(page).getByText(basename(context.resumePath), { exact: true });
      resumeUploaded = await filename.first().isVisible().catch(() => false);
    }
    if (!resumeUploaded) errors.push('Selected resume is not verifiably attached');
    return { ...inspection, identityVerified, resumeUploaded, errors };
  }

  async verifyIdentity(page: Page, context: ApplicationContext): Promise<boolean> {
    const scope = this.name === 'generic' && this.formFrame ? page : this.scope(page);
    const expected = new URL(context.job.applicationUrl ?? context.job.jobUrl);
    const current = new URL(scope.url());
    // Require the expected ATS job id or the complete original path to survive redirects.
    const id = context.job.externalId ?? context.job.atsJobId?.split(':').pop();
    const idMatches = id ? current.pathname.split('/').some((part) => decodeURIComponent(part) === id || decodeURIComponent(part).endsWith(`_${id}`)) || [...current.searchParams.values()].includes(id) : current.pathname.replace(/\/(apply|application)\/?$/, '') === expected.pathname.replace(/\/(apply|application)\/?$/, '');
    const sameAts = this.name !== 'generic' && this.matches(expected.href) && this.matches(current.href);
    if (!idMatches || (current.hostname !== expected.hostname && !sameAts)) return false;
    const headingText = await scope.locator('h1, h2, [data-automation-id="jobPostingHeader"], .posting-headline').allTextContents();
    const heading = normalized(headingText.join(' '));
    const title = meaningfulTokens(context.job.title);
    // Many career sites title the role in an h3/h4 under an h1 of the company, or only in the page title (live 2026-09-30: Recooty,
    // Manatal and Infor application forms, all for the right role, were refused). The role must still be whole in one place.
    // One read, not locator waits: most pages have no og:title, and waiting for it cost the whole default timeout on every job.
    const titles = await scope.evaluate(() => [document.title, document.querySelector('meta[property="og:title"]')?.getAttribute('content') ?? '']).catch(() => [] as string[]);
    const places = [heading, ...(await scope.locator('h3, h4').allTextContents()).map(normalized), ...titles.map(normalized)];
    if (!title.length || !places.some((place) => title.every((token) => place.includes(token)))) return false;
    const body = normalized((await scope.locator('body').innerText()).slice(0, 60000));
    const company = meaningfulTokens(context.job.company);
    const route = normalized(decodeURIComponent(current.href));
    if (company.length > 0 && company.every((token) => body.includes(token) || route.replace(/ /g, '').includes(token))) return true;
    // A brand posting under its parent's careers site ("Welo Data" on Welocalize's) names only the parent. That is accepted only on the very
    // host the posting linked to, with the role matched above, and the company's first word opening a word on the page or in the address.
    const brand = company[0];
    return current.hostname === expected.hostname && !!brand && brand.length >= 4 && new RegExp(`(?:^| )${brand}`).test(`${body} ${normalized(current.hostname)}`);
  }

  async readyToSubmit(page: Page): Promise<boolean> {
    const control=this.form(page).locator(this.profile.submitSelector);
    return await control.count()===1 && await control.isVisible() && await control.isEnabled();
  }

  async submit(page: Page, context: ApplicationContext): Promise<ApplicationInspection> {
    const validation = await this.validate(page, context);
    if (validation.errors?.length) return { ...validation, blockedReason: validation.errors.join('; ') };
    const submit = this.form(page).locator(this.profile.submitSelector);
    if (await submit.count() !== 1 || !await submit.isVisible() || !await submit.isEnabled()) return { ...validation, blockedReason: 'No unique enabled final submit button found' };
    await submit.click({ timeout: 15000 });
    await this.scope(page).locator(`${this.profile.successSelector ?? '[role="status"]'}, [role="status"]`).filter({ hasText: /application (?:has been |was |is )?(?:successfully )?(?:submitted|received)|thank you for applying/i }).first().waitFor({ state: 'visible', timeout: 10000 }).catch(() => undefined);
    return this.captureResult(page, context);
  }

  async captureResult(page: Page, _context: ApplicationContext): Promise<ApplicationInspection> {
    const scope = this.scope(page);
    const text = await scope.locator('body').innerText().catch(() => '');
    const confirmation = text.match(/(?:your application (?:has been |was |is )?(?:successfully )?(?:submitted|received)|thank you for (?:applying|your application)|we(?:'ve| have) received your application)[^\n.!]*(?:[.!]|$)/i)?.[0];
    // A confirmation phrase on a still-visible populated form is not sufficient evidence.
    const stillHasForm = await this.form(page).isVisible().catch(() => false);
    return { fields: [], confirmationText: confirmation && !stillHasForm ? confirmation : undefined, finalUrl: page.url(), blockedReason: confirmation && !stillHasForm ? undefined : 'Submission outcome is unconfirmed; inspect evidence before any retry' };
  }

  protected async readFields(page: Page): Promise<FormField[]> {
    const form = this.form(page);
    if (!await form.count()) return [];
    return form.evaluate((root) => {
      // Object methods (not `const x = () => ...`) avoid tsx/esbuild's keepNames helpers inside browser-serialized callbacks.
      // Several of these are passed as bare callbacks (e.g. `.map(dom.labelText)`), which detaches them from `dom`; they
      // must not use `this` for that reason, but a plain reference to `dom` from within its own methods (closure, not
      // `this`) still works, since by the time a method actually runs, `dom` has already finished being assigned.
      const dom = {
        visible(element: Element): boolean {
          const style = getComputedStyle(element);
          return style.display !== 'none' && style.visibility !== 'hidden' && element.getClientRects().length > 0;
        },
        // A visible label often sits beside a duplicate screen-reader-only copy with no separator ("Email addressEmail address");
        // an exact first-half/second-half match is almost certainly that duplication, never a genuinely repeated real label.
        dedupe(value: string): string {
          const half = value.length / 2;
          return Number.isInteger(half) && half > 0 && value.slice(0, half) === value.slice(half) ? value.slice(0, half) : value;
        },
        text(element: Element | null): string { return dom.dedupe(element?.textContent?.replace(/\s+/g, ' ').trim() ?? ''); },
        labelText(element: Element): string {
          const clone = element.cloneNode(true) as Element;
          clone.querySelectorAll('input, select, textarea, button').forEach((control) => control.remove());
          return dom.dedupe(clone.textContent?.replace(/\s+/g, ' ').trim() ?? '');
        }
      };
      const controls = Array.from(root.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>('input, textarea, select'));
      const fields: FormField[] = [];
      const radioGroups = new Set<string>();
      let serial = 0;
      for (const control of controls) {
        if (control.disabled || ('readOnly' in control && control.readOnly) || ['hidden', 'submit', 'button', 'reset'].includes(control.type) || (control.type !== 'file' && control.getAttribute('aria-hidden') === 'true')) continue;
        if (control.type !== 'file' && !dom.visible(control)) continue;
        const labelled = control.getAttribute('aria-labelledby')?.split(/\s+/).map((id) => dom.text(document.getElementById(id))).join(' ');
        const labels = Array.from(control.labels ?? []).map(dom.labelText).join(' ');
        const container = control.closest('fieldset, [role="group"], .application-question, .field, [data-automation-id="formField"]') ?? control.parentElement;
        const legend = dom.text(container?.querySelector('legend, .application-label, [data-automation-id="formLabel"]') ?? null);
        // Some forms title a field with a plain text element just before it instead of a <label> (LinkedIn's "Location (city)*" typeahead:
        // <p>Location (city)*</p> … <input placeholder="Enter city or location">). With no real label, the nearest such text before the
        // control, within a few levels, names it (and its "*" marks it required); only then do the placeholder or the name stand in.
        let heading = '';
        if (!(labelled || labels || control.getAttribute('aria-label') || legend)) {
          for (let node = control.parentElement, depth = 0; node && depth < 4 && !heading; node = node.parentElement, depth++) {
            for (const child of Array.from(node.children)) {
              if (child.contains(control)) break;
              // A wrapper div holding only the title text counts too (Manatal: <div><label><p>What is your phone number?</p></label></div>).
              const titleOnly = child.tagName === 'DIV' && !!child.querySelector('label, p') && !child.querySelector('input, select, textarea, button');
              if (['P', 'SPAN', 'LABEL', 'H3', 'H4', 'LEGEND'].includes(child.tagName) || titleOnly) { const text = dom.text(child); if (text && text.length <= 150) heading = text; }
            }
          }
        }
        let label = labelled || labels || control.getAttribute('aria-label') || legend || heading || control.getAttribute('placeholder') || control.name || 'unlabeled field';
        let required = control.required || control.getAttribute('aria-required') === 'true' || /\*|\(required\)/i.test(label);
        let type = control.tagName === 'SELECT' ? 'select' : control.type;
        const key = String(serial++);
        let options: Array<{ label: string; value: string }> | undefined;
        let value = control.value;
        if (control.type === 'radio') {
          const name = control.name;
          if (radioGroups.has(name)) continue;
          radioGroups.add(name);
          const radios = controls.filter((item) => item.type === 'radio' && item.name === name) as HTMLInputElement[];
          const group = control.closest('fieldset, [role="radiogroup"], .application-question') ?? container;
          if (!group) continue;
          group.setAttribute('data-job-link-field', key);
          label = dom.text(group.querySelector('legend, .application-label')) || group.getAttribute('aria-label') || label;
          const preceding = dom.text(group.previousElementSibling);
          required = required || radios.some((radio) => radio.required) || group.getAttribute('aria-required') === 'true' || /\*|\(required\)/i.test(label)
            || (/\*/.test(preceding) && preceding.replace(/\*/g,'').replace(/\s+/g,' ').trim().toLowerCase() === label.replace(/\*/g,'').replace(/\s+/g,' ').trim().toLowerCase());
          options = radios.map((item) => {
            const explicit=Array.from(item.labels ?? []).map(dom.labelText).join(' ').trim();
            let visible=explicit;
            for(let node=item.parentElement,depth=0;node && depth<6 && !visible;node=node.parentElement,depth++){
              if(node.querySelectorAll('input[type="radio"]').length!==1)break;
              const text=dom.text(node);
              if(text && text.length<=100 && text.toLowerCase()!==label.replace(/\*/g,'').trim().toLowerCase())visible=text;
            }
            return {label:visible||item.value,value:item.value};
          });
          const selected=radios.findIndex(item=>item.checked);
          value=selected<0?'':radios.every(item=>item.value==='on')?options[selected].label:radios[selected].value;
        } else {
          control.setAttribute('data-job-link-field', key);
          if (control.tagName === 'SELECT') options = Array.from((control as HTMLSelectElement).options).map((option) => ({ label: option.text, value: option.value }));
          if (control.type === 'checkbox') value = (control as HTMLInputElement).checked ? 'true' : '';
          if (control.type === 'file') value = Array.from((control as HTMLInputElement).files ?? []).map((file) => file.name).join(', ');
          if (control.getAttribute('role') === 'combobox' || control.getAttribute('aria-autocomplete') === 'list') {
            type = 'combobox';
            const selection = control.closest('[class*="control" i]')?.querySelector('[class*="singleValue"], [class*="single-value"]');
            if (selection) {
              value = dom.text(selection);
              const flag=Array.from(selection.querySelectorAll<HTMLElement>('[class*="iti__"]')).flatMap(node=>Array.from(node.classList)).find(name=>/^iti__[a-z]{2}$/.test(name));
              if(flag && /^\+\d+$/.test(value))value=new Intl.DisplayNames(['en'],{type:'region'}).of(flag.slice(-2).toUpperCase()) ?? value;
            }
          }
        }
        // One dedupe covers every path to `label` above: the raw aria-label attribute (written by the site itself, so it can carry
        // this same duplication without ever passing through dom.text/labelText), and the radio-group override a few lines up.
        fields.push({ label: dom.dedupe(label.trim()), key, name: control.name || undefined, required, type, value, options, valid: control.validity.valid, validationMessage: control.validationMessage });
      }
      for (const custom of Array.from(root.querySelectorAll<HTMLElement>('[role="combobox"]:not(input):not(select), [role="checkbox"]:not(input), [role="radiogroup"], [contenteditable="true"]'))) {
        if (!dom.visible(custom) || custom.querySelector('input, select, textarea') || custom.getAttribute('aria-disabled') === 'true') continue;
        const key = String(serial++);
        custom.setAttribute('data-job-link-field', key);
        // A styled checkbox is usually named only by the text beside it ("I agree to the terms and conditions & privacy policy", Manatal).
        const beside = custom.parentElement && custom.parentElement.querySelectorAll('input:not([aria-hidden="true"]), select, textarea, [role="checkbox"], [role="combobox"]').length === 1 ? dom.text(custom.parentElement) : '';
        const label = dom.dedupe((custom.getAttribute('aria-label') || custom.getAttribute('aria-labelledby')?.split(/\s+/).map((id) => dom.text(document.getElementById(id))).join(' ') || dom.text(custom.closest('fieldset')?.querySelector('legend') ?? null)
          || dom.text(custom.closest('label')) || (beside.length <= 150 ? beside : '') || 'unlabeled custom control').trim());
        const type = custom.getAttribute('role') === 'combobox' ? 'custom-select' : 'unsupported-custom';
        const value = custom.getAttribute('aria-valuetext') || (custom.getAttribute('aria-checked') === 'true' ? 'true' : type === 'custom-select' ? dom.text(custom) : '');
        fields.push({ label, key, type, required: custom.getAttribute('aria-required') === 'true' || /\*|\(required\)/i.test(label), value });
      }
      return fields;
    });
  }
}
