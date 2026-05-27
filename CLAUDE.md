# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SMU Badminton Court Booking System - A web application for booking badminton courts at Shanghai Maritime University. The system features:
- CAS (Central Authentication Service) authentication with OCR-based captcha solving
- Real-time availability checking for badminton courts
- Immediate and scheduled booking with multi-threaded concurrent requests
- FastAPI backend with SQLite storage

## Development Commands

### Running the Server

```bash
# Development (with auto-reload)
python server_fastapi.py

# Or using uvicorn directly
uvicorn server_fastapi:app --host 0.0.0.0 --port 5000 --reload

# Production (Docker)
docker-compose up --build
```

### Environment Setup

1. Copy `.env.example` to `.env` and configure:
   - `CAS_ORIGIN`, `WF_ORIGIN`, `WF_API_URL` - University platform URLs
   - `OAUTH_CLIENT_ID` - OAuth client identifier
   - `BADMINTON_TYPE_ID` - Resource type ID for badminton courts

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### Debug Mode

Enable verbose booking logs:
```bash
BOOKING_DEBUG=1 python server_fastapi.py
```

## Architecture

### Core Modules

| File | Purpose |
|------|---------|
| `server_fastapi.py` | FastAPI application with all REST endpoints, middleware (CORS, rate limiting, metrics), and WebSocket handling |
| `cas_login_requests.py` | CAS authentication flow, booking API interactions, token management, and network time synchronization |
| `cas_manager.py` | `BookingManager` class for creating/tracking/stopping booking jobs (immediate and scheduled) |
| `cas_ocr.py` | NCNN-based OCR using ResNet models for captcha solving |
| `core_utils.py` | Thread-safe SQLite connection pool, custom exceptions, error handling decorators, password obfuscation |
| `config.py` | Environment configuration loaded from `.env` |

### Data Flow

1. **Login Flow**: `cas_login_requests.login_with_retry()` → CAS login with captcha OCR → OIDC token extraction
2. **Immediate Booking**: `POST /api/book` → `cas_manager.book_badminton_slot()` → single-threaded booking attempt
3. **Scheduled Booking**: `POST /api/book/schedule` → `BookingManager.start_scheduled_booking()` → waits until target time (7 days before bookdate) → multi-threaded concurrent booking

### Key Design Patterns

- **Token Caching**: `get_token_cached()` caches OIDC tokens per-user with configurable TTL (default 900s)
- **Resource Locking**: asyncio locks per `(resources_name, bookdate, kssj, jssj)` tuple prevent duplicate concurrent bookings
- **Local Booking Tracking**: SQLite table `local_bookings` with UNIQUE constraint prevents race conditions
- **Job Persistence**: `scheduled_jobs` table survives server restarts; pending jobs are restored on startup
- **Password Obfuscation**: XOR + base64 encoding for stored passwords (not encryption, just prevents plaintext exposure)

### Database Schema

Two SQLite tables managed via `core_utils.DatabasePool`:

- `local_bookings`: Tracks active bookings to prevent duplicates
- `scheduled_jobs`: Persists booking jobs across restarts

### OCR Models

Located in `model/` directory:
- `resnet34_digit_latest.fp32.*` - Digit recognition
- `resnet18_operator_latest.fp32.*` - Operator recognition (+, -, *)
- `resnet18_equal_symbol_latest.fp32.*` - Equal symbol type detection (Chinese vs symbolic)

### API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/book` | POST | Immediate booking attempt |
| `/api/book/schedule` | POST | Schedule booking for specific time |
| `/api/availability` | POST | Check court availability for a date |
| `/api/jobs` | GET | List all booking jobs |
| `/api/jobs/{job_id}/stop` | POST | Cancel a scheduled job |
| `/api/local_bookings` | GET | List local booking records for a date |
| `/api/config` | GET | Get frontend configuration |
| `/health` | GET | Health check endpoint |

### Scheduled Booking Timing

Target booking time is calculated as `bookdate - 7 days + target_time_str`. For example, booking for 2025-12-18 with target time 21:00:00 means the system will attempt booking at 2025-12-11 21:00:00.
