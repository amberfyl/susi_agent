# Config Builder Skill

## Purpose
把已篩選候選轉成 machine-B 自動化測試用 config JSON。

## Inputs
- `diagram_filter_skill` 輸出
- 固定 section schema / naming contract

## Output
- per-section config JSON（派工檔）
- individual ini fragments（若流程需要）

## Build Rules
- 缺必要參數 -> `BLOCKED_PARAMETER`
- 缺治具/條件 -> `BLOCKED_FIXTURE`
- 僅產配置，不執行硬體動作

## Scope Boundary
- 不做 DB query
- 不做 BIOS/電路圖判讀
- 不做 PASS/FAIL 定案
