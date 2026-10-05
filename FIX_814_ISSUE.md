# Fix: Three LLM Call-Stack Bugs (Issue #814)

**Upstream issue:** [666ghj/MiroFish#814](https://github.com/666ghj/MiroFish/issues/814)  
**Labels:** `help wanted`, `LLM API`  
**PR:** [varun-projects/MiroFish#2](https://github.com/varun-projects/MiroFish/pull/2)

---

## Overview

Three independent bugs in the LLM call stack cause silent data loss, long-run crashes, and undiagnosable provider errors.

---

## Bug 1 — `reasoning_content` silently discarded for reasoning models

### Symptom
Reasoning models (e.g. DeepSeek-R1, NVIDIA NIM reasoning endpoints) set `message.content = None` and place their actual output in `message.reasoning_content`. The function `extract_chat_completion_text()` was returning an empty string instead of the model's output, causing every call using a reasoning model to silently produce blank results.

### Root Cause
**File:** `backend/app/utils/openai_chat_compat.py`, line 70 (before fix)

```python
# BEFORE (broken)
content = getattr(message, "content", "")
# getattr default only fires when attribute is ABSENT.
# When content=None the attribute EXISTS, so default "" is never used,
# and None falls through to str(content or "") == "" at line 101.
```

### Fix
```python
# AFTER
content = getattr(message, "content", None)

if content is None:
    # Reasoning models set content=None; output is in reasoning_content.
    reasoning = getattr(message, "reasoning_content", None)
    if isinstance(reasoning, str) and reasoning:
        return reasoning
    return ""
```

### Files Changed
| File | Change |
|---|---|
| `backend/app/utils/openai_chat_compat.py` | Guard against `content=None`; fall back to `reasoning_content` |

---

## Bug 2 — Negative `max_tokens` crashes simulation on long runs

### Symptom
On long simulations (e.g. round 94/120), the provider returns HTTP 400:
> `max_tokens` value `-365748` is invalid

### Root Cause
camel-ai's `ModelFactory.create()` is called without specifying the model's context window. When the model name (e.g. `qwen-plus`, `deepseek-chat`) is not in camel-ai's known `ModelType` enum, camel-ai defaults `token_limit = 4096`. This value is used as the token budget ceiling. After 94 rounds, the accumulated token count (369,844) exceeds 4096:

```
4096 − 369844 = −365748  →  passed as max_tokens to the API  →  HTTP 400
```

### Fix
Pass the real context window via `model_config_dict` so camel-ai uses the correct budget. The value is read from `LLM_CONTEXT_WINDOW` (new env var, default `65536`).

```python
# BEFORE
return ModelFactory.create(
    model_platform=ModelPlatformType.OPENAI,
    model_type=llm_model,
)

# AFTER
context_window = int(os.environ.get("LLM_CONTEXT_WINDOW", "65536"))
return ModelFactory.create(
    model_platform=ModelPlatformType.OPENAI,
    model_type=llm_model,
    model_config_dict={"max_tokens": context_window},
)
```

### Files Changed
| File | Change |
|---|---|
| `backend/scripts/run_parallel_simulation.py` | Pass `model_config_dict` with `max_tokens` |
| `backend/scripts/run_twitter_simulation.py` | Same |
| `backend/scripts/run_reddit_simulation.py` | Same |
| `.env.example` | Document `LLM_CONTEXT_WINDOW` |

### Configuration
Add to your `.env` if using a model with a different context window:
```env
# Default is 65536. For 128k models (e.g. qwen-max, deepseek-chat):
LLM_CONTEXT_WINDOW=131072
```

---

## Bug 3 — Bare `except Exception` discards provider error details

### Symptom
Provider errors (HTTP 410 Gone for deprecated models, 429 rate limit, 400 bad request) surface as generic HTTP 500 responses with only `str(e)` as the message. The structured provider attributes (`status_code`, `request_id`) that identify the exact provider failure are lost.

### Root Cause
**File:** `backend/app/api/graph.py` — 4 call sites using the same pattern:

```python
# BEFORE (broken — loses provider error details)
except Exception as e:
    return jsonify({
        "success": False,
        "error": str(e),           # raw exception message, may contain request bodies
        "traceback": traceback.format_exc()  # leaks internal stack to client
    }), 500
```

The same file already had a correct provider-aware handler at line 394 (`generate_ontology`), but it was not applied to the other endpoints.

### Fix
Apply the same provider-aware pattern to all 4 bare `except` blocks:

```python
# AFTER
except Exception as e:
    provider_status = getattr(e, "status_code", None)
    request_id = getattr(e, "request_id", None)
    if isinstance(provider_status, int):
        public_error = f"Provider request failed (HTTP {provider_status})"
        if request_id:
            safe_id = re.sub(r"[^a-zA-Z0-9._:-]", "", str(request_id))[:128]
            if safe_id:
                public_error += f" (request_id: {safe_id})"
        logger.error(
            "<endpoint> provider request failed: type=%s status=%s request_id=%s",
            type(e).__name__, provider_status, request_id or "unknown",
        )
        return jsonify({"success": False, "error": public_error}), 502
    logger.exception("Unexpected failure in <endpoint>")
    return jsonify({"success": False, "error": "<safe message>; see server logs"}), 500
```

This:
- Returns `502` (not `500`) for provider errors — correctly signals an upstream failure
- Strips provider error bodies from the response (they may echo request content)
- Keeps `status_code` and `request_id` in server logs for diagnosis
- Does not leak stack traces to API clients

### Files Changed
| File | Locations |
|---|---|
| `backend/app/api/graph.py` | `build_task()` background thread; `_build_graph_impl()` outer handler; `get_graph_data()`; `delete_graph()` |

---

## Summary of All Changes

| Bug | Severity | Files |
|---|---|---|
| 1 — `reasoning_content` silently discarded | Silent data loss | `openai_chat_compat.py` |
| 2 — Negative `max_tokens` on long runs | Crash at round 94+ | 3 runner scripts + `.env.example` |
| 3 — Bare `except` loses provider errors | Undiagnosable 500s | `graph.py` (4 sites) |

---

## Testing

### Bug 1
Use any reasoning model endpoint (e.g. DeepSeek-R1). Confirm report sections are non-empty.

### Bug 2
Run a 120-round simulation. Without the fix, round ~94 crashes with HTTP 400 (`max_tokens: -365748`). With the fix, the simulation completes.  
Or: set `LLM_CONTEXT_WINDOW=100` to force early exhaustion and confirm a graceful termination instead of a provider 400.

### Bug 3
Use a deprecated or rate-limited model key. Without the fix: `500, {"error": "... request body ..."}`. With the fix: `502, {"error": "Provider request failed (HTTP 410) (request_id: ...)"}`.
