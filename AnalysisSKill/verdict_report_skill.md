# Verdict Report Skill

## Purpose
彙整 machine-B 驗證結果，輸出可回饋總控的結論與建議。

## Inputs
- section validation reports
- candidate trace（query/filter/build chain）

## Outputs
- per-section verdict
- overall verdict
- fail taxonomy（FAIL_API/FAIL_READBACK/BLOCKED_* 等）
- 回寫建議（可回寫/不可回寫）

## DB Feedback Rule
- 成功案例：允許回寫考古 DB（由 orchestrator 執行寫回）
- 失敗案例：產生回歸校正建議，不直接改 DB

## Scope Boundary
- 不直接操作 DB 寫入
- 不改 orchestrator 狀態機
