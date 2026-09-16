 1) /home/company2/AIagent_susi/susi_gen.py
    - 主入口（extract -> understand -> generate）
    - /susiagent 直接呼叫它

    2) /home/company2/AIagent_susi/fetch_probe.py
    - 抓 target probe report（你現在用的 ssh 模式）

    3) /home/company2/AIagent_susi/parse_probe.py
    - 解析 *_susi_board_probe_report.txt 給 susi_gen 使用

    4) /home/company2/AIagent_susi/extract_pdf.py
    - PDF -> form json（含 pdfplumber / fitz 路徑）

    5) /home/company2/AIagent_susi/understand.py
    - form json -> spec json（LLM 判讀）

    6) /home/company2/AIagent_susi/query_config_db.py
    - generate 階段的 config_new.db deterministic query（目前核心）

    7) /home/company2/AIagent_susi/config_new.db
    - 現行唯一查詢來源（你要求的 config_new.db-only）

    8) Analysis skill文件
    - /home/company2/AIagent_susi/AnalysisSKill/orchestrator_skill.md
    - /home/company2/AIagent_susi/AnalysisSKill/candidate_query_skill.md
    - /home/company2/AIagent_susi/AnalysisSKill/bios_circuit_image_analysis_rule.md

    有條件有用（不是主路徑，但可能會用）
    1) /home/company2/AIagent_susi/generate_ini.py
    - 目前不是主生成功能（chip_db flow 在檔內已被 policy 擋掉）
    - 但 susi_gen.py 仍 import 它的 resolve_paths

    2) /home/company2/AIagent_susi/diff_ini.py
    - 做 golden diff 時才會用
