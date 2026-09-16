# HWM.Fan / HWM.Fan.Control 最小 JSON 契約

目的
- 給 vision 分析「直接回傳可落地」的最小 JSON。
- `build_fan_pairing.py` 會把各種輸入形狀正規化成這個最小格式。

輸出檔
- `/home/company2/AIagent_susi/CASES/<PROJECT>/<PROJECT>-fan-pairing.json`

必要欄位（只有這兩層）
```json
{
  "topology_status": "FAN_ONE_TO_ONE_CONFIRMED",
  "items": [
    { "key": "FCPU", "fanin_idx_candidate": 0, "control_idx_candidate": 0 },
    { "key": "FSYS", "fanin_idx_candidate": 1, "control_idx_candidate": 1 }
  ]
}
```

`topology_status` 允許值
- `FAN_ONE_TO_ONE_CONFIRMED`
- `FAN_ONE_TO_MANY_CONFIRMED`
- `FAN_PAIRING_NEEDS_FANCONTROL_EVIDENCE`
- `FAN_PAIRING_AMBIGUOUS`

`items[]` 必要語義
- `key`: `FCPU` / `FCPU2` / `FSYS` / `FOEM<n>`
- `fanin_idx_candidate`: FANIN idx（可省略）
- `control_idx_candidate`: 控制 idx（可省略，可重複表示 one-to-many）

備註
- 契約刻意精簡；不需要 `schema_version/project/source/summary/reason`。
- `susi_gen.py` 只吃 `topology_status + items(key, fanin_idx_candidate, control_idx_candidate)`。