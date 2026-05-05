"""Eval harness para el endpoint /scrape (P2.7).

Lee `scripts/cases.yaml`, hace POST contra un server corriendo en
localhost:8000 (configurable via --base-url), y compara cada response
contra las assertions del caso. Emite un reporte tabular con pass/fail
y metricas resumidas.

Uso basico:
  # Levantar el server primero en otra terminal:
  uvicorn app.main:app --host 0.0.0.0 --port 8000

  # Luego correr el eval:
  python scripts/eval.py
  python scripts/eval.py --base-url http://localhost:8000
  python scripts/eval.py --skip-falabella       # skipea casos que requieren Playwright+LLM
  python scripts/eval.py --case ml_listing_iphone   # corre un solo caso
  python scripts/eval.py --json reports/eval_results.json   # dump JSON adicional

Exit codes:
  0  -- todos los casos pasaron
  1  -- al menos un caso fallo (assertion violada)
  2  -- error de configuracion (cases.yaml invalido, server no responde, ...)

Decisiones TL:
- NO levanta el server por nosotros: deliberadamente desacoplado. El
  eval testea contra un server real (con su grafo, sus adapters, su
  LLM). Si quisieramos un eval unit-testy seria una redundancia con
  los 113 tests de pytest.
- assertions se leen del YAML como datos, no se hardcodean: es facil
  agregar casos sin tocar el codigo del harness.
- Compatible con CI: exit code 0/1/2 + opcion --json para que un
  pipeline pueda parsear los resultados.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import httpx
    import yaml
except ImportError as e:
    print(f"ERROR: dependencia faltante: {e}. Run `pip install httpx pyyaml`.", file=sys.stderr)
    sys.exit(2)


# ANSI color codes (degradan a no-color si el terminal no soporta)
class C:
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    @classmethod
    def disable(cls) -> None:
        for k in dir(cls):
            if k.isupper() and isinstance(getattr(cls, k), str):
                setattr(cls, k, "")


# -----------------------------------------------------------------------------
# Result types
# -----------------------------------------------------------------------------
@dataclass
class CaseResult:
    case_id: str
    description: str
    passed: bool
    duration_s: float
    failures: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    response_summary: dict = field(default_factory=dict)


# -----------------------------------------------------------------------------
# Assertion runner
# -----------------------------------------------------------------------------
def assert_case(expect: dict[str, Any], status_code: int, body: dict, total_time_s: float) -> list[str]:
    """Devuelve lista de assertion failures; vacia = pass."""
    failures: list[str] = []

    # Status HTTP
    if "status_code" in expect:
        want = expect["status_code"]
        if status_code != want:
            failures.append(f"status_code: want={want}, got={status_code}")

    # response.status (campo del JSON: 'ok' o 'error')
    if "response_status" in expect:
        got = body.get("status")
        if got != expect["response_status"]:
            failures.append(f"response_status: want={expect['response_status']!r}, got={got!r}")

    # response.site
    if "site" in expect:
        got = body.get("site")
        if got != expect["site"]:
            failures.append(f"site: want={expect['site']!r}, got={got!r}")

    methods = body.get("payment_methods") or []
    metadata = body.get("metadata") or {}
    product = body.get("product") or {}
    error = body.get("error") or {}

    if "n_methods_min" in expect:
        if len(methods) < expect["n_methods_min"]:
            failures.append(f"n_methods_min: want>={expect['n_methods_min']}, got={len(methods)}")

    if "n_methods_max" in expect:
        if len(methods) > expect["n_methods_max"]:
            failures.append(f"n_methods_max: want<={expect['n_methods_max']}, got={len(methods)}")

    if "source" in expect:
        want = expect["source"]
        got = metadata.get("payment_methods_source")
        if isinstance(want, list):
            if got not in want:
                failures.append(f"source: want one of {want}, got={got!r}")
        else:
            if got != want:
                failures.append(f"source: want={want!r}, got={got!r}")

    if "llm_calls_max" in expect:
        got = metadata.get("llm_calls", 0)
        if got > expect["llm_calls_max"]:
            failures.append(f"llm_calls_max: want<={expect['llm_calls_max']}, got={got}")

    if "duration_ms_max" in expect:
        got = metadata.get("duration_ms", 0)
        if got > expect["duration_ms_max"]:
            failures.append(f"duration_ms_max: want<={expect['duration_ms_max']}, got={got}")

    if "brands_must_include" in expect:
        got_brands = {m.get("brand") for m in methods}
        for b in expect["brands_must_include"]:
            if b not in got_brands:
                failures.append(f"brands_must_include: missing {b!r} (got {sorted(got_brands)})")

    if "brands_must_not_include" in expect:
        got_brands = {m.get("brand") for m in methods}
        for b in expect["brands_must_not_include"]:
            if b in got_brands:
                failures.append(f"brands_must_not_include: forbidden {b!r} present")

    if "product_title_contains" in expect:
        title = (product.get("title") or "")
        if expect["product_title_contains"].lower() not in title.lower():
            failures.append(
                f"product_title_contains: want substring {expect['product_title_contains']!r} "
                f"in title={title!r}"
            )

    if "product_currency" in expect:
        cur = (product.get("price") or {}).get("currency")
        if cur != expect["product_currency"]:
            failures.append(f"product_currency: want={expect['product_currency']!r}, got={cur!r}")

    if "error_code" in expect:
        if error.get("code") != expect["error_code"]:
            failures.append(f"error_code: want={expect['error_code']!r}, got={error.get('code')!r}")

    if "error_stage" in expect:
        if error.get("stage") != expect["error_stage"]:
            failures.append(f"error_stage: want={expect['error_stage']!r}, got={error.get('stage')!r}")

    if "max_total_time_s" in expect:
        if total_time_s > expect["max_total_time_s"]:
            failures.append(
                f"max_total_time_s: want<={expect['max_total_time_s']}, got={total_time_s:.2f}"
            )

    return failures


# -----------------------------------------------------------------------------
# Case runner
# -----------------------------------------------------------------------------
def should_skip(case: dict, args: argparse.Namespace) -> tuple[bool, str]:
    """Politica de skip: --skip-falabella, requires, env vars."""
    case_id = case.get("id", "?")

    if args.case and case_id != args.case:
        return True, f"--case={args.case} (skipped {case_id})"

    if args.skip_falabella and "falabella" in case_id.lower():
        return True, "--skip-falabella"

    skip_env = case.get("skip_if_env")
    if skip_env and os.environ.get(skip_env, "").lower() in ("true", "1", "yes"):
        return True, f"env {skip_env}=true"

    return False, ""


def run_case(case: dict, base_url: str, args: argparse.Namespace) -> CaseResult:
    case_id = case.get("id", "?")
    description = case.get("description", "")
    expect = case.get("expect", {})
    request = case.get("request", {})

    skip, reason = should_skip(case, args)
    if skip:
        return CaseResult(
            case_id=case_id, description=description, passed=True,
            duration_s=0.0, skipped=True, skip_reason=reason,
        )

    url = request.get("url")
    payload = {"url": url}
    timeout = expect.get("max_total_time_s", 60.0) + 10.0  # margen para pretty-print

    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(f"{base_url}/scrape", json=payload)
        total_time_s = time.perf_counter() - t0
    except httpx.TimeoutException:
        total_time_s = time.perf_counter() - t0
        return CaseResult(
            case_id=case_id, description=description, passed=False,
            duration_s=total_time_s,
            failures=[f"httpx timeout after {timeout:.0f}s"],
        )
    except httpx.RequestError as e:
        total_time_s = time.perf_counter() - t0
        return CaseResult(
            case_id=case_id, description=description, passed=False,
            duration_s=total_time_s,
            failures=[f"httpx request error: {e}"],
        )

    try:
        body = r.json()
    except json.JSONDecodeError:
        return CaseResult(
            case_id=case_id, description=description, passed=False,
            duration_s=total_time_s,
            failures=[f"response not JSON (status={r.status_code}): {r.text[:200]!r}"],
        )

    failures = assert_case(expect, r.status_code, body, total_time_s)

    summary = {
        "status_code": r.status_code,
        "response_status": body.get("status"),
        "site": body.get("site"),
        "n_methods": len(body.get("payment_methods") or []),
        "source": (body.get("metadata") or {}).get("payment_methods_source"),
        "llm_calls": (body.get("metadata") or {}).get("llm_calls", 0),
        "duration_ms": (body.get("metadata") or {}).get("duration_ms", 0),
        "total_time_s": round(total_time_s, 2),
    }
    if body.get("status") == "error":
        summary["error_code"] = (body.get("error") or {}).get("code")
        summary["error_stage"] = (body.get("error") or {}).get("stage")

    return CaseResult(
        case_id=case_id, description=description,
        passed=(len(failures) == 0),
        duration_s=total_time_s, failures=failures, response_summary=summary,
    )


# -----------------------------------------------------------------------------
# Reporters
# -----------------------------------------------------------------------------
def print_case_result(result: CaseResult) -> None:
    if result.skipped:
        print(f"  {C.YELLOW}-{C.RESET} {result.case_id} {C.DIM}({result.skip_reason}){C.RESET}")
        return

    status = f"{C.GREEN}PASS{C.RESET}" if result.passed else f"{C.RED}FAIL{C.RESET}"
    print(f"  {status} {result.case_id} {C.DIM}({result.duration_s:.2f}s){C.RESET}")
    print(f"        {C.DIM}{result.description}{C.RESET}")
    if result.response_summary:
        s = result.response_summary
        line = (
            f"        HTTP {s.get('status_code')}  "
            f"site={s.get('site')}  n_methods={s.get('n_methods')}  "
            f"source={s.get('source')}  llm={s.get('llm_calls')}  "
            f"server_ms={s.get('duration_ms')}"
        )
        if "error_code" in s:
            line += f"  error={s['error_code']}/{s.get('error_stage')}"
        print(C.DIM + line + C.RESET)
    for f in result.failures:
        print(f"        {C.RED}*{C.RESET} {f}")


def print_summary(results: list[CaseResult]) -> None:
    total = len(results)
    skipped = sum(1 for r in results if r.skipped)
    ran = total - skipped
    passed = sum(1 for r in results if r.passed and not r.skipped)
    failed = ran - passed
    total_time = sum(r.duration_s for r in results)

    print()
    print(f"{C.BOLD}Summary:{C.RESET}")
    print(f"  Total cases: {total}  ({ran} ran, {skipped} skipped)")
    print(f"  {C.GREEN}Passed:{C.RESET}  {passed}")
    if failed:
        print(f"  {C.RED}Failed:{C.RESET}  {failed}")
    print(f"  Total time: {total_time:.2f}s")
    print()


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Eval harness for /scrape endpoint.")
    p.add_argument("--base-url", default="http://localhost:8000",
                   help="Base URL of the running server (default: http://localhost:8000)")
    p.add_argument("--cases", default=str(Path(__file__).parent / "cases.yaml"),
                   help="Path to cases.yaml (default: scripts/cases.yaml)")
    p.add_argument("--case", default=None,
                   help="Run only this single case_id")
    p.add_argument("--skip-falabella", action="store_true",
                   help="Skip cases whose id contains 'falabella' (faster eval)")
    p.add_argument("--json", default=None,
                   help="Also write results to this JSON path (e.g. reports/eval_results.json)")
    p.add_argument("--no-color", action="store_true",
                   help="Disable ANSI color output")
    return p.parse_args()


def health_check(base_url: str) -> bool:
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{base_url}/health")
        return r.status_code == 200
    except httpx.RequestError:
        return False


def main() -> int:
    args = parse_args()
    if args.no_color or not sys.stdout.isatty():
        C.disable()

    cases_path = Path(args.cases)
    if not cases_path.exists():
        print(f"{C.RED}ERROR{C.RESET} cases file not found: {cases_path}", file=sys.stderr)
        return 2

    with open(cases_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    cases = config.get("cases", [])
    if not cases:
        print(f"{C.RED}ERROR{C.RESET} no cases in {cases_path}", file=sys.stderr)
        return 2

    print(f"{C.BOLD}Eval harness{C.RESET} -- target {args.base_url}")
    print(f"  Cases file: {cases_path} ({len(cases)} cases)")
    print()

    if not health_check(args.base_url):
        print(f"{C.RED}ERROR{C.RESET} server not responding at {args.base_url}/health")
        print(f"  Make sure to start it: uvicorn app.main:app --host 0.0.0.0 --port 8000")
        return 2

    print(f"  {C.GREEN}/health OK{C.RESET}")
    print()
    print(f"{C.BOLD}Running cases:{C.RESET}")

    results: list[CaseResult] = []
    for case in cases:
        result = run_case(case, args.base_url, args)
        print_case_result(result)
        results.append(result)

    print_summary(results)

    if args.json:
        out_path = Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps([
            {
                "case_id": r.case_id,
                "passed": r.passed,
                "skipped": r.skipped,
                "skip_reason": r.skip_reason,
                "duration_s": r.duration_s,
                "failures": r.failures,
                "response_summary": r.response_summary,
            }
            for r in results
        ], indent=2), encoding="utf-8")
        print(f"  JSON results: {out_path}")
        print()

    failed = sum(1 for r in results if not r.passed and not r.skipped)
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
