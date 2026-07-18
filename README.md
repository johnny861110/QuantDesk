# QuantDesk 起手包 — 操作指南

這是可以直接 clone 下去、開 Claude Code 平行開發的 repo 起手包。照以下流程走。

## 這個起手包裡有什麼

```
CLAUDE.md                    ← Claude Code 每次 session 都會讀的「憲法」（三條鐵則+架構）
docs/
  spec.md                    ← 完整系統規格（放進來給 agent 查，不貼進對話）
  rag_spec.md                ← 財報 RAG 詳細設計（chunking/檢索/評估）
  tasks/phase_0.md ~ 6.md    ← 每個 Phase 的獨立任務描述 + 完成標準
schemas/agent_signal.py      ← 六個 agent 的共同合約（骨架已寫好）
adapters/base.py             ← 資料源抽象基類（骨架已寫好）
supervisor/graph.py          ← Supervisor 骨架（Phase 0 stub）
tests/test_phase0.py         ← Phase 0 驗證測試
.claude/agents/*.md          ← 預設的 subagent 定義（risk/technical/data 工程師）
```

## 開發前置（用 uv 管理）

本專案全程用 [uv](https://docs.astral.sh/uv/) 管理 Python 版本、虛擬環境與依賴。

```bash
# 安裝 uv（若尚未安裝）
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS/Linux

cd quantdesk-starter
uv sync                    # 一鍵：建 .venv + 裝 dependencies + dev group + 產生 uv.lock
uv run pytest -q           # 確認 Phase 0 骨架測試能過（4 passed）
```

`uv sync` 會依 `pyproject.toml` 建立環境並鎖定到 `uv.lock`（進版控，確保團隊/CI 一致）。
之後所有指令都用 `uv run <cmd>` 執行，不需手動 activate 虛擬環境。

**常用 uv 指令**
```bash
uv add <package>              # 加一般依賴（自動更新 pyproject.toml + uv.lock）
uv add --dev <package>        # 加開發依賴（進 dev group）
uv run pytest -q              # 跑測試
uv run ruff check .           # lint
uv run mypy .                 # 型別檢查
uv lock                       # 只重新鎖定不安裝
uv export --format requirements-txt > requirements.txt  # 匯出給不支援 uv 的環境
```

## 核心流程：先序列立骨架，再平行長肌肉

### 第 1 步 — Phase 0（序列，不可平行，最需要你 review）
開 Claude Code，第一句這樣說：

> 讀 CLAUDE.md 和 docs/spec.md，然後只做 docs/tasks/phase_0.md。
> 完成後停下讓我 review，不要往下做。

Phase 0 的骨架起手包已附大部分，這步主要是補 requirements/pyproject 並確認 schema
符合你的需求。**schema 一旦鎖定，後面平行才不會打架。**

### 第 2 步 — Phase 1（序列，財報 domain，投報率最高）
schema 鎖定後：

> 現在做 docs/tasks/phase_1.md，把我的 FinancialReports 和 Financial_Agent
> 兩個 repo 接進來當財報 domain agent。完成後停下。

（先把你兩個現有 repo 放進來，或給 Claude Code 存取路徑。）

### 第 3 步 — Phase 2-4（此時才開平行）
schema 已鎖定、財報 domain 已驗證，現在可以平行實作其他 agent。這樣說：

> 用 subagent 平行實作以下三個任務，每個都嚴格遵守 schemas/agent_signal.py，
> 不得修改共同 schema：
> 1. risk-engineer 做 docs/tasks/phase_2.md（Greeks 風控）
> 2. technical-engineer 做 docs/tasks/phase_3.md（技術面+跨市場）
> 3. data-engineer 做 docs/tasks/phase_4.md（新聞+總經）
> 各自完成後跑 pytest，回報結果。

**必須明確講「用 subagent 平行」**，否則 Claude Code 可能依序處理。

### 第 4 步 — Phase 5（序列，Supervisor 仲裁）
六個 agent 都能獨立運作後：

> 做 docs/tasks/phase_5.md，把 Supervisor 從 stub 升級成三層仲裁。
> 特別測試：技術面看多 + 風控觸限 → 最終建議必須降級。

### 第 5 步 — Phase 6（Production 硬化）

## Git / 版控工作流程

### 第 0 步：先建 GitHub repo，再開始開發
建議在跑任何 Claude Code 指令之前就先建好 repo——這樣每個 Phase 完成都能直接
commit/push，你會有完整的開發軌跡（面試時可以直接秀 commit history，證明你是
一步步用工程紀律做出來的，不是一次生成）。

```bash
# 1. GitHub 上建一個空 repo（不要勾 README/gitignore/license，我們自己有）
# 2. 本地初始化並推上去
cd quantdesk-starter
git init
git add .
git commit -m "chore: scaffold — CLAUDE.md, schema, adapter base, phase docs, CI"
git branch -M main
git remote add origin <你的 repo URL>
git push -u origin main
```

**建議在 GitHub repo 設定裡開 Branch protection rule**（Settings → Branches）：
`main` 要求 PR + CI 通過才能合併。這一步很重要——它讓「每個 Phase 結束一定有可驗證
產出」這條 CLAUDE.md 鐵則變成真正的技術強制，而不只是寫給 agent 看的建議。

### 分支策略：對應 Phase 的 branch，schema 鎖定點打 tag

```
main ──●(Phase 0 骨架 merge)──tag: v0.1-schema-locked
         │
         ├──●(Phase 1 merge, branch: phase-1-fundamental)
         │
         ├──●(Phase 2 merge, branch: phase-2-risk-greeks)      ┐
         ├──●(Phase 3 merge, branch: phase-3-technical-...)    ├─ 從 tag 分出，
         ├──●(Phase 4 merge, branch: phase-4-news-macro)       ┘  可平行、互不衝突
         │
         ├──●(Phase 5 merge, branch: phase-5-supervisor)
         └──●(Phase 6 merge, branch: phase-6-hardening)
```

**為什麼 Phase 2-4 平行不會衝突**：三個 branch 各自只碰自己的檔案
（`agents/risk_agent.py` vs `agents/technical_agent.py` vs `agents/news_agent.py`
等），只要沒人動 `schemas/agent_signal.py` 或 `supervisor/graph.py`（CLAUDE.md
已經禁止），三條 branch 合併回 main 時就是乾淨的、不會有 merge conflict。這也是
「先鎖 schema 再平行」在版控層面的具體體現——schema 就是分工的邊界線。

Phase 0 完成、schema 定案後打個 tag：
```bash
git tag v0.1-schema-locked
git push origin v0.1-schema-locked
```
之後開 Phase 2-4 的三條平行 branch 都從這個 tag 分出去：
```bash
git checkout -b phase-2-risk-greeks v0.1-schema-locked
git checkout -b phase-3-technical-crossmarket v0.1-schema-locked
git checkout -b phase-4-news-macro v0.1-schema-locked
```

### 每個 Phase 的標準流程
```bash
git checkout -b phase-N-xxx v0.1-schema-locked   # 或從 main 分（依賴前面 phase 時）
# ... Claude Code / subagent 在這條 branch 上開發 ...
uv run pytest -q                                  # 本地先過一次
git push -u origin phase-N-xxx
# GitHub 上開 PR → CI 跑 lint+test → 你 review → squash merge 進 main
```

### CI（已內建）
`.github/workflows/ci.yml` 在每次 push/PR 時自動 `uv sync` + `ruff check` + `pytest`。
這直接對應 spec 文件裡「golden set 要跑進 CI/CD，每次改動都要驗證不能迴歸」的原則
——只是現在先從最基本的 lint+test 開始，Phase 5 之後可以把 golden set 評估也接進來。

## 你現有的兩個 repo（FinancialReports / Financial_Agent）怎麼處理

**建議：兩個都保持獨立 repo，不要合併進 QuantDesk。** 理由：
1. 它們各自已經是完整、可獨立展示的作品集項目，合併成 monorepo 會讓兩者的
   commit history 混在一起，面試時反而不好單獨講清楚每個項目的技術重點。
2. 這剛好符合 QuantDesk 架構本身的哲學——**adapter pattern**：外部系統透過
   抽象介面被消費，不需要把程式碼吃進來變成同一個 repo。財報 domain 的
   `FundamentalAdapter`（`docs/tasks/phase_1.md`）就是設計成呼叫
   FinancialReports 暴露出來的介面（DB 直連或 API），而不是把它的程式碼複製過來。

**實作上兩種接法，看你要多緊密**：
- **鬆耦合（建議）**：FinancialReports 跑成獨立服務（FastAPI 對外開查詢端點，或至少
  讓 QuantDesk 能直連它的 SQLite/pg），`FundamentalAdapter` 呼叫這個介面。三個 repo
  各自獨立部署、獨立版控，QuantDesk 只依賴介面合約。
- **緊耦合（若要直接重用 Financial_Agent 的分析函數，不想重寫）**：用
  `git submodule add <Financial_Agent repo URL> external/financial-agent`，
  QuantDesk 明確 pin 住某個 commit，`agents/fundamental_agent.py` 直接 import
  submodule 裡的分析函數。缺點是 submodule 對 Claude Code 平行開發不太友善
  （容易忘記同步、diff 難看），**只有在真的需要複用程式碼、且不打算重寫時才用**。

兩個 repo 目前的技術債（Phase 1 提到的資料層打通、RAG 補完）可以留在各自的 repo 裡
獨立修，QuantDesk 這邊只等它們的介面穩定後再對接，這樣三個 repo 的開發節奏互不卡住。



1. **成本**：六個 agent 平行約 6x token 消耗。骨架階段（Phase 0-1）用主 session
   慢慢做，平行只用在 Phase 2-4。做完大 workflow 後去 Console 看用量。
2. **schema 是紅線**：平行時任何 subagent 想改 `schemas/agent_signal.py` 都要先停下問你。
   這是六個 agent 對接的唯一保證。
3. **一次一個 Phase**：Phase 沒過測試不要往下。每個 Phase 都 git commit。
4. **它想一次改太多就拉回來**：「這個 PR 只處理 X，其他先不要動」。
5. **subagent vs agent teams**：先用 subagents 就夠（你六個 agent 大多獨立）。
   只有需要「邊寫邊討論協調」時才考慮開 agent teams（成本更高）。

## 為什麼這樣拆

Multi-agent 系統的地基是「標準 schema + Supervisor 骨架」。如果一開始就六路平行，
subagent 會在沒有共同 schema 的情況下各寫各的，最後輸出格式對不起來。
先立骨架、鎖 schema、再平行，是唯一能讓 coding agent 做完大型系統而不崩的方法。
