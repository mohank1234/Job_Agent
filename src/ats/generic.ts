import { HostedFormAdapter } from './form.js';

export class GenericAtsAdapter extends HostedFormAdapter {
  constructor() { super({ name: 'generic', hosts: /.*/, formSelector: 'form:has(input[type="file"]), form:has(button:text-is("Submit application")), form:has(button:text-is("Next")), [role="form"]:has(input)', resumeSelector: 'input[type="file"][name*="resume" i], input[type="file"][id*="resume" i], input[type="file"][aria-label*="resume" i], input[type="file"][name*="cv" i]', submitSelector: 'button[type="submit"]:has-text("Submit application"), input[type="submit"][value="Submit application"]' }); }
}
