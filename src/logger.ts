import { appendFile, mkdir } from 'node:fs/promises';

export async function logEvent(root: string, event: Record<string, unknown>): Promise<void> {
  await mkdir(`${root}/logs`, { recursive: true });
  await appendFile(`${root}/logs/events.jsonl`, `${JSON.stringify({ timestamp: new Date().toISOString(), ...event })}\n`);
  console.log(`[${String(event.stage ?? 'EVENT').toUpperCase()}] ${JSON.stringify(event)}`);
}
