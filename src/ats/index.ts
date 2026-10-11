import { AtsRegistry } from './adapter.js';
import { GreenhouseAdapter } from './greenhouse.js';
import { LeverAdapter } from './lever.js';
import { AshbyAdapter } from './ashby.js';
import { WorkdayAdapter } from './workday.js';
import { GenericAtsAdapter } from './generic.js';
import { LinkedInEasyApplyAdapter } from './linkedin-easy-apply.js';

export function defaultAtsRegistry(): AtsRegistry {
  return new AtsRegistry([new GreenhouseAdapter(), new LeverAdapter(), new AshbyAdapter(), new WorkdayAdapter(), new LinkedInEasyApplyAdapter(), new GenericAtsAdapter()]);
}
