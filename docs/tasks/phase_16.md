# Phase 16-18 — 全盤重評估與修改計畫

> ## ✅ Phase 16 執行完成（2026-08-05，branch `phase-16-hardening`）
>
> | 項目 | commit | 結果 |
> |---|---|---|
> | 16-A 文件真實性對帳 | `de7e080` | D1-D12 + Q1/Q3 決策文件化 |
> | 16-C 測試環境隔離 | `141602a` | Langfuse 逾時訊息消失 |
> | 16-B chip Verifier + **16-F** | `b47c646` | 888 → 894 tests |
> | 16-D SDK 統一 + Langfuse cost | `0fc545b` | langchain 完全脫離、8/8 drop-in |
> | 16-E query_type 切分 | `ff9885a` | 894 → 903 tests |
>
> **最終驗收**：ruff ✅ / mypy ✅ / pytest **903 passed, 1 skipped**（基準 888）
>
> ### 🔴 16-F —— 計畫外的 P0 發現（本次最有價值的產出）
>
> 寫 16-B 測試時意外揭露：**`agents/verifier.py` 對 4 位數以上、無千分位逗號的
> 數字完全偵測不到**（`1234` / `987654` / `-80773006` 全部漏掉），
> 而這正是 LLM 在財經語境最常幻覺的數字型態。
>
> 根因：`_NUM_RE` 整數部分為 `\d{1,3}(?:,\d{3})*`，搭配前後
> `(?<!\d)`/`(?!\d)` 邊界後長數字一個都匹配不到。原本「排除 19xx/20xx 年份」
> 的 lookahead 因此是死碼——反證原作者以為 4 位數會被攔下。
>
> 影響：**全部 6 個接 Verifier 的 agent**。這是設計原則①
> 「LLM 永遠不產出數字」唯一的程式化防線。
>
> 修復：regex 改為「千分位格式 | 任意連續數字」；新增 `symbol_allowlist()`
> 處理台股 4 碼代號的誤判（修好 regex 後「台積電 2330」會被當成幻覺數字）；
> `check_narrative()` 新增 `allow` 參數（預設 None，向後相容）。
>
> **這件事的意義**：一個存在已久、三關驗收全綠、888 個測試都測不出來的
> 核心防線缺陷，是「補一個 0 覆蓋函式的測試」時才浮出來的。
> 這正是 Phase 17 評估框架的價值預演——**沒有測試就沒有發現**。
>
> ### 執行過程的其他修正（與原計畫不同之處）
>
> 1. **16-B 處置方式修正**：原計畫寫「驗證失敗 → fallback 模板」，
>    實查其餘 5 個 agent 全部是「記錄錯誤進 errors、保留 narrative」
>    （technical:616 / macro:744 / news:665 / cross_market:528 / risk:693）。
>    改為比照 5:0 的既有慣例，保留違規證據供 Supervisor 調整 data_quality。
> 2. **16-C 需要兩道閘門**：換上 `langfuse.openai` drop-in 後逾時訊息復發。
>    原因是 drop-in 是 SDK 內建 instrumentation，**完全不看**專案自訂的
>    `LANGFUSE_ENABLED`，必須另外設 SDK 官方的 `LANGFUSE_TRACING_ENABLED`。
> 3. **D10 未處理**：`AGENTS.md` / `CLAUDE.md` 的 GitNexus 區塊重複，
>    但兩處皆由 `<!-- gitnexus:start/end -->` 標記由工具自動注入管理，
>    手動去重會被下次 `gitnexus analyze` 還原，故不處理。
> 4. **`supervisor/graph.py` 改名取消**：實測 blast radius 14 個 import 點，
>    純美觀改名成本效益不划算，改為只修文件說明。
>
> ---

> 制定日期：2026-08-05（取代 2026-08-04 初版）
> 依據：三關驗收 + GitNexus 圖譜 + **全部 14 份專案 markdown 逐份閱讀** + 逐檔案讀碼
> 前置狀態：ruff/mypy 全綠、888 passed / 1 skipped、85% 覆蓋率、0 import 循環依賴

---

## 零、本次重評估的核心結論

**程式碼品質是好的，問題在文件宣稱與實作的落差。**

初版計畫（2026-08-04）聚焦「程式碼技術債」，方向沒錯但**漏掉了更大的一層**：
把 14 份 markdown 全部讀完後，發現多處文件描述的系統比實際存在的更先進。
對一個明確以面試敘事為目標的專案（`spec.md §11`、`rag_spec.md §7`），
**文件與實作對不上的風險，高於任何一項程式碼技術債**——
面試官翻開 `docs/PROGRESS.md` 看到「Phase 0-2 完成」，或追問
「你 spec 裡寫的 golden set 評估怎麼做的？」時，這是無法回答的。

因此本次重排優先序：**文件真實性對帳 → 正確性修復 → 補建最高價值的缺失能力（評估框架）**。

---

## 一、文件真實性落差清單（本輪新增發現）

| # | 文件宣稱 | 實際狀況 | 嚴重度 |
|---|---|---|---|
| D1 | `docs/PROGRESS.md`：「上次進度（2026-07-19）Phase 0-2 完成」 | 專案已到 Phase 15，此檔停留在 13 個 Phase 前 | 🔴 高 |
| D2 | `CLAUDE.md` 目前進度列到 Phase 14 為止 | Phase 15 已完成（README 與 memory 皆確認），CLAUDE.md 漏記 | 🔴 高 |
| D3 | `README.md`：「867 passed」、「tests/ 867 tests」 | 實際 888 passed + 1 skipped | 🟡 中 |
| D4 | `docs/spec.md`：全篇「**六**大 Domain Agent」 | 實際 7 個（Phase 7 新增 chip agent）。「chip/籌碼」在 spec.md 全文只出現 1 次 | 🔴 高 |
| D5 | `docs/refactor_plan.md §二`：目標架構「7 個 ReAct Agent」輸出 `DomainReport` | **只有 `chip_agent` 輸出 DomainReport**，其餘 6 個仍輸出 AgentSignal | 🔴 高 |
| D6 | `schemas/domain_report.py`：`ReasoningStep.thought # LLM 的推理` | chip_agent 的 `thought` 是**寫死的中文字串**（如「外資動向是最重要的先行指標。」），無 LLM 參與 | 🔴 高 |
| D7 | `spec.md §8.2`：golden set 迴歸測試跑進 CI | **零實作**。全 codebase 無任何 eval / golden / faithfulness 程式碼 | 🔴 高 |
| D8 | `spec.md §4.3` + `rag_spec.md`（26KB）：財報質化 RAG | **本 repo 零實作**（無 embedding / vector / retrieval）。但依 `spec.md §1`，此工作歸屬 `FinancialReports` repo → 見決策 Q3 | 🟡 中（歸屬待確認） |
| D9 | `CLAUDE.md`：「框架 LangGraph……Supervisor 是編排 graph」 | `supervisor/graph.py` 完全無 `StateGraph`（0 個 `add_node`） | 🟡 中 |
| D10 | `AGENTS.md` 與 `CLAUDE.md` 各含一份完全相同的 GitNexus 區塊 | 內容重複，兩處需同步維護 | 🟢 低 |
| D11 | `README.md` 查詢類型路由表 | Phase 16-E 新增 `stock_investment` 後會過期，需同步 | 🟢 低（隨 E 一起修） |
| D12 | `SESSION_HANDOFF.md §七` P0/P1/P2 清單 | 部分已解、部分未解，未標註狀態 | 🟡 中 |

---

## 二、架構層級發現（本輪新增）

### A1 — 雙 schema 是「刻意停在半途」，不是意外

`refactor_plan.md §十二` 明確寫：
> 4. **最後 Phase D（各 Agent ReAct 化）** — 最費工，但有了前三步系統已可實用

Phase D 被**刻意延後**，所以現況是：

```
chip_agent      → DomainReport ─┐
                                ├─ domain_report_to_agent_signal() ─→ Supervisor
其餘 6 個 agent  → AgentSignal ──┘
```

橋接函式在 `api/main.py:561` 與 `supervisor/graph.py:483` 各呼叫一次。
**這不是 bug，但 `refactor_plan.md` 把「7 個 ReAct Agent」寫成目標架構卻沒標註未完成**，
讀者會誤以為已達成。需要在文件中明確標記 Phase D 的狀態與決策。

### A2 — 最大測試盲區：**改任何 prompt，888 個測試全部照過**

已驗證：
- 沒有任何測試斷言 prompt 內容（`grep _SYSTEM_PROMPT` in tests → 空）
- 所有 LLM 呼叫在測試中一律 mock（5 個測試檔用 MagicMock / fake client）

代表 `_ROUTER_SYSTEM_PROMPT`、`_BULL/_BEAR/_PM_SYSTEM_PROMPT`、chip / fundamental /
news / synthesis 的 prompt **全部無測試守護**。對一個 LLM narrative 是核心輸出的系統，
這是最大的迴歸風險——也正是 `spec.md §8.2` 要求 golden set 的原因。

現況有的是 `tests/test_phase5_supervisor.py` 的 91 個測試（含 S1-S5 情境類），
但它們驗證的是**確定性規則引擎**，不是 LLM 輸出品質。兩者不能互相替代。

### A3 — `rag_spec.md` 可能放錯 repo

`spec.md §1` 明確把 RAG 歸屬給 `FinancialReports`
（「FinancialReports……`document_chunks` 補完 RAG 後成為質化檢索來源」，
Phase 1b「補完 **FinancialReports** 的 pgvector RAG」）。
但 26KB 的 `rag_spec.md` 放在 `quantdesk-starter/docs/`。需決策歸屬（Q3）。

### A4 — 上一輪已確認、仍然有效的程式碼技術債

| 項目 | 位置 | 已於本輪複驗 |
|---|---|---|
| chip_agent 唯一未接 Verifier | `agents/chip_agent.py:306-376` | ✅ 仍是 |
| 測試依賴本機 `.env`（Langfuse 逾時） | `.env` + `langfuse_setup.py:107` | ✅ 仍是 |
| LangChain 孤例 + 隱性跨 repo 依賴 | `agents/fundamental_agent.py:555` | ✅ 仍是 |
| Langfuse 無 token/cost 追蹤 | 8 個 LLM 呼叫點全部 | ✅ 仍是 |
| 組合層 risk 汙染個股建議 | `router/intent_router.py:152` | ✅ 仍是 |

---

## 三、Phase 16 — Truth & Correctness（本次主體）

> 順序原則：先讓文件說真話（最便宜、風險最高的先解），再修正確性。

### 16-A｜文件真實性對帳（**最優先，零程式碼風險**）

| 動作 | 檔案 |
|---|---|
| 重寫或刪除過期進度檔 | `docs/PROGRESS.md`（D1） |
| 補上 Phase 15 | `CLAUDE.md` 目前進度（D2） |
| 測試數 867 → 888 | `README.md` ×2 處（D3） |
| 加註 chip agent（第 7 個）+ 標記 spec 版本狀態 | `docs/spec.md`（D4） |
| 標記 Phase D 未執行 + 記錄決策理由 | `docs/refactor_plan.md`（D5、A1） |
| `ReasoningStep.thought` 註解改為「步驟說明（確定性文字，非 LLM 生成）」 | `schemas/domain_report.py`（D6） |
| Supervisor 描述改為「純 Python 三層規則引擎，刻意不用 StateGraph」+ 理由 | `CLAUDE.md`（D9） |
| GitNexus 區塊擇一保留，另一處改為指向連結 | `AGENTS.md` / `CLAUDE.md`（D10） |
| P0/P1/P2 逐項標註 ✅已解 / ⬜未解 | `docs/SESSION_HANDOFF.md`（D12） |
| **【決策 Q1】** 明文記錄雙 schema 正式凍結：`AgentSignal` 為唯一跨 agent 契約，`DomainReport` 為 chip agent 專用擴充，Phase D 正式放棄並記錄理由 | `CLAUDE.md` + `docs/refactor_plan.md`（A1） |
| **【決策 Q3】** 頂部加範圍註記：此規格描述的 RAG 依 `spec.md §1` 屬 `FinancialReports` repo 職責，本 repo 不實作，保留作架構參考 | `docs/rag_spec.md`（A3、D8） |

**工作量**：0.5-1 天。**風險**：零（純文件）。

---

### 16-B｜chip_agent 接上 Verifier（憲法違規，最高程式碼優先）

- `_llm_synthesize_chip()` 回傳前加 `check_narrative(content, key_findings)`
- 比照 `technical_agent.py:531` 的 `@observe(..., as_type="tool")` 包裝模式
- 驗證失敗 → 走**既有**的 `_fallback_narrative()`
- **必須補測試**：coverage 顯示 `chip_agent.py` 327-376 目前 **0 覆蓋**

**工作量**：0.5 天

---

### 16-C｜測試環境隔離

**⚠️ 不可用 autouse fixture**：`langfuse_setup.py:107` 的 `_activate()` 在 module import
時執行（collection 階段），早於任何 fixture。

`tests/conftest.py` **最頂端、所有 import 之前**：

```python
import os
# 必須在任何 agent / observability import 之前執行：
# langfuse_setup._activate() 在 module import 時就讀此變數並綁定 observe。
# 用 os.environ[...] 而非 setdefault —— 全專案 8 處 load_dotenv() 皆為
# override=False，不會覆蓋此設定。
os.environ["LANGFUSE_ENABLED"] = "false"
```

**驗收**：`pytest -q` 結尾不再出現 `Failed to export span batch due to timeout`

**工作量**：0.5 天

---

### 16-D｜SDK 統一 + Langfuse 官方 drop-in（**須在 16-B 之後**）

順序理由：本項會置換 16-B 改過的同一檔案。

1. `agents/fundamental_agent.py:555-556` LangChain → 原生 OpenAI SDK，移除 2 個 `type: ignore`
   （切斷「LLM narrative 繫於 `Financial_Agent` 順帶帶入 langchain」的隱性依賴）
2. 8 個呼叫點 `from openai import ...` → `from langfuse.openai import ...`
   （已驗證 `langfuse==4.14.0` 內建，API 相容 drop-in，自動記錄 model / usage / cost）
3. 外層 `@observe(..., as_type="generation")` 改為不指定 `as_type`，避免 generation 巢狀

**驗收特殊**：pytest 測不出 dashboard 有無 cost，需手動確認一次 trace 畫面。

**工作量**：1-1.5 天

---

### 16-E｜`stock_investment` query_type 切分（P0-2）

**問題**：`investment_strategy` 含 risk agent 且 `run_supervisor=True`，
但 risk 讀 `positions.yaml` 分析的是**整個組合**。
問「台積電值得買嗎」會被不相關部位的 breach 強制降級（confidence 壓到 0.35）。

**現況佐證**：`router/intent_router.py:195-197` keyword fallback 中
`_STRATEGY_KEYWORDS` 命中時 `scenario = "single_stock"`，
prompt 裡該類型三個範例也全是個股 → 證明它今天語意上**就是個股建議**。

| query_type | 語意 | agents | supervisor / debate |
|---|---|---|---|
| `stock_investment`（新增） | 個股投資建議 | technical, chip, news, fundamental, macro, cross_market（**無 risk**） | ✅ / ✅ |
| `investment_strategy`（收窄為組合層） | 組合策略調整 | 全 agent **含 risk** | ✅ / ✅ |
| `portfolio_risk`（不變） | 純 Greeks 查詢 | risk only | ❌ / ❌ |

**改動清單**：
1. `schemas/domain_report.py:133` — `query_type` Literal 加 `"stock_investment"`
   （註：這是 `RouterOutput`，**非**已鎖定的共用契約 `schemas/agent_signal.py`，
   不影響 6 個 domain agent 輸出格式，不觸發 CLAUDE.md「先停下來問人」條款）
2. `router/intent_router.py:152` — `_QUERY_TYPE_AGENTS` 新增鍵
3. `_ROUTER_SYSTEM_PROMPT` — 新增類型說明；現有三個個股範例**移到** `stock_investment`，
   為 `investment_strategy` 補組合層範例
4. `router/intent_router.py:197` — keyword fallback 改為 `stock_investment`
5. **6 處硬編碼比較改集合判斷**：`api/main.py:309,310`、`router/intent_router.py:217,218,256,257`
   ```python
   _SUPERVISOR_QUERY_TYPES = frozenset({"stock_investment", "investment_strategy"})
   ```
6. **同步更新 `README.md` 查詢類型路由表**（D11）

**已知取捨**：此模式不評估組合風控。建議加被動提示
（「本次分析未納入您的組合風險，如需請改問組合策略」）。

**工作量**：1 天

---

## 四、Phase 17 — Evaluation Framework（**已拍板：L1-L4 全做**）

### 為什麼這是最高價值的下一步

1. `spec.md §8.2` 明文承諾但**零實作**（D7）
2. 補上 A2 這個最大測試盲區——目前改任何 prompt 888 個測試全過
3. 對面試敘事而言，「多 agent 系統怎麼做 LLM 迴歸測試」是比
   「你用了什麼框架」深得多的問題，且是目前 spec 裡唯一講了卻做不出來的部分

### 建議範圍（由小到大，可分批）

| 層次 | 內容 | 產出 |
|---|---|---|
| L1 | **Prompt 快照測試** — 對每個 system prompt 做 golden 快照，改動時強制 review | 最便宜，先做 |
| L2 | **Router golden set** — 30-50 組（查詢 → 期望 query_type / agents），純確定性斷言 | 不需真 LLM，可全 mock |
| L3 | **Supervisor golden set** — 把 `phase_5_supervisor_design.md §三` 的 S1-S5 情境從硬寫測試改為**資料驅動** YAML/JSON fixture | 可擴充、可版本化 |
| L4 | **LLM 輸出品質評估** — narrative 的 faithfulness（數字是否全部來自 metrics），可用既有 `verifier.py` 當評分器 | 復用現有資產 |

L1+L2 約 2 天且不需要呼叫真實 LLM（可進 CI）；
L3+L4 約 2-3 天，L4 若要跑真 LLM 需另設 CI job（不阻塞主線）。

**已拍板：L1-L4 全做**，完整兌現 `spec.md §8.2` 承諾。建議仍分批交付：
L1+L2 先進 CI（純確定性、零 LLM 成本），L3 把 `phase_5_supervisor_design.md §三`
的 S1-S5 從硬寫測試改為 YAML fixture，L4 的 faithfulness 評分器直接復用
`agents/verifier.py::check_narrative`（已存在的資產，不需重寫規則）。

**⚠️ L4 前置依賴**：faithfulness 評分要對「所有 agent 的 narrative」生效，
需要 chip_agent 先接上 Verifier（16-B）。故 **Phase 17 L4 必須排在 16-B 之後**。

**工作量**：4-5 天（分批交付）

---

## 五、已拍板的決策紀錄（2026-08-05）

| # | 決策 | 結論 | 落地位置 |
|---|---|---|---|
| Q1 | 雙 schema 何去何從 | **正式凍結 + 文件化**。`AgentSignal` 為唯一跨 agent 契約，`DomainReport` 為 chip agent 專用擴充，Phase D 正式放棄。用文件消除不一致，不做重構（省下約 5 天） | 16-A |
| Q2 | Evaluation Framework 範圍 | **L1-L4 全做**，完整兌現 `spec.md §8.2` | Phase 17 |
| Q3 | `rag_spec.md` 歸屬 | **留下但加範圍註記**——依 `spec.md §1` 屬 FinancialReports 職責，本 repo 不實作，保留作架構參考 | 16-A |

---

## 六、Phase 19 — 前端品質（未排程，待需求觸發）

`dashboard/` 目前 **0 測試、無 ESLint**，CI 只跑 `tsc -b && vite build`。
2474 行 TSX（`PositionsPanel.tsx` 421 行、`AgentCard.tsx` 365 行、`App.tsx` 359 行）
全靠手動驗證，`tsc` 型別檢查 ≠ 行為正確性。

建議範圍：`vitest` + `@testing-library/react`，優先覆蓋 `useAnalysis.ts`（SSE 狀態機，
邏輯最重）與 `PositionsPanel.tsx` 欄位驗證；加 flat config ESLint，CI 補 `npm run lint`。

**工作量**：2-3 天。**未納入本輪**，Phase 16-17 完成後再評估。

---

## 七、執行順序總表

```
Phase 16（3.5-4.5 天）
  16-A 文件對帳 ──┐（零風險，最優先；含 Q1 凍結決策 + Q3 範圍註記）
  16-C 測試隔離 ──┤（獨立）
  16-E query_type ┘（獨立）
  16-B chip Verifier ──→ 16-D SDK + Langfuse（嚴格順序，同檔案）
         │
         └──────────────────────────────┐
                                        ▼（L4 前置依賴）
Phase 17（4-5 天，分批交付）
  L1 prompt 快照 ─→ L2 router golden ─→ L3 supervisor 資料驅動 ─→ L4 faithfulness
  └─ L1+L2 純確定性、零 LLM 成本，可直接進 CI ─┘   └─ L4 需另設 CI job ─┘

Phase 19（未排程）
  前端測試 + ESLint
```

**合計 Phase 16+17 約 8-10 天。**
16-A 建議第一天就完成——零風險，且消除最高的面試可信度風險。

---

## 八、完成標準（Phase 16）

- [ ] `uv run ruff check .` 零 error 零 warning
- [ ] `uv run mypy .` zero issues
- [ ] `uv run pytest -q` 全過（新增測試後 > 888）
- [ ] `pytest` 結尾不再出現 `Failed to export span batch due to timeout`
- [ ] `agents/chip_agent.py` `_llm_synthesize_chip` 有測試覆蓋（原為 0）
- [ ] `grep -rn "import langchain" agents/` 無結果
- [ ] Langfuse dashboard 可見各 agent token 用量與成本（手動驗證）
- [ ] 「台積電值得買嗎」→ `stock_investment`，不因無關組合部位被強制降級
- [ ] 「我的組合該怎麼調整」→ `investment_strategy`，保留 risk 強制降級
- [ ] **D1-D12 逐項確認文件與實作一致**
- [ ] 雙 schema 凍結決策已寫入 `CLAUDE.md` + `refactor_plan.md`（Q1）
- [ ] `rag_spec.md` 已加範圍註記（Q3）
- [ ] 每個 PR 於 feature branch 完成、CI 全綠後才合併（不直接 push main）

---

## 九、完成標準（Phase 17）

- [ ] L1：每個 system prompt 有 golden 快照，改動 prompt 會讓測試失敗並強制 review
- [ ] L2：Router golden set ≥ 30 組（查詢 → 期望 query_type / agents），全確定性、進 CI
- [ ] L3：`phase_5_supervisor_design.md §三` S1-S5 情境改為資料驅動 fixture，可擴充
- [ ] L4：narrative faithfulness 評分器上線（復用 `verifier.py::check_narrative`），
      涵蓋全部 7 個 agent（**依賴 16-B 先完成**）
- [ ] 三關驗收全過，CI 綠燈
