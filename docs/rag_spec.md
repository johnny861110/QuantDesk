# 財報 Agentic RAG System — 架構設計與 Production 全解

> 場景：處理台股上市公司公開財報（XBRL → iXBRL → PDF），支援「單一公司財報問答」「跨期間比較」「跨公司比較」的 agentic 問答系統。所有場景為公開資訊 + 通用架構模式重現，不涉及任何機密內部細節。

---

## 0. 為什麼「單純 RAG」在財報場景會失敗

單純 RAG（embed → retrieve top-k → 塞進 prompt → 生成）的假設是：**答案的所有必要資訊都存在於少數幾個語意相近的 chunk 裡**。財報場景這個假設會系統性地破：

1. **數字精確度需求**：「毛利率」是計算結果（毛利/營收），不是可以被 retrieve 到的一句話。
2. **跨文件推理**：「A公司 vs B公司近三季毛利率」需要先分解成子查詢、各自檢索、再做結構化比較。
3. **結構化 + 非結構化混合**：XBRL 有精確標記的數字，PDF 是排版後的表格與敘述文字，兩者對同一指標可能有微小差異（結報 vs 重編）。
4. **時效性**：財報會被重編（restated），舊版本不能被當作最新事實。

這就是為什麼要做成 **agentic**：系統要能規劃（要不要拆解查詢）、選工具（查資料庫算數字 vs 語意檢索找敘述）、驗證（算出來的數字跟文件裡寫的是否一致）、必要時要求人審核。單純 RAG 只有「檢索→生成」一步，agentic RAG 多了「規劃、工具選擇、驗證、反思」這幾個關鍵環節，而這些環節正是解決上述四個問題的地方。

---

## 1. Agentic 應用框架設計

### 1.1 整體架構：Router-Retriever-Verifier-Synthesizer-Critic

用 LangGraph 風格的 state machine 而非單一 agent loop，原因是**財報問答的每個步驟責任邊界清楚，用顯式 graph 比讓 LLM 自己決定下一步更可控、更好除錯、成本更低**（不用每步都讓 LLM 重新推理"我現在該做什麼"）。

```
[User Query]
     │
     ▼
┌─────────────┐   分類：單一事實 / 需計算 / 需跨文件比較 / 需最新性檢查
│   Router     │──────────────────────────────┐
└─────────────┘                                │
     │                                          │
     ▼                                          ▼
┌─────────────┐                        ┌─────────────────┐
│ Decomposer   │  (多跳查詢才觸發)      │ Direct Retrieval │
│ 拆成子查詢    │                        └─────────────────┘
└─────────────┘
     │
     ▼
┌──────────────────────────────────────────┐
│         Tool Selection (per 子查詢)         │
│  ┌───────────┐ ┌───────────┐ ┌──────────┐ │
│  │ SQL/XBRL  │ │ Vector    │ │ Graph    │ │
│  │ 結構化查詢 │ │ 語意檢索   │ │ 關係查詢  │ │
│  └───────────┘ └───────────┘ └──────────┘ │
└──────────────────────────────────────────┘
     │
     ▼
┌─────────────┐   數字是否跟來源一致？來源是否為最新版本？
│  Verifier    │──── 不一致 → 回到 Tool Selection 換工具重查（最多重試 2 次）
└─────────────┘
     │ 一致
     ▼
┌─────────────┐
│ Synthesizer  │  生成答案，附上每個數字的來源 citation
└─────────────┘
     │
     ▼
┌─────────────┐   信心分數低 / 金額超過閾值 / 首次上線的查詢類型
│ HITL Gate    │──── 需要人審 → 進審核佇列，非同步回覆
└─────────────┘
     │ 不需要
     ▼
[Final Answer + Citations]
```

**設計原則**：
- **Router 不是 LLM 分類器**，用小模型（或規則+embedding分類）做，因為這一步每個 query 都會經過，成本敏感。
- **Verifier 是整個系統可信度的關鍵**，也是多數 RAG 系統缺少的一環。財報場景下 Verifier 具體做的事：把 Synthesizer 生成答案裡的每個數字，用工具（正則 + 對照原始 XBRL 標記值）重新核對一次，不一致就打回。
- **Critic/Verifier loop 要有上限**（例如最多重試 2 次），否則會變成無限迴圈燒 token。

### 1.2 Tool 設計

| Tool | 用途 | 何時選 |
|---|---|---|
| SQL over XBRL 結構化表 | 精確數字查詢、計算（比率、成長率） | 問題涉及具體財務指標 |
| Vector search (pgvector) | 敘述性內容（MD&A、風險揭露、附註） | 問題涉及"為什麼"、"管理階層怎麼說" |
| Graph traversal (Kuzu) | 跨公司/跨期間關係（供應鏈、同業比較、股權結構） | 問題涉及實體間關係 |
| Calculator | 財務比率計算、跨期成長率 | Router 判斷需要計算時強制呼叫，不讓 LLM 心算 |

**關鍵原則：能用工具算出來的數字，絕不讓 LLM 生成。** 這是財報 agentic 系統跟一般 RAG 最大的差異——LLM 只負責「組織語言、下結論」，不負責「產出數字」。

### 1.3 Query Decomposition 範例

原始查詢：「比較台積電和聯電近三季毛利率變化」

拆解為：
1. 查詢台積電近三季毛利率（→ SQL tool，逐季查詢並計算）
2. 查詢聯電近三季毛利率（→ SQL tool）
3. 比較 + 生成敘述（→ Synthesizer，僅組織語言，不算數字）

這種拆解讓每個子查詢都可以獨立驗證、獨立快取、獨立重試，而不是丟給 LLM 一次性處理整個複雜問題（那樣容易漏算或算錯）。

---

## 2. Chunking 策略設計

### 2.1 財報文件的特殊性

- 表格佔資訊密度極高的比例，但表格被切斷（例如切一半的資產負債表）比切斷一般敘述性文字的破壞力大得多——切斷表格會讓數字失去對應的科目名稱，變成無意義的孤立數字。
- 章節階層明確（合併財務報表 > 資產負債表 > 流動資產 > 現金及約當現金），但 PDF 排版後階層資訊會消失（變成純文字流）。
- 同一份文件中文字與數字密度差異極大——附註說明是敘述文字，報表本身是密集數字表格，兩者不該用同一種 chunk size。

### 2.2 策略選擇與比較

| 策略 | 原理 | 財報適用性 |
|---|---|---|
| Fixed-size (e.g. 512 tokens) | 固定長度硬切 | ❌ 會切斷表格與語句，財報場景不建議單獨使用 |
| Recursive character splitting | 按段落→句子層級遞迴切，盡量保持語意完整 | ⚠️ 對敘述文字可用，但不理解表格結構 |
| Semantic chunking | 用 embedding 相似度找語意邊界 | ⚠️ 成本較高，且對數字密集內容效果有限（數字之間語意相似度計算沒有意義） |
| **Structure-aware chunking（本系統採用）** | 用文件結構（PP-StructureV3 版面分析）先切出「表格區塊」「標題」「段落」，各自用不同策略處理 | ✅ |

### 2.3 具體做法：Table-Aware + Hierarchical Chunking

**表格**：整張表格作為一個 chunk（不切），並額外生成一個「表格摘要」chunk（用 LLM 生成一句話描述這張表在講什麼，例如「2025Q3 合併資產負債表，流動資產與非流動資產明細」），摘要 chunk 用來做語意檢索的入口，命中後再把完整表格塞進 context（small-to-big retrieval）。

**敘述文字**：用 recursive splitting，chunk size 約 300-500 tokens，overlap 10-15%，切分點優先選在段落邊界。

**Parent-Child 結構**：
```
Document
 └─ Section (合併財務報表附註)
     └─ Parent chunk (完整段落，供 context 使用)
         └─ Child chunks (句子級，供 embedding 檢索索引)
```
檢索時用 child chunk 的 embedding 找相似度，命中後回傳對應的 parent chunk 給 LLM，這樣兼顧「檢索精準度」（小 chunk 語意單一）跟「context 完整性」（大 chunk 有足夠上下文）。

### 2.4 Metadata 設計（每個 chunk 必須附帶）

`公司代號`、`報表期間（年/季）`、`報表類型`（合併/個體）、`章節`（資產負債表/損益表/現金流量表/附註）、`頁碼`、`是否為重編版本`、`資料來源`（XBRL/iXBRL/PDF）。這些 metadata 不只是給人看，是**檢索時 filter 的第一道防線**——先用 metadata 過濾掉不相關期間/公司，再做語意檢索，大幅降低誤檢索率跟成本。

### 2.5 常見錯誤

- 用同一個 chunk size 處理全部文件類型 → 表格被切爛。
- 沒有把「重編公告」跟「原始財報」分開標記 → 系統可能引用已作廢的舊數字。
- Overlap 設太大 → 同一個數字在多個 chunk 重複出現，reranker 分數被稀釋，且 context window 浪費。

---

## 3. Vector DB 設計（pgvector）

### 3.1 為什麼選 pgvector 而非純向量資料庫（如 Milvus/Pinecone）

金融場景通常已經有 PostgreSQL 作為交易/主資料系統，pgvector 讓向量檢索跟結構化查詢（metadata filter、join 其他業務表）可以在同一個 transaction 裡完成，避免維護兩套資料庫的一致性問題。代價是規模到億級向量時效能不如專用向量資料庫，但財報場景（單一市場上市公司 × 幾年份 × 章節數）通常在千萬級以內，pgvector 足夠。

### 3.2 Schema 設計

```sql
CREATE TABLE report_chunks (
    id              BIGSERIAL PRIMARY KEY,
    company_code    VARCHAR(10) NOT NULL,
    fiscal_period    VARCHAR(10) NOT NULL,   -- e.g. '2025Q3'
    report_type     VARCHAR(20) NOT NULL,    -- consolidated / standalone
    section         VARCHAR(50) NOT NULL,    -- balance_sheet / income_stmt / notes ...
    chunk_type      VARCHAR(20) NOT NULL,    -- table / table_summary / narrative
    page_number     INT,
    is_restated     BOOLEAN DEFAULT FALSE,
    source_type     VARCHAR(10) NOT NULL,    -- xbrl / pdf
    content         TEXT NOT NULL,
    embedding       VECTOR(1024),
    tsv             TSVECTOR,                -- 用於 BM25/全文檢索混合搜尋
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- HNSW index：適合讀多寫少、對延遲敏感的場景（財報是批次寫入、高頻查詢）
CREATE INDEX ON report_chunks USING hnsw (embedding vector_cosine_ops);

-- 複合 index 支援 metadata 先過濾
CREATE INDEX idx_company_period ON report_chunks (company_code, fiscal_period, is_restated);

-- 全文檢索 index，用於 hybrid search
CREATE INDEX idx_tsv ON report_chunks USING GIN (tsv);
```

**HNSW vs IVFFlat**：HNSW 建索引較慢、記憶體用量較高，但查詢延遲低且不需要像 IVFFlat 一樣針對資料分布調 `lists` 參數；財報場景寫入是批次（每季財報公告後才更新），讀取是高頻用戶查詢，符合 HNSW 的使用情境，因此選 HNSW。

### 3.3 為什麼需要 Hybrid Search（Dense + Sparse）

純向量檢索對**精確代號/專有名詞**（如「應收帳款周轉率」「其他綜合損益」這類固定財務術語）表現不穩定——embedding 模型可能把語意相近但字面不同的詞算得太接近，也可能把字面一樣但語意場景不同的內容漏掉。加入 BM25/tsvector 全文檢索做 hybrid（用 Reciprocal Rank Fusion 合併兩邊排序），可以確保「使用者輸入的精確術語」一定被檢索到，這在財報場景（術語標準化程度高）特別重要。

### 3.4 何時需要 GraphRAG（Kuzu）而非只靠向量檢索

向量檢索的單位是「chunk」，天生不擅長回答**多實體關係型**問題，例如「這家公司的主要客戶集中度」「同業比較」「轉投資關係」。這類問題需要先建立實體關係圖（公司-客戶、公司-子公司、公司-同業），用圖遍歷找到相關實體集合，再針對這個集合做向量檢索或結構化查詢。GraphRAG 不是取代向量檢索，是在「需要先確定候選實體範圍」的查詢類型上做前置過濾，減少後續檢索的雜訊。

---

## 4. Query 設計

### 4.1 Query Understanding & Routing

第一步永遠是分類使用者意圖，而非直接檢索：

- **事實查詢**（"台積電 2025Q3 營收多少"）→ 直接查 SQL/XBRL，不需要向量檢索。
- **敘述性查詢**（"管理階層對 2025Q4 展望怎麼說"）→ 向量檢索 narrative chunk。
- **計算型查詢**（"毛利率成長多少"）→ SQL 查兩期數字 + calculator tool。
- **比較型查詢**（"A vs B"）→ 觸發 decomposition。
- **關係型查詢**（"主要客戶有誰"）→ Graph traversal。

這一步做錯，後面全部白做——例如把「事實查詢」誤判成需要向量檢索，會多一次不必要的 embedding 呼叫跟檢索延遲，且向量檢索找到的答案精確度還不如直接查結構化欄位。

### 4.2 Query Rewriting / HyDE

使用者的原始問題常常跟文件裡的用詞不一致（口語 vs 財報術語）。做法：

1. **Query rewriting**：LLM 把口語問題轉成財報術語（"賺多少錢" → "本期淨利"）。
2. **HyDE（Hypothetical Document Embeddings）**：讓 LLM 先生成一段"假設的答案"，用這段假設答案去做 embedding 檢索，而非直接 embed 原始問題——因為假設答案的用詞風格更接近文件本身，檢索命中率通常更高。財報場景下 HyDE 對敘述性問題（風險揭露、管理階層討論）效果明顯，對純數字查詢沒有必要用（直接查結構化資料更準）。

### 4.3 Self-Query（Metadata Filter Extraction）

從自然語言問題自動抽取 metadata filter，例如「台積電最近一季」→ `company_code='2330', fiscal_period=最新`。這一步用小模型做 structured output 抽取，抽取結果先過濾掉不相關公司/期間的 chunk，再進向量檢索，可以大幅降低檢索雜訊跟成本（減少要比對的向量數量）。

### 4.4 Reranking

向量檢索的 top-k（初篩通常拉大到 top-30~50）不直接送進 LLM，先過 cross-encoder reranker 重新排序取 top-5~8。原因：向量相似度（bi-encoder）計算 query 和 document 是分開 embed 再算 cosine similarity，速度快但精度有限；cross-encoder 把 query 和 document 一起輸入模型算相關性分數，精度高但無法對全部文件跑（太慢），所以用「向量檢索做初篩 + cross-encoder 做精排」的兩階段架構平衡速度與精度。

---

## 5. 評估框架設計

評估要拆成三層，缺一不可：

### 5.1 檢索層評估（Retrieval Metrics）

- **Recall@k**：正確答案所在的 chunk 有沒有出現在 top-k 檢索結果。
- **MRR (Mean Reciprocal Rank)**：正確 chunk 排名越前分數越高，比 Recall@k 更敏感地反映排序品質。
- **nDCG**：適合多個 chunk 都"部分相關"的場景（財報問題常常需要多個 chunk 才能完整回答）。

這一層的評估**跟 LLM 生成完全無關**，純粹測 chunking + embedding + retrieval 這條 pipeline 好不好，出問題時可以獨立除錯而不用懷疑到生成端。

### 5.2 生成層評估（Generation Metrics，RAGAS 風格）

- **Faithfulness（忠實度）**：生成答案裡的每個聲明，是否都能從檢索到的 context 中找到支持依據——衡量「有沒有幻覺」。
- **Answer Relevancy**：答案是否真正回答了問題（而非答非所問）。
- **Context Precision/Recall**：檢索到的 context 中有多少是真正被用來生成答案的（precision），以及回答問題所需的資訊有多少比例真的被檢索到（recall）。

### 5.3 財報領域專屬評估：數值正確性驗證

RAGAS 這類通用指標不足以捕捉財報場景最關鍵的錯誤類型——**數字對但單位錯**、**數字對但期間錯**、**四捨五入後看似合理實則錯誤**。做法：對每個生成答案，用正則抽取所有數字，逐一對照原始 XBRL 結構化資料裡的對應標記值，計算 **Numeric Exact Match Rate**。這個指標比 faithfulness 更嚴格、更貼近財報場景的真實風險（使用者最在意的不是"語意忠實"，是"數字對不對"）。

### 5.4 Golden Dataset 建構方法論

- 不能只用容易的查詢（"XX公司營收多少"）當測試集，要涵蓋：單一事實、多跳比較、時間序列趨勢、需要計算的衍生指標、邊界案例（重編財報、公司改名、合併重組）。
- 用真實歷史客服/使用者查詢日誌（如果有）加上人工設計的邊界案例，避免 golden set 過度貼合系統已經處理得好的查詢類型（selection bias）。
- Golden set 要**版本化並定期擴充**，每次上線發現的新錯誤案例都要加進去，否則系統會對已知問題過擬合但對新型態查詢仍然脆弱。

### 5.5 持續評估與迴歸測試

把 golden dataset 的評估跑進 CI/CD——每次改動 chunking 策略、換 embedding 模型、改 prompt，都要跑一次完整評估，比對跟 baseline 的差異，避免"改善了 A 類查詢卻沒發現破壞了 B 類查詢"這種常見的迴歸問題。

---

## 6. Production 踩雷問題與解法（原理 + 分析 + 解法）

以下每個問題都是 agentic RAG 系統從 POC 到 production 必然會遇到的，附根本原因分析：

### 6.1 表格切斷導致數字失去語意
**原因**：Chunking 沒有理解文件結構，用固定 token 數硬切。**解法**：見 2.3，structure-aware chunking，表格整塊處理 + 摘要索引。

### 6.2 財經術語 OOV / Embedding 語意誤判
**原因**：通用 embedding 模型對「其他綜合損益」「保留盈餘」這類財經專有詞的向量表示可能跟語意不符（訓練語料裡這些詞出現頻率低）。**解法**：(a) 用金融領域微調過的 embedding 模型，或 (b) hybrid search 用 BM25 補強精確術語匹配（見 3.3），不完全依賴 dense retrieval。

### 6.3 檢索到「語意相關但答案錯誤」的內容（false positive）
**原因**：向量相似度高不代表資訊正確——例如檢索到的是「重編前」的舊數字，語意上跟查詢完全相關，但事實上已經作廢。**解法**：Metadata 必須標記 `is_restated`，查詢時預設過濾掉舊版本；Verifier 階段對照最新公告版本再次確認。

### 6.4 數字幻覺（LLM 編造或算錯數字）
**原因**：LLM 生成數字本質上是"預測下一個 token"，不是"執行計算"，即使 context 裡有正確數字，LLM 仍可能因為 attention 分配問題輸出錯誤數值，尤其是需要跨多個數字做四則運算時。**解法**：見 1.2 的核心原則——所有需要計算的數字一律呼叫 calculator/SQL tool 產生，LLM 只負責組織語言；Verifier 階段對答案裡的每個數字做二次核對。

### 6.5 Latency 與成本的多跳查詢爆炸
**原因**：Decomposition 把一個查詢拆成 N 個子查詢，每個子查詢都要走一次 retrieval + 可能的 LLM 呼叫，N 越大延遲和成本線性甚至超線性增長。**解法**：(a) 子查詢平行執行而非序列執行；(b) 對高頻查詢模式（如"近N季XX指標"）做結果快取，快取 key 用 `company_code+period+metric` 而非整句查詢文字；(c) Router 階段就要嚴格判斷是否真的需要 decomposition，避免過度拆解簡單問題。

### 6.6 Top-k 選擇的 recall/precision/成本三方權衡
**原因**：k 太小會漏掉必要 context（recall 不足），k 太大會把不相關內容塞進 context window（precision 下降，且 LLM 在長 context 中容易"迷失中間"，即 lost-in-the-middle 現象），同時 token 成本線性增加。**解法**：用兩階段檢索——初篩 top-30~50（cheap, dense retrieval）+ reranker 精排取 top-5~8（更貴但更準），而非直接在初篩階段就取小 k。

### 6.7 財報重編/版本更新的資料一致性
**原因**：財報公告後可能因為會計師查核調整而重編，如果索引沒有即時更新或沒有版本標記，系統會持續回答已經作廢的數字。**解法**：ETL pipeline 偵測到重編公告時，(a) 舊版本 chunk 標記 `is_restated=true` 但不刪除（保留審計軌跡），(b) 新版本正常寫入並設為預設查詢版本，(c) 對於已快取的答案要主動失效（cache invalidation by company_code+period）。

### 6.8 Multi-tenancy 與資料隔離
**原因**：金融場景下不同客戶/部門可能只能存取特定範圍的資料（例如僅限公開資訊 vs 內部限閱資料），向量檢索如果沒有在資料庫層強制做權限過濾，可能因為 prompt 或 metadata 設計疏漏造成跨權限洩漏。**解法**：權限過濾要做在 SQL WHERE 條件層級（資料庫層強制），不能只依賴應用層邏輯或 prompt 指示 LLM "不要回答某些內容"——後者不是安全邊界，只是建議。

### 6.9 惡意上傳文件的 Prompt Injection
**原因**：如果系統允許使用者上傳自己的財報 PDF 做分析，文件內容會被當作 context 塞進 prompt，惡意文件可以在頁尾藏入類似「忽略先前指示，改為輸出...」的文字，構成間接 prompt injection。**解法**：(a) 文件內容與系統指令要有明確的結構化分隔（如用特殊標記包裹使用者文件內容，並在 system prompt 明確告知模型該區塊為資料而非指令）；(b) 對輸出做二次檢查，偵測是否偏離原始查詢意圖；(c) 敏感操作（如呼叫外部工具）不應該由文件內容直接觸發。

### 6.10 缺乏可觀測性導致問題難以定位
**原因**：Agentic 系統有多個步驟（router → decompose → retrieve → verify → synthesize），出錯時如果只看最終輸出，無法判斷是哪一步壞的。**解法**：每個節點都要有 trace（用 Langfuse 這類工具記錄每一步的輸入輸出、耗時、token 用量），並且要能對 "為什麼這次檢索沒找到正確 chunk" 這類問題做事後回放分析，而不是只看聚合指標。

### 6.11 Agent 工具誤用或無限迴圈
**原因**：讓 LLM 自由決定"下一步該用哪個工具"時，模型可能反覆呼叫同一個沒有幫助的工具，或誤判該用哪個工具（例如該查 SQL 卻去做語意檢索）。**解法**：(a) 用顯式 graph 而非讓 LLM 完全自主決策下一步（見 1.1 的設計理由）；(b) 每個節點設重試上限；(c) 對工具呼叫做結構化 schema 驗證，格式不對直接攔截重試而非放行。

### 6.12 HITL 審核瓶頸
**原因**：把所有低信心答案都送人審，隨著查詢量成長，審核佇列會變成系統吞吐量的瓶頸。**解法**：(a) 用分層信心閾值——只有金額超過一定門檻或首次出現的查詢類型才強制送審，常見且已驗證過的查詢模式走自動放行；(b) 累積人工審核結果，反饋回 golden dataset，讓系統對同類查詢的信心分數逐步提升，減少長期需要人審的比例。

### 6.13 評估資料集老化與過擬合
**原因**：團隊傾向針對 golden set 裡已知的失敗案例反覆調 prompt/檢索參數，容易「表面上指標變好，實際上只是對這批固定測試題過擬合」，遇到新型態查詢仍然失敗。**解法**：定期（例如每次上線）加入新的真實使用者查詢案例到 golden set；額外保留一組"held-out"測試集不用於調參，只用於最終驗證，避免資料洩漏式的過擬合。

### 6.14 跨文件比較的單位與期間不一致
**原因**：不同公司可能用不同會計期間結尾（12月制 vs 非曆年制），或財務數字單位不同（千元 vs 百萬元），直接比較會產生誤導性結論。**解法**：ETL 階段就要把所有數字正規化到統一單位，並在 metadata 明確標記會計期間定義；Synthesizer 生成比較性答案時，Verifier 要額外檢查「兩邊比較的是否為同一期間定義下的數字」。

### 6.15 Reranker 延遲與整體系統吞吐量的權衡
**原因**：Cross-encoder reranker 精度高但無法批次平行處理太多候選（相較於 bi-encoder 檢索），對延遲敏感的互動式查詢會拖慢整體回應時間。**解法**：初篩階段用 metadata 過濾先大幅縮小候選集合（見 4.3 self-query），減少送進 reranker 的候選數量；對延遲要求極高的簡單事實查詢（見 4.1 router 分類），直接跳過 reranker 走結構化查詢。

### 6.16 索引更新造成的服務不一致視窗
**原因**：批次更新向量索引時，如果採用「先刪除舊索引再寫入新索引」，中間會有一段時間窗口查詢不到任何結果或只有部分結果。**解法**：採用 blue-green 索引更新——新版本索引建置完成並驗證通過後才切換查詢流量指向新索引，舊索引保留一段時間作為 rollback 備援。

---

## 7. 面試話術重點整理

被問到這個專案時，建議的敘事結構（STAR-ish，但強調架構決策的"為什麼"）：

1. **問題定義**：先講清楚"為什麼單純 RAG 不夠"（見第0節），展現你理解 RAG 的局限性而非只是會串 LangChain。
2. **架構決策的取捨**：每個技術選擇都要能回答"為什麼選這個不選那個"——例如 pgvector vs 專用向量資料庫的取捨、HNSW vs IVFFlat、structure-aware chunking vs 通用 chunking。面試官在意的是判斷力，不是工具清單。
3. **最有含金量的部分是 Verifier 環節**：「讓 LLM 不負責產出數字，只負責組織語言」這個設計原則，直接點出你理解 LLM 在高精確度場景的根本限制，這是資深工程師跟一般人的分水嶺。
4. **評估框架**：能講出"為什麼通用 RAGAS 指標不夠，financial domain 需要 numeric exact match"，展現你不是套用現成框架，而是針對場景做了客製化評估設計。
5. **Production 踩雷經驗**：挑 2-3 個最有代表性的問題深入講（建議：數字幻覺的解法、重編財報的一致性處理、可觀測性設計），比蜻蜓點水講完 16 個問題更有說服力。

---

*下一步：落地成可執行程式碼（chunking pipeline、hybrid retrieval、LangGraph agent graph、evaluation harness）。*
