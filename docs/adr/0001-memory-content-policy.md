# Shared memory accepts only shareable knowledge

The Answer Cache is shared across all sessions and users, and cached question text is user-generated — so caching a question containing personal data would serve one user's PII to another and place it beyond URL-based erasure. We decided that preflight classifies every query for PII (`contains_pii`, failing closed), and flagged turns are answered normally but never written to the Answer Cache; `volatile`, `degraded`, and `refused` turns are likewise never cached, while every other validated synthesis — chunk-tier included — is promoted into the cache. The Knowledge Base is exempt from the gate because it ingests only public web content.

## Considered Options

- **Do nothing beyond documenting the risk** — ship the shared cache storing whatever users type, and merely note the PII-leakage risk in the security docs as a POC limitation. Rejected: leaves cross-session PII exposure in place and makes the GDPR posture incoherent.
- **Per-session cache namespacing** — give each session a private Answer Cache so nothing leaks across users. Rejected: eliminates cross-user reuse, which is the cache's entire economic point.
- **PII gate on cache writes (chosen)** — PII-flagged questions are answered but never written to the shared cache. One extra field in an existing utility-model call; erasure completed by `forget --question` alongside `forget --url`.
