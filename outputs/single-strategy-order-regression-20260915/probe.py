"""Small real-model API regression on the changed checkout; NOT public acceptance."""
import getpass
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fastapi.testclient import TestClient
from ashare_lab.bootstrap import create_configured_app
from ashare_lab.settings import AppSettings
from ashare_lab.adapters.language.openai_compatible import OpenAICompatibleCandidateTransport

OUT = Path(__file__).parent / (sys.argv[2] if len(sys.argv) > 2 else ".")
OUT.mkdir(exist_ok=True)
# Diagnostic evidence is restricted to task-scoped model results, never headers,
# credentials or request configuration. The transport itself remains unchanged.
original_generate = OpenAICompatibleCandidateTransport.generate_json
trace_index = 0
async def traced_generate(self, request):
    global trace_index
    response = await original_generate(self, request)
    trace_index += 1
    (OUT / f"model-{trace_index}-{request.response_schema_name}.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2))
    if request.response_schema_name == "verified_stock_strategy_pairing" or (
        request.response_schema_name == "dialogue_reply_semantic_review"
        and "unexecutedProposalRules" in (request.user_payload or {})
    ):
        # Only this synthetic in-process test's public data and strategy review;
        # never serialize the transport, credentials, headers or live sessions.
        (OUT / f"pairing-context-{trace_index}.json").write_text(json.dumps({
            "purpose": request.response_schema_name,
            "context": request.user_payload,
            "contract": request.system_contract,
            "response": response,
        }, ensure_ascii=False, indent=2))
    return response
OpenAICompatibleCandidateTransport.generate_json = traced_generate
CASES = [
    ("historical_buy", "平安银行一年前买入10000股，盈利20%全部卖出", "conditional"),
    ("historical_buy_unsized", "平安银行一年前买入，涨20%卖出", None),
    ("start_buy", "平安银行回测开始时买入1000股，涨20%卖出", "conditional"),
    ("sell_word_first", "东方财富涨1卖，跌1买", "grid"),
    ("buy_word_first", "东方财富跌1买，涨1卖", "grid"),
    ("maotai_grid", "贵州茅台网格1%买卖", "grid"),
    ("explicit_sequence", "东方财富已有1000股可卖底仓，先涨1元卖100股，再跌1元买回100股，至少保留500股", "conditional"),
    ("macd_control", "东方财富MACD金叉买入，MACD死叉卖出", None),
    ("rsi_new_occurrence", "东方财富日线RSI低于40买入，RSI高于99卖出", None),
    ("roe_death_cross", "指南针roe低则买入，死叉卖出", None),
    ("unspecified_entry_stop_loss", "东方财富买一点，亏5%就走", None),
    ("inspiration_lychee", "我想吃荔枝，我是杨玉环，给我对应的交易策略", None),
    ("maotai_alias_grid", "茅台网格", "grid"),
    ("eastmoney_alias_monthly", "东财每月买，死叉卖", "scheduled"),
    ("blue_spaced_pe", "蓝色 光标 PE好于10买入，死叉卖出", None),
    ("spaced_name_breakout", "怡 亚 通放量突破买，跌破20日均线卖", None),
    ("low_pe_macd_pairing", "帮我选低市盈率股票，金叉买死叉卖", None),
    ("ambiguous20_lychee", "我想吃荔枝，给我交易灵感", None),
]
if len(sys.argv) > 1:
    CASES = [case for case in CASES if case[0] == sys.argv[1]]
key = subprocess.check_output(["security", "find-generic-password", "-s",
    "com.openai.codex.ashare-backtest.deepseek", "-a", getpass.getuser(), "-w"], text=True).strip()
with tempfile.TemporaryDirectory(prefix="price-order-regression-") as temporary:
    settings = AppSettings(
        market_data_profile="eastmoney_skill", event_data_required=False,
        minute_grid_enabled=True, external_minute_root=Path("/Volumes/外地磁盘/stock_1min"),
        candidate_provider_mode="openai_compatible", candidate_provider_name="deepseek",
        candidate_provider_endpoint="https://api.deepseek.com/chat/completions",
        candidate_provider_model="deepseek-v4-flash", candidate_provider_api_key=key,
        candidate_provider_timeout_seconds=30,
        candidate_provider_response_mode="json_object", candidate_provider_thinking="disabled",
        plan_deep_provider_mode="openai_compatible", plan_deep_provider_name="deepseek",
        plan_deep_provider_endpoint="https://api.deepseek.com/chat/completions",
        plan_deep_provider_model="deepseek-v4-pro", plan_deep_provider_api_key=key,
        plan_deep_provider_timeout_seconds=180, plan_deep_provider_thinking="enabled",
        plan_deep_provider_reasoning_effort="high",
        database_url="sqlite+pysqlite:///" + str(Path(temporary) / "run.sqlite"),
    )
    results = []
    with TestClient(create_configured_app(settings)) as client:
        for name, utterance, kind in CASES:
            start = time.monotonic()
            print(json.dumps({"started": name, "utterance": utterance}, ensure_ascii=False), flush=True)
            response = client.post("/api/v1/strategy-drafts", json={
                "utterance": utterance, "as_of_date": "2026-09-15"})
            body = response.json()
            initial_body = body
            selection_http = None
            if body.get("diagnostic_code") == "idea_guidance_required":
                proposals = body["idea_route"]["proposals"]
                if proposals:
                    answer = client.post(
                        f"/api/v1/strategy-drafts/{body['draft_id']}"
                        f"/revisions/{body['revision']}/clarification-answers",
                        json={"answer": proposals[0]["id"]},
                    )
                    selection_http = answer.status_code
                    if answer.status_code == 200:
                        body = answer.json()["draft"]
            if name == "roe_death_cross" and body.get("diagnostic_code") == "numeric_threshold_requires_clarification":
                answer = client.post(
                    f"/api/v1/strategy-drafts/{body['draft_id']}"
                    f"/revisions/{body['revision']}/clarification-answers",
                    json={"answer": "10%"},
                )
                selection_http = answer.status_code
                if answer.status_code == 200:
                    body = answer.json()["draft"]
            if name == "blue_spaced_pe" and body.get("status") == "needs_clarification":
                answer = client.post(
                    f"/api/v1/strategy-drafts/{body['draft_id']}"
                    f"/revisions/{body['revision']}/clarification-answers",
                    json={"answer": "对的，macd死叉"},
                )
                selection_http = answer.status_code
                if answer.status_code == 200:
                    body = answer.json()["draft"]
            strategy = body.get("strategy") or {}
            plan = strategy.get("trading_plan") or {}
            result = {"case": name, "utterance": utterance, "http": response.status_code,
                "status": body.get("status"), "diagnostic": body.get("diagnostic_code"),
                "expected_kind": kind, "actual_kind": plan.get("kind"),
                "elapsed_seconds": round(time.monotonic() - start, 2), "response": body,
                "selection_http": selection_http}
            if initial_body is not body:
                result["initial_response"] = initial_body
            results.append(result)
            (OUT / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps({k: v for k, v in result.items()
                              if k not in {"response", "initial_response"}}, ensure_ascii=False), flush=True)
    (OUT / "results.json").write_text(json.dumps({
        "scope": "changed local checkout + real model API; draft-generation regression, not public deployment or backtest returns",
        "cases": results}, ensure_ascii=False, indent=2))
