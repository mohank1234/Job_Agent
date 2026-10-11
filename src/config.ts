import { readFile } from 'node:fs/promises';
import { z } from 'zod';

const optionalUrl = z.union([z.literal(''), z.string().url()]);
export const candidateSchema = z.object({
  full_name: z.string().trim().min(1), email: z.string().email(), phone: z.string().min(1), location: z.string(),
  // The phone's international dialling code ("+91"). Phone boxes with their own country picker get the number in that form, because such a
  // picker can default to another country (live 2026-09-30: a Manatal form showed a 10-digit Indian number as "(889) 740-4807", a US number).
  phone_country_code: z.string().regex(/^\+\d{1,4}$/).optional(),
  linkedin: optionalUrl, github: optionalUrl, portfolio: optionalUrl,
  years_experience: z.number().min(0).max(80).nullable(), current_company: z.string(),
  previous_companies: z.array(z.string()), skills: z.array(z.string()),
  automation_skills: z.array(z.string()), ai_testing_skills: z.array(z.string()),
  education: z.array(z.string()), notice_period: z.string(), expected_salary: z.string(),
  current_salary: z.string(), work_authorization: z.string(), preferred_locations: z.array(z.string()),
  remote_preference: z.string(),
  first_name: z.string().optional(), last_name: z.string().optional(), city: z.string().optional(),
  country: z.string().optional(), current_title: z.string().optional(), degree: z.string().optional(),
  sponsorship_required: z.boolean().optional(), authorized_countries: z.array(z.string()).optional(),
  // Your own years with each skill, e.g. { "Selenium": 5, "API Testing": 4 }: answers "How many years of experience do you have with X?"
  skill_years: z.record(z.string().min(1), z.union([z.number().min(0).max(80), z.string().min(1)])).optional()
}).strict();

export const searchSchema = z.object({
  role_keywords: z.array(z.string().min(1)).min(1), locations: z.array(z.string()), sources: z.array(z.string()),
  max_age_hours: z.number().positive().max(8760), priority_window_hours: z.number().positive(),
  allow_unknown_posted_at: z.boolean().default(false),
  // Skip postings written for less experience than this ("2-3 years", "3+ years"); 0 turns it off. Postings that state none are kept.
  min_job_experience_years: z.number().min(0).max(40).default(0),
  // Internships, trainee, fresher, apprentice and junior roles are never applied to.
  exclude_internships: z.boolean().default(true),
  // Work order inside each age batch: first entry first, every other location after them.
  location_priority: z.array(z.string().min(1)).default(['Hyderabad', 'Remote', 'Bangalore'])
}).strict().refine(value => value.priority_window_hours <= value.max_age_hours, 'priority_window_hours cannot exceed max_age_hours');
export const applicationSchema = z.object({
  auto_apply_min_score: z.number().min(0).max(100), review_min_score: z.number().min(0).max(100),
  max_jobs_per_run: z.number().int().positive().max(10000), batch_size: z.number().int().positive().max(100).default(25),
  headless: z.boolean(), require_submission_confirmation: z.boolean(), allow_resume_tailoring: z.boolean(),
  auto_submit: z.boolean().default(false), max_attempts: z.number().int().min(1).max(5).default(2),
  retry_delay_seconds: z.number().min(0).max(86400).default(60),
  page_timeout_ms: z.number().int().min(1000).max(120000).default(30000),
  delay_between_jobs_ms: z.number().int().min(0).max(60000).default(1000),
  answers: z.record(z.string(), z.string()).default({})
}).strict().refine(value => value.review_min_score <= value.auto_apply_min_score, 'review_min_score cannot exceed auto_apply_min_score');
export const outreachSchema = z.object({ max_contacts_per_job: z.number().int().min(0).max(10), max_contacts_per_company: z.number().int().min(1).max(10).default(3), auto_send_connection_requests: z.boolean(), auto_send_inmail: z.boolean(), same_company_cooldown_days: z.number().min(0), connection_note_limit: z.number().int().min(1).max(300), signature: z.string().trim().min(1).optional(), exact_note_length: z.boolean().default(true) }).strict();
export const safetySchema = z.object({ pause_on_captcha: z.boolean(), pause_on_mfa: z.boolean(), pause_on_restriction: z.boolean(), pause_on_unknown_required_question: z.boolean(), manual_confirmation_before_linkedin_send: z.boolean(),
  linkedin_daily_page_limit: z.number().int().min(0).max(1000).default(150), linkedin_block_cooldown_hours: z.number().min(0).max(720).default(72),
  linkedin_min_seconds_between_pages: z.number().min(0).max(120).default(10) }).strict();

export type Config = { candidate: z.infer<typeof candidateSchema>; search: z.infer<typeof searchSchema>; application: z.infer<typeof applicationSchema>; outreach: z.infer<typeof outreachSchema>; safety: z.infer<typeof safetySchema> };

export async function loadConfig(root: string): Promise<Config> {
  async function read<S extends z.ZodTypeAny>(name: string, schema: S): Promise<z.output<S>> {
    try { return schema.parse(JSON.parse(await readFile(`${root}/config/${name}.json`, 'utf8'))); }
    catch (error) { throw new Error(`Invalid config/${name}.json: ${error instanceof Error ? error.message : String(error)}`); }
  }
  return {
    candidate: await read('candidate', candidateSchema), search: await read('search', searchSchema),
    application: await read('application', applicationSchema), outreach: await read('outreach', outreachSchema),
    safety: await read('safety', safetySchema)
  };
}

