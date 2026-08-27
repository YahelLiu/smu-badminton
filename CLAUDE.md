# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SMU Badminton Court Booking System - Web app for booking badminton courts at Shanghai Maritime University. CAS authentication with OCR captcha solving, real-time availability checking, immediate/scheduled booking with multi-threaded concurrent requests. FastAPI backend, SQLite storage.

## Development Commands

```bash
# Install (editable)
pip install -e .

# Run dev server (port 5002, auto-reload)
python -m smu_badminton.server_fastapi

# Run with uvicorn directly
uvicorn smu_badminton.server_fastapi:app --host 0.0.0.0 --port 5000 --reload

# Debug mode (verbose booking logs)
BOOKING_DEBUG=1 python -m smu_badminton.server_fastapi

# Production
docker-compose up --build

# Tests
python -m pytest tests/ -v                          # all
python -m pytest tests/unit/ -v                     # unit only
python -m pytest tests/integration/ -v              # integration only
python -m pytest tests/unit/test_obfuscate.py -v    # single file
python -m pytest tests/unit/test_obfuscate.py::test_roundtrip -v  # single test
```

## Environment Setup

Copy `.env.example` to `.env` and configure:
- `CAS_ORIGIN`, `WF_ORIGIN`, `WF_API_URL` - University platform URLs
- `OAUTH_CLIENT_ID` - OAuth client identifier
- `BADMINTON_TYPE_ID` - Resource type ID for badminton courts
- `SERVER_PORT` - (optional) Override dev server port, defaults to 5002

## Architecture

### Package Layout

`src/smu_badminton/` with src layout. Entry point: `smu_badminton.server_fastapi:main`.

### Core Modules

| Module | Purpose |
|--------|---------|
| `server_fastapi.py` | FastAPI app, all REST endpoints, lifespan, static files |
| `server_models.py` | Pydantic request/response models, MetricsMiddleware, RateLimitMiddleware, resource locks, job state, public availability cache |
| `cas_login.py` | CAS auth flow: URL resolution, captcha prep, login with auto/manual captcha, error detection |
| `cas_login_requests.py` | Compat layer: HTTP retry logic, token cache, network time sync, re-exports from `cas_login` and `booking_api` |
| `booking_api.py` | Resource queries, time slot queries, appointment creation, availability computation (parallel via ThreadPoolExecutor + shared Session) |
| `cas_manager.py` | `BookingManager` singleton: job create/track/stop, DB persistence, scheduled/immediate booking orchestration |
| `cas_ocr.py` | Arithmetic captcha OCR via ddddocr whole-image recognition (replaced deprecated NCNN ResNet pipeline after site font change) |
| `core_utils.py` | Thread-safe SQLite `DatabasePool`, custom exceptions (`BookingError`, `DatabaseError`, `LoginError`, `ResourceLockedError`), error handling decorators (`handle_errors`, `db_operation`), password obfuscation |
| `config.py` | Environment configuration from `.env` |

### Module Dependencies

```
server_fastapi → server_models, cas_manager, cas_login, cas_login_requests, booking_api, config, core_utils
cas_manager → cas_login_requests, core_utils
cas_login_requests → cas_login, booking_api  (compat/bridge layer)
booking_api → cas_login_requests  (for HTTP retry helpers)
cas_login → cas_ocr, config
```

### Data Flow

1. **Login**: `prepare_login_session()` / `login_with_auto_captcha()` → CAS login page → captcha OCR → POST credentials → follow redirects → extract OIDC tokens (access_token + id_token) from URL fragment
2. **Availability**: `POST /api/availability` → check 60s public cache (shared across users, keyed by bookdate) → cache HIT: only query appointments for `bookedByMe`; cache MISS: full query (resources + time slots + appointments) via shared `requests.Session` with connection pooling → store slots in public cache, merge `bookedByMe` per-user
3. **Immediate Booking**: `POST /api/book` → lock resource → insert `local_bookings` → `book_badminton_slot()` → single-threaded attempt
4. **Scheduled Booking**: `POST /api/book/schedule` → `BookingManager.start_scheduled_booking()` → wait until target time → login → prefetch → spawn N barrier-synchronized worker threads → fire booking requests simultaneously

### Key Design Patterns

- **Token Caching**: `get_token_cached()` per-user dict with configurable TTL (default 900s), thread-safe
- **Profile Caching**: `_TOKEN_PROFILE_CACHE` stores user profile from JWT claims
- **Public Availability Cache**: `_avail_public_cache` keyed by bookdate (60s TTL), shared across all users — slots data is the same for everyone; only `bookedByMe` is queried per-user on cache HIT
- **Shared HTTP Session**: `_shared_session()` creates `requests.Session` with connection pool (20 conns) for reuse across parallel GraphQL queries within a single availability request
- **GraphQL Request Helper**: `_make_graphql_request()` centralizes retry logic (2 attempts, 0.3s backoff), SSL error detection, and slow-query logging (>500ms)
- **Resource Locking**: asyncio locks per `(resources_name, bookdate, kssj, jssj)` prevent duplicate concurrent bookings; separate thread locks for sync code
- **Local Booking Tracking**: SQLite `local_bookings` UNIQUE constraint `(bookdate, resources_name, kssj, jssj)` prevents race conditions at DB level
- **Job Persistence**: `scheduled_jobs` table survives server restarts; `load_pending_jobs()` restores on startup
- **Password Obfuscation**: XOR + base64 using `SECRET_KEY` env var (not encryption, prevents plaintext exposure)
- **Dual Login Strategy**: `CAS_LOGIN_STABLE_FIRST` env var controls which login method is tried first, with fallback
- **Thread-safe SQLite**: `DatabasePool` uses `threading.local()` for per-thread connections, WAL mode

### Database Schema

Two SQLite tables via `core_utils.DatabasePool`:
- `local_bookings`: Tracks active bookings (UNIQUE on bookdate, resources_name, kssj, jssj)
- `scheduled_jobs`: Persists booking jobs across restarts

### Scheduled Booking Timing

Target time = `bookdate - 7 days + target_time_str`. E.g., booking for 2025-12-18 with target 21:00:00 means attempt at 2025-12-11 21:00:00.

### Upstream Constraints (measured 2026-08-27 via scripts/test_captcha_reuse.py)

| Constraint | Measured behavior | Design consequence |
|---|---|---|
| Slide captcha lifetime | Single-use: second save with same credentials returns 「验证码不能重复使用」; server still recognizes it ≥3 min later | Every shot needs its own captcha; solve pool during prefetch window |
| Captcha validation order | Server validates captcha BEFORE business rules; unverified captcha → 「系统异常」, valid captcha → business errors | Calibrated negative control possible; classifier keywords stable |
| Solver reliability | ~70% per attempt (fails with checkCaptcha 4001), retry succeeds | Retry redundancy belongs in prefetch window (cheap time), not at T-0 |
| Save rate limit | Per-account: ~2 rapid saves OK, 3rd immediately banned for exactly 3 minutes | `MAX_UPSTREAM_BURST = 2` hard cap on concurrent shots |
| Official cancel API | `checkAppointmentCancelTime(id)` + `updateAppointmentInformationState(id, state="1")` works | Available for implementing real cancellation later |

### Rush Scheduling Pipeline (start_scheduled_booking)

T-75s wake → `ClockSync.sync()` measures network-vs-local offset → login (JWT exp pre-checked against target time, refreshed early if needed) → dedupe check + resource/time prefetch + user_info resolution all through one warmed `_shared_session()` → build captcha pool (`min(num_threads, 2)` credentials, retry until T-35s) → workers wait via local clock + offset (zero HTTP near T-0) → barrier → each shot fires single-shot (`allow_retry=False`, 4s timeout) with its own credential → first success stops the rest. Ban responses are detected and logged with the 3-minute unban implication.

### OCR Models (deprecated)

`model/` directory (gitignored) previously held the NCNN ResNet triplet used by the old captcha
pipeline (`resnet34_digit_*`, `resnet18_operator_*`, `resnet18_equal_symbol_*`). After the CAS site
changed its captcha font these self-trained models could no longer recognize the operators/digits and
could not be retrained (no training infra). Captcha recognition now uses **ddddocr** whole-image
classification + regex parse (see `cas_ocr.py`); the NCNN models + `ncnn` dependency have been removed.
The `model/` files may still exist on disk but are no longer loaded.

### API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/availability` | POST | Check court availability (token + bookdate) |
| `/api/book` | POST | Immediate booking attempt |
| `/api/book/schedule` | POST | Schedule booking for specific time |
| `/api/jobs` | GET | List all booking jobs（支持 `?username=` 过滤，前端轮询用） |
| `/api/jobs/{job_id}/stop` | POST | Cancel a scheduled job |
| `/api/jobs/stop_by_params` | POST | Cancel job by booking params（同时尽力撤销学校侧预约：调用方传 `access_token` 或凭服务端保存账号静默重登） |
| `/api/local_bookings` | GET | List local booking records |
| `/api/login` | POST | CAS login with captcha |
| `/api/captcha` | POST | Get captcha image |
| `/api/logout` | POST | Clear token cache |
| `/api/auth/refresh` | POST | Silent re-login with saved account, returns fresh access_token |
| `/api/auth/check` | GET | Check auth status |
| `/api/config` | GET | Frontend configuration |
| `/api/metrics` | GET | Request metrics |
| `/health` | GET | Health check |

### Port Convention

Dev server defaults to port 5002 (`__main__` block, overridable via `SERVER_PORT` env var). Docker/production uses port 5000 (`SERVER_PORT=5000` in docker-compose).

## Code Maintenance

### Recently Removed Dead Code (2026-05-28)

The following unused code was removed after verification:

| File | Removed Items |
|------|---------------|
| `core_utils.py` | `error_response()`, `error_response_from_exception()`, `retry_on_error()`, `PermissionDeniedError`, `ResourceAlreadyBookedError`, `JobNotFoundError` |
| `config.py` | `LOCK_MAX_AGE_SEC` (not wired to any code) |
| `cas_ocr.py` | `draw_split_lines_on_image()` (debug helper) |
| `booking_api.py` | `check_resource_availability_on_date()`, `find_resources_id_by_name()`, `demo_check_availability()` (old query functions) |
| `cas_login_requests.py` | `book_task_with_network_date()`, `run_concurrent_booking_threads()`, `test_user_info()`, `num_threads`, `barrier`, `__main__` block (legacy booking logic replaced by cas_manager) |
| `server_models.py` | `_availability_cache`, `_availability_locks`, `_availability_guard`, `_availability_ttl_sec`, `_availability_cleanup()`, `_get_avail_lock()`, `_convert_to_minimal()` (legacy per-user cache system) |

See `dead-code-analysis.md` for detailed evidence and grep traces.
