import type { Page } from 'playwright';
import type { ApplicationContext, ApplicationInspection } from '../core/types.js';

export interface AtsAdapter {
  readonly name: string;
  matches(url: string): boolean;
  prepare(page: Page, context: ApplicationContext): Promise<void>;
  /** Some sites show a "you already applied" state without any form; an adapter that can tell says so here. */
  alreadyApplied?(page: Page): Promise<boolean>;
  inspect(page: Page, context: ApplicationContext): Promise<ApplicationInspection>;
  fillKnownFields(page: Page, context: ApplicationContext): Promise<ApplicationInspection>;
  validate(page: Page, context: ApplicationContext): Promise<ApplicationInspection>;
  readyToSubmit?(page: Page): Promise<boolean>;
  submit(page: Page, context: ApplicationContext): Promise<ApplicationInspection>;
  captureResult(page: Page, context: ApplicationContext): Promise<ApplicationInspection>;
}

export class AtsRegistry {
  constructor(private readonly adapters: AtsAdapter[]) {}
  forUrl(url: string): AtsAdapter | undefined { return this.adapters.find((adapter) => adapter.matches(url)); }
  forPage(page: Page): AtsAdapter | undefined {
    for (const frame of page.frames()) {
      const adapter = this.adapters.find((candidate) => candidate.name !== 'generic' && candidate.matches(frame.url()));
      if (adapter) return adapter;
    }
    return this.forUrl(page.url());
  }
}
