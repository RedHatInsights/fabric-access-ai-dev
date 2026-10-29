---
description: Implements cloudwatch-aggregator changes in the Python/Flask logging proxy service.
mode: subagent
model: rehor-openai/gpt-6-luna
request:
  body:
    reasoning:
      effort: medium
permissions:
  - { action: subagent, resource: "*", effect: deny }
---

## cloudwatch-aggregator Guidelines

You are working on **cloudwatch-aggregator**, a Python 3.13/Flask REST service
(pipenv) that batches log data to AWS CloudWatch and Splunk over HTTP POST and
forwards it non-blockingly (gunicorn + gevent in production).

- App logic lives in three files under `app/`: `app/__init__.py` (app factory,
  strict_slashes disabled), `app/log.py` (routes `/ping` and
  `/log/<log_stream>`, CloudWatch/Splunk handler setup, `Cache` class),
  `app/utils.py` (`truthy_string()` helper).
- No test suite exists yet. When adding tests, use pytest with Flask's test
  client, mock `boto3.Session` and `SplunkHandler`, and control configuration
  with `unittest.mock.patch.dict(os.environ, ...)`. `truthy_string()` is a good
  first unit-test candidate.
- Run `pipenv install --dev` and `pipenv run pre-commit run --all-files`.
- Configuration is env-var based and Clowder-aware: `app-common-python`
  `LoadedConfig` when `isClowderEnabled()`, plain env vars otherwise. Required
  for CloudWatch: `AWS_LOG_GROUP`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `AWS_REGION_NAME`. Feature flags `LOG_TO_CLOUDWATCH`, `LOG_TO_SPLUNK` use
  `truthy_string()`; `CLOUD_WATCH_ALLOWED_STREAMS` is a comma-separated
  stream allowlist (unlisted streams get 403).
- Pitfalls: `Cache.allowed_streams` is read at import time; `boto_client` is
  created at module level when `LOG_TO_CLOUDWATCH` is true (tests importing
  `app.log` must mock boto3 without AWS credentials); `Cache.lock` guards
  handler creation per stream.
- CI: Konflux/Tekton pipelines under `.tekton/` plus GitHub Actions pre-commit,
  labeler, JSON/YAML validation, Quay image check, and Grype/Syft security scan.
