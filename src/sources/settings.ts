import { readFile } from 'node:fs/promises';
import { z } from 'zod';

const httpUrl = z.string().url().refine((value) => /^https?:\/\//i.test(value), 'Expected an HTTP(S) URL');
const base = { name: z.string().min(1), enabled: z.boolean().default(true), max_jobs: z.number().int().positive().max(20_000).default(5_000) };
const board = { ...base, board: z.string().regex(/^[a-zA-Z0-9_-]+$/), company: z.string().min(1) };
export const browserSourceSchema = z.object({
  ...base, type: z.literal('browser').default('browser'), urls: z.array(httpUrl).min(1),
  cardSelector: z.string().min(1), titleSelector: z.string().min(1), companySelector: z.string().optional(), company: z.string().optional(),
  locationSelector: z.string().optional(), descriptionSelector: z.string().optional(), linkSelector: z.string().min(1),
  postedAtSelector: z.string().optional(), applicationLinkSelector: z.string().optional(), externalIdAttribute: z.string().optional(),
  nextPageSelector: z.string().optional(), max_pages: z.number().int().positive().max(100).default(10),
  detail: z.object({ descriptionSelector: z.string().optional(), postedAtSelector: z.string().optional(), applicationLinkSelector: z.string().optional(),
    descriptionTextAnchor: z.object({ start: z.string().min(1), end: z.string().min(1).optional() }).optional(), easyApplySelector: z.string().optional() }).optional(),
  requires_login: z.boolean().default(false), emptyResultsSelector: z.string().optional(),
  // Some sites draw only the first few result cards until the list is scrolled (LinkedIn shows 7 of every 25).
  scrollToLoad: z.boolean().default(false)
}).strict();
export const sourceSchema = z.discriminatedUnion('type', [
  z.object({ ...board, type: z.literal('greenhouse') }).strict(),
  z.object({ ...board, type: z.literal('lever'), region: z.enum(['global', 'eu']).default('global') }).strict(),
  z.object({ ...board, type: z.literal('ashby'), board: z.string().min(1).max(200).regex(/^[\p{L}\p{N} ._-]+$/u) }).strict(),
  z.object({ ...base, type: z.literal('workday'), company: z.string().min(1), url: httpUrl, tenant: z.string().regex(/^[\w-]+$/), site: z.string().regex(/^[\w-]+$/), search_text: z.string().default(''), max_pages: z.number().int().positive().max(200).default(50) }).strict(),
  z.object({ ...base, type: z.literal('linkedin'), urls: z.array(httpUrl).default([]), max_pages: z.number().int().positive().max(40).default(5), max_searches: z.number().int().positive().max(100).default(6), requires_login: z.boolean().default(false) }).strict(),
  browserSourceSchema
]);
export const sourceSettingsSchema = z.object({
  sources: z.array(sourceSchema).default([]), browser_sources: z.array(browserSourceSchema.omit({ name: true }).extend({ name: z.string().default('legacy-browser') })).default([]),
  manual_login_required: z.boolean().default(false), request_timeout_ms: z.number().int().min(1_000).max(120_000).default(30_000),
  concurrency: z.number().int().min(1).max(5).default(3)
}).strict().superRefine((settings, context) => {
  const names = new Set<string>();
  for (const source of [...settings.sources, ...settings.browser_sources]) {
    if (names.has(source.name)) context.addIssue({ code: z.ZodIssueCode.custom, message: `Duplicate source name: ${source.name}` });
    names.add(source.name);
    if (source.type === 'browser' && !source.company && !source.companySelector) context.addIssue({ code: z.ZodIssueCode.custom, message: `${source.name} requires company or companySelector` });
  }
});
export type SourceSettings = z.infer<typeof sourceSettingsSchema>;
export type SourceConfig = z.infer<typeof sourceSchema>;
export type BrowserSourceConfig = z.infer<typeof browserSourceSchema>;

export async function loadSourceSettings(root: string): Promise<SourceSettings> {
  return sourceSettingsSchema.parse(JSON.parse(await readFile(`${root}/config/sources.json`, 'utf8')));
}
