# Changelog

## 2026-09-01

### Fixed
- **Tailscale remote access**: Added tailscale FQDN to `allowed_hosts` in `src/crawl4ai_mcp/__main__.py` to resolve `421 Misdirected Request` when accessing via tailscale serve proxy.
  - Added: `instance-20250526-0820.taila20d2.ts.net`
  - Added: `instance-20250526-0820.taila20d2.ts.net:11237`
  - tailscale serve endpoint: `https://instance-20250526-0820.taila20d2.ts.net:11237/mcp`

### Changed
- Client config (`~/.config/opencode/opencode.jsonc`) updated from `http://...:11236/mcp` to `https://...:11237/mcp` to match the tailscale serve proxy endpoint.

### Notes
- The service still binds to `127.0.0.1:11236` only (by design, enforced by `loopback_only` validator in `config.py`).
- Remote access is provided via `sudo tailscale serve --bg --https=11237 http://127.0.0.1:11236`.
- If the tailscale node name changes (e.g. instance rebuild), update `allowed_hosts` accordingly.
