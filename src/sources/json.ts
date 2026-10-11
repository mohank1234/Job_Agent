import { readFile } from 'node:fs/promises';
import type { Config } from '../config.js';
import type { Job } from '../core/types.js';
import type { JobSource } from './source.js';

export class JsonJobSource implements JobSource {
  readonly name = 'json-feed';
  constructor(private readonly path: string) {}
  async discover(_config: Config): Promise<Job[]> {
    return JSON.parse(await readFile(this.path, 'utf8')) as Job[];
  }
}
