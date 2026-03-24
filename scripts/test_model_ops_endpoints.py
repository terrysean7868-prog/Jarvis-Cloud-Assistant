from __future__ import annotations

import asyncio
import json

import apps.web.app as A


async def run() -> dict:
    results: dict = {
        "endpoint_tests": {},
        "auth_tests": {},
    }

    # Success path with patched auth guards.
    A._require_authenticated_session = lambda sid: {"username": "tester", "role": "admin"}
    A._require_admin_session = lambda sid: {"username": "tester", "role": "admin"}

    status = await A.model_ops_status("sid")
    catalog = await A.model_ops_catalog("sid")
    recommend = await A.model_ops_recommend(
        A.ModelOpsRecommendRequest(session_id="sid", mode="hybrid", constraints={"ram_gb": 16})
    )
    readiness = await A.model_ops_readiness_check(A.ModelOpsReadinessRequest(session_id="sid"))
    prepare = await A.model_ops_prepare_finetune(
        A.ModelOpsPrepareFinetuneRequest(session_id="sid", dry_run=True)
    )
    benchmark = await A.model_ops_benchmark(A.ModelOpsBenchmarkRequest(session_id="sid"))

    results["endpoint_tests"] = {
        "status_ok": status.get("status") == "success",
        "catalog_models": len(catalog.get("models") or []),
        "recommendation_present": bool(recommend.get("recommendation")),
        "readiness_present": bool(readiness.get("readiness")),
        "prepare_present": bool(prepare.get("result")),
        "benchmark_present": bool(benchmark.get("benchmark")),
    }

    # Auth behavior checks.
    A._require_authenticated_session = lambda sid: (_ for _ in ()).throw(PermissionError("auth_failed"))
    A._require_admin_session = lambda sid: (_ for _ in ()).throw(PermissionError("admin_required"))

    auth_errors = []
    try:
        await A.model_ops_status("bad")
    except Exception as e:
        auth_errors.append(str(e))

    try:
        await A.model_ops_prepare_finetune(A.ModelOpsPrepareFinetuneRequest(session_id="bad", dry_run=True))
    except Exception as e:
        auth_errors.append(str(e))

    results["auth_tests"] = {
        "blocked_calls": len(auth_errors),
        "errors": auth_errors,
    }

    return results


if __name__ == "__main__":
    out = asyncio.run(run())
    print(json.dumps(out, indent=2, ensure_ascii=False))
