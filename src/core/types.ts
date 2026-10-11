export type JobStatus = 'NEW' | 'REVIEW' | 'SKIPPED' | 'DUPLICATE' | 'APPLICATION_READY' | 'NEEDS_USER_INPUT' | 'FAILED' | 'PAUSED' | 'SUBMITTED' | 'BLOCKED_SECURITY' | 'LOGIN_REQUIRED' | 'ALREADY_APPLIED';
export type ApplicationStatus = 'EXCLUDED' | 'CLOSED_JOB' | 'UNSUPPORTED_ATS' | 'VALIDATION_FAILURE' | 'RETRYABLE_TECHNICAL_FAILURE' | 'SUBMISSION_UNCONFIRMED' | 'READY_FOR_REVIEW' | 'IN_PROGRESS' | 'READY_FOR_SUBMISSION' | 'SUBMITTING' | 'SUBMITTED' | 'NEEDS_USER_INPUT' | 'FAILED' | 'PAUSED' | 'BLOCKED_SECURITY' | 'LOGIN_REQUIRED' | 'ALREADY_APPLIED';
export type OutreachType = 'CONNECTION' | 'INMAIL';

export type Job = {
  id?: number;
  company: string;
  title: string;
  location: string;
  workMode: string;
  jobUrl: string;
  source: string;
  postedAt: string;
  description: string;
  applicationUrl?: string;
  externalId?: string;
  atsJobId?: string;
  discoveredAt?: string;
  metadata?: Record<string, unknown>;
};

export type CandidateAnswer = { value: string; confidence: 'verified' | 'configured' };
export type FormField = { label: string; name?: string; required: boolean; value?: string; key?: string; type?: string; options?: Array<{ label: string; value: string }>; valid?: boolean; validationMessage?: string; classification?: 'KNOWN' | 'MISSING' | 'SENSITIVE' | 'SECURITY'; answer?: string; dialCode?: string };
export type ApplicationContext = { jobId: number; job: Job; resumePath: string; candidateAnswers: Record<string, CandidateAnswer> };
export type ApplicationInspection = { fields: FormField[]; confirmationText?: string; blockedReason?: string; screenshotPath?: string; fieldsFilled?: number; resumeUploaded?: boolean; errors?: string[]; identityVerified?: boolean; finalUrl?: string; optionalBlank?: string[] };

export type Contact = { company: string; name: string; title: string; profileUrl: string; relevanceScore: number };
export type OutreachDraft = { type: OutreachType; recipient: Contact; message: string; characterCount: number };
export type OutreachStatus = 'OUTREACH_READY' | 'READY_TO_SEND' | 'SENT' | 'ALREADY_CONNECTED' | 'PENDING' | 'NEEDS_USER_INPUT' | 'BLOCKED_SECURITY' | 'LOGIN_REQUIRED' | 'FAILED' | 'DECLINED';
