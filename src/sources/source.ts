import type { Job } from '../core/types.js';
import type { Config } from '../config.js';

export interface JobSource {
  readonly name: string;
  discover(config: Config): Promise<Job[]>;
}

export class SourceRegistry {
  private readonly sources: JobSource[] = [];
  register(source: JobSource): this { this.sources.push(source); return this; }
  async discoverAll(config: Config): Promise<Job[]> {
    const batches = await Promise.all(this.sources.map((source) => source.discover(config)));
    return batches.flat();
  }
}
