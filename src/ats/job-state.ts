/** Explicit posting notices only; ordinary job-description words are not closure evidence. */
export function isClosedJob(text: string): boolean {
  return /\b(?:not currently|no longer) accepting applications\b|\bapplications (?:are |have been )?closed\b|\b(?:this )?job is no longer available\b|\b(?:the )?position has been filled\b|\b(?:job|posting|requisition) (?:has )?(?:expired|removed|closed)\b|\bposting (?:was |has been )?removed\b|\b(?:this )?role is closed\b|\bjob no longer exists\b/i.test(text);
}
export function isAlreadyApplied(text: string): boolean {
  return /\b(?:already applied|you applied on|you have applied (?:for|to) this (?:job|position)|application (?:already )?(?:exists|submitted)|previously submitted)\b/i.test(text);
}
