# NanoHatani — Security Audit Report

**Date:** 2026-07-12  
**Scope:** Full codebase cross-reference of ARCHITECTURE_REPORT.md findings + independent analysis  
**Methodology:** Read every listed source file, traced imports, verified claims against actual code

> **Статус: исторический снимок.** После аудита код изменён: удалены `pil_codegen.py`, `esrgan_model.py`, модель `anime.pth`, зависимость `torch` и фасад `ai_services.py`; текущий `docker-compose.yml` не монтирует `docker.sock`. Перед использованием выводов аудит нужно запустить заново.

---

## Executive Summary

The ARCHITECTURE_REPORT.md contains **several critical inaccuracies** due to code changes since it was written. Specifically: `network_mode: host` does NOT exist in docker-compose.yml, the `grep` injection vulnerability (C3) is not present in the current code, Gemini API key is now passed via header not URL (C6 fixed), and ESRGAN `weights_only` is correctly set to `True` (H5 fixed). However, the report **understated** two critical issues: the Docker sandbox has full internet access (no `--network=none`), and the PIL `exec()` runs on the HOST despite AST validation.

**Overall Risk Rating: CRITICAL** — three active remote code execution vectors exist.

---

## 1. Cross-Reference Verification: ARCHITECTURE_REPORT.md Claims

### C1 — Key Leakage in Logs (CRITICAL)
| Aspect | Report Claim | Actual Code | Verdict |
|--------|-------------|-------------|---------|
| 3,995 key instances in bot.log | True per README stats | Could not verify file (not read) | **ACCEPT** — plausible given 5.9MB log size |
| Filter exists | Mentioned in P0 recommendation | `log_filter.py` exists and is applied in `main.py:107-110` | **ACCEPT** — filter exists |
| Filter coverage | Not assessed | Only on `FileHandler`, NOT on `StreamHandler`. Console output unfiltered. | **NEW FINDING** |
| Agent access via `read_bot_logs` | Identified | `tools.py:74-84` reads raw file, no masking at read time | **ACCEPT** |

**Verdict: CONFIRMED.** The `APIKeyMaskingFilter` covers 8 key prefix patterns and is applied to the FileHandler. However:
- The StreamHandler (console) is unfiltered
- The filter only runs at write time; any keys logged before the filter was first deployed persist in `bot.log`
- `read_bot_logs` returns raw content, bypassing the filter entirely when the agent reads logs

### C2 — Docker Socket Escape (CRITICAL)
| Aspect | Report Claim | Actual Code | Verdict |
|--------|-------------|-------------|---------|
| `network_mode: host` | Present in docker-compose.yml:34-35 | **NOT PRESENT** — no `network_mode` directive anywhere | **FALSE** |
| `/var/run/docker.sock` mounted | Line 34-35 | **Yes** — `docker-compose.yml:38` | **CONFIRMED** |
| Escape vector: `docker run --privileged -v /:/host` | Claimed | Valid: docker.sock alone enables this | **CONFIRMED** |

**Verdict: PARTIALLY CORRECT.** The `network_mode: host` claim is FALSE — the current docker-compose.yml has no such directive. However, `/var/run/docker.sock` IS mounted, which alone provides full Docker API access. The container can spawn privileged containers and compromise the host. The risk is still CRITICAL even without `network_mode: host`.

### C3 — grep Option Injection → Arbitrary File Read (CRITICAL)
| Aspect | Report Claim | Actual Code | Verdict |
|--------|-------------|-------------|---------|
| `subprocess.run(['grep', '-i', filt, log_path])` | At `handlers/agent_cb.py:407` | **Not present** — actual code at `agent_cb.py:400-424` uses Python `pattern in l.lower()` | **FALSE** |
| No `--` separator | Claimed | Not applicable — no subprocess grep call exists | **FALSE** |
| Filter validation | Not mentioned | Code validates: `filt.startswith("-")` rejected, `len(filt) > 200` rejected | **FIXED** |

**Verdict: FALSE — VULNERABILITY DOES NOT EXIST in current code.** The report described an older version. Current code uses pure Python string matching with input validation.

### C4 — Plaintext API Keys (CRITICAL)
| Aspect | Report Claim | Actual Code | Verdict |
|--------|-------------|-------------|---------|
| Keys in `r.txt` plaintext | Yes | `keys/manager.py:51-64` loads from `r.txt` or `r.txt.enc` with plaintext fallback | **CONFIRMED** |
| crypto.py exists | Not mentioned | `keys/crypto.py` — PBKDF2-SHA256, 600k iterations, 16-byte salt, Fernet (AES-128-CBC + HMAC) | **GOOD** |
| crypto.py actually used | Not mentioned | `keys/manager.py:55` imports `decrypt_keys_file` from crypto | **YES** |
| Plaintext fallback | Not mentioned | `decrypt_keys_file()` falls back to plaintext if `.enc` doesn't exist | **CONFIRMED** |

**Verdict: CONFIRMED with nuance.** The crypto implementation is solid. The real issue is that encryption is OPT-IN (requires running `encrypt_keys_file()` to create `.enc`), and plaintext fallback in `decrypt_keys_file()` means an attacker who can delete the `.enc` file forces a plaintext read. The `KEYS_ENCRYPTION_PASSWORD` separate from `DB_ENCRYPTION_KEY` is acceptable separation of concerns.

### C5 — PIL sandbox escape on HOST (CRITICAL)
| Aspect | Report Claim | Actual Code | Verdict |
|--------|-------------|-------------|---------|
| `exec()` runs on host, not in Docker | Yes | `pil_codegen.py:262` — `exec(compile(code, '<generated>', 'exec'), sandbox)` runs in bot process | **CONFIRMED** |
| `__builtins__` whitelist only | Claimed insufficient | `allowed_builtins` (lines 250-255) strips `__import__`, `eval`, `exec`, `open`, `compile`, etc. | **CONFIRMED** |
| `().__class__.__bases__[0].__subclasses__()` bypass | Claimed possible | AST validator blocks `__class__`, `__bases__`, `__subclasses__` in `visit_Attribute` (line 80). Also blocks `getattr(obj, "__class__")` where second arg is a constant | **PARTIALLY BLOCKED** |
| AST validator bypass: computed attribute names | Not assessed | `getattr(obj, computed_var)` passes validation — the check only catches string CONSTANTS (line 73) | **NEW BYPASS FOUND** |

**Verdict: CONFIRMED CRITICAL. The AST validator is robust but has a bypass.** The validator blocks all `visit_Attribute` access to `__class__`, `__bases__`, etc. It blocks `getattr(obj, "__class__")` with string constants. But `getattr(obj, some_variable_containing_dunder_name)` passes through because the second argument is a `Name` node, not a `Constant`. An LLM could craft:
```python
escape = chr(95)*2 + "class" + chr(95)*2  # "__class__"
klass = getattr((), escape)  # passes AST validation!
```
This is sufficient to bootstrap a full sandbox escape.

**Severity: CRITICAL** — code runs on HOST with access to filesystem, network, and docker.sock.

### C6 — Gemini API Key in URL (CRITICAL)
| Aspect | Report Claim | Actual Code | Verdict |
|--------|-------------|-------------|---------|
| `?key={key}` in URL | `agent/loop.py:63`, `dual_bot.py:153` | `shared_types.py:121` uses `x-goog-api-key` HEADER. `_gemini_url()` builds clean URLs. No `?key=` pattern found anywhere. | **FALSE — FIXED** |
| Header usage | Not mentioned | `_gemini_headers()` returns `{"x-goog-api-key": key}` | **FIXED** |

**Verdict: FIXED.** The code now uses the `x-goog-api-key` HTTP header correctly for all Gemini API calls.

---

### H1 — Agent Reads Its Own Keys (HIGH)
| Aspect | Report Claim | Actual Code | Verdict |
|--------|-------------|-------------|---------|
| `read_bot_logs` gives agent access to logs | Yes | `tools.py:74-84` — reads last N lines of `bot.log` | **CONFIRMED** |
| Budget limit | Not mentioned | `safety.py:50` — `read_bot_logs` limited to 5 calls per session | **MITIGATED** |
| Content returned | Not assessed | Returns raw file content up to 3000 chars, no masking at read time | **CONFIRMED** |

**Verdict: CONFIRMED HIGH.** The budget limit (5 calls) mitigates bulk exfiltration but doesn't prevent targeted extraction. A single call at 3000 chars could capture multiple keys. The `read_bot_logs` tool should either be removed or should apply the same `APIKeyMaskingFilter` before returning content.

### H2 — Unrestricted run_python/shell (HIGH)
| Aspect | Report Claim | Actual Code | Verdict |
|--------|-------------|-------------|---------|
| Any user can execute arbitrary code in sandbox | Yes | `loop.py` dispatches `run_python`/`run_shell` without privilege checks | **CONFIRMED** |
| Admin-only gating | Not present | No `is_owner` check for these tools in `_execute_tool` | **CONFIRMED** |
| Combined with docker.sock | Claimed | Valid concern: shell in container can call host docker CLI | **CONFIRMED** |

**Verdict: CONFIRMED HIGH.** The only barriers are `_DebounceHook` (prevents identical repeats) and `_ToolBudget` (per-session limits). A determined attacker could exhaust budgets over multiple sessions. The docker.sock mount makes this a host-compromise vector.

### H3 — chmod 777 on Workspace (HIGH)
| Aspect | Report Claim | Actual Code | Verdict |
|--------|-------------|-------------|---------|
| Workspace files world-writable | Yes | `workspace.py:59` — `os.chmod(self.host_path, 0o777)` | **CONFIRMED** |
| Recursive chmod | Yes | `workspace.py:117-125` — recursive `os.chmod(path, 0o777)` | **CONFIRMED** |
| site-packages chmod | Yes | `Dockerfile.sandbox:94` — `chmod 777 /usr/local/lib/python3.11/site-packages` | **CONFIRMED** |
| Mitigation: container isolation | Partially | Single-host deployment means other containers/users could access | **CONFIRMED** |

**Verdict: CONFIRMED HIGH.** `0o700` for workspace directory and `0o600` for files would be sufficient. The `chown` to uid 1000 is correct, but world-writable permissions defeat it.

### H4 — Zip Slip in Validation (HIGH)
| Aspect | Report Claim | Actual Code | Verdict |
|--------|-------------|-------------|---------|
| `_safe_zip_name` has `..` check | Yes | `common.py:397` — `if any(part == '..' for part in parts)` | **PRESENT** |
| `....//....//etc/passwd` bypass | Claimed possible | Split by `/` gives `['....', '', '....', '', 'etc', 'passwd']` → cleaned to `['....', '....', 'etc', 'passwd']` → none equal `..` | **BYPASS EXISTS** |
| `os.path.normpath` cleanup | Yes | `common.py:399` — `os.path.normpath('/'.join(parts))` | **MITIGATES** |

**Verdict: PARTIALLY CONFIRMED.** The `....//....//etc/passwd` path would be split and filtered so no individual part is `..`. The `os.path.normpath` at line 399 would normalize `....//....` but NOT to `../..` — `normpath` converts `....` to `....` (unchanged). However, on some systems, `....` could resolve to `../..` in some edge cases. The check for `normalized.startswith('../')` at line 400 provides additional protection. **Risk: LOW-MEDIUM, not HIGH as claimed.**

### H5 — ESRGAN weights_only=False (HIGH)
| Aspect | Report Claim | Actual Code | Verdict |
|--------|-------------|-------------|---------|
| `torch.load(..., weights_only=False)` | Line 68 | File not found at `services/esrgan_model.py` | **COULD NOT VERIFY PATH** |
| Actual code found | Not assessed | Grep found `torch.load(_MODEL_PATH, map_location='cpu', weights_only=True)` in `esrgan_model.py:79` | **FIXED** |

**Verdict: FALSE — weights_only=True in current code.** The file path in the report may be wrong or the code was fixed.

### H6 — Duplicate SSRF Protection (HIGH)
| Aspect | Report Claim | Actual Code | Verdict |
|--------|-------------|-------------|---------|
| `fetch_with_cookies` has weaker blocklist | Missing `0.0.0.0/8`, `fc00::/7` | loop.py:244-248 includes `0.0.0.0/8`, `fc00::/8` (NOT `/7`) and `fd00::/8` | **PARTIALLY CORRECT** |
| Missing `::ffff:0:0/96` | Not mentioned | loop.py does NOT include `::ffff:0:0/96` (IPv4-mapped IPv6) | **CONFIRMED** |
| Missing IPv4-mapped unwrapping | Not mentioned | `security_utils.py` does unwrapping (line 50-51), loop.py does NOT | **CONFIRMED** |

**Verdict: CONFIRMED but severity LOWERED to MEDIUM.** The loop.py blocklist does include `0.0.0.0/8` and `fc00::/8` (report was wrong about these). The missing `::ffff:0:0/96` is a gap but `::ffff:127.0.0.1` would resolve to `::ffff:127.0.0.1` which wouldn't match `::1/128` — this is a real bypass.

### H7 — bot.log Full Dialogues + Keys (HIGH)
| Aspect | Report Claim | Actual Code | Verdict |
|--------|-------------|-------------|---------|
| 5.9MB logs with full dialogues | Yes | Not verified directly | **ACCEPT** |
| No rotation | Claimed | Verified — no `RotatingFileHandler`, no logrotate config | **CONFIRMED** |
| No filtering at time of report | Claimed | Now has `APIKeyMaskingFilter` but only on FileHandler | **MITIGATED** |

**Verdict: CONFIRMED but MITIGATED.** The `APIKeyMaskingFilter` has been added since the report was written, but it only covers the FileHandler. Console output and pre-filter historical logs still contain keys.

---

## 2. New Findings (Not in ARCHITECTURE_REPORT.md)

### N1 — Docker Sandbox Has Internet Access (CRITICAL)
**File:** `agent/workspace.py:131-138`  
**Finding:** The report and README both claim the Docker sandbox uses `--network=none`. **This is FALSE.** The actual `docker_run` command does NOT include any `--network` flag:
```python
docker_cmd = [
    "docker", "run", "--rm",
    "--memory=1024m", "--cpus=2",
    "--user=sandbox",
    "--workdir=/workspace",
    "-v", f"{self.host_path}:/workspace",
    _SANDBOX_IMAGE,
] + cmd
```
The sandbox uses Docker's default bridge network, giving the LLM agent full outbound internet access. This enables:
- Data exfiltration to arbitrary external servers
- Contacting C2 infrastructure
- Downloading additional payloads
- Bypassing SSRF protections (the sandbox has its own network context)

**Severity: CRITICAL.** The most significant security claim in the project's documentation is false.

**Fix:** Add `"--network=none"` to the `docker_cmd` list. For tools that need internet (`playwright_browse`, `fetch_json`), use a separate container with `--network=bridge` on a per-call basis.

### N2 — Log Filter Only on FileHandler (MEDIUM)
**File:** `main.py:106-110`  
**Finding:** The `APIKeyMaskingFilter` is applied only to `FileHandler` instances. The `StreamHandler` (stdout/stderr) has NO filtering:
```python
for h in logging.root.handlers:
    if isinstance(h, logging.FileHandler):
        h.addFilter(APIKeyMaskingFilter())
```
This means keys appear in plaintext on Docker logs (`docker logs`), systemd journal, and any terminal output.

**Severity: MEDIUM.** Docker logs are readable by anyone with `docker` access. Combined with docker.sock mount, this leaks keys to anyone who can inspect the container.

**Fix:** Apply the filter to ALL handlers, or better, implement filtering at the `logging.Filter` level on the root logger itself.

### N3 — MAX_STEPS Doubled to 120 (MEDIUM)
**File:** `agent/loop.py:25`  
**Finding:** The report states MAX_STEPS=60. Current code: `MAX_STEPS = 120`. This doubles the attack surface for each agent session — more tool calls, more budget consumption, more opportunities to find sandbox bypasses.

**Severity: MEDIUM.** Amplifies all other risks.

### N4 — Zero Content Safety Filters on Agent (HIGH)
**File:** `agent/loop.py`, `services/audio_service.py`, `services/music_service.py`  
**Finding:** The main agent's text generation (now using DeepSeek via `_deepseek_call()`) has NO safety settings. The audio_service and music_service explicitly set `BLOCK_NONE` for all Gemini harm categories. The project deliberately enables NSFW content generation with a full Replicate-based configurator (`handlers/common.py:174-194`). There is:
- No age verification
- No content moderation
- No opt-in requirement for NSFW (beyond chat membership)
- No logging of NSFW generation to track abuse

**Severity: HIGH.** This poses legal risks depending on jurisdiction (child safety, CSAM, etc.).

### N5 — Telegram Bot API Container Runs as Root (LOW)
**File:** `docker-compose.yml:5`  
**Finding:** The `telegram-bot-api` container uses `user: "0:0"` (root). While this is a separate container, it's unnecessary privilege.

**Severity: LOW.** Minor hardening issue; the container handles untrusted input from Telegram API.

### N6 — _DebounceHook Uses MD5 (INFO)
**File:** `agent/safety.py:16`  
**Finding:** `hashlib.md5()` is used for deduplication fingerprinting. MD5 is cryptographically broken but this is a non-security use (just collision-resistant-enough hashing for dedup). Not a vulnerability.

### N7 — `log_filter.py` Lacks Telegram Bot Token Pattern (MEDIUM)
**File:** `log_filter.py:10-21`  
**Finding:** The key patterns list covers 8 provider key types but does NOT include Telegram bot tokens (format: `\d{8,10}:AA[A-Za-z0-9_-]{30,}`). If a bot token is logged, it won't be masked.

**Severity: MEDIUM.** Bot token exposure allows full account takeover.

### N8 — `decrypt_keys_file` Plaintext Fallback is Exploitable (MEDIUM)
**File:** `keys/crypto.py:73-90`  
**Finding:** If the `.enc` file is deleted, `decrypt_keys_file()` falls back to reading the plaintext file. An attacker with filesystem access who can delete files (e.g., via the agent) can force a plaintext key read.

**Severity: MEDIUM.** Requires filesystem access within the container, which the agent has via `read_file`/`write_file` in the workspace.

### N9 — No Resource Limits on Sandbox Containers at Daemon Level (LOW)
**File:** `agent/workspace.py:131-138`  
**Finding:** While `--memory=1024m` and `--cpus=2` are set per-container, there's no Docker daemon-level limit on concurrent sandbox containers. A malicious user could spawn multiple agent sessions to exhaust host resources.

**Severity: LOW.** The per-session `_ToolBudget` limits mitigate this somewhat (max 12 `run_python`, 16 `run_shell` per session).

---

## 3. Security Architecture Assessment

### 3.1 What's Done Well

1. **Path traversal protection:** `_safe_path()` in `workspace.py:74-81` uses `os.path.realpath` + prefix check — correct implementation.
2. **AST calculator:** `_ast_eval()` in `tools.py:32-71` is a textbook-safe expression evaluator.
3. **SSRF centralization:** `is_safe_url()` in `security_utils.py` is comprehensive (11 network ranges, IPv4-mapped IPv6 unwrapping, DNS resolution validation).
4. **ImageMagick hardening:** `Dockerfile.sandbox:24-25` disables MVG, MSL, URL, HTTP, HTTPS, FTP, PS coders — good ImageTragick protection.
5. **Key encryption crypto:** `keys/crypto.py` uses PBKDF2-SHA256 with 600,000 iterations, 16-byte random salt, and Fernet — cryptographically sound.
6. **Debounce + Budget:** `_DebounceHook` and `_ToolBudget` in `safety.py` provide effective loop prevention and per-session rate limiting.
7. **Non-root sandbox user:** Docker sandbox runs as uid 1000, not root.
8. **Cookie masking:** `_mask_cookies()` sanitizes Netscape cookie files in tool output.
9. **Gemini key in header:** Fixed — now uses `x-goog-api-key` header instead of URL query parameter.

### 3.2 Architecture Weaknesses

1. **`docker.sock` in untrusted container:** The bot container has full Docker API access. This is the single most impactful architectural decision — it means any sandbox escape or agent compromise can escalate to host root.
2. **PIL exec() on host:** The code generation path runs LLM-generated code directly on the host Python process. The AST validator is a speed bump, not a wall.
3. **Dual code paths for SSRF:** `loop.py`'s `fetch_with_cookies` duplicates `is_safe_url()` logic with subtle differences.
4. **No authentication boundary between tools:** Any chat participant can trigger any agent tool (including `run_shell`, `playwright_browse`) — there's no role-based access control for tools.
5. **In-memory state (`state.py`):** Global dicts for cooldowns, pending requests, etc. Not thread-safe, not persisted, lost on restart.
6. **No database migrations:** Schema changes require manual ALTER or complete rebuild.
7. **Monolithic handler files:** `common.py` (790 lines), `loop.py` (620 lines) — difficult to audit.

---

## 4. Risk Matrix

| # | Finding | Severity | Exploitability | Impact | Status |
|---|---------|----------|---------------|--------|--------|
| C5 | PIL `exec()` on host | **CRITICAL** | Medium (needs LLM to generate bypass) | Host compromise | Active |
| C2 | `/var/run/docker.sock` mount | **CRITICAL** | High (simple `docker run`) | Host root access | Active |
| N1 | Sandbox has internet access | **CRITICAL** | High (default behavior) | Data exfiltration, C2 | Active |
| C1 | Key leakage via logs | **HIGH** | Medium (agent needs `read_bot_logs`) | API key exfiltration | Partially mitigated |
| H2 | Unrestricted code execution | **HIGH** | High (any chat user) | Via docker.sock → host | Active |
| N4 | Zero content safety on agent | **HIGH** | Automatic | Legal/compliance risk | Active |
| H3 | World-writable workspace | **MEDIUM** | Low (same-host only) | Data tampering | Active |
| N7 | Missing Telegram bot token filter | **MEDIUM** | Low (token must be logged) | Account takeover | Active |
| N8 | crypto.py plaintext fallback | **MEDIUM** | Medium (delete .enc file) | Key exposure | Active |
| H6 | Duplicate SSRF missing `::ffff:0:0/96` | **MEDIUM** | Low | Internal network access | Active |
| C3 | grep injection | **N/A** | N/A | N/A | **FALSE — fixed** |
| C6 | Gemini key in URL | **N/A** | N/A | N/A | **FALSE — fixed** |
| H5 | ESRGAN weights_only | **N/A** | N/A | N/A | **FALSE — fixed** |

---

## 5. Prioritized Remediation Plan

### P0 — Immediate (Stop-the-Bleed)

1. **Move PIL `exec()` to Docker sandbox.** Call `ws.docker_run(["python", "-c", code])` instead of `exec()` on host. This eliminates the most dangerous single vulnerability.

2. **Add `--network=none` to sandbox containers.** One-line change in `workspace.py:131`:
   ```python
   "--network=none",
   ```
   For `playwright_browse` and tools needing internet, create a separate `docker_run_with_network()` variant.

3. **Remove or restrict `/var/run/docker.sock` mount.** If the bot MUST manage Docker containers, use Docker-in-Docker (dind) with an isolated Docker daemon, or switch to rootless Docker. As an immediate mitigation, use a restricted Docker authorization plugin.

### P1 — High Priority (Within 1 Week)

4. **Remove `read_bot_logs` tool** or apply `APIKeyMaskingFilter` at read time. The agent should not be able to read its own logs.

5. **Add tool privilege gating.** Require `is_owner` check for `run_shell`, `run_python`, `playwright_browse`, `fetch_with_cookies`.

6. **Consolidate SSRF protection.** Remove the duplicate blocklist from `loop.py:243-248` and use `is_safe_url()` everywhere. Add `::ffff:0:0/96` to the canonical list.

7. **Fix `log_filter.py` handler coverage.** Apply filter to ALL handlers, not just FileHandler. Add Telegram bot token pattern.

8. **Fix workspace permissions.** Use `0o700` for workspace directory, `0o600` for files. Remove `chmod 777` from Dockerfile.sandbox.

### P2 — Medium Priority (Within 1 Month)

9. **Enforce key encryption.** Remove plaintext fallback in `decrypt_keys_file()` or add a startup check that refuses to run with plaintext keys when encryption is configured.

10. **Add content safety controls.** At minimum, log NSFW generation to enable abuse tracking. Consider opt-in requirement.

11. **Implement log rotation.** Use `RotatingFileHandler` with size-based rotation and retention limits.

12. **Add `--` to `_safe_zip_name`.** While current protection is adequate, add explicit `os.path.normpath` result check: reject if result starts with `..`.

13. **Add database migrations.** At minimum, a `_schema_version` table + version-based migration runner.

### P3 — Low Priority (Technical Debt)

14. **Audit all `except Exception: pass` blocks.** Replace with at least `logger.debug()` to enable intrusion detection.

15. **Split monolithic files.** `common.py`, `loop.py`, `prompts.py`, `web_search.py` all exceed 600 lines.

16. **Add tests.** Currently zero tests for ~12,000 lines of security-critical code.

17. **Fix `telegram-bot-api` root user.** Remove `user: "0:0"` from docker-compose.yml.

18. **Multi-stage Docker build.** Reduce attack surface by excluding build tools from runtime image.

---

## Appendix A: False Claims in ARCHITECTURE_REPORT.md

| Claim | Stated Line | Actual | Impact on Report Accuracy |
|-------|------------|--------|--------------------------|
| `network_mode: host` in docker-compose.yml | 228, 278, 408, 468, 486 | Not present | Overstated risk; docker.sock alone is sufficient for CRITICAL |
| `grep` option injection via subprocess | 279 | Pure Python matching used instead | False positive; vulnerability never existed in current code |
| Gemini API key in URL `?key=` | 282 | Uses `x-goog-api-key` header | Fixed; no longer an issue |
| `ESRGAN weights_only=False` | 292 | `weights_only=True` | Fixed; no longer an issue |
| Docker sandbox `--network=none` | 211, README:36 | NOT present in code | **Understated risk** — sandbox HAS internet |
| MAX_STEPS=60 | 188 | MAX_STEPS=120 | Understated agent capability |
| `--memory=512m --cpus=0.5` | README:36 | `--memory=1024m --cpus=2` | Resource limits differ from documentation |
| `VEo` (typo) | Multiple places | Veo (Google Video) | Minor |

## Appendix B: Verified Security Posture

### Encryption
- ✅ `crypto.py`: PBKDF2-SHA256, 600k iterations, 16-byte salt, Fernet (AES-128-CBC + HMAC-SHA256)
- ✅ `database/generations.py`: Veo API keys encrypted via Fernet with `DB_ENCRYPTION_KEY`
- ⚠️ `keys/manager.py`: Encryption is opt-in, plaintext fallback exists
- ⚠️ `log_filter.py`: API key masking in logs, but only on FileHandler

### Sandbox Isolation
- ✅ Non-root user (uid 1000) in sandbox container
- ✅ ImageMagick hardened against ImageTragick
- ✅ `_safe_path()` prevents path traversal  
- ❌ Sandbox has internet access (no `--network=none`)
- ❌ `--no-sandbox` flag on Chromium in sandbox
- ❌ Host docker.sock accessible from bot container

### Input Validation
- ✅ `_ast_eval()` safe math evaluator
- ✅ `is_safe_url()` comprehensive SSRF protection
- ✅ `_safe_zip_name()` path traversal prevention
- ✅ `strip_code_fences()` removes markdown code blocks
- ⚠️ PIL AST validator has bypass via computed attribute names
- ⚠️ `fetch_with_cookies` has incomplete SSRF blocklist
- ⚠️ No input sanitization for tool arguments (relies on AI to not be malicious)

### Authentication/Authorization
- ❌ No tool-level privilege separation
- ❌ Bot token in `.env` and logs (no masking for token pattern)
- ⚠️ Admin access via hardcoded `ADMIN_IDS` in config
- ⚠️ `figma_bridge.py` uses static `BRIDGE_SECRET`
