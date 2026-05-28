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
| `server_models.py` | Pydantic request/response models, MetricsMiddleware, RateLimitMiddleware, resource locks, job state, public availability cache + per-user availability cache |
| `cas_login.py` | CAS auth flow: URL resolution, captcha prep, login with auto/manual captcha, error detection |
| `cas_login_requests.py` | Compat layer: HTTP retry logic, token cache, network time sync, re-exports from `cas_login` and `booking_api` |
| `booking_api.py` | Resource queries, time slot queries, appointment creation, availability computation (parallel via ThreadPoolExecutor + shared Session) |
| `cas_manager.py` | `BookingManager` singleton: job create/track/stop, DB persistence, scheduled/immediate booking orchestration |
| `cas_ocr.py` | NCNN-based OCR using ResNet models for captcha solving |
| `core_utils.py` | Thread-safe SQLite `DatabasePool`, custom exceptions, error handling decorators, password obfuscation |
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
- **Per-User Availability Cache**: `_availability_cache` (legacy, keyed by token+bookdate) retained for compat but no longer used in the availability endpoint
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

### OCR Models

Located in `model/` directory (gitignored):
- `resnet34_digit_latest.fp32.*` - Digit recognition
- `resnet18_operator_latest.fp32.*` - Operator recognition (+, -, *)
- `resnet18_equal_symbol_latest.fp32.*` - Equal symbol type detection (Chinese vs symbolic)

### API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/availability` | POST | Check court availability (token + bookdate) |
| `/api/book` | POST | Immediate booking attempt |
| `/api/book/schedule` | POST | Schedule booking for specific time |
| `/api/jobs` | GET | List all booking jobs |
| `/api/jobs/{job_id}/stop` | POST | Cancel a scheduled job |
| `/api/jobs/stop_by_params` | POST | Cancel job by booking params |
| `/api/local_bookings` | GET | List local booking records |
| `/api/login` | POST | CAS login with captcha |
| `/api/captcha` | POST | Get captcha image |
| `/api/logout` | POST | Clear token cache |
| `/api/auth/check` | GET | Check auth status |
| `/api/config` | GET | Frontend configuration |
| `/api/metrics` | GET | Request metrics |
| `/health` | GET | Health check |

### Port Convention

Dev server defaults to port 5002 (`__main__` block, overridable via `SERVER_PORT` env var). Docker/production uses port 5000 (`SERVER_PORT=5000` in docker-compose).
