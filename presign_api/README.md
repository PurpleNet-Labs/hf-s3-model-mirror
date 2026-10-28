# FastAPI pre-signed URL service

This is a minimal FastAPI app that returns short-lived S3 pre-signed URLs for downloading the mirrored model artifacts.

## Why

- Keep the S3 bucket private.
- Never ship AWS credentials to the mobile app.
- Your backend uses its IAM role to mint pre-signed URLs.

## Models Available

The service provides access to two types of models:

### STT Language Models
- **Path**: `models/indicconformer/{lang}/`
- **Languages**: Hindi (`hi`), Tamil (`ta`), Kannada (`kn`), Telugu (`te`), Malayalam (`ml`)
- **Format**: Single `.nemo` file per language

### LLM Models
- **Android (Gemma 3 1B)**: `models/llm/android/gemma3-1b/gemma3-1b-it-int4.task`
  - Single `.task` file (~1GB)
  - MediaPipe format
  
- **iOS (Qwen 2.5 0.5B)**: `models/llm/ios/qwen2.5-0.5b/`
  - Multiple files (`.safetensors`, config files)
  - MLX format

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r presign_api/requirements.txt

export AWS_REGION=ap-south-1
export AWS_PROFILE=your-profile   # optional (see AWS credentials below)
export API_KEY=change-me          # optional (see env vars below)

uvicorn presign_api.main:app --host 0.0.0.0 --port 8000 --reload
```

## AWS Credentials

The service needs AWS credentials to generate pre-signed URLs. Configure using one of these methods:

### Option 1: AWS Profile (recommended for local development)

```bash
# List available profiles
aws configure list-profiles

# Set profile for the session
export AWS_PROFILE=your-profile-name
```

### Option 2: AWS Access Keys (for environments without profiles)

```bash
export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
export AWS_REGION=ap-south-1
```

**Security Note**: Never commit access keys to version control. Use environment variables or `.env` files (which are gitignored).

### Option 3: IAM Role (recommended for production/EC2)

Best practice for production: attach an instance profile/role with permissions:

- `s3:GetObject` on `arn:aws:s3:::glycosense-models-v1/models/indicconformer/*`
- `s3:GetObject` on `arn:aws:s3:::glycosense-models-v1/models/llm/*`

Then you do not need static access keys on the server. boto3 will automatically use the instance role.

### Option 4: .env file (local development)

Create a `.env` file in the `presign_api/` directory:

```bash
# AWS Configuration
AWS_REGION=ap-south-1
AWS_PROFILE=your-profile
# OR
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

## Environment variables

- `S3_BUCKET` (default: `glycosense-models-v1`)
- `S3_PREFIX_BASE` (default: `models/indicconformer`)
- `S3_LLM_PREFIX_BASE` (default: `models/llm`)
- `PRESIGN_TTL_SECONDS` (default: `900`)

### Authentication

The service does not verify tokens and holds no JWT secret. Clients call
`/downloader/v1/...` on the public API host. rhmp-services' Envoy (`:8041`) puts every request
except `/downloader/v1/health` through ext_authz: auth-service checks the bearer token and the
license tier (Basic and up), then Envoy sets `X-User-Id` and `X-User-Role`. Client-sent copies
are stripped first. The service requires `X-User-Id` and answers 401 without it.

That only holds while the service is reachable through rhmp's Envoy alone, so uvicorn and this
service's Envoy (`:8060`) both listen on `127.0.0.1`. Do not change either to a routable address.

Before Oct 2026 the service verified tokens itself with `AUTH_JWT_SECRET`. That copy drifted
when rhmp rotated its secret, and every request failed with 401.

To call it locally, set the header yourself:

```bash
curl -H "X-User-Id: 1" http://127.0.0.1:3060/downloader/v1/models
```

## Endpoints

### STT Language Models

- **`GET /v1/models`** - List all available STT language models
  
  Response:
  ```json
  {
    "bucket": "glycosense-models-v1",
    "prefix_base": "models/indicconformer",
    "models": [
      {
        "lang": "hi",
        "bucket": "glycosense-models-v1",
        "key": "models/indicconformer/hi/indicconformer_stt_hi_hybrid_rnnt_large.nemo"
      }
    ]
  }
  ```

- **`GET /v1/models/{lang}`** - Get pre-signed URL for specific language
  
  Where `lang` is one of: `hi`, `ta`, `kn`, `te`, `ml`
  
  Response:
  ```json
  {
    "bucket": "glycosense-models-v1",
    "key": "models/indicconformer/hi/indicconformer_stt_hi_hybrid_rnnt_large.nemo",
    "url": "https://glycosense-models-v1.s3.amazonaws.com/...",
    "expires_at": "2026-01-30T12:00:00+00:00"
  }
  ```

### LLM Models

- **`GET /v1/llm-models`** - List all available LLM models
  
  Response:
  ```json
  {
    "bucket": "glycosense-models-v1",
    "prefix_base": "models/llm",
    "models": [
      {
        "os": "android",
        "model_name": "gemma3-1b",
        "bucket": "glycosense-models-v1",
        "key": "models/llm/android/gemma3-1b/gemma3-1b-it-int4.task",
        "is_directory": false
      },
      {
        "os": "ios",
        "model_name": "qwen2.5-0.5b",
        "bucket": "glycosense-models-v1",
        "key": "models/llm/ios/qwen2.5-0.5b/",
        "is_directory": true
      }
    ]
  }
  ```

- **`GET /v1/llm-models/{os}/{model_name}`** - Get pre-signed URL for specific LLM model
  
  Where:
  - `os` is one of: `android`, `ios`
  - `model_name` is one of: `gemma3-1b` (Android), `qwen2.5-0.5b` (iOS)
  
  Examples:
  - `GET /v1/llm-models/android/gemma3-1b` - Returns presigned URL for single file
  - `GET /v1/llm-models/ios/qwen2.5-0.5b` - Returns directory info (client must list objects)
  
  Response for single file (Android):
  ```json
  {
    "bucket": "glycosense-models-v1",
    "key": "models/llm/android/gemma3-1b/gemma3-1b-it-int4.task",
    "url": "https://glycosense-models-v1.s3.amazonaws.com/...",
    "expires_at": "2026-01-30T12:00:00+00:00",
    "is_directory": false
  }
  ```
  
  Response for directory (iOS):
  ```json
  {
    "bucket": "glycosense-models-v1",
    "key": "models/llm/ios/qwen2.5-0.5b/",
    "url": "https://glycosense-models-v1.s3.amazonaws.com/models/llm/ios/qwen2.5-0.5b/",
    "expires_at": "2026-01-30T12:00:00+00:00",
    "is_directory": true
  }
  ```

## Example Usage

### Without Authentication

```bash
# List STT models
curl http://localhost:8000/v1/models

# Get presigned URL for Hindi STT model
curl http://localhost:8000/v1/models/hi

# List LLM models
curl http://localhost:8000/v1/llm-models

# Get presigned URL for Android Gemma model
curl http://localhost:8000/v1/llm-models/android/gemma3-1b
```

### With JWT Authentication (Recommended)

```bash
# Set your JWT token
export JWT_TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."

# List STT models
curl -H "Authorization: Bearer $JWT_TOKEN" http://localhost:8000/v1/models

# Get presigned URL for Hindi STT model
curl -H "Authorization: Bearer $JWT_TOKEN" http://localhost:8000/v1/models/hi

# List LLM models
curl -H "Authorization: Bearer $JWT_TOKEN" http://localhost:8000/v1/llm-models

# Get presigned URL for iOS Qwen model
curl -H "Authorization: Bearer $JWT_TOKEN" http://localhost:8000/v1/llm-models/ios/qwen2.5-0.5b
```

### With API Key Authentication (Legacy)

```bash
# List STT models
curl -H "X-API-Key: change-me" http://localhost:8000/v1/models

# Get presigned URL for Hindi STT model
curl -H "X-API-Key: change-me" http://localhost:8000/v1/models/hi

# List LLM models
curl -H "X-API-Key: change-me" http://localhost:8000/v1/llm-models

# Get presigned URL for iOS Qwen model
curl -H "X-API-Key: change-me" http://localhost:8000/v1/llm-models/ios/qwen2.5-0.5b
```

### Error Responses

**401 Unauthorized** - Missing or invalid authentication:
```json
{
  "detail": "Token has expired"
}
```

**401 Unauthorized** - Invalid token format:
```json
{
  "detail": "Invalid authorization header format. Expected: Bearer <token>"
}
```

**401 Unauthorized** - Invalid issuer:
```json
{
  "detail": "Invalid token issuer"
}
```
