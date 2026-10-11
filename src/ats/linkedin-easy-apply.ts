import { basename } from 'node:path';
import type { Locator, Page } from 'playwright';
import type { ApplicationContext, ApplicationInspection, FormField } from '../core/types.js';
import { HostedFormAdapter } from './form.js';
import { normalizeQuestion } from './answers.js';

// LinkedIn has shipped two layouts of this modal: the older `<div role="dialog">` (stable aria-labels on its buttons) and, seen live
// from 2026-09-29, a native `<dialog>` with hashed class names, a `#dialog-header` title, "N/M pages" progress and buttons named only
// by their text ("Next"). Everything below locates things by what a person sees (role + accessible name), which both layouts share.
const DIALOG = 'dialog:visible, div[role="dialog"]:visible';
const HEADER = '#jobs-apply-header, #dialog-header h2, dialog h2, [role="dialog"] h2';
const NEXT = /^(?:next|review|continue|continue to next step|review your application)$/i;
const SUBMIT = /^submit(?: application)?$/i;
const normalized = (value: string): string => value.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
const tokens = (value: string): string[] => normalized(value).split(' ').filter((word) => word.length > 2 && !['the', 'and', 'for', 'with', 'inc', 'ltd', 'limited', 'llc', 'private'].includes(word));
const digits = (value: string): string => value.replace(/\D/g, '');
const compact = (value: string): string => normalized(value).replace(/ /g, '');
const MAX_STEPS = 12;

/**
 * LinkedIn's own "Easy Apply" dialog. It is a short multi-step form (contact info, résumé, extra questions, review) inside one modal.
 * Everything filled here comes from the candidate profile or an answer the user saved; anything else stops the walk and becomes a question.
 * The final "Submit application" click happens only through submit(), which the caller reaches only after the user's explicit confirmation.
 */
export class LinkedInEasyApplyAdapter extends HostedFormAdapter {
  private resumeVerified = false;
  private walked = false;
  private readonly optionalBlank = new Set<string>();
  constructor() { super({ name: 'linkedin-easy-apply', hosts: /(^|\.)linkedin\.com$/i, formSelector: DIALOG, resumeSelector: 'input[type="file"]', submitSelector: 'button[aria-label="Submit application"]' }); }

  matches(url: string): boolean {
    try { const parsed = new URL(url); return /(^|\.)linkedin\.com$/i.test(parsed.hostname) && /^\/jobs\/view\//.test(parsed.pathname); } catch { return false; }
  }

  private dialog(page: Page): Locator { return page.locator(DIALOG).first(); }
  private submitButton(page: Page): Locator { return this.dialog(page).getByRole('button', { name: SUBMIT }); }
  private nextButton(page: Page): Locator { return this.dialog(page).getByRole('button', { name: NEXT }).first(); }
  // The job's own control is named "Easy Apply…" ("Easy Apply to this job"). Anchored, because the "More jobs" cards below are links named
  // by their whole text, badge included ("QA Engineer (Remote) … Easy Apply"): unanchored, a card for another job was clicked (live 2026-09-30).
  private easyApplyControl(page: Page): Locator { return page.getByRole('button', { name: /^easy apply\b/i }).or(page.getByRole('link', { name: /^easy apply\b/i })).first(); }

  async prepare(page: Page, _context: ApplicationContext): Promise<void> {
    if (await this.dialog(page).isVisible().catch(() => false)) { await this.waitForStep(page); return; }
    if (await this.alreadyApplied(page)) return; // nothing to open; the caller reads the applied state
    const control = this.easyApplyControl(page);
    await control.waitFor({ state: 'visible', timeout: 12000 }).catch(() => undefined);
    if (!await control.isVisible().catch(() => false)) return;
    await control.click({ timeout: 8000 });
    await this.dialog(page).waitFor({ state: 'visible', timeout: 15000 }).catch(() => undefined);
    await page.locator(HEADER).first().waitFor({ state: 'visible', timeout: 8000 }).catch(() => undefined);
    await this.waitForStep(page);
  }

  /** The native-<dialog> layout shows its title at once and each page's form a moment later, with a spinner (data-testid="loader") in
   *  between. Seen live on 2026-09-29: reading the dialog as soon as the title appeared found no fields at all, so the job was wrongly
   *  sent to "apply manually". Wait until the dialog holds real controls and no spinner (old layout: already true on arrival). */
  private async waitForStep(page: Page): Promise<void> {
    await page.waitForFunction(() => Array.from(document.querySelectorAll('dialog[open], div[role="dialog"]')).some((dialog) =>
      dialog.getClientRects().length > 0 && !Array.from(dialog.querySelectorAll('[data-testid="loader"], [aria-busy="true"]')).some(el => el.getClientRects().length > 0) && Array.from(dialog.querySelectorAll('input:not([type="hidden"]), select, textarea, button')).some(el => !(el as HTMLInputElement).disabled && el.getClientRects().length > 0 && (el.tagName !== 'BUTTON' || /^(?:next|continue|review|submit|upload resume)/i.test(el.textContent?.trim() ?? '')))), undefined, { timeout: 20000 });
  }

  async alreadyApplied(page: Page): Promise<boolean> {
    if (await this.easyApplyControl(page).isVisible().catch(() => false)) return false;
    const text = await page.locator('body').innerText().catch(() => '');
    return /(?:^|\n)\s*Applied\s+(?:\d+\s+\w+\s+ago|on\s)|See application status/i.test(text);
  }

  /** LinkedIn fills contact details from the member's own profile; those are accepted when they agree with the candidate profile. */
  async inspect(page: Page, context: ApplicationContext): Promise<ApplicationInspection> {
    const base = await super.inspect(page, context);
    const candidatePhone = digits(context.candidateAnswers['phone']?.value ?? '');
    const fullName = (context.candidateAnswers['full name']?.value ?? '').replace(/\s+/g, ' ').trim().toLowerCase();
    const fields = base.fields.map((field): FormField => {
      const label = normalizeQuestion(field.label);
      if (field.type === 'radio' && this.resumeVerified && field.value && /resume|cv|r[?e]sum[?e]/i.test(label)) return { ...field, classification: 'KNOWN', answer: field.value };
      const current = field.options?.find((option) => option.value === field.value)?.label ?? field.value ?? '';
      const placeholder = !current || /^select an option$/i.test(current);
      // These two selects offer only the account's own on-file values (its verified email addresses, its recognised phone country); there is no
      // free-text alternative to type a different one into, so whichever real option is already chosen is accepted as-is rather than
      // compared against config/candidate.json (which may legitimately name a different email you use for job hunting).
      if (field.type === 'select' && (/(?:phone )?country code/.test(label) || /^email(?: address)?$/.test(label))) return placeholder ? field : { ...field, classification: 'KNOWN', answer: current };
      if (/^(?:mobile |cell )?phone(?: number)?$/.test(label) && field.type !== 'select') {
        const shown = digits(field.value ?? '');
        if (shown && candidatePhone.endsWith(shown)) return { ...field, classification: 'KNOWN', answer: field.value };
        return candidatePhone ? { ...field, classification: 'KNOWN', answer: candidatePhone.length > 10 ? candidatePhone.slice(-10) : candidatePhone } : field;
      }
      // The native-<dialog> layout also asks First/Last name, prefilled from the member's own profile. A full name is never split by
      // guesswork (see buildCandidateAnswers), but LinkedIn's own split is accepted when it reads back as the configured full_name.
      const shown = (field.value ?? '').replace(/\s+/g, ' ').trim().toLowerCase();
      if (field.classification !== 'KNOWN' && fullName && shown && field.type !== 'select') {
        if (label === 'first name' && fullName.startsWith(`${shown} `)) return { ...field, classification: 'KNOWN', answer: field.value };
        if (label === 'last name' && fullName.endsWith(` ${shown}`)) return { ...field, classification: 'KNOWN', answer: field.value };
      }
      return field;
    });
    return { ...base, fields };
  }

  /** The candidate's chosen résumé must be the one attached: pick it if LinkedIn already lists it, otherwise upload it. Returns 'ok', an error message, or undefined when this step has no résumé section. */
  private async attachResume(page: Page, context: ApplicationContext): Promise<string | undefined> {
    const dialog = this.dialog(page);
    const fileInput = dialog.locator('input[type="file"]').first();
    // The native-<dialog> layout has no file input at all: an "Upload resume" button opens the file picker (seen live 2026-09-29, Codefeast,
    // where the résumé list shares page 1 with the contact details). Either one, or a list of résumé cards, means this step is the résumé step.
    const uploadButton = dialog.getByRole('button', { name: /^upload resume$/i }).first();
    const stem = compact(basename(context.resumePath).replace(/\.[^.]+$/, ''));
    const wanted = new RegExp(`^${stem}\\d{0,4}$`);
    // A résumé card is a radio whose card shows a file name: the older layout puts it in the card's <h3> (radio id
    // "jobsDocumentCardToggle-N"); otherwise it is read as a line of text ending in .pdf/.doc/.docx inside the card, where the card is
    // every ancestor that still holds this radio and no other. Radios with no file name (a yes/no question) never count as ours.
    const listed = async () => dialog.locator('input[type="radio"]').evaluateAll((radios) => radios.map((radio, index) => {
      const file = /\.(?:pdf|docx?)$/i;
      let node: Element | null = radio; let name = (radio.getAttribute('aria-label') ?? '').trim(); // native-<dialog> layout: the radio is named by its file
      for (let level = 0; node && level < 8 && !file.test(name); level++, node = node.parentElement) name = node.querySelector('h3')?.textContent?.trim() ?? '';
      if (!file.test(name)) {
        let found = '';
        for (let card: Element | null = radio.parentElement; card && !found && card.querySelectorAll('input[type="radio"]').length === 1; card = card.parentElement)
          found = ((card as HTMLElement).innerText ?? '').split('\n').map((line) => line.trim()).find((line) => file.test(line)) ?? '';
        name = found || (radio.id.startsWith('jobsDocumentCardToggle') ? name : '');
      }
      return { index, id: radio.id, name, checked: (radio as HTMLInputElement).checked };
    })).then((cards) => cards.filter((card) => card.name));
    const isOurs = (name: string): boolean => wanted.test(compact(name.replace(/\.[^.]+$/, '')));
    let cards = await listed();
    const hasInput = await fileInput.count() > 0;
    const hasButton = await uploadButton.count() > 0;
    if (!hasInput && !hasButton && !cards.length) return undefined;
    if (!cards.some((card) => isOurs(card.name))) {
      if (hasInput) await fileInput.setInputFiles(context.resumePath, { timeout: 15000 });
      else if (hasButton) {
        const chooser = page.waitForEvent('filechooser', { timeout: 10000 });
        await uploadButton.click({ timeout: 5000 });
        await (await chooser).setFiles(context.resumePath, { timeout: 15000 });
      } else return `Resume upload failed: ${basename(context.resumePath)} is not among LinkedIn's saved résumés and this step offers no way to upload it`;
      const deadline = Date.now() + 25000;
      while (Date.now() < deadline && !cards.some((card) => isOurs(card.name))) { await page.waitForTimeout(1000); cards = await listed(); }
      if (!cards.some((card) => isOurs(card.name))) return `Resume upload failed: LinkedIn did not list ${basename(context.resumePath)} after the upload`;
    }
    const ours = cards.find((card) => isOurs(card.name))!;
    if (!ours.checked) {
      // The older layout's card label is the click target; the native-<dialog> layout's <label for> is empty (zero size), so the radio itself is.
      const radio = dialog.locator('input[type="radio"]').nth(ours.index);
      const label = ours.id ? dialog.locator(`label[for="${ours.id}"]`).first() : undefined;
      if (label && await label.isVisible().catch(() => false)) await label.click({ timeout: 5000 }).catch(() => undefined);
      if (!await radio.isChecked().catch(() => false)) await radio.check({ force: true, timeout: 5000 }).catch(() => undefined);
    }
    const now = await listed();
    const selected = now.find((card) => card.checked);
    if (!selected || !isOurs(selected.name)) return `Resume upload failed: ${basename(context.resumePath)} could not be selected (LinkedIn has "${selected?.name ?? 'nothing'}" selected)`;
    return 'ok';
  }

  /** LinkedIn ticks "Follow company" by default; applying should not also follow the company. */
  private async declineFollow(page: Page): Promise<void> {
    const dialog = this.dialog(page);
    // Older layout: id "follow-company-checkbox". The native-<dialog> layout uses generated ids, so its label ("Follow <company> …") identifies it.
    await dialog.locator('input[type="checkbox"]').evaluateAll((boxes) => {
      for (const box of boxes) {
        // Native-<dialog> layout: the <label for> is empty and the words sit in the checkbox's own aria-label ("Follow Codefeast to stay up to date…").
        const label = [box.getAttribute('aria-label') ?? '', ...Array.from((box as HTMLInputElement).labels ?? []).map((item) => item.textContent ?? '')].join(' ').trim();
        if (box.id === 'follow-company-checkbox' || /^follow\b(?!-up)/i.test(label)) box.setAttribute('data-job-link-follow', 'true');
      }
    }).catch(() => undefined);
    const follow = dialog.locator('input[type="checkbox"][data-job-link-follow="true"]').first();
    if (await follow.count() && await follow.isChecked().catch(() => false)) {
      await follow.uncheck({ force: true, timeout: 3000 }).catch(() => undefined);
      const id = await follow.getAttribute('id').catch(() => null);
      if (id && await follow.isChecked().catch(() => false)) await dialog.locator(`label[for="${id}"]`).first().click({ timeout: 3000 }).catch(() => undefined);
    }
  }

  private async hasValidationError(page: Page): Promise<boolean> {
    const messages=await this.dialog(page).locator('[role="alert"], .artdeco-inline-feedback--error').allInnerTexts().catch(()=>[] as string[]);
    return messages.some(message=>/\b(?:required|error|invalid|please (?:enter|select|choose)|unable|failed)\b/i.test(message));
  }

  private async controlsSignature(page: Page): Promise<string> {
    return this.dialog(page).evaluate(dialog => Array.from(dialog.querySelectorAll('input, select, textarea, button'))
      .filter(el => el.getClientRects().length > 0)
      .map(el => [el.tagName,el.getAttribute('id'),el.getAttribute('name'),el.getAttribute('aria-label'),el.tagName==='BUTTON' ? el.textContent?.trim() : ''].join(':')).join('|'));
  }

  private async stepSignature(page: Page): Promise<string> {
    // One read of whatever the current layout shows: the older layout's aria-labelled progress region and step heading, or the
    // native-<dialog> layout's visible "2/7 pages" counter. Waiting on each piece separately cost a timeout per missing piece per call
    // (and the walk calls this repeatedly after every click), so absent pieces are simply empty here.
    return this.dialog(page).evaluate((dialog) => {
      const progress = dialog.querySelector('[role="region"][aria-label*="progress" i]')?.getAttribute('aria-label') ?? '';
      const heading = dialog.querySelector('h3, h4')?.textContent?.replace(/\s+/g, ' ').trim() ?? '';
      const pages = /\b\d+\s*\/\s*\d+\s*pages?\b/i.exec((dialog as HTMLElement).innerText ?? '')?.[0] ?? '';
      const controls = Array.from(dialog.querySelectorAll('input, select, textarea, button')).map(el => [el.tagName, el.getAttribute('name'), el.getAttribute('aria-label'), el.getAttribute('type'), el.tagName === 'BUTTON' ? el.textContent : '', (el as HTMLInputElement).labels ? Array.from((el as HTMLInputElement).labels ?? []).map(label => label.textContent).join(' ') : ''].join(':')).join(';');
      return `${progress}|${heading}|${pages}|${controls}`;
    }, undefined, { timeout: 1500 }).catch(() => '');
  }

  /** Waits after a Next/Back click until the dialog shows another page's controls (or a validation error), then until that page has loaded. */
  private async waitForNewPage(page: Page, beforeControls: string): Promise<void> {
    // A progress bar may advance before LinkedIn mounts the next page. Wait for its actual controls.
    await page.waitForFunction((previous) => {
      const dialog = document.querySelector('dialog[open], div[role="dialog"]');
      if (!dialog) return false;
      if (dialog.querySelector('[data-testid="loader"], [aria-busy="true"]')) return false;
      const controls = Array.from(dialog.querySelectorAll('input, select, textarea, button'))
        .filter(el => el.getClientRects().length > 0)
        .map(el => [el.tagName,el.getAttribute('id'),el.getAttribute('name'),el.getAttribute('aria-label'),el.tagName==='BUTTON' ? el.textContent?.trim() : ''].join(':')).join('|');
      return controls !== previous || Array.from(dialog.querySelectorAll('[role="alert"], .artdeco-inline-feedback--error')).some(el => /\b(?:required|error|invalid|please (?:enter|select|choose)|unable|failed)\b/i.test(el.textContent ?? ''));
    },beforeControls,{timeout:8000}).catch(()=>undefined);
    await this.waitForStep(page);
  }

  /** LinkedIn reopens a saved application on the page it was left at, up to the final review (live 2026-09-30: seven pages skipped, eleven
   *  questions never looked at, reported ready). The walk starts from page 1 so every page gets the same checks; pages have no Back. */
  private async rewindToFirstPage(page: Page): Promise<void> {
    const back = (): Locator => this.dialog(page).getByRole('button', { name: /^back(?: to previous step)?$/i }).first();
    for (let step = 0; step < MAX_STEPS && await back().isVisible().catch(() => false); step++) {
      const before = await this.controlsSignature(page);
      await back().click({ timeout: 8000 });
      await this.waitForNewPage(page, before);
    }
  }

  async fillKnownFields(page: Page, context: ApplicationContext): Promise<ApplicationInspection> {
    const errors: string[] = [];
    let fieldsFilled = 0;
    // Only on the first walk: after a manual pause the person has just answered this page, and the walk continues from it.
    if (!this.walked) { this.walked = true; await this.rewindToFirstPage(page); }
    for (let step = 0; step < MAX_STEPS; step++) {
      const beforeFields=await this.inspect(page,context);
      if(process.env.JOB_LINK_DEBUG_EASY_APPLY==='1')console.log('[EASY_APPLY_STEP]',JSON.stringify({step:step+1,fields:beforeFields.fields.map(f=>({label:f.label,type:f.type,classification:f.classification,required:f.required}))}));
      const filled = await this.fillFields(page, beforeFields);
      fieldsFilled += filled.fieldsFilled; errors.push(...filled.errors);
      const resume = await this.attachResume(page, context);
      if (resume === 'ok') this.resumeVerified = true; else if (resume) errors.push(resume);
      await this.declineFollow(page);
      const after = await this.inspect(page, context);
      const unresolved = after.fields.filter((field) => field.type !== 'file' && field.required && (field.classification !== 'KNOWN' || !field.value?.trim() || field.valid === false));
      // Optional questions the profile cannot answer are left blank; they are named in the result so a person sees them before submitting.
      for (const field of after.fields) {
        if (field.value?.trim()) this.optionalBlank.delete(field.label); // answered since (e.g. during a manual pause)
        else if (!field.required && !['checkbox', 'file'].includes(field.type ?? '') && field.classification !== 'KNOWN') this.optionalBlank.add(field.label);
      }
      if(process.env.JOB_LINK_DEBUG_EASY_APPLY==='1')console.log('[EASY_APPLY_STEP_RESULT]',JSON.stringify({step:step+1,fieldsFilled:filled.fieldsFilled,errors:filled.errors,unresolved:unresolved.map(f=>({label:f.label,type:f.type,classification:f.classification,required:f.required}))}));
      if (errors.length || unresolved.length) break;
      if (await this.submitButton(page).count()) break; // the final step: everything is filled and only the submit click remains
      const next = this.nextButton(page);
      if (!await next.count()) {
        // Say where and why rather than stopping silently (which would surface later as a misleading "résumé not attached").
        const where = (await this.stepSignature(page)).split('|').slice(0,3).filter(Boolean).join(', ') || `step ${step + 1}`;
        errors.push(`Easy Apply stopped at ${where}: no Next, Review or Submit button was recognised on this step`);
        break;
      }
      const before = await this.stepSignature(page);
      const beforeControls = await this.controlsSignature(page);
      await next.click({ timeout: 8000 });
      await this.waitForNewPage(page, beforeControls);
      if (await this.hasValidationError(page)) break;
      if (await this.stepSignature(page) === before) {
        // LinkedIn refused to leave this page: something on it is still unanswered, often a field that is not marked up as required
        // (live 2026-09-29: a "Location (city)*" typeahead). Clicking Next again cannot help, so say what is shown and what is empty.
        const where = before.split('|').slice(0,3).filter(Boolean).join(', ') || `step ${step + 1}`;
        const shown = /this field is required|please (?:enter|select|make)[^\n.]*/i.exec(await this.dialog(page).innerText({ timeout: 1500 }).catch(() => ''))?.[0];
        const empty = (await this.inspect(page, context)).fields.filter((field) => field.type !== 'file' && !field.value?.trim() && (field.required || (field.classification !== 'KNOWN' && field.type !== 'checkbox'))).map((field) => field.label);
        errors.push(`LinkedIn stayed on ${where} after Next${shown ? ` ("${shown}")` : ''}${empty.length ? `; still empty: ${empty.join(', ')}` : ''}`);
        break;
      }
    }
    return { ...await this.inspect(page, context), fieldsFilled, resumeUploaded: this.resumeVerified, errors, optionalBlank: [...this.optionalBlank] };
  }

  async validate(page: Page, context: ApplicationContext): Promise<ApplicationInspection> {
    const inspection = await this.inspect(page, context);
    const errors: string[] = [];
    const identityVerified = await this.verifyIdentity(page, context);
    if (!identityVerified) errors.push('Application identity could not be verified against the expected role, company and job URL');
    if (!inspection.fields.length && !await this.submitButton(page).count()) errors.push('No supported application form found');
    if (!await this.submitButton(page).isVisible().catch(() => false)) errors.push('Application has not reached the final review step');
    for (const field of inspection.fields) {
      if (field.type === 'file') continue;
      if (field.required && !field.value?.trim()) errors.push(`Required question has no answer: ${field.label}`);
      else if (field.valid === false && field.value) errors.push(`${field.label}: ${field.validationMessage || 'Browser validation rejected the value'}`);
    }
    return { ...inspection, identityVerified, resumeUploaded: this.resumeVerified, errors };
  }

  /** The job number in the address, the company in the dialog title and the role in the page title must all match what was queued. */
  async verifyIdentity(page: Page, context: ApplicationContext): Promise<boolean> {
    const jobId = (address: string): string | undefined => { try { return /(\d{7,})/.exec(new URL(address).pathname)?.[1] ?? new URL(address).searchParams.get('currentJobId') ?? undefined; } catch { return undefined; } };
    const wanted = jobId(context.job.applicationUrl ?? context.job.jobUrl) ?? jobId(context.job.jobUrl);
    if (!wanted || wanted !== jobId(page.url())) return false;
    const header = normalized(await page.locator(HEADER).first().innerText({ timeout: 1500 }).catch(() => ''));
    const company = tokens(context.job.company);
    const title = normalized(await page.title().catch(() => ''));
    const role = tokens(context.job.title);
    // The job number already pins the posting; employers edit titles after posting (live 2026-09-30: "…Selenium with Java" became
    // "…Selenium"), so the role only has to keep two thirds of its words.
    return company.length > 0 && company.every((token) => header.includes(token)) && role.length > 0 && role.filter((token) => title.includes(token)).length >= Math.ceil(role.length * 2 / 3);
  }

  override async readyToSubmit(page: Page): Promise<boolean> {
    const control=this.submitButton(page);
    return await control.count()===1 && await control.isVisible() && await control.isEnabled();
  }

  async submit(page: Page, context: ApplicationContext): Promise<ApplicationInspection> {
    const validation = await this.validate(page, context);
    if (validation.errors?.length) return { ...validation, blockedReason: validation.errors.join('; ') };
    const submit = this.submitButton(page);
    if (await submit.count() !== 1 || !await submit.isEnabled().catch(() => false)) return { ...validation, blockedReason: 'The final "Submit application" button is not available on this step' };
    await submit.click({ timeout: 15000 });
    // LinkedIn can auto-advance to a follow-up "nudge" dialog (e.g. "Turn your resume into a profile") within a second or two of
    // showing this, replacing it in the DOM. The confirmation text is read from the very element just found, in the same step
    // that found it visible — not by re-scanning the page afterward, by which point the confirmation can already be gone even
    // though the submission genuinely succeeded (seen live: a real "Your application was sent!" was missed this way).
    const confirmed = page.getByText(/your application was sent|application (?:was )?sent/i);
    const appeared = await confirmed.first().waitFor({ state: 'visible', timeout: 20000 }).then(() => true, () => false);
    if (appeared) {
      // Both the specific "your application was sent to X" line and a plainer "Application sent" heading commonly appear
      // together in the same instant; grabbing every match in one call and preferring the specific one keeps that preference
      // (a company name is worth having) without losing the moment to the race the comment above describes.
      const texts = await confirmed.allInnerTexts().catch(() => [] as string[]);
      const text = texts.find((candidate) => /your application was sent/i.test(candidate)) ?? texts[0] ?? 'Your application was sent';
      return { fields: [], confirmationText: text, finalUrl: page.url() };
    }
    return this.captureResult(page, context);
  }

  async captureResult(page: Page, _context: ApplicationContext): Promise<ApplicationInspection> {
    const text = await page.locator('body').innerText().catch(() => '');
    const confirmation = text.match(/your application was sent to [^\n.!]*/i)?.[0] ?? text.match(/application (?:was )?sent/i)?.[0];
    const stillHasSubmit = await this.submitButton(page).isVisible().catch(() => false);
    return { fields: [], confirmationText: confirmation && !stillHasSubmit ? confirmation : undefined, finalUrl: page.url(), blockedReason: confirmation && !stillHasSubmit ? undefined : 'Submission outcome is unconfirmed; check your LinkedIn applications before any retry' };
  }
}
