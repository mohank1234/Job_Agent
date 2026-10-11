# Application adapters and safety

`src/ats/index.ts` registers separate Greenhouse, Lever, Ashby and Workday profiles followed by the generic HTML form adapter. Profiles define form, application-link, resume and final-submit controls. Shared code extracts semantic labels, associates radio options, handles native text/select/checkbox/file controls and exact-match autocomplete choices. It does not treat every input as a text box.

## Supported surfaces

| Adapter | Implemented | Boundaries |
| --- | --- | --- |
| Greenhouse | Hosted application forms, known fields, native upload, validation and guarded final submit | Tenant custom controls, embedded cross-domain identity mismatches and visible CAPTCHA require review. |
| Lever | Hosted `/apply` forms, labeled questions, resume upload, validation and guarded final submit | Custom questions require exact configured answers; CAPTCHA/account restrictions stop the page. |
| Ashby | Hosted forms, application navigation, semantic fields, resume upload, validation and guarded final submit | Custom or anonymous upload widgets may need tenant-specific selectors. Complex repeatable education/employment editors require review. |
| Workday | ATS detection, application navigation, supported visible fields and upload inspection | Account creation, tenant-specific multi-page experience forms, attestations and final submission are manual. The adapter never automatically submits Workday applications. |
| Generic | A semantic HTML form with a recognizably named resume/CV input and explicit “Submit application” button | Unrecognized forms fail explicitly. Arbitrary SPA wizards, custom shadow controls and hidden required logic are not assumed supported. |

These are implementation capabilities. Controlled browser tests exercise all five detection paths and Greenhouse, Lever, Ashby and generic form profiles. Workday's tenant wizard is not covered by a successful submission test. Consult the run reports for actual live verification; passing a local form test is not live ATS proof.

## Factual answers

`candidate.json` is authoritative. Optional `first_name`, `last_name`, `city`, `country`, `current_title` and `degree` must be configured when a form needs them. The system does not guess legal name components or derive country-specific work permission from a generic “Yes”. Specific skills experience questions do not inherit total years of experience.

Use `config/application.json` → `answers` for an exact question and verified answer, for example:

```json
{
  "answers": {
    "An exact question copied from a report": "Your verified answer"
  }
}
```

Sensitive questions (including consent, sponsorship, citizenship and voluntary demographic fields) are left untouched unless an exact answer is explicitly configured. Unknown required questions are persisted with their question text, available options and classification. Optional unknown questions are left blank. Passwords and security codes are never filled even through an answer override.

## Upload and submission evidence

Upload validation checks local file existence, the input's accepted formats, and the selected filename. The reviewed form must still have the selected file or show its exact filename. Browser/server validation errors keep the application out of submission.

Before filling, the adapter checks the expected ATS/job identifier, host, role heading and company. Before submission it checks them again, checks required answers and verifies the selected resume. A caller-provided durable database claim is mandatory immediately before the final click. The configured confirmation policy remains in force.

`SUBMITTED` requires a visible confirmation phrase and removal of the active application form. An ambiguous post-click result becomes a manual reconciliation item; it must not be automatically retried. Screenshots and final URLs are recorded for review. A visible CAPTCHA, MFA/OTP prompt, restriction or login requirement stops the affected application without attempts to bypass it.

## Implementation references

The adapters operate hosted browser forms. Employer-side API submission credentials are not assumed. Official references: [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html), [Lever postings API](https://github.com/lever/postings-api), [Ashby application form API](https://developers.ashbyhq.com/reference/applicationformsubmit), [Playwright input interactions](https://playwright.dev/docs/input).
