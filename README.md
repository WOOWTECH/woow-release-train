# woow release train：HA ↔ PaaS 版本清冊

這個 repo 用一份清冊 `woow-versions.yaml` 規定每個產品的上游版本，HA add-on 和 PaaS 兩邊都對齊它。另外有一支只用 Python 標準函式庫的小工具 `tools/wvt.py`，負責檢查清冊格式、比對各邊的釘點，並每天產出一張漂移（drift）報告。

- **主 repo**：Gitea `woow-paas/woow-release-train`（https://git-prod.woowtech.io/woow-paas/woow-release-train）
- **公開鏡像**：GitHub `WOOWTECH/woow-release-train`，唯讀，由 Gitea Actions 單向推送。GitHub 端不要直接改，下次鏡像會覆蓋。
- **目前階段**：第 0 期，**只報告，不擋任何流程**。

## 為什麼要有這個 repo

HA 和 PaaS 各自升版，結果常常不一致。例如 n8n 在 HA 已是 2.43.6，PaaS 範本還停在 2.38.0，線上甚至還有 2.35.3。過去的對齊都是單一產品各做各的：LiteLLM 有外掛 lock、EMQX 有授權測試、code-server 有 PARITY_CONTRACT，沒有一個通用機制。

這個 repo 補上三件事：

1. 一份宣告「應該是哪一版」的清冊；
2. 一支能讀懂兩邊釘點的比對工具；
3. 一張每天自動更新的漂移追蹤 issue。

## 已定案的決定（負責人，2026-10-15／16）

| 項目 | 決定 |
|---|---|
| 真相來源 | 單一清冊 `woow-versions.yaml`，HA 與 PaaS 都對齊它 |
| 放哪裡 | Gitea 為主，GitHub 公開鏡像（Gitea Actions＋deploy key 推送） |
| 範圍 | 只管 HA 與 PaaS。Podman、k3s 不納入，清冊裡也不建模 |
| lockstep（兩邊必須同一顆上游 digest） | EMQX、LiteLLM；之後加 OpenDesign、code-server |
| range（允許落後，但有下限與天數上限） | n8n；之後加 Hermes、Omnigent、Headscale、Tailscale |
| 自動化程度 | 機器人只開 PR、跑測試；合併與發版一律由人做 |
| Gitea | 機器人**永遠不合併** Gitea PR，也**不簽** paas-odoo-ci 的 `/approve` |
| 第 0 期 | 只放 EMQX、LiteLLM、n8n 三個產品，只出報告 |

## 清冊格式（schema 1）

清冊是 YAML，但只用一個小子集：區塊 mapping／sequence、單行 flow（`[a, b]`、`{k: v}`）、引號字串、註解、`|`／`>` 區塊字串。`wvt` 內建這個子集的解析器，所以 runner 不需要安裝 PyYAML。測試時如果偵測到 PyYAML，會順便交叉比對兩者的解析結果。

**為什麼不用 JSON**：清冊要寫政策理由，例如「為什麼授權上限是 5.9.0」「例外為什麼可以存在」。JSON 不能寫註解。`wvt` 也接受 `.json` 檔，日後真的要換格式不必改程式。

```yaml
schema: 1
phase: 0                      # 0 只報告 → 1 警告＋硬規則 → 2 機器人 PR → 3 擴大
enforcement: report-only
sources:
  prod_platform:              # 正式站 Fleet 釘的平台版（從 CD values-prod.yaml 的註解讀 addon_ref）
    repo: "gitea:woow-paas/paas-app-bundle-cd"
    file: paas-odoo-platform/values-prod.yaml
products:
  <產品>:
    upstream: { image: docker.io/..., source: "github:org/repo" }
    policy:
      mode: lockstep | range | independent
      license_ceiling: "<5.9.0"     # 選填；只看上游版本，不看 add-on 版號
      min_version: "2.38.0"         # range 必填：安全下限
      max_days_behind: 30           # range 必填：從 target.since 起算
      hard_rules: [...]             # 第 1 期起會擋的規則：license_ceiling / min_version / digest / shared_files / exception_expired
    target:
      version: "x.y.z"
      since: "YYYY-MM-DD"           # 目標生效日（落後天數的起點）
      index_digest: "sha256:..."
      platforms: { amd64: "sha256:...", arm64: "sha256:..." }
      artifacts: { <名稱>: "sha256:..." }   # 非映像的上游產物，例如 tarball
    known_versions: [...]           # 舊版的 digest，用來把「落後的 digest」換算回版本
    shared_files: [...]             # 共用檔（例如 LiteLLM Woow 外掛）：來源 commit＋各邊 lock
    legs:
      ha:   { repo: "github:WOOWTECH/...", pins: [...], package_version: [...], published: {kind: catalog, ...} }
      paas: { pins: [...], package_version: [...] }
    exceptions:                     # 必須有 what / reason / approved_by / expires；到期 validate 就失敗
      - { leg: ha, what: ..., reason: ..., approved_by: ..., expires: "YYYY-MM-DD", waives: [behind] }
observations: [...]                 # 線上部署的手動快照（非真相來源，只供報告參考）
probes: { api.github.com: https://api.github.com/rate_limit }   # 只測連線，不影響漂移判定
```

### 釘點（pin）與抽取器

每個釘點寫明「去哪個 repo 的哪個檔、用什麼抽取器、和清冊的哪個值比」。新增產品只要加設定，不用改程式。

| `kind` | 用途 | 必填欄位 |
|---|---|---|
| `dockerfile_arg` | Dockerfile 的 `ARG NAME=value`（支援 `${OTHER}` 代換） | `name` |
| `yaml` | Chart.yaml、values.yaml、config.yaml 的路徑值 | `path`（例 `image.digest`） |
| `json` | JSON 檔的路徑值 | `path` |
| `seed_image` | Odoo seed XML：`<record id=...>` 的 `helm_default_values` → `image.repository/tag` | `record` |
| `seed_field` | Odoo seed XML 的某個 `<field>` | `record`、`field` |
| `catalog` | Woow_HA_App_Store `.addon-sync/catalog.json` 的元件 | `id` |

- `parse`：`version`（預設）、`image_ref`（`repo:tag@sha256:…`）或 `digest`（純 sha256）。
- `compare`：`version`、`index_digest`、`platforms.<arch>`、`artifacts.<名稱>`，或 `info`（只列參考，不比較）。
- `ref`：分支或 tag。`$prod_platform` 代表正式站平台版；`also_refs: [develop]` 會同時比 develop。

**只比上游身分。** add-on 版號、chart 版號、JCR 時間戳 tag 都是各邊自己的包裝，只放在 `package_version` 列參考。例如 HA EMQX 的 add-on 標 5.9.1，實際包的是上游 5.8.9；授權上限 `<5.9.0` 只看 5.8.9。

## 工具：`tools/wvt.py`

只用 Python 3 標準函式庫。

```sh
python3 tools/wvt.py validate                       # 格式＋一致性＋例外到期（到期＝失敗，14 天內＝警告）
python3 tools/wvt.py check --leg ha --repo-dir .    # 在某個 HA／PaaS repo 的 CI 裡比對自己的釘點（離線）
python3 tools/wvt.py check --leg paas --repo-dir . --fail-on violation   # 第 1 期：只有硬規則會失敗
python3 tools/wvt.py check --leg paas --repo-dir . --strict              # 任何漂移都失敗（之後的期別）
python3 tools/wvt.py drift --out drift.md --json drift.json              # 抓真實來源、產生報告
python3 tools/wvt.py issue --report drift.md --json drift.json           # 建立／更新／關閉追蹤 issue
python3 -m unittest discover -s tests -v
```

`check` 會從 `git remote` 推斷自己是哪個 repo，也可以用 `--repo github:OWNER/REPO` 指定。第 0 期一律回傳 0，只印出警告。

`drift` 的資料來源：

- **HA**：GitHub 公開 raw；讀不到時改用 api.github.com。另外讀 Store `catalog.json`，並在 catalog 記錄的 `source_sha` 上重讀釘點，這就是「已發佈」的版本。
- **PaaS**：Gitea API。seed 比兩個版本：正式站 CD 釘的平台 tag，以及 develop。charts 和 paas-odoo-ci 比 main。
- **共用檔**：比對 HA lock 記錄的 git blob 和 charts main 的 blob。
- **連線狀況**：每個端點的連線結果會列在報告最後。

## 自動化（Gitea Actions）

| Workflow | 觸發 | 做什麼 |
|---|---|---|
| `ci.yml` | push、PR | `wvt validate`＋單元測試。只擋本 repo 的清冊 PR |
| `drift.yml` | 每天台北 08:41、手動 | 跑 `wvt drift`，維護**一張**帶 `version-drift` label 的 issue：有漂移就建立或更新，沒有就留言後關閉 |
| `mirror-github.yml` | push main／tag、每天、手動 | 用 deploy key（repo secret `GH_MIRROR_DEPLOY_KEY`）把 main 和 tag 推到 GitHub |

權杖：

- 跨 repo 讀取用 org secret `CI_GITEA_TOKEN`，因為 `paas-app-bundle-cd` 是私人 repo。
- 寫本 repo 的 issue 用每次執行自動發的 `GITEA_TOKEN`。
- 沒有任何 workflow 會合併或核可東西。

## 新增一個產品

1. 用 `crane manifest <image>:<version>` 取得 index 與各架構 digest，寫進 `target`，並在 `verified` 註明日期與指令。
2. 決定 `policy.mode`。range 必須寫 `min_version`、`max_days_behind`、`target.since`。
3. 為 `ha`、`paas` 各列出釘點（`pins`），包裝版號放 `package_version`；HA 有上架 Store 的加 `published`。
4. 如果某邊目前停在舊 digest，把該版加進 `known_versions`，報告才能換算出版本。
5. 本機跑 `python3 tools/wvt.py validate`、`python3 -m unittest discover -s tests`，以及 `drift`（需要 Gitea 權杖，放環境變數 `GITEA_READ_TOKEN`）。
6. 開 PR。改清冊就是改版本政策，**一律人工合併**。

## 分期

| 期 | 內容 | 會擋什麼 |
|---|---|---|
| **0（現在）** | 清冊列 EMQX、LiteLLM、n8n 的現況；drift 報告＋追蹤 issue；GitHub 鏡像 | 什麼都不擋（只擋本 repo 自己的 CI） |
| **1** | 各邊 CI 加 `wvt check --fail-on violation`（HA：Store 的可重用 workflow；PaaS：seed 測試、charts、paas-odoo-ci） | 只開兩條硬規則：**EMQX 授權上限**；**LiteLLM base digest＋外掛 lock commit／blob 必須等於清冊**（在 Gitea 驗，補上 GitHub 端只能 offline 的缺口）。其他一律警告 |
| **2** | 機器人：監看上游、開清冊 PR、扇出各邊 PR、dispatch mirror-image、開 seed PR | 不擋；只產生 PR |
| **3** | 擴大到 OpenDesign、code-server（lockstep），Hermes、Omnigent、Headscale、Tailscale（range），以及 pi-agent、Nextcloud、Odoo、MCP | range 的落後天數改成硬擋，配 SLA |

## 誰核可什麼

| 角色 | 負責 |
|---|---|
| 版本負責人（woowtech） | 合併本 repo 的清冊 PR；決定 lockstep 或 range、目標版本；核准例外（`approved_by`） |
| HA 各 repo 維護者 | 合併 HA 端的 PR；Store 同步自動進行 |
| PaaS 覆核者（Eugene） | 合併 seed 與 charts PR；paas-odoo-ci `/approve` 由人簽 |
| 平台營運 | 發版、CD、租戶 Redeploy（範本對齊不等於線上服務已對齊） |
| 機器人／agent | 只開 PR、跑測試、維護追蹤 issue；**不合併 Gitea PR、不簽 `/approve`** |

## 已知限制

- PaaS「已部署」欄目前是手動快照（`observations`，有日期，超過 14 天會警告）。第 1 期起改由 operator 回報。
- 正式站平台版只寫在 CD values 的註解裡（`Built from addon_ref v18.0.1.0.NNN`）。讀不到時報告會註明，並退回只比 develop。
- JCR 需要憑證，本 repo 不讀 JCR。PaaS 的 digest 身分靠 seed 裡的 `tag@sha256`：JCR 鏡像用 `--preserve-digests`，與上游 digest 逐字相同。
- YAML 子集不支援 anchor、alias、tag、多文件，也不支援「折疊字串裡再縮排」這類罕見寫法。各邊的 Chart.yaml、values.yaml、config.yaml 已驗證可正確解析。
