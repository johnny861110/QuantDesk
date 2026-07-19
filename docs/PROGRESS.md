## 上次進度（2026-07-19）
Phase 0-2 完成並 merge 進 main（tag: v0.2-two-domains-complete）。
下一步：Phase 3（技術面+跨市場，可平行）或 Phase 4，開工前先更新
docs/tasks/phase_3.md / phase_4.md 補上這幾條教訓：
- pyproject.toml/uv.lock 是全域檔案，平行開發前先統一把需要的套件一次裝好
- 三關驗收（ruff/mypy/pytest）要寫進每個 subagent 的任務描述
- 新 agent 的 narrative 檢查要用 agents/verifier.py 共用模組，不要各寫一份
