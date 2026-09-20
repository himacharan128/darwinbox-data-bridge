# Running and deploying

## Locally, for development

```bash
uv sync && pnpm install
make run           # builds the UI, starts the destination and the API
```

→ **http://127.0.0.1:8080**. No database to start, no cloud account, no credentials.
The model runs from recorded replays committed under `tests/fixtures/model-cache`.

## Locally, in containers

The same thing a hosted deployment runs:

```bash
make stack         # docker compose up --build
make stack-down    # and tear it down, volumes included
```

The destination is on an internal network only — the API reaches it, you cannot.
That is deliberate: a destination you can reach around is not one you have
integrated with.

## With the live model

Everything works offline from replays. To call the real model, put credentials
**outside the repository** and let direnv load them:

```bash
mkdir -p ~/.darwinbox-agent && chmod 700 ~/.darwinbox-agent
cat > ~/.darwinbox-agent/.env <<'ENV'
OPENAI_BASE_URL=https://bedrock-runtime.ap-south-1.amazonaws.com/openai/v1
OPENAI_API_KEY=<bedrock long-term api key>
MODEL_ID=openai.gpt-oss-120b-1:0
AWS_REGION=ap-south-1
ENV
chmod 600 ~/.darwinbox-agent/.env
cp .envrc.example .envrc && direnv allow
```

`.env`, `.envrc` and `Credentials/` are gitignored, and a pre-commit hook scans
staged changes for key material. The repository is public; that guard is commit #1.

### Minting the key

Bedrock long-term API keys are IAM service-specific credentials, so no console is
needed. Create a principal that can do exactly one thing:

```bash
aws iam create-user --user-name dbx-migration-agent

cat > /tmp/policy.json <<'JSON'
{ "Version": "2012-10-17", "Statement": [
  { "Sid": "BearerTokenAuth", "Effect": "Allow",
    "Action": "bedrock:CallWithBearerToken", "Resource": "*" },
  { "Sid": "InvokeGptOssOnly", "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": [
      "arn:aws:bedrock:ap-south-1::foundation-model/openai.gpt-oss-120b-1:0",
      "arn:aws:bedrock:ap-south-1::foundation-model/openai.gpt-oss-20b-1:0"] } ] }
JSON

aws iam put-user-policy --user-name dbx-migration-agent \
  --policy-name dbx-bedrock-invoke --policy-document file:///tmp/policy.json

aws iam create-service-specific-credential --user-name dbx-migration-agent \
  --service-name bedrock.amazonaws.com --credential-age-days 90
```

Two things that cost time if you don't know them:

- The secret is returned in `ServiceCredentialSecret`, not `ServiceApiKeyValue`. It
  is shown **once**.
- `bedrock:CallWithBearerToken` must be scoped to `Resource: "*"`. Scoping it to a
  model ARN returns 401 with a message about the key being invalid, which points at
  entirely the wrong problem.

### Another provider

Forced tool use is the same pattern on any OpenAI-compatible endpoint, so Groq,
Cerebras, Together and a local Ollama all work by changing two variables:

```bash
OPENAI_BASE_URL=https://api.groq.com/openai/v1
MODEL_ID=openai/gpt-oss-120b
```

## Hosting it

The image serves the API and the built UI on one port, so anything that runs a
container works — Fly, Render, Railway, Cloud Run, ECS:

```bash
docker build -t dbx-data-bridge .
docker run -p 8080:8080 \
  -e MOCK_TARGET_URL=http://mock-target:8081 \
  -e OPENAI_API_KEY=... -e AWS_BEARER_TOKEN_BEDROCK=... \
  dbx-data-bridge
```

State lives in `DBX_DATA_DIR` (SQLite by default). Set `DATABASE_URL` for Postgres
where a single writable volume is not available.

**Cost note.** The only paid dependency is model inference, roughly a tenth of a cent
per run at current gpt-oss pricing, and nothing at all when running from replays.

## Optional: live OCR

Scanned PDFs read from OCR output recorded beside them, so the demo and tests need
nothing extra. To re-read a scan live:

```bash
uv pip install "python-doctr" torch torchvision
```

Roughly 2 GB of wheels, which is why it is optional rather than a dependency.
