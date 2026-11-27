# ApplyPilot AI — Windows Desktop Application (Tauri v2)

Reuses the shared `apps/web` Next.js BFF application inside a native Tauri v2 Windows shell (`apps/desktop/src-tauri`).

## Architecture & Security
- **Shared Frontend + BFF:** Uses `apps/web` (`devUrl: http://localhost:3000` in development). Because `apps/web` hosts security-critical `HttpOnly` cookie and OAuth BFF routes (`/api/session/*`, `/api/backend/[...path]`, `/api/integrations/gmail/callback`), production desktop packaging runs `next start` on loopback (`http://127.0.0.1:3000`) rather than a static `output: "export"` bundle.
- **No Secret / Token Storage:** OAuth refresh tokens and JWTs remain strictly server-side (PostgreSQL AES-256-GCM and `HttpOnly` cookies); `apps/desktop` never stores credentials.
- **Backend Readiness & Notifications:** Exposes `check_backend_health` (`/health/ready`) and `tauri-plugin-notification` in `src-tauri/src/main.rs`.
- **Auto-Updates:** Disabled unless explicitly configured with signed release artifacts.

## Local & CI Verification
- Config & security tests: `npm test` (`node --test desktop.test.mjs`)
- Native Rust/Tauri build (requires Rust toolchain + MSVC Build Tools on Windows):
  ```powershell
  cd apps/desktop
  cargo check --manifest-path src-tauri/Cargo.toml
  npm run build
  ```
- Automated Windows CI workflow: `.github/workflows/desktop-windows.yml`.
