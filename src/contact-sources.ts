import { readFile } from 'node:fs/promises';
import { z } from 'zod';
import type { Contact } from './core/types.js';

const httpUrl = z.string().url().refine((value) => /^https?:\/\//i.test(value), 'Must use HTTP(S).');
export const contactSchema = z.object({ company: z.string().min(1), name: z.string().min(1), title: z.string().min(1), profileUrl: httpUrl, relevanceScore: z.number().min(0).max(100).default(0) }).strict();
export const contactSettingsSchema = z.object({
  contacts: z.array(contactSchema).default([]),
  sources: z.array(z.object({ name: z.string().min(1), company: z.string().min(1), url: httpUrl, type: z.enum(['json', 'structured-data']), enabled: z.boolean().default(true) }).strict()).default([])
}).strict();
export type ContactDiscoveryResult = { contacts: Contact[]; sourcesAttempted: number; sourcesWorking: number; errors: string[] };
export function companyKey(value: string): string { return value.toLowerCase().replace(/\b(?:incorporated|inc|limited|ltd|llc|pvt|private|corp|corporation)\b/g, '').replace(/[^a-z0-9]/g, ''); }
export function canonicalContactUrl(value: string): string {
  const url = new URL(value);
  if (!['https:', 'http:'].includes(url.protocol)) throw new Error('Contact profile must use HTTP(S).');
  url.search = ''; url.hash = ''; url.hostname = url.hostname.replace(/^www\./, '');
  return url.toString().replace(/\/$/, '');
}
export function parseStructuredContacts(html: string, company: string): Contact[] {
  const result: Contact[] = [];
  const walk = (value: unknown): void => {
    if (!value || typeof value !== 'object') return;
    if (Array.isArray(value)) { value.forEach(walk); return; }
    const item = value as Record<string, unknown>;
    if (item['@type'] === 'Person' && typeof item.name === 'string' && typeof item.jobTitle === 'string') {
      const sameAs = Array.isArray(item.sameAs) ? item.sameAs : [item.sameAs];
      const profileUrl = sameAs.find((entry): entry is string => typeof entry === 'string' && /^https?:\/\/(?:www\.)?linkedin\.com\/in\//i.test(entry)) ?? (typeof item.url === 'string' ? item.url : undefined);
      if (profileUrl) {
        const parsed = contactSchema.safeParse({ company, name: item.name, title: item.jobTitle, profileUrl, relevanceScore: 0 });
        if (parsed.success) result.push(parsed.data);
      }
    }
    Object.values(item).forEach(walk);
  };
  for (const script of html.matchAll(/<script\b[^>]*type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi)) {
    try { walk(JSON.parse(script[1])); } catch { /* Malformed unrelated structured data is not contact evidence. */ }
  }
  return result;
}

export async function discoverContacts(root: string, companies: string[]): Promise<ContactDiscoveryResult> {
  const raw = await readFile(`${root}/config/contacts.json`, 'utf8').catch((error: NodeJS.ErrnoException) => { if (error.code === 'ENOENT') return '{}'; throw error; });
  const settings = contactSettingsSchema.parse(JSON.parse(raw.replace(/^\uFEFF/, '')));
  const wanted = new Set(companies.map(companyKey));
  const result: ContactDiscoveryResult = { contacts: settings.contacts.filter((contact) => wanted.has(companyKey(contact.company))), sourcesAttempted: 0, sourcesWorking: 0, errors: [] };
  for (const source of settings.sources.filter((item) => item.enabled && wanted.has(companyKey(item.company)))) {
    result.sourcesAttempted++;
    try {
      const response = await fetch(source.url, { signal: AbortSignal.timeout(20000), headers: { Accept: source.type === 'json' ? 'application/json' : 'text/html' } });
      if (!response.ok) throw new Error(`HTTP ${response.status}${[401, 403, 429].includes(response.status) ? ': login or platform restriction; no bypass attempted' : ''}`);
      const text = await response.text();
      if (text.length > 5_000_000) throw new Error('Contact source exceeds 5 MB.');
      if (/<title>[^<]*(?:captcha|sign in|login|access denied|just a moment)/i.test(text)) throw new Error('LOGIN_REQUIRED or BLOCKED_SECURITY');
      const contacts = source.type === 'json' ? z.array(contactSchema).parse(JSON.parse(text)) : parseStructuredContacts(text, source.company);
      result.contacts.push(...contacts.filter((contact) => companyKey(contact.company) === companyKey(source.company)));
      result.sourcesWorking++;
    } catch (error) { result.errors.push(`${source.name}: ${error instanceof Error ? error.message : String(error)}`); }
  }
  result.contacts = [...new Map(result.contacts.map((contact) => { const profileUrl = canonicalContactUrl(contact.profileUrl); return [profileUrl, { ...contact, profileUrl }]; })).values()];
  return result;
}
