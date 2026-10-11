import type { Page } from 'playwright';
import type { ApplicationContext, ApplicationInspection } from '../core/types.js';
import { HostedFormAdapter } from './form.js';

/** Bounded Workday step navigation; account/security steps remain human-owned. */
export class WorkdayAdapter extends HostedFormAdapter {
  constructor() { super({ name: 'workday', hosts: /(^|\.)(myworkdayjobs\.com|myworkdaysite\.com)$/, formSelector: 'form:has([data-automation-id]), [data-automation-id="applyFlowPage"]', resumeSelector: 'input[type="file"][data-automation-id="file-upload-input-ref"], input[type="file"][name*="resume" i]', submitSelector: '[data-automation-id="bottom-navigation-next-button"]:has-text("Submit")', successSelector: '[data-automation-id="applicationSubmitted"]', applySelector: '[data-automation-id="adventureButton"], a[data-automation-id="applyLink"]' }); }
  private attached = false;
  private verifiedUrl?: string;
  override async verifyIdentity(page: Page, context: ApplicationContext): Promise<boolean> {
    if (this.verifiedUrl === page.url()) return true;
    const verified = await super.verifyIdentity(page, context);
    if (verified) this.verifiedUrl = page.url();
    return verified;
  }
  override async fillKnownFields(page: Page, context: ApplicationContext): Promise<ApplicationInspection> {
    let fieldsFilled = 0;
    for (let step = 0; step < 15; step++) {
      const before = await this.inspect(page, context);
      if (before.fields.some(f => f.classification === 'SECURITY')) return {...before, fieldsFilled, resumeUploaded:this.attached, errors:['Authentication or security verification requires manual action']};
      const filled = await this.fillFields(page, before);
      fieldsFilled += filled.fieldsFilled;
      if (!this.attached && await (await this.resumeControl(page)).count()) {
        const uploaded = await super.fillKnownFields(page, context);
        this.attached ||= !!uploaded.resumeUploaded;
        if (uploaded.errors?.length) return {...uploaded,fieldsFilled,resumeUploaded:this.attached};
      }
      const current = await this.inspect(page, context);
      if (filled.errors.length || current.fields.some(f => f.required && f.type !== 'file' && (f.classification !== 'KNOWN' || !f.value || f.valid === false)))
        return {...current, fieldsFilled, resumeUploaded:this.attached, errors:filled.errors};
      const submit = this.form(page).locator(this.profile.submitSelector);
      if (await submit.isVisible().catch(()=>false)) return {...current,fieldsFilled,resumeUploaded:this.attached};
      const next = this.scope(page).getByRole('button',{name:/^(?:next|save and continue|continue|review)$/i}).first();
      if (!await next.isVisible().catch(()=>false)) return {...current,fieldsFilled,resumeUploaded:this.attached,errors:['Workday has no supported next/review control; inspect the current account or application step']};
      const old = await this.form(page).innerText();
      await next.click({timeout:8000});
      try {
        await page.waitForFunction(({selector,old}) => {
          const el = document.querySelector(selector);
          return el && ((el as HTMLElement).innerText !== old || !!el.querySelector('[role="alert"], [aria-invalid="true"]'));
        },{selector:this.profile.formSelector,old},{timeout:10000});
      } catch { return {...await this.inspect(page,context),fieldsFilled,resumeUploaded:this.attached,errors:['Workday did not advance after Continue']}; }
    }
    return {...await this.inspect(page,context),fieldsFilled,resumeUploaded:this.attached,errors:['Workday exceeded the supported step limit']};
  }
  override async validate(page: Page, context: ApplicationContext): Promise<ApplicationInspection> {
    const result = await super.validate(page,context);
    this.attached ||= !!result.resumeUploaded;
    const final = await this.readyToSubmit(page);
    const errors = (result.errors ?? []).filter(error => !(final && error === 'No supported application form found')).filter(error => this.attached ? error !== 'Selected resume is not verifiably attached' : true);
    if (!await this.form(page).locator(this.profile.submitSelector).isVisible().catch(()=>false)) errors.push('Workday has not reached final review');
    return {...result,resumeUploaded:this.attached,errors};
  }
}
