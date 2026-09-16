# Config Builder Skill

## Purpose
由同一支 `build_machineB_section_configs.py` 把已產生的 section INI 轉成 Machine B 自動化測試用派工 JSON。

這支 builder 只做組態產生，不把不同 section 合併成一份 JSON。

## Inputs
- `*-section-matrix.json`
- 已產生的 section INI
- 可選的既有測試 policy（只提供 sampling/control policy，不提供硬體 ID）

## Output Rule

每個已產生的 section INI 各自產生一個 JSON；section 狀態不是 `GENERATED`、path 缺失或 section INI 不存在時，不產生對應 JSON。

| Section | Output | Runner |
|---|---|---|
| `HWM.Fan` | `<model>_fan.json` | `run_hwm_fan_validation.ps1` |
| `HWM.Fan.Control` | `<model>_fancontrol.json` | `run_hwm_fan_control_validation.ps1` |

所有輸出都包含：
- `schema_version`
- `category`
- `model`
- `source_ini.file`
- `source_ini.path`
- `source_ini.section`
- `source_ini.sha256`
- section 自己的 policy 欄位
- safety/dependency gate

JSON 不複製 INI tuple，也不產生 `channel_id_map`、`control_id_map` 或猜測的 Control API ID。Runner 依 section key 使用既有 canonical SUSI mapping，控制路徑使用 SmartFan API。

## Contract: HWM.Fan

`<model>_fan.json` 只描述讀取與 sampling policy：

- `required_channels`
- `sample_count`
- `sample_interval_ms`
- `settle_time_sec`
- `expected_delta_rpm_min`
- `maximum_plausible_rpm`
- `minimum_success_rate`
- optional `bios_reference`
- `safety.hardware_write=false`

硬體 tuple 只存在 `source_ini.file` 指向的 `[HWM.Fan]` INI。

## Contract: HWM.Fan.Control

`<model>_fancontrol.json` 只描述 SmartFan 控制 policy：

- `control_channels`
- `test_sequence`
- `enabled`
- `settle_time_sec`
- `set_get_tolerance`
- `rpm_dependency`
- RPM sampling/correlation policy
- explicit control/restore safety gates

`rpm_dependency` 只 reference `<model>_fan.json` 與 `[HWM.Fan]` INI，必要時保存明確的 key-to-key pairing；不嵌入 fan JSON，也不複製 fan tuple。

## Build Rules
- 缺必要來源或 section -> 不產生該 section JSON，並回報 build error
- 缺 fan dependency -> Control JSON 可以產生，但標記 dependency `BLOCKED`，不可宣稱功能 PASS
- 缺治具/條件 -> `BLOCKED_FIXTURE`
- 僅產配置，不初始化 SUSI、不呼叫硬體 API

## Scope Boundary
- 不做 DB query
- 不做 BIOS/電路圖判讀
- 不猜測 Control API ID
- 不做 PASS/FAIL 定案
