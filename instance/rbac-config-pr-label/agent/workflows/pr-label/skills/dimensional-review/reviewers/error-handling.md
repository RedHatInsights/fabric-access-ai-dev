# Error handling review

Check whether failures are surfaced, translated, retried, and cleaned up correctly. Look for swallowed exceptions, incorrect status or error formats, lost context, retry loops without limits or backoff, missing rollback or resource cleanup, and behavior that contradicts callers' expectations. Report only concrete failure paths.
