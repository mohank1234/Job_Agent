import { HostedFormAdapter } from './form.js';

export class AshbyAdapter extends HostedFormAdapter {
  constructor() { super({ name: 'ashby', hosts: /(^|\.)ashbyhq\.com$/, formSelector: 'form:has(input[type="file"]), form:has(input[type="email"])', resumeSelector: 'input[type="file"][name*="resume" i], input[type="file"][aria-label*="resume" i], input[type="file"][id*="resume" i]', submitSelector: 'button[type="submit"]', successSelector: 'main, [class*="success" i]', applySelector: 'a[href$="/application"], button:has-text("Apply for this job"), button:has-text("Apply now")' }); }
}
