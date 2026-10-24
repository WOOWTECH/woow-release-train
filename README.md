# woow release train：HA ↔ PaaS 版本清冊

這個 repo 用一份清冊 `woow-versions.yaml` 規定每個產品的上游版本，HA add-on 和 PaaS 兩邊都對齊它。另外有一支只用 Python 標準函式庫的小工具 `tools/wvt.py`，負責檢查清冊格式、比對各邊的釘點，並每天產出一張漂移（drift）報告。

- **主 repo**：Gitea `woow-paas/woow-release-train`（https://git-prod.woowtech.io/woow-paas/woow-release-train）
- **公開鏡像**：GitHub `WOOWTECH/woow-release-train`，唯讀，由 Gitea Actions 單向推送。GitHub 端不要直接改，下次鏡像會覆蓋。
- **目前階段**：第 1 期（2026-10-24 起）。HA 與 PaaS 的 CI 都跑 `wvt check`，**只有各產品的硬規則會擋，其他只警告**。第 2 期機器人（`tools/wvtbot.py`）已就緒：propose 開清冊 PR，兩個 fanout 先 dry-run。

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
| lockstep（兩邊必須同一顆上游 digest） | EMQX、LiteLLM、OpenDesign、MCP servers；之後加 code-server |
| range（允許落後，但有下限與天數上限） | n8n、Hermes、MCP Python SDK；之後加 Omnigent、Headscale、Tailscale |
| 自動化程度 | 機器人只開 PR、跑測試；合併與發版一律由人做 |
| Gitea | 機器人**永遠不合併** Gitea PR，也**不簽** paas-odoo-ci 的 `/approve` |
| 第 0 期 | 只放 EMQX、LiteLLM、n8n 三個產品，只出報告 |
| 雙向同步（2026-10-24） | 照建議進行第 1–3 期：兩邊 CI 檢查 → 機器人雙向開 PR → 加入 Hermes、OpenDesign、MCP servers。兩邊都人工合併；GitHub 端機器人用 GitHub App；公開鏡像的早期歷史一併清理 |

## 清冊格式（schema 1）

清冊是 YAML，但只用一個小子集：區塊 mapping／sequence、單行 flow（`[a, b]`、`{k: v}`）、引號字串、註解、`|`／`>` 區塊字串。`wvt` 內建這個子集的解析器，所以 runner 不需要安裝 PyYAML。測試時如果偵測到 PyYAML，會順便交叉比對兩者的解析結果。

**為什麼不用 JSON**：清冊要寫政策理由，例如「為什麼授權上限是 5.9.0」「例外為什麼可以存在」。JSON 不能寫註解。`wvt` 也接受 `.json` 檔，日後真的要換格式不必改程式。

```yaml
schema: 1
phase: 1                      # 0 只報告 → 1 警告＋硬規則 → 2 機器人 PR → 3 擴大
enforcement: hard-rules       # 各邊 CI 擋什麼：report-only（都不擋）／hard-rules（只擋硬規則）／strict（任何漂移都擋）
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
    shared_files:                   # 共用檔（例如 LiteLLM Woow 外掛）：來源 commit＋blob、各邊 lock
      - source: { leg: paas, repo: ..., path: ..., commit: "<40 hex>", files: [...], blobs: { <檔名>: "<git blob>" } }
        known_commits: [{ commit: "<舊 commit>", blobs: {...} }]   # lock 停在這些只算落後
        consumers: [{ leg: ha, repo: ..., lock: ... }]
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
| `dockerfile_from` | Dockerfile 的 `FROM` 映像（代換 ARG 預設值；`stage`＝AS 名稱或序號，預設最後一個） | — |
| `regex` | 任一文字檔的擷取群組（env 檔、Python 常數、文件；`group`、`unique`） | `pattern` |
| `pep508` | `pyproject.toml` 等的 `名稱==版本`（範圍不算釘點） | `package` |
| `uv_lock` | `uv.lock` 的 `[[package]]`（`field`：`version` 或 `sdist_sha256`） | `package` |
| `json_select` | JSON 陣列裡符合 `where` 的紀錄的欄位（`extract` 正規表示式、`path`） | `where`、`field` |
| `file_sha256` | 整個檔案的 sha256（vendor 進來的程式碼） | — |

- `parse`：`version`（預設）、`image_ref`（`repo:tag@sha256:…`）或 `digest`（純 sha256）。
- `compare`：`version`、`index_digest`、`platforms.<arch>`、`artifacts.<名稱>`、`identity.<名稱>`（`target.identity` 裡的 commit、build id、fork 的上游版本），或 `info`（只列參考，不比較）。
- `match: prefix`：identity 允許短 sha（至少 7 碼）；`tag_as_version: true`：seed 映像的 tag 就是版本（例如 OpenDesign 外殼 tag＝core build id）。
- `ref`：分支或 tag。`$prod_platform` 代表正式站平台版；`also_refs: [develop]` 會同時比 develop。

**只比上游身分。** add-on 版號、chart 版號、JCR 時間戳 tag 都是各邊自己的包裝，只放在 `package_version` 列參考。例如 HA EMQX 的 add-on 標 5.9.1，實際包的是上游 5.8.9；授權上限 `<5.9.0` 只看 5.8.9。

## 工具：`tools/wvt.py`

只用 Python 3 標準函式庫。

```sh
python3 tools/wvt.py validate                       # 格式＋一致性＋例外到期（到期＝失敗，14 天內＝警告）
python3 tools/wvt.py check --leg ha --repo-dir .    # 在某個 HA／PaaS repo 的 CI 裡比對自己的釘點（離線）
python3 tools/wvt.py check --leg paas --repo-dir . --fail-on none        # 只報告（本機查看用）
python3 tools/wvt.py check --leg paas --repo-dir . --strict              # 任何漂移都失敗
python3 tools/wvt.py check --leg ha --repo-dir . --json out.json --summary summary.md
python3 tools/wvt.py drift --out drift.md --json drift.json              # 抓真實來源、產生報告
python3 tools/wvt.py issue --report drift.md --json drift.json           # 建立／更新／關閉追蹤 issue
python3 -m unittest discover -s tests -v
```

`check` 會從 `git remote` 推斷自己是哪個 repo，也可以用 `--repo github:OWNER/REPO` 指定。不帶 `--fail-on` 時照清冊的 `enforcement` 決定擋什麼，所以各邊 CI 的判準只寫在清冊：要全部放寬（緊急開關），開一張把 `enforcement` 改成 `report-only` 的清冊 PR 即可。`--json` 輸出機器可讀的結果；在 GitHub Actions／Gitea act_runner 裡會自動寫 `$GITHUB_STEP_SUMMARY` 摘要，並對每項發現加上 `::error`／`::warning` 註記。

`check` 的判定規則：

- **硬規則**（各產品 `policy.hard_rules`）違反或不符 → 失敗；其他差異（落後、領先、例外快到期）只警告。
- **讀不到就擋（fail closed）**：有硬規則的產品，釘點檔案或 lock 讀不到、解析失敗，視同硬規則無法驗證 → 失敗。
- **授權上限要能驗**：有 `license_ceiling` 的產品，若本 repo 所有釘點都判讀不出上游版本（例如 seed 釘了清冊不認得的 digest）→ 失敗。先把該 digest 登記進清冊（`target` 或 `known_versions`）。
- **例外到期只影響自己**：`check` 不因別的產品例外過期而整份清冊失效；過期的例外只讓該產品的那一邊失敗（`exception_expired`）。
- **共用檔兩端分開看**：消費端（HA lock）的 commit／blob 必須等於清冊，複本內容必須等於 lock；lock 停在 `known_commits` 只算落後。來源端（charts）先改了共用檔只算領先（警告），合併後開清冊 PR 換 commit。

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
| `.github/workflows/wvt-check.yml` | 只在 GitHub（`workflow_call`） | HA repo 呼叫的可重用檢查。Gitea 有 `.gitea/workflows` 就不讀 `.github/workflows`，所以它只在鏡像上被使用 |
| `bots.yml` | 每天台北 09:11、清冊合併到 main、手動 | 第 2 期機器人 propose／fanout，做什麼照清冊 `bots:`；結果寫在執行摘要 |

權杖：

- 跨 repo 讀取用 org secret `CI_GITEA_TOKEN`，因為 `paas-app-bundle-cd` 是私人 repo。
- 寫本 repo 的 issue 用每次執行自動發的 `GITEA_TOKEN`。
- 沒有任何 workflow 會合併或核可東西。

## 第 1 期：兩邊怎麼接

HA 端（GitHub）不連 Gitea：讀公開鏡像的清冊，離線比對。PaaS 端（Gitea）從叢集內網 clone 清冊，不經 GitHub。兩邊用的是同一份 main。

**HA add-on repo**：新增 `.github/workflows/release-train.yml`

```yaml
name: Release train
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:
permissions:
  contents: read
jobs:
  check:
    name: Release train
    uses: WOOWTECH/woow-release-train/.github/workflows/wvt-check.yml@main
```

check 名稱是 `Release train / wvt check`。Woow_HA_App_Store 的 `registry.json` 把 `.github/workflows/release-train.yml` 列進該來源的 `required_workflows`：發布與消費都會確認這個 commit 的檢查成功，沒過就不上架。

**PaaS repo**（woow-paas-charts、woow_paas_platform、paas-odoo-ci）：新增 `.gitea/workflows/release-train.yml`（charts 用 `.yaml`）。刻意**不加 paths 過濾**：清冊改了也要重驗，而且日後設成 required check 時才不會讓沒碰到那些路徑的 PR 永遠缺一個 check。

```yaml
name: release-train
on:
  pull_request:
  push:
    branches: [main]          # woow_paas_platform 用 develop
  workflow_dispatch: {}
concurrency:
  group: release-train-${{ github.ref }}
  cancel-in-progress: true
jobs:
  wvt-check:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - name: 取清冊（woow-release-train main，叢集內網）
        env:
          U: ${{ secrets.CI_GITEA_USERNAME }}
          T: ${{ secrets.CI_GITEA_TOKEN }}
        run: |
          set -euo pipefail
          d="${RUNNER_TEMP:-/tmp}/release-train"
          rm -rf "$d"
          git -c http.extraHeader="Authorization: Basic $(printf '%s:%s' "${U:-ci}" "$T" | base64 -w0)" \
            clone -q --depth 1 http://gitea-http.gitea.svc.cluster.local:3000/woow-paas/woow-release-train.git "$d"
          git -C "$d" log -1 --format='清冊 %h %s'
      - name: wvt check
        run: python3 "${RUNNER_TEMP:-/tmp}/release-train/tools/wvt.py" check --leg paas --repo "gitea:${{ github.repository }}" --repo-dir .
```

順序：先合併本 repo 的 PR（鏡像推到 GitHub 後 HA 端才呼叫得到可重用 workflow），再合併各邊的接線 PR；Store 的 `required_workflows` 最後才加，否則沒有檢查紀錄的新版本會被擋。

## 第 2 期：機器人開 PR

雙向同步不是兩邊互抄版本：某一邊先升，機器人把它寫回清冊（propose）；清冊合併後，機器人再到還沒跟上的那一邊開 PR（fanout）。兩個動作只看「比清冊新」與「比清冊舊／不等於清冊」的邊，所以不會互相觸發成迴圈。所有 PR 都由人合併。

| 機器人 | 做什麼 | 權杖 | 誰合併 |
|---|---|---|---|
| `propose` | 某一邊的預設分支（HA main、charts／paas-odoo-ci main、平台 develop）比清冊新 → 到 registry 查該版的 index 與各架構 digest（EMQX 另取官方 tarball 的 `.sha256`）→ 開清冊 PR，舊目標移到 `known_versions`。共用檔來源先改 → 換 commit＋blobs，並把共用檔 import 的同目錄模組一起列入共用清單 | `GITEA_TOKEN`（本 repo） | 版本負責人 |
| `fanout_ha` | 清冊比 HA 新 → 每個 HA repo 一張 PR：改 Dockerfile ARG／lock 檔、add-on 版號（`bump` 規則）、CHANGELOG 加一行待改的說明、LiteLLM 共用檔複本＋lock | GitHub App（`WVT_GH_APP_ID`、`WVT_GH_APP_PRIVATE_KEY`） | HA 維護者 |
| `fanout_paas` | 清冊比 PaaS 新 → 先找 paas-odoo-ci 的 mirror-image 核可單（沒有就 dispatch，等人 `/approve`）；放行後改 seed／charts／paas-odoo-ci | `WVT_GITEA_BOT_TOKEN` | Eugene、Elmo |

- **模式在清冊**：`bots.<名稱>` 是 `off`、`dry-run`（只在執行摘要列計畫）或 `open-pr`。改模式也是清冊 PR。
- **一個產品、一個 repo、一個分支**：`wvt/propose-<產品>`、`wvt/fanout-<產品>`。重跑只更新同一張 PR；內容沒變就不推；那一邊對齊後，機器人關掉自己的 PR。
- **被關掉的提案不重開**：版本負責人關掉（不合併）的同一個提案不會再開。要重提，手動執行 `bots.yml` 並填 `product`、`version`。
- **安全邊界**：不提超過授權上限的版本；某一邊釘的 digest 跟 registry 查到的不同就不提案；禁止降版的產品不往下提；鏡像核可單被拒或逾時不自動重送。
- **PR 只是起點**：機器人只改清冊知道的釘點。文件、測試、遷移說明由各 repo 的 CI 與審核者補齊；資料遷移單向的產品（n8n）PR 內文要求附升級證據。

```sh
GITEA_READ_TOKEN=… python3 tools/wvtbot.py --mode dry-run          # 本機看三個機器人的計畫
python3 tools/wvtbot.py --bot propose --product litellm --version 1.105.0 --mode dry-run   # 先改清冊的手動升版
```

第 2 期用到的清冊欄位：

| 欄位 | 用途 |
|---|---|
| `upstream.image_tag` | 版本對應的映像 tag，預設 `{version}`（Hermes 用 `v{version}`） |
| `upstream.artifact_sha256.<名稱>` | 非映像產物（例如 EMQX tarball）的 `.sha256` 網址，含 `{version}` |
| `package_version[].bump` | fanout 時包裝版號怎麼變：`same`＝等於上游版本、`patch`＝+0.0.1、`none`（預設）＝不動 |
| `legs.<邊>.fanout.changelog` | 要加一行的 CHANGELOG；`changelog_entry`／`changelog_shared` 改寫那一行的文字 |
| `legs.paas.mirror` | `{dest_name, digest}`：PaaS 釘的是 JCR 鏡像時，fanout 先 dispatch mirror-image |
| 釘點 `mirror: true` | 這個釘點指向 JCR，新值要等鏡像 |
| 釘點 `write: "{jcr_tag}"` | 只列參考的釘點（例如 chart 的 JCR tag），其他釘點要改時一起改成這個值 |

## 新增一個產品

1. 用 `crane manifest <image>:<version>` 取得 index 與各架構 digest，寫進 `target`，並在 `verified` 註明日期與指令。
2. 決定 `policy.mode`。range 必須寫 `min_version`、`max_days_behind`、`target.since`。
3. 為 `ha`、`paas` 各列出釘點（`pins`），包裝版號放 `package_version`；HA 有上架 Store 的加 `published`。
4. 如果某邊目前停在舊 digest，把該版加進 `known_versions`，報告才能換算出版本。
5. 本機跑 `python3 tools/wvt.py validate`、`python3 -m unittest discover -s tests`，以及 `drift`（需要 Gitea 權杖，放環境變數 `GITEA_READ_TOKEN`）。單元測試讀的是凍結快照 `tests/fixtures/woow-versions.yaml`（和 fixtures 配套），線上清冊只驗格式；機器人改線上清冊不會讓測試壞掉。新產品要測讀法時，把它加進快照與 fixtures。
6. 開 PR。改清冊就是改版本政策，**一律人工合併**。

## 分期

| 期 | 內容 | 會擋什麼 |
|---|---|---|
| **0**（10-16 上線） | 清冊列 EMQX、LiteLLM、n8n 的現況；drift 報告＋追蹤 issue；GitHub 鏡像 | 什麼都不擋（只擋本 repo 自己的 CI） |
| **1（現在）** | 各邊 CI 跑 `wvt check`（HA：各 repo 的 `release-train.yml` 呼叫可重用 workflow，Store 以 `required_workflows` 把關；PaaS：charts、woow_paas_platform、paas-odoo-ci 各一支） | 只開兩條硬規則：**EMQX 授權上限**；**LiteLLM base digest＋外掛 lock commit／blob（含複本內容）必須等於清冊**。讀不到釘點一律擋。其他一律警告 |
| **2** | 機器人：某一邊先升 → 清冊 PR；清冊合併 → 扇出各邊 PR、dispatch mirror-image、開 seed／charts PR（`tools/wvtbot.py`） | 不擋；只產生 PR |
| **3**（10-24 開始） | 先加 Hermes（range）、OpenDesign core（lockstep）、MCP servers：n8n、Odoo、LiteLLM、Hermes、EMQX、Nextcloud（lockstep）＋MCP Python SDK（range，下限 1.30.0）。之後 code-server、Omnigent、Headscale、Tailscale、pi-agent、Nextcloud、Odoo | 新產品先只觀測：`hard_rules` 留空，OpenDesign 與 MCP 的 fanout 關閉（要人工匯入 core、重算 patch）。穩定後再決定硬規則與 SLA |

## 誰核可什麼

| 角色 | 負責 |
|---|---|
| 版本負責人（woowtech） | 合併本 repo 的清冊 PR；決定 lockstep 或 range、目標版本；核准例外（`approved_by`） |
| HA 各 repo 維護者 | 合併 HA 端的 PR；Store 同步自動進行 |
| PaaS 覆核者 | 合併 seed 與 charts PR；paas-odoo-ci `/approve` 由人簽 |
| 平台營運 | 發版、CD、租戶 Redeploy（範本對齊不等於線上服務已對齊） |
| 機器人／agent | 只開 PR、跑測試、維護追蹤 issue；**不合併 Gitea PR、不簽 `/approve`** |

## 已知限制

- PaaS「已部署」欄目前是手動快照（`observations`，有日期，超過 14 天會警告）。改由平台自動回報列在後續工作，第 1 期仍是手動。
- 正式站平台版只寫在 CD values 的註解裡（例如 `# 2026-10-22: addon .253（v18.0.1.0.253 → …）`）。`wvt` 取最上面一筆發版註記，並要求該註記寫到 `image.tag` 才算交叉驗證；驗不到會在報告備註，完全讀不到時退回只比 develop。
- JCR 需要憑證，本 repo 不讀 JCR。PaaS 的 digest 身分靠 seed 裡的 `tag@sha256`：JCR 鏡像用 `--preserve-digests`，與上游 digest 逐字相同。
- YAML 子集不支援 anchor、alias、tag、多文件，也不支援「折疊字串裡再縮排」這類罕見寫法。各邊的 Chart.yaml、values.yaml、config.yaml 已驗證可正確解析。
