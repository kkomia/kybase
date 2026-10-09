# 沙箱基线清单（技能脚本要能跑）

> 来源：`docs/计划与记录/开发计划-v0.1.md` 那条"178 个空壳技能"的补装收尾（2026-09-29）。
> 这份文件是**一处可复现的清单**：沙箱里该有什么、为什么、以及**哪些不做**。
> **它不修改本机环境**（下面第 1 节的命令是给**镜像/容器**用的；本机是开发机，不是产品环境）。

## 0. 沙箱到底是什么（决定了这份清单落在哪）

| 路径 | 实际是什么 | 基线落在哪 |
| --- | --- | --- |
| Linux + 容器 | `isolation.DOCKER_IMAGE = "python:3.12-slim"`（`isolation.py:105`），`_docker_plan` 起容器跑命令 | **镜像**：`backend/Dockerfile` 的 runtime 阶段（或换一个自带基线工具的镜像） |
| Linux 无容器 | bubblewrap：`DEFAULT_BIND_RO` 只读挂 `/usr /bin /lib* …`（`isolation.py:92-101`）——**宿主有什么它才有什么** | 宿主/镜像的 PATH 与包 |
| Windows（本机开发） | 没有 bwrap/sandbox-exec → 降级 `direct`：命令**原样在本机跑**、继承 PATH（`isolation.py:189-213`） | **本机 PATH**（清单第 1 节的 PowerShell 片段只影响**沙箱进程**，不改系统） |
| macOS | sandbox-exec（Seatbelt profile） | 宿主 PATH 与 Homebrew 包 |

本机实测（2026-09-29）：`python` = `backend/.venv`（B 那条 lane 把 direct 档的解释器接到了 venv），
`node` / `npm` / `npx` / `pnpm` / `git` / `uv` / `pip` / `curl` / `tar` / `java` / `playwright` 都在 PATH 上。

## 1. 立刻做（已批准；风险低、收益最大）

按"影响面 × 确定性"排：

| # | 做什么 | 救多少技能 | 为什么 / 怎么落到基线 |
| --- | --- | --- | --- |
| 1 | **`pip install defusedxml requests`** | `defusedxml` 5 个 + `requests` 25 个 | Office 三件套（`docx`/`pptx`/`xlsx`）的**每一个**声明入口都在 import 期就死：`merge_runs.py:33` / `thumbnail.py:26` / `office/validate.py:22` / `comment.py:34` / `clean.py:23` 全要 `defusedxml`。落点：`backend/pyproject.toml` 的一个新 extra（建议名 `skills`）或镜像 `pip install` |
| 2 | **`python3` 别名 → 解释器本身** | **48 个** | 技能正文普遍写 `python3 scripts/…`，而 Windows 上没有 `python3`（有 `py -3`）。落点：镜像里 `ln -s "$(command -v python)" /usr/local/bin/python3`；Windows 上造一个 `python3.cmd` 放进**沙箱 PATH 靠前**的目录（**不是**再装一个 Python） |
| 3 | **`grep` / `sed` / `awk` 进 PATH** | 17 + 2 + 2 | 本机 Git 自带这三个（`C:\Program Files\Git\usr\bin`），只是不在 PATH 上；镜像里 `apt-get install -y grep sed gawk`（`python:3.12-slim` 默认只有简版） |
| 4 | ⚠️ **ImageMagick 要在 PATH 上，且排在 `System32` 之前** | **10 个** | **本机 PATH 上的 `convert` 是 `C:\Windows\System32\convert.exe` —— FAT→NTFS 磁盘转换工具，不是 ImageMagick**。技能点名 `convert` 想要的是 `magick convert`；这条会**静默调到一个完全不同的程序上**（比"缺"危险）。基线里：装 ImageMagick 并把它的目录**前置**；或在沙箱里给 `convert` 设成 `magick` 的别名 |

### 一段可直接粘的 PowerShell（只影响**沙箱进程**的 PATH，不改系统环境）

```powershell
# 沙箱启动器里执行（不是系统属性里改）：
$git = if (Test-Path 'C:\Program Files\Git\usr\bin') { 'C:\Program Files\Git\usr\bin' }
$im  = 'C:\Program Files\ImageMagick-7.1.1-Q16-HDRI'   # 装了才有；按实际版本改
$env:PATH = (@($im, $git) | Where-Object { $_ }) -join ';' + ';' + $env:PATH
# python3 别名：放在沙箱 PATH 最前面的一个目录里
$aliasDir = Join-Path $env:LOCALAPPDATA 'kylab-sandbox-bin'
New-Item -ItemType Directory -Force $aliasDir | Out-Null
Set-Content (Join-Path $aliasDir 'python3.cmd') '@py -3 %*' -Encoding ascii
$env:PATH = "$aliasDir;$env:PATH"
```

> 这一段**是给沙箱启动的那一层用的**（`agent_exec` 组装环境时），
> 不是让用户在自己机器上改系统 PATH——产品基线要么进镜像、要么进沙箱进程的环境。

### 一段可直接粘的 Dockerfile 片段（容器路径）

```dockerfile
# runtime 阶段（backend/Dockerfile）
RUN apt-get update && apt-get install -y --no-install-recommends \
        grep sed gawk imagemagick \
    && ln -sf "$(command -v python)" /usr/local/bin/python3 \
    && rm -rf /var/lib/apt/lists/*
# 技能脚本的 Python 依赖（另见 pyproject 的 skills extra）
RUN pip install --no-cache-dir defusedxml requests
```

## 2. 只标注、不装（长尾）

按"影响技能数"从高到低，**建议在技能侧标注「本平台不可用（缺依赖 X）」**——理由：装它们要么体积巨大
（`torch` 2 GB+、`cv2`、`scipy`）、要么是某个平台的专有物（`AppKit` 是 macOS 专有，永远装不上）：

| 依赖 | 影响技能 | 处置 |
| --- | --- | --- |
| `scipy` | 9 | 标注（体积 ~40 MB，可再议） |
| `cv2`（opencv-python） | 8 | 标注 |
| `pdfplumber` | 7 | 标注（`pypdf`/`PyMuPDF` 已装，能覆盖一部分） |
| `moviepy` / `torch` | 7 / 6 | 标注（`torch` 明确不装） |
| `bs4`（beautifulsoup4） | 5 | 标注（很轻，15 MB，可再议） |
| `librosa` / `sklearn` / `rdkit` / `h5py` / `transformers` / `openai` / `langchain_*` | 2–4 | 标注 |
| `AppKit` | 3 | **永远标注**（macOS 专有） |
| Node 包 29 个（`pptxgenjs` 等） | 1–3 | 标注（技能正文写"preinstalled"，与事实不符） |
| `soffice`（LibreOffice） | 5 | **等用户定**：~700 MB，`xlsx` 公式重算 / `pptx` 缩略图 / `docx` 转 PDF 都要它 |

## 3. 不是"缺依赖"的两条（别混进上面那张表）

1. ⚠️ **`xlsx/scripts/recalc.py` 在 Windows 上起不来**，原因在它自己的代码里：
   ```
   {"error": "Could not prepare the LibreOffice environment: module 'socket' has no attribute 'AF_UNIX'"}
   ```
   `AF_UNIX` 是 Unix 专有 → 这个技能应标注「**Linux 专用**」（装上 LibreOffice 也没用），
   免得模型反复重试一条走不通的路；
2. **`pptx/scripts/thumbnail.py` 之外，那套 Office 技能里另有 1136 个脚本依赖齐备、能直接跑**
   （实测抽 4 个跑 `--help` 全绿：`ab-test-analysis/ab_test_analyzer.py`、
   `abstract-trimmer/main.py`、`academic-abstract-refiner/refine_abstract.py`、
   `academic-abstract-refiner/validate_skill.py`）——**基线不是全红**，缺的是上面那几样。

## 4. 这份清单的测量口径（可复现）

| 数字 | 怎么量出来的 | 落点 |
| --- | --- | --- |
| 外部可执行 × 影响技能数 | **只认命令位**（围栏代码块 + 行内代码的首词），不是正文词频——第一版用词频数出 `make` 148 / `go` 90，**全是假数字** | `.workflow/skills-baseline-audit.py` → `.shots/skills-baseline.json` |
| Python/Node 第三方依赖 × 影响技能数 | **`ast` 取真 import**（不是正则：第一版把 docstring 散文算成包，数出叫 `the` 的"包"） | `.workflow/skills-deps-audit.py` → `.shots/skills-deps.json` |
| 真跑 | 走 `isolation.run_isolated(..., direct_isolation())`（与 `run_command` 同一执行层），按 `SKILL.md` **声明的入口**跑 | `.workflow/skills-realrun.py` → `.shots/skills-realrun.json` |
| 反向验证 | `pptx/scripts` 在场 → 文件真的被执行（死在依赖）；挪走 → `can't open file`；恢复 → 与①逐字相同 | `.workflow/skills-reverse-check.py` → `.shots/skills-reverse.json` |

**验收口径的一条纪律（本轮踩出来的）**："**带脚本**（看扩展名）"与"**能跑**（看依赖）"是两件事，
报告里必须分开说；否则"101 个带脚本"这种数字会让人以为它们都能用。
