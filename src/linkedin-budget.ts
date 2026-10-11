import type Database from 'better-sqlite3';

export type LinkedInLimits = { linkedin_daily_page_limit: number; linkedin_block_cooldown_hours: number; linkedin_min_seconds_between_pages?: number };
export type PageGuard = { allow(kind: string, detail?: string): boolean; reserve?(kind: string, detail?: string): Promise<boolean> };
export type BudgetStatus = 'OK' | 'EXHAUSTED' | 'BLOCKED';

/** Every LinkedIn page load goes through here; a guard that can pace itself is asked to, otherwise it is a plain yes/no. */
export const reservePage = (guard: PageGuard, kind: string, detail?: string): Promise<boolean> | boolean => guard.reserve ? guard.reserve(kind, detail) : guard.allow(kind, detail);

/** A block is a fact about one network location: a refusal seen from GitHub's servers says nothing about your own connection.
 * The daily page count, by contrast, belongs to the LinkedIn account and is shared everywhere the database travels. */
export const currentScope = (): string => (process.env.CI ? 'ci' : 'local');

/** Caps LinkedIn page loads per UTC day across every feature, spaces them out, and pauses LinkedIn after any login/security/rate-limit block seen from this environment. */
export class LinkedInBudget implements PageGuard {
  private lastPageAt = 0;
  constructor(private readonly db: Database.Database, private readonly limits: LinkedInLimits, private readonly clock: () => Date = () => new Date(), private readonly scope: string = currentScope(),
    private readonly sleep: (ms: number) => Promise<void> = (ms) => new Promise((resolve) => setTimeout(resolve, ms))) {
    this.db.prepare('DELETE FROM linkedin_actions WHERE day < ?').run(this.day(new Date(this.clock().getTime() - 30 * 86_400_000)));
  }
  private day(date: Date = this.clock()): string { return date.toISOString().slice(0, 10); }
  private state(key: string): string | undefined { return (this.db.prepare('SELECT value FROM linkedin_state WHERE key=?').get(key) as { value: string } | undefined)?.value; }
  private key(name: string): string { return `${name}:${this.scope}`; }

  used(): number { return (this.db.prepare('SELECT COUNT(*) n FROM linkedin_actions WHERE day=?').get(this.day()) as { n: number }).n; }
  /** A daily limit of 0 means no daily cap at all; the pacing and the pause after a block still apply. */
  unlimited(): boolean { return this.limits.linkedin_daily_page_limit === 0; }
  remaining(): number { return this.unlimited() ? Number.POSITIVE_INFINITY : Math.max(0, this.limits.linkedin_daily_page_limit - this.used()); }
  blockedUntil(): Date | undefined {
    const value = this.state(this.key('blocked_until'));
    const until = value ? new Date(value) : undefined;
    return until && until.getTime() > this.clock().getTime() ? until : undefined;
  }
  status(): BudgetStatus { return this.blockedUntil() ? 'BLOCKED' : this.remaining() <= 0 ? 'EXHAUSTED' : 'OK'; }

  /** Reserve one page load. Returns false, and records nothing, when LinkedIn is paused or today's budget is spent. */
  allow(kind: string, detail?: string): boolean {
    return this.db.transaction(() => {
      if (this.status() !== 'OK') return false;
      this.db.prepare('INSERT INTO linkedin_actions(day,kind,detail,created_at) VALUES(?,?,?,?)').run(this.day(), kind, detail ?? null, this.clock().toISOString());
      return true;
    })();
  }
  /** Like allow, but first waits until the minimum gap since the previous LinkedIn page has passed.
   * Requests fired seconds apart are what draws HTTP 429; a steady, unhurried pace does not. */
  async reserve(kind: string, detail?: string): Promise<boolean> {
    if (this.status() !== 'OK') return false;
    const wait = this.lastPageAt ? this.lastPageAt + (this.limits.linkedin_min_seconds_between_pages ?? 0) * 1000 - this.clock().getTime() : 0;
    if (wait > 0) await this.sleep(wait);
    const allowed = this.allow(kind, detail);
    if (allowed) this.lastPageAt = this.clock().getTime();
    return allowed;
  }
  recordBlock(reason: string): Date {
    const until = new Date(this.clock().getTime() + this.limits.linkedin_block_cooldown_hours * 3_600_000);
    const save = this.db.prepare('INSERT INTO linkedin_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value');
    this.db.transaction(() => { save.run(this.key('blocked_until'), until.toISOString()); save.run(this.key('blocked_reason'), reason); })();
    return until;
  }
  /** Clears this environment's pause, plus any pause recorded before pauses were scoped per environment. */
  clearBlock(): void { this.db.prepare('DELETE FROM linkedin_state WHERE key IN (?,?,?,?)').run(this.key('blocked_until'), this.key('blocked_reason'), 'blocked_until', 'blocked_reason'); }
  describe(): string {
    const until = this.blockedUntil();
    if (until) return `LinkedIn is paused until ${until.toISOString()} after: ${this.state(this.key('blocked_reason')) ?? 'a login or security block'}. Run \`login\` or \`linkedin-reset\` once you have resolved it.`;
    return this.unlimited() ? `LinkedIn pages loaded today: ${this.used()} (no daily limit)` : `LinkedIn page budget today: ${this.used()}/${this.limits.linkedin_daily_page_limit} used`;
  }
}
