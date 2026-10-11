import { HostedFormAdapter } from './form.js';

export class LeverAdapter extends HostedFormAdapter {
  constructor() { super({ name: 'lever', hosts: /(^|\.)lever\.co$/, formSelector: 'form#application-form, form:has(input[name="name"]):has(input[name="email"])', resumeSelector: 'input[type="file"][name="resume"], input[type="file"]#resume-upload-input', submitSelector: 'button[type="submit"], button#btn-submit', successSelector: '.application-confirmation, .confirmation, .content', applySelector: 'a.postings-btn[href$="/apply"], a[href$="/apply"]' }); }
}
