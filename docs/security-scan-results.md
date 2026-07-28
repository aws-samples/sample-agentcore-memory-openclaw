# Local Security Pre-Scan Results

> Evidence for the PCSR / AppSec review. These are **local pre-scans** run before
> the authoritative CRUX / q-scanner / acat / Probe / Slingshot scans in
> GitFarm/GitLab. They exist to catch High/Critical findings early. Re-run the
> authoritative scanners in the review pipeline and attach their exports too.
>
> Environment: Python 3.11 venv. Commands are reproducible from the repo root.

## Summary

| Scanner | Scope | High/Critical | Result |
|---------|-------|---------------|--------|
| bandit 1.9.x | `telegram-webhook/`, `agent-container/` (excl. tests) | **0** | 2 Medium, 10 Low — all dispositioned below |
| cfn-lint | `openclaw-telegram.yaml` | **0** | 1 Warning (W1030) — expected, dispositioned |
| detect-secrets | all tracked files + history check | **0** | 9 "Secret Keyword" hits — all false positives |
| pip-audit | `boto3>=1.40.0`, `requests>=2.32.0` | **0** | No known vulnerabilities |

**No High or Critical findings.** All Medium/Low/Warning items are documented
false positives or intentional, sample-scope design decisions.

## bandit (Python SAST)

```
bandit -r telegram-webhook agent-container \
  -x '*/tests/*,*/.pytest_cache/*,*/.hypothesis/*' -ll
```

Totals: High **0**, Medium **2**, Low **10** (2210 LOC scanned).

| ID | Location | Finding | Disposition |
|----|----------|---------|-------------|
| B104 | `agent-container/server.py:1398` | Binding to `0.0.0.0` | **Accepted / required.** The AgentCore Runtime container contract requires the server to listen on `0.0.0.0:8080` inside the isolated microVM. The container network is not directly internet-exposed; access is via the AgentCore data plane. |
| B108 | `agent-container/server.py:984` | Insecure temp dir (`/tmp/sprout-workspace`) | **Accepted.** Ephemeral per-invocation workspace inside the isolated container/Lambda `/tmp`; not multi-tenant on-host. Contents are per-`chat_id` and re-synced from S3. |
| B110/B112 ×10 | various | `try/except/pass` / continue (Low) | **Accepted.** Deliberate graceful-degradation paths (memory retrieval, workspace I/O, Telegram delivery) documented in code; failures are logged and swallowed by design so a user still gets a response. |

> Optional: annotate the two Medium lines with `# nosec B104` / `# nosec B108`
> plus the justification above for a zero-finding scanner run.

## cfn-lint (IaC)

```
cfn-lint openclaw-telegram.yaml
```

| ID | Location | Finding | Disposition |
|----|----------|---------|-------------|
| W1030 | `AgentCoreRuntime` `ContainerUri` | Default value doesn't match the private-ECR URI regex | **Expected.** The default is a non-functional `public.ecr.aws/<ECR_PUBLIC_ALIAS>/...` placeholder (this sample publishes no prebuilt image); `deploy.sh` overrides it with the private ECR URI it builds and pushes. Warning only, no error. |

## detect-secrets

```
detect-secrets scan --all-files (excluding .git, caches, .env)
```

- `.env` is **untracked and gitignored** (verified with `git ls-files` +
  `git check-ignore`); the real bot token never enters the repo or its history.
- The 9 hits in tracked files are all **"Secret Keyword"** heuristic matches on
  variable names / IAM action strings / test fixtures — **no secret values**:

| File:line | Content | Why false positive |
|-----------|---------|--------------------|
| `openclaw-telegram.yaml:245` | `secretsmanager:GetSecretValue` | IAM action string |
| `scripts/deploy.sh:413` | `webhook_secret_arn` null check | variable name |
| `cron_handler.py:50`, `handler.py:70/76/84` | `*_SECRET_ARN`/`*_SECRET_TOKEN` env-var name constants | identifiers, not values |
| `tests/test_handler.py:40` | `arn:...:111122223333:secret:bot-token` | dummy ARN, placeholder account |
| `tests/test_handler.py:174`, `test_cron_handler.py:37` | `"WRONG-TOKEN"` etc. | literal test fixtures |

## pip-audit (dependency CVEs)

```
pip-audit -r telegram-webhook/requirements.txt
```

`No known vulnerabilities found` for the resolved `boto3` / `requests` tree.
Both `requirements.txt` files declare the same two runtime dependencies.

## Still to run in the review pipeline

- `checkov -f openclaw-telegram.yaml` (deeper IaC policy checks)
- `gitleaks detect` over full git history (belt-and-suspenders vs. detect-secrets)
- Container image scan of the built arm64 image (ECR enhanced scanning / Inspector / `trivy`)
- CRUX (q-scanner + acat) in GitFarm, or Probe in GitLab, or Slingshot upload
