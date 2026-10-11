"""One definition of fresh, policy-current scoring backlog."""
from .matcher import policy_version, rule_match
from .roles import classify, candidates
from .store import row_to_job


def pending_jobs(rows, profile, llm_cfg, max_age_days=None, *, store=None):
    pv = policy_version(profile, llm_cfg)
    pending = []
    for row in rows:
        row = dict(row)
        job = row_to_job(row)
        if max_age_days is not None and job.age_days is not None and job.age_days > float(max_age_days):
            continue
        job.apply_classification(classify(job, profile))
        changed = row.get("content_hash") != job.content_hash(pv)
        if store is not None and changed:
            # Persist demotions as well as candidates: an old high-scoring
            # non-QA role must not remain in the digest after policy changes.
            rule_match(job, profile)
            store.record_evaluation(job, pv)
        if not candidates([job], qa_only=profile.qa_roles_only):
            continue
        if row.get("scored_by") in ("llm", "cache") and not changed:
            continue
        rule_match(job, profile)
        pending.append(job)
    return sorted(pending, key=lambda j: (-j.base_score, j.freshness[0], j.fingerprint))
