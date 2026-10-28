from __future__ import annotations

import logging
import logging.handlers
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import boto3
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
import time


Language = Literal["hi", "ta", "kn", "te", "ml"]
ALL_LANGS: tuple[Language, ...] = ("hi", "ta", "kn", "te", "ml")

OS = Literal["android", "ios"]
ModelName = Literal["gemma3-1b", "qwen2.5-0.5b", "gemma4-e4b", "gemma4-e2b"]

# Map OS to model names. The legacy gemma3-1b/qwen2.5-0.5b entries stay until
# clients in the field have updated - they still request those keys.
OS_MODELS: dict[OS, tuple[ModelName, ...]] = {
    "android": ("gemma3-1b", "gemma4-e4b", "gemma4-e2b"),
    "ios": ("qwen2.5-0.5b", "gemma4-e4b", "gemma4-e2b"),
}

Tier = Literal["e4b", "e2b"]

# Largest first - _resolve_tier picks the first tier the device satisfies.
TIERS: tuple[Tier, ...] = ("e4b", "e2b")

TIER_MODELS: dict[Tier, ModelName] = {
    "e4b": "gemma4-e4b",
    "e2b": "gemma4-e2b",
}


class Settings(BaseSettings):
    # When running from /opt/presign-api with WorkingDirectory=/opt/presign-api
    # the .env file is at presign_api/.env
    model_config = SettingsConfigDict(
        env_file="presign_api/.env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # AWS Configuration
    aws_region: str = "ap-south-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""

    # S3 location
    s3_bucket: str = "glycosense-models-v1"
    s3_prefix_base: str = "models/indicconformer"
    s3_llm_prefix_base: str = "models/llm"

    # Pre-signed URL TTL
    presign_ttl_seconds: int = 900

    # On-device model tier policy. Env-overridable so thresholds can be retuned
    # by restarting the service, without shipping a new app build. The RAM cutoffs
    # are initial guesses - the resolve decision logs exist to correct them.
    tier_e4b_min_ram_bytes_android: int = 11 * 1024**3
    tier_e2b_min_ram_bytes_android: int = 7 * 1024**3
    tier_e4b_min_ram_bytes_ios: int = 6 * 1024**3
    tier_e2b_min_ram_bytes_ios: int = 5 * 1024**3
    ios_min_gpu_family: int = 7  # Apple GPU family 7 = A14+; mirrors the native Metal gate
    android_required_abi: str = "arm64-v8a"
    # iOS downloads a zip and extracts it, so it needs the artifact size roughly
    # twice over plus slack. Applied on both platforms for simplicity.
    tier_disk_headroom_multiplier: float = 2.5

    # No JWT settings: rhmp-services' Envoy authenticates every request (ext_authz) and sets
    # X-User-Id / X-User-Role. See _require_auth.

    # Logging configuration
    log_level: str = "info"
    log_file_path: str = "./logs/presign-api.log"
    log_max_size_mb: int = 100  # MB
    log_max_backups: int = 5
    log_max_age_days: int = 30
    log_format: str = "json"  # json or text
    environment: str = "production"


settings = Settings()


# Configure structured logging
def setup_logging():
    """Configure structured logging with rotation similar to care service."""
    # Create logs directory if it doesn't exist
    log_dir = Path(settings.log_file_path).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    # Set log level
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Create custom formatter for JSON-like structured logs
    class StructuredFormatter(logging.Formatter):
        def format(self, record):
            log_data = {
                "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname.lower(),
                "service": "presign-api",
                "version": "1.0",
                "environment": settings.environment,
                "message": record.getMessage(),
            }

            # Add extra fields if present
            if hasattr(record, "trace_id"):
                log_data["trace_id"] = record.trace_id
            if hasattr(record, "request_id"):
                log_data["request_id"] = record.request_id
            if hasattr(record, "user_id"):
                log_data["user_id"] = record.user_id
            if hasattr(record, "duration_ms"):
                log_data["duration_ms"] = record.duration_ms
            if hasattr(record, "status_code"):
                log_data["status_code"] = record.status_code
            if hasattr(record, "method"):
                log_data["method"] = record.method
            if hasattr(record, "path"):
                log_data["path"] = record.path
            if hasattr(record, "ip"):
                log_data["ip"] = record.ip

            # Add exception info if present
            if record.exc_info:
                log_data["exception"] = self.formatException(record.exc_info)

            if settings.log_format == "json":
                import json
                return json.dumps(log_data)
            else:
                # Text format for development
                msg = f"{log_data['timestamp']} [{log_data['level'].upper()}] {log_data['message']}"
                if "trace_id" in log_data:
                    msg += f" [trace_id={log_data['trace_id']}]"
                return msg

    # Configure root logger
    logger = logging.getLogger()
    logger.setLevel(log_level)
    logger.handlers.clear()

    # Console handler (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(StructuredFormatter())
    logger.addHandler(console_handler)

    # File handler with rotation (similar to lumberjack)
    file_handler = logging.handlers.RotatingFileHandler(
        filename=settings.log_file_path,
        maxBytes=settings.log_max_size_mb * 1024 * 1024,  # Convert MB to bytes
        backupCount=settings.log_max_backups,
        encoding="utf-8"
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(StructuredFormatter())
    logger.addHandler(file_handler)

    return logger


# Initialize logging
logger = setup_logging()
logger.info("Presign API service starting up...")

app = FastAPI(
    title="Model Presign API",
    version="1.0",
    description="Pre-signed URL generator for model downloads"
)


# Request logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all HTTP requests with distributed tracing support."""
    start_time = time.time()

    # Generate IDs for distributed tracing
    request_id = str(uuid.uuid4())
    trace_id = request.headers.get("X-Trace-ID", str(uuid.uuid4()))

    # Store in request state for use in endpoints
    request.state.request_id = request_id
    request.state.trace_id = trace_id

    # Process request
    try:
        response = await call_next(request)
    except Exception as e:
        logger.error(
            f"Request failed: {str(e)}",
            extra={
                "trace_id": trace_id,
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "ip": request.client.host if request.client else "unknown",
            },
            exc_info=True
        )
        raise

    # Calculate duration
    duration_ms = int((time.time() - start_time) * 1000)

    # Log request
    logger.info(
        f"{request.method} {request.url.path} - {response.status_code}",
        extra={
            "trace_id": trace_id,
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "ip": request.client.host if request.client else "unknown",
            "user_agent": request.headers.get("user-agent", "unknown"),
        }
    )

    # Add trace headers to response
    response.headers["X-Trace-ID"] = trace_id
    response.headers["X-Request-ID"] = request_id

    return response


def _require_auth(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
) -> dict:
    """
    Require the identity that rhmp-services' Envoy sets after ext_authz.

    ext_authz (auth-service /auth/validate-token) has already checked the bearer token and the
    caller's license tier, and Envoy strips any client-sent X-User-* first (D-325.1). This service
    holds no JWT secret, so a rotation in rhmp-services can't make it reject valid tokens (staging,
    27 Oct 2026). That is only safe because uvicorn and this service's Envoy listen on 127.0.0.1.

    Returns:
        dict: user_id (int) and role, for logging

    Raises:
        HTTPException: 401 if the request carries no gateway identity
    """
    if not x_user_id or not x_user_id.isdigit() or int(x_user_id) <= 0:
        logger.warning("Request has no gateway identity - it did not come through rhmp Envoy ext_authz")
        raise HTTPException(status_code=401, detail="Authentication required")
    return {"user_id": int(x_user_id), "role": x_user_role}


def _model_key(lang: Language) -> str:
    # Keep this mapping server-side so clients can't request arbitrary keys.
    # Layout mirrors what mirror_5langs.sh uploads.
    prefix = settings.s3_prefix_base.strip("/")
    return f"{prefix}/{lang}/indicconformer_stt_{lang}_hybrid_rnnt_large.nemo"


def _llm_model_key(os: OS, model: ModelName) -> str:
    # LLM model key mapping for models/llm/{os}/{model}/
    # Android runs LiteRT-LM and takes the .litertlm file directly.
    # iOS runs MLX and takes the whole HF repo as a zip.
    prefix = settings.s3_llm_prefix_base.strip("/")

    if os == "android":
        if model == "gemma3-1b":
            return f"{prefix}/{os}/{model}/gemma3-1b-it-int4.task"
        if model == "gemma4-e4b":
            return f"{prefix}/{os}/{model}/gemma-4-E4B-it.litertlm"
        if model == "gemma4-e2b":
            return f"{prefix}/{os}/{model}/gemma-4-E2B-it.litertlm"
    elif os == "ios":
        if model == "qwen2.5-0.5b":
            return f"{prefix}/{os}/{model}/qwen2.5-0.5b.zip"
        if model in ("gemma4-e4b", "gemma4-e2b"):
            return f"{prefix}/{os}/{model}/{model}.zip"

    raise ValueError(f"Unknown model configuration: {os}/{model}")


def _s3_client():
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


def _presign(key: str) -> tuple[str, str]:
    """Generate a pre-signed GET URL for a bucket key.

    Returns (url, expires_at_iso).
    """
    s3 = _s3_client()

    try:
        url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": settings.s3_bucket, "Key": key},
            ExpiresIn=settings.presign_ttl_seconds,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate presigned URL: {e}")

    expires_at = datetime.now(timezone.utc).timestamp() + settings.presign_ttl_seconds
    return url, datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()


def _object_size(key: str) -> int | None:
    """Exact object size, so the client can pre-check disk and verify the download.

    Returns None if the object cannot be statted. A tier whose artifact is not in
    the bucket yet is treated as unavailable rather than as an error, so a partially
    mirrored bucket degrades to the smaller tier instead of failing every resolve.
    """
    try:
        return _s3_client().head_object(Bucket=settings.s3_bucket, Key=key)["ContentLength"]
    except Exception as e:
        logger.warning(f"Could not stat s3://{settings.s3_bucket}/{key}: {e}")
        return None


def _resolve_tier(
    os: OS,
    ram_bytes: int,
    free_disk_bytes: int,
    gpu_family: int | None,
    abi: str | None,
) -> tuple[Tier, int, str] | None:
    """Pick the largest tier this device can run, or None if it can run none.

    Returns (tier, size_bytes, reason) on success; None means "use cloud inference".
    """
    # Hard gates first - these disqualify the device regardless of RAM.
    if os == "ios":
        if gpu_family is None or gpu_family < settings.ios_min_gpu_family:
            return None
    else:
        if abi != settings.android_required_abi:
            return None

    if os == "android":
        min_ram = {
            "e4b": settings.tier_e4b_min_ram_bytes_android,
            "e2b": settings.tier_e2b_min_ram_bytes_android,
        }
    else:
        min_ram = {
            "e4b": settings.tier_e4b_min_ram_bytes_ios,
            "e2b": settings.tier_e2b_min_ram_bytes_ios,
        }

    for tier in TIERS:
        if ram_bytes < min_ram[tier]:
            continue
        size_bytes = _object_size(_llm_model_key(os, TIER_MODELS[tier]))
        if size_bytes is None:
            continue
        needed = int(size_bytes * settings.tier_disk_headroom_multiplier)
        if free_disk_bytes < needed:
            continue
        return tier, size_bytes, f"ram={ram_bytes} disk={free_disk_bytes} needed={needed}"

    return None


class ModelInfo(BaseModel):
    lang: Language
    bucket: str
    key: str


class ModelsResponse(BaseModel):
    bucket: str
    prefix_base: str
    models: list[ModelInfo]


class LLMModelInfo(BaseModel):
    os: OS
    model_name: ModelName
    bucket: str
    key: str


class LLMModelsResponse(BaseModel):
    bucket: str
    prefix_base: str
    models: list[LLMModelInfo]


class PresignResponse(BaseModel):
    bucket: str
    key: str
    url: str
    expires_at: str


class ResolveResponse(PresignResponse):
    tier: Tier
    model_name: ModelName
    size_bytes: int


@app.get("/downloader/v1/models/{lang}", response_model=PresignResponse)
def presign_model_download(lang: Language, _claims: dict = Depends(_require_auth)) -> PresignResponse:
    key = _model_key(lang)
    url, expires_at = _presign(key)

    return PresignResponse(
        bucket=settings.s3_bucket,
        key=key,
        url=url,
        expires_at=expires_at,
    )


# NOTE: must be declared before /llm-models/{os}/{model_name}, otherwise FastAPI
# matches "resolve" as a model_name and rejects it against the ModelName Literal.
@app.get("/downloader/v1/llm-models/{os}/resolve", response_model=ResolveResponse)
def resolve_llm_model(
    os: OS,
    request: Request,
    ram_bytes: int,
    free_disk_bytes: int,
    gpu_family: int | None = None,
    abi: str | None = None,
    os_version: str | None = None,
    _claims: dict = Depends(_require_auth),
):
    """Pick the on-device model tier for a device and return a pre-signed URL for it.

    The tier decision lives here rather than in the app so the thresholds can be
    retuned without shipping a new build. Returns 204 when the device cannot run
    any tier - the client treats that as "use cloud inference", not an error.
    """
    resolved = _resolve_tier(os, ram_bytes, free_disk_bytes, gpu_family, abi)

    log_context = {
        "trace_id": getattr(request.state, "trace_id", None),
        "request_id": getattr(request.state, "request_id", None),
        "user_id": _claims.get("user_id"),
    }

    if resolved is None:
        logger.info(
            f"Model tier resolve: {os} -> ineligible "
            f"(ram={ram_bytes} disk={free_disk_bytes} gpu_family={gpu_family} "
            f"abi={abi} os_version={os_version})",
            extra=log_context,
        )
        return Response(status_code=204)

    tier, size_bytes, reason = resolved
    model_name = TIER_MODELS[tier]
    key = _llm_model_key(os, model_name)
    url, expires_at = _presign(key)

    logger.info(
        f"Model tier resolve: {os} -> {tier} ({reason} gpu_family={gpu_family} "
        f"abi={abi} os_version={os_version})",
        extra=log_context,
    )

    return ResolveResponse(
        bucket=settings.s3_bucket,
        key=key,
        url=url,
        expires_at=expires_at,
        tier=tier,
        model_name=model_name,
        size_bytes=size_bytes,
    )


@app.get("/downloader/v1/llm-models/{os}/{model_name}", response_model=PresignResponse)
def presign_llm_model_download(
    os: OS, model_name: ModelName, _claims: dict = Depends(_require_auth)
) -> PresignResponse:
    # Validate OS and model combination
    if model_name not in OS_MODELS.get(os, ()):
        raise HTTPException(
            status_code=400,
            detail=f"Model '{model_name}' not available for OS '{os}'. "
            f"Available models: {OS_MODELS.get(os, ())}",
        )

    key = _llm_model_key(os, model_name)
    url, expires_at = _presign(key)

    return PresignResponse(
        bucket=settings.s3_bucket,
        key=key,
        url=url,
        expires_at=expires_at,
    )


@app.get("/downloader/v1/llm-models", response_model=LLMModelsResponse)
def list_llm_models(_claims: dict = Depends(_require_auth)) -> LLMModelsResponse:
    models = []
    for os, model_names in OS_MODELS.items():
        for model_name in model_names:
            models.append(
                LLMModelInfo(
                    os=os,
                    model_name=model_name,
                    bucket=settings.s3_bucket,
                    key=_llm_model_key(os, model_name),
                )
            )
    
    return LLMModelsResponse(
        bucket=settings.s3_bucket,
        prefix_base=settings.s3_llm_prefix_base,
        models=models,
    )


@app.get("/downloader/v1/models", response_model=ModelsResponse)
def list_models(_claims: dict = Depends(_require_auth)) -> ModelsResponse:
    return ModelsResponse(
        bucket=settings.s3_bucket,
        prefix_base=settings.s3_prefix_base,
        models=[
            ModelInfo(lang=lang, bucket=settings.s3_bucket, key=_model_key(lang))
            for lang in ALL_LANGS
        ],
    )


@app.get("/health")
@app.get("/downloader/v1/health")
async def health_check():
    """
    Health check endpoint for load balancers and monitoring.

    Returns service status, version, and basic S3 connectivity check.
    """
    health_status = {
        "status": "healthy",
        "service": "presign-api",
        "version": "1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "environment": settings.environment,
        "checks": {
            "s3_configured": bool(settings.s3_bucket),
        }
    }

    # Optional: Quick S3 connectivity check (only if configured)
    if settings.s3_bucket:
        try:
            # Just check if we can create the client with credentials
            # Don't actually call S3 to avoid extra costs and latency
            _ = boto3.client(
                "s3",
                region_name=settings.aws_region,
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
            )
            health_status["checks"]["s3_client"] = "ok"
            health_status["checks"]["aws_credentials"] = bool(settings.aws_access_key_id)
        except Exception as e:
            health_status["checks"]["s3_client"] = "error"
            health_status["checks"]["s3_error"] = str(e)
            health_status["status"] = "degraded"
            logger.warning(f"S3 health check failed: {str(e)}")

    # Log health check (only at debug level to avoid spam)
    logger.debug("Health check performed", extra={"status": health_status["status"]})

    status_code = 200 if health_status["status"] == "healthy" else 503
    return JSONResponse(content=health_status, status_code=status_code)
