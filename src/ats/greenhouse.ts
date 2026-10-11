import { HostedFormAdapter } from './form.js';

export class GreenhouseAdapter extends HostedFormAdapter {
  constructor() { super({ name: 'greenhouse', hosts: /(^|\.)(greenhouse\.io|greenhouse\.com)$/, formSelector: 'form#application_form, form#application-form, form:has(input[name="first_name"]), form:has(input#first_name)', resumeSelector: 'input[type="file"][name*="resume" i], input[type="file"]#resume', submitSelector: 'button[type="submit"], input[type="submit"]#submit_app', successSelector: '#application_confirmation, .confirmation, main', applySelector: 'a[href="#app"], button:has-text("Apply")' }); }
}
