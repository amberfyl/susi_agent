# Hermes susiagent skill（repo 存放版本）

這個資料夾是 Hermes 代理的 `susiagent` skill 的 repo 版本，跟程式碼一起用 git 管理版本。

## 放在哪裡

Hermes 實際讀取的位置（WSL 內）：

```
~/.hermes/skills/software-development/susiagent/
├── SKILL.md
└── references/
```

Windows 檔案總管路徑：`\\wsl.localhost\Ubuntu\home\company2\.hermes\skills\software-development\susiagent\`

## 兩邊各自保留，不自動同步

- repo 這份和 `~/.hermes` 那份是**各自獨立的檔案**，改一邊不會影響另一邊。
- 只收 `SKILL.md` 與 `references/`（runbook）。`Legacy_不可使用/`、舊的 `不用SKILL*` 備份、`*.zip` 不收。

## 安裝（repo → Hermes）

要讓 Hermes 用 repo 版本時，先備份再複製：

```bash
DEST=~/.hermes/skills/software-development/susiagent
cp -p "$DEST/SKILL.md" "$DEST/SKILL.md.bk_$(date +%Y%m%d_%H%M%S)"
cp -p hermes_skill/susiagent/SKILL.md "$DEST/SKILL.md"
cp -rp hermes_skill/susiagent/references/. "$DEST/references/"
```

## 收回（Hermes → repo）

Hermes 依 SKILL.md 第 8 節自行更新了 runbook，或你直接改了 `~/.hermes` 那份時，要收回 repo：

```bash
SRC=~/.hermes/skills/software-development/susiagent
cp -p "$SRC/SKILL.md" hermes_skill/susiagent/SKILL.md
cp -rp "$SRC/references/." hermes_skill/susiagent/references/
git diff hermes_skill/
```

複製前先用 `diff -ru ~/.hermes/skills/software-development/susiagent/references hermes_skill/susiagent/references` 確認差異，避免互相覆蓋。
