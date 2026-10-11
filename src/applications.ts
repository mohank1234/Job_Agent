import { isClosedJob, isAlreadyApplied } from './ats/job-state.js';
import type { AtsAdapter } from './ats/adapter.js';
import { mkdir, writeFile } from 'node:fs/promises';
import type { Page } from 'playwright';
import type { ApplicationContext, ApplicationStatus, FormField } from './core/types.js';
import { defaultAtsRegistry } from './ats/index.js';
import { detectSafetyStop } from './safety.js';

export type ApplicationQuestion = { question: string; classification: 'MISSING' | 'SENSITIVE' | 'SECURITY' | 'MANUAL_APPLY'; required: boolean; fieldName?: string; options?: string[] };
export type ApplicationResult = {
  status: ApplicationStatus;
  ats: string;
  fieldsFilled: number;
  resumeUploaded: boolean;
  formsInspected: number;
  questions: ApplicationQuestion[];
  screenshot?: string;
  reason?: string;
  confirmation?: string;
  finalUrl: string;
  submissionAttempted: boolean;
  fields?: FormField[];
};
export type ApplicationPolicy = { allowSubmit: boolean; beforeSubmit?: () => Promise<void>; onUserInput?: (result: ApplicationResult, adapter: AtsAdapter) => Promise<boolean> };

/** Process one already-open application page. The caller owns durable state and the submission claim. */
export async function processApplicationPage(page: Page, root: string, context: ApplicationContext, policy: ApplicationPolicy, continuingAdapter?: AtsAdapter): Promise<ApplicationResult> {
  let adapter = continuingAdapter ?? defaultAtsRegistry().forPage(page)!;
  const result: ApplicationResult = { status: 'FAILED', ats: adapter.name, fieldsFilled: 0, resumeUploaded: false, formsInspected: 0, questions: [], finalUrl: page.url(), submissionAttempted: false };
  const finish = async (): Promise<ApplicationResult> => {
    result.finalUrl = page.url();
    await mkdir(`${root}/screenshots`, { recursive: true });
    if (!result.screenshot) {
      const screenshot = `${root}/screenshots/${Date.now()}_job-${context.jobId}_${result.status.toLowerCase()}.png`;
      try { await page.screenshot({ path: screenshot, fullPage: true, timeout: 15000 }); result.screenshot = screenshot; }
      catch (error) { result.reason = [result.reason, `Evidence capture failed: ${String(error)}`].filter(Boolean).join('; '); }
    }
    // LinkedIn changes the Easy Apply markup without notice (it did on 2026-09-29). Keeping the dialog's HTML beside the screenshot lets a
    // stop on an unfamiliar step be diagnosed and turned into a test fixture without walking a live application again.
    if (adapter.name === 'linkedin-easy-apply' && result.status !== 'SUBMITTED') {
      const html = await page.locator('dialog:visible, div[role="dialog"]:visible').first().evaluate((element) => element.outerHTML, undefined, { timeout: 2000 }).catch(() => undefined);
      if (html) await writeFile(`${root}/screenshots/${Date.now()}_job-${context.jobId}_dialog.html`, html).catch(() => undefined);
    }
    result.fields = result.fields ?? [];
    const evidence = await page.locator('dialog:visible, [role="dialog"]:visible, form:visible').first().evaluate(element => element.outerHTML, undefined, { timeout: 1000 }).catch(() => undefined);
    if (evidence) await writeFile(`${root}/screenshots/job-${context.jobId}-form.html`, evidence).catch(() => undefined);
    return result;
  };
  const safety = async (): Promise<boolean> => {
    const stop = await detectSafetyStop(page, root, `job-${context.jobId}`);
    if (!stop.stopped) return false;
    result.status = stop.status ?? 'BLOCKED_SECURITY'; result.reason = stop.reason; result.screenshot = stop.screenshot;
    return true;
  };
  try {
    const pageText = await page.locator('body').innerText();
    if (isClosedJob(pageText)) { result.status = 'CLOSED_JOB'; result.reason = 'Posting explicitly closed or inactive'; return finish(); }
    if (isAlreadyApplied(pageText) || await adapter.alreadyApplied?.(page)) { result.status = 'ALREADY_APPLIED'; result.reason = 'Application page reports a previous application'; return finish(); }
    if (await safety()) return finish();
    if (!continuingAdapter) {
      const entryUrl = page.url();
      const entryAdapter = adapter.name;
      await adapter.prepare(page, context);
      if (entryAdapter === 'generic' && page.url() !== entryUrl) context = {...context,job:{...context.job,applicationUrl:page.url(),externalId:undefined,atsJobId:undefined}};
      const detected = defaultAtsRegistry().forPage(page)!;
      if (detected.name !== adapter.name) { adapter = detected; await adapter.prepare(page, context); }
    }
    result.ats = adapter.name;
    if (await safety()) return finish();
    const text = await page.locator('body').innerText();
    if (isClosedJob(text)) { result.status='CLOSED_JOB'; result.reason='Posting explicitly closed or inactive'; return finish(); }
    if (isAlreadyApplied(text) || await adapter.alreadyApplied?.(page)) {
      result.status = 'ALREADY_APPLIED'; result.reason = 'Application page reports a previous application'; return finish();
    }
    const initial = await adapter.inspect(page, context);
    result.formsInspected = initial.fields.length ? 1 : 0;
    result.fields = initial.fields;
    const securityField = initial.fields.find((field) => field.classification === 'SECURITY');
    if (securityField) {
      result.status = 'BLOCKED_SECURITY'; result.reason = `Security verification field requires manual action: ${securityField.label}`;
      result.questions.push({ question: securityField.label, classification: 'SECURITY', required: securityField.required, fieldName: securityField.name });
      return finish();
    }
    if (!initial.fields.length) {
      result.status = isClosedJob(text) ? 'CLOSED_JOB' : 'UNSUPPORTED_ATS';
      result.reason = result.status === 'CLOSED_JOB' ? 'Posting explicitly closed or inactive' : `No supported application form after navigation: ${page.url()}`;
      return finish();
    }
    // Verify job/company before putting personal candidate data in the page.
    const identity = await adapter.validate(page, context);
    if (!identity.identityVerified) { result.reason = 'Application identity could not be verified against the expected role, company and job URL'; return finish(); }
    const filled = await adapter.fillKnownFields(page, context);
    result.fieldsFilled = filled.fieldsFilled ?? 0;
    result.resumeUploaded = filled.resumeUploaded ?? false;
    result.fields = filled.fields;
    if (await safety()) return finish();
    const validation = await adapter.validate(page, context);
    result.resumeUploaded = validation.resumeUploaded ?? result.resumeUploaded;
    result.fields = validation.fields;
    result.questions = validation.fields.filter((field) => field.type !== 'file' && field.required && (field.classification !== 'KNOWN')).map((field) => ({ question: field.label, classification: field.classification === 'SENSITIVE' ? 'SENSITIVE' : field.classification === 'SECURITY' ? 'SECURITY' : 'MISSING', required: true, fieldName: field.name, options: field.options?.map((option) => option.label) }));
    if(filled.errors?.some(error=>/LinkedIn stayed on/.test(error)&&/required/i.test(error))) {
      for(const field of validation.fields.filter(field=>field.type!=='file'&&field.type!=='checkbox'&&!field.value?.trim()&&field.classification!=='KNOWN'))
        if(!result.questions.some(question=>question.question===field.label))result.questions.push({question:field.label,classification:'MISSING',required:true,fieldName:field.name,options:field.options?.map(option=>option.label)});
    }
    for(const error of filled.errors??[]) {
      if(!/Configured answer does not match (?:an|a unique) available choice|Autocomplete requires an unambiguous matching option/.test(error)||/no options appeared/.test(error))continue;
      const field=validation.fields.find(candidate=>error.startsWith(`${candidate.label}:`));
      if(field && !result.questions.some(question=>question.question===field.label))result.questions.push({question:field.label,classification:'MISSING',required:true,fieldName:field.name,options:field.options?.map(option=>option.label)});
    }
    // An explicit "the upload itself failed" error is always a real failure. But on a multi-step form (LinkedIn Easy Apply) the résumé
    // step may simply not have been reached yet, because an earlier step has a question only the user can answer; that is
    // NEEDS_USER_INPUT, not a résumé failure, so the generic "not attached" fallback below only fires once nothing else explains it.
    const uploadFailure = filled.errors?.find((error) => error.startsWith('Resume upload failed'));
    if (uploadFailure) { result.reason = uploadFailure; return finish(); }
    // A submit button that stays disabled is often waiting on a box only the person may tick (live 2026-09-30, Manatal: "I agree to the terms
    // and conditions & privacy policy"). Such consent boxes are questions for the person, not a validation failure.
    if (!result.questions.length && adapter.readyToSubmit && !await adapter.readyToSubmit(page).catch(() => true))
      for (const field of validation.fields.filter((field) => ['checkbox', 'unsupported-custom'].includes(field.type ?? '') && !field.value && field.classification === 'SENSITIVE'))
        result.questions.push({ question: field.label, classification: 'SENSITIVE', required: true, fieldName: field.name });
    if (result.questions.length || filled.errors?.length || validation.errors?.length) {
      result.status = result.questions.length ? 'NEEDS_USER_INPUT' : 'VALIDATION_FAILURE';
      result.reason = [...(filled.errors ?? []), ...(validation.errors ?? [])].join('; ') || 'Required question needs an explicit verified answer';
      await finish();
      if (result.questions.length && policy.onUserInput && await policy.onUserInput(result, adapter))
        return processApplicationPage(page, root, context, policy, adapter);
      return result;
    }
    if (!result.resumeUploaded) { result.reason = 'Selected resume is not verifiably attached'; return finish(); }
    if (adapter.readyToSubmit && !await adapter.readyToSubmit(page)) { result.status='VALIDATION_FAILURE'; result.reason='Application has not reached an enabled final Submit control'; return finish(); }
    result.status = 'READY_FOR_SUBMISSION';
    // Optional questions nobody answered go out blank; naming them lets a person decide before submitting (live 2026-09-30: an hourly-rate
    // and a weekly-hours question sat blank on a "ready" application).
    const blank = [...new Set([...(filled.optionalBlank ?? []), ...validation.fields.filter((field) => !field.required && !['checkbox', 'file'].includes(field.type ?? '') && !field.value?.trim() && field.classification !== 'KNOWN').map((field) => field.label)])]
      .filter((label) => !/^unlabeled /.test(label));
    const blankNote = blank.length ? `; optional questions left blank: ${blank.join(' | ')}` : '';
    if (!policy.allowSubmit || !policy.beforeSubmit) { result.reason = (!policy.allowSubmit ? 'Submission requires human confirmation under application policy' : 'Durable submission guard was not provided') + blankNote; return finish(); }
    if (await safety()) return finish();
    // Revalidate immediately before the durable claim and click. Unknown fields may appear dynamically.
    const finalValidation = await adapter.validate(page, context);
    if (finalValidation.errors?.length || finalValidation.fields.some((field) => field.required && field.type !== 'file' && field.classification !== 'KNOWN')) {
      result.questions = finalValidation.fields.filter((field) => field.required && field.type !== 'file' && (field.classification !== 'KNOWN')).map((field) => ({ question: field.label, classification: field.classification === 'SENSITIVE' ? 'SENSITIVE' : field.classification === 'SECURITY' ? 'SECURITY' : 'MISSING', required: true, fieldName: field.name, options: field.options?.map((option) => option.label) }));
      result.status = result.questions.length ? 'NEEDS_USER_INPUT' : 'VALIDATION_FAILURE'; result.reason = finalValidation.errors?.join('; ') || 'A new required question appeared'; return finish();
    }
    await finish(); // Capture the exact reviewed state before authorizing the irreversible step.
    await policy.beforeSubmit();
    result.submissionAttempted = true;
    result.screenshot = undefined;
    const submitted = await adapter.submit(page, context);
    if (await safety()) return finish();
    if (submitted.confirmationText) { result.status = 'SUBMITTED'; result.confirmation = submitted.confirmationText; result.reason = undefined; }
    else { result.status = 'SUBMISSION_UNCONFIRMED'; result.reason = submitted.blockedReason ?? 'Submission outcome is unconfirmed; do not retry before manual reconciliation'; }
    return finish();
  } catch (error) {
    // A click that times out behind a sign-in wall or a bot check is that wall, not a technical failure to retry (live 2026-09-30: an
    // employer's Apply opened a login dialog and the next click timed out behind it).
    if (!result.submissionAttempted && !page.isClosed() && await safety().catch(() => false)) return finish();
    result.status = result.submissionAttempted ? 'SUBMISSION_UNCONFIRMED' : /timeout|ERR_|connection|browser.*closed/i.test(String(error)) ? 'RETRYABLE_TECHNICAL_FAILURE' : 'FAILED';
    result.reason = `${result.submissionAttempted ? 'Submission outcome is uncertain; do not retry. ' : ''}${error instanceof Error ? error.message : String(error)}`;
    return finish();
  }
}
