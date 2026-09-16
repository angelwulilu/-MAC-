# 在免费的 Mac 环境里打包测试 —— 完整流程

> 目标：**用 GitHub 免费的 macOS 机器替你跑一次 PyInstaller**，把打好的
> `视频工具.app` 下载回 Windows 这边。
>
> 为什么需要这个：PyInstaller **不能交叉编译**，Windows 上打不出 macOS 包。
> 你自己没 Mac，那 GitHub 的 macOS runner 就是最现实的替代 —— 它是**真实的 Mac 机器**，不是模拟器。

---

## 一、先搞清楚「免费 Mac 环境」有哪几种（别走错路）

| 方案 | 能打包吗 | 能跑 .app 吗 | 免费额度 | 适合你吗 |
|---|---|---|---|---|
| **GitHub Actions**（macos runner） | ✅ 能 | ⚠️ 只能命令行自检，点不了界面 | 公开仓库**无限免费**；私有仓库约 200 个 macOS 分钟/月 | ✅ **推荐，本文走这条** |
| 本地虚拟机（在 PC 上装 macOS） | ✅ 能 | ✅ 能 | 免费但要折腾，且**法律/GitHub 都不待见** | ❌ 太麻烦 |
| 云 Mac（MacStadium / MacinCloud） | ✅ 能 | ✅ 能 | 试用期短，之后**收费** | ❌ 不免费 |
| 网页版「在线 Mac 模拟器」 | ❌ 不能 | ❌ 不能 | — | ❌ **是玩具，跑不了 Python** |

⚠️ **重点**：网上那些「免费在线 Mac」基本只是**外观皮肤**，不是真 macOS，装不了 Python。
别在那边浪费时间。

**GitHub 的 macos-latest 是真机器**，架构是 **arm64（M 系列芯片）** →
所以打出来的 `.app` **只能在 Apple 芯片的 Mac 上运行**。
（需要 Intel 版就用 `macos-13`，但排队更久、额度一样算。）

---

## 二、开始前要知道的成本

| 仓库类型 | macOS 额度 | 本流程一次约跑 | 大概能跑几次 |
|---|---|---|---|
| **公开仓库** | **无限免费** | 6~10 分钟 | 随便跑 |
| **私有仓库** | 2000 分钟/月，但 **macOS 按 10 倍折算** ≈ 200 分钟 | 6~10 分钟 | **约 20~30 次** |

**建议**：如果代码不介意公开 → 建**公开仓库**，随便跑。
如果必须私有 → 注意别浪费，两个 workflow 我都已经改成**只能手动点，不会 push 就自动跑**。

---

## 三、完整步骤（从零开始）

### 第 1 步：注册 / 登录 GitHub

有账号就跳过。没有的话去 <https://github.com> 注册，免费。

### 第 2 步：装 Git + 配 Git（**一次性**）

⚠️ **注意：你机器上现在还没装 Git for Windows** —— 所以这一步分两小步。

#### 2.1 先装 Git for Windows

打开 <https://git-scm.com/download/win>，点 **64-bit Git for Windows Setup**，
双击安装，**一路 Next 用默认设置即可**（别改安装路径）。

或者更快：按 `Win` 键 → 输入 `powershell` → 回车 → 粘贴这行：

```powershell
winget install Git.Git
```

装完后「开始菜单」里会出现 **Git Bash**。

#### 2.2 用向导配好（推荐）

回到 `video-tool` 文件夹，**双击 `配置git.bat`**。

它会问你名字和邮箱，然后自动写入这 4 条配置并自检：

| 配置 | 值 | 为什么 |
|---|---|---|
| `user.name` | 你的名字 | commit 记录里的作者名，随便取 |
| `user.email` | 你的邮箱 | commit 记录里的邮箱，**必填** |
| `core.quotepath` | `false` | 中文文件名不乱码 |
| `core.autocrlf` | `input` | **推送时 CRLF 转 LF** |

#### 2.3 或者手动敲（想自己来的话）

打开 Git Bash，依次跑：

```bash
# ① 告诉 git 你是谁（换成你自己的邮箱和名字）
git config --global user.name "你的名字"
git config --global user.email "你的邮箱@example.com"

# ② 让中文文件名不乱码（Windows 上很重要）
git config --global core.quotepath false

# ③ 🔴 推送时把 CRLF 转成 LF（必须！理由见下）
git config --global core.autocrlf input

# ④ 确认配好了
git config --global --list
```

> ⚠️ **`core.autocrlf input` 是这里最关键的一条**。
> 你的 `打包mac.sh` 和 `一键打包.command` 在 Mac 上必须是 **LF** 换行。
> 带 CRLF 的话 Mac 会报 `'\r': command not found`，**整个脚本跑不起来**。
>
> **为什么是 `input` 而不是网上教程常说的 `true`？**
>
> | 取值 | 推送时 | 检出回 Windows 时 | 结果 |
> |---|---|---|---|
> | `true` | CRLF→LF | LF→CRLF（**改写你本地文件**） | 文件被反复改，编辑器老报「已被外部修改」 |
> | **`input`** | CRLF→LF | **不动** | ✅ 推出去是 LF，本地保持原样 |
> | `false` | 不转 | 不动 | ❌ CRLF 原样传上去，Mac 上脚本炸 |
>
> 你的 `app.py`、`utils.py` 现在都是 CRLF（Windows 原生），所以 `input` 最合适。
>
> 另外还有个小细节：`subtitle_presets/` 里 **2 个 .ass 是 CRLF、2 个是 LF**。
> 设成 `input` 后推到 Mac 上会**统一成 LF**，这反而更安全 ——
> 因为用 `split('\n')` 那种解析方式读 CRLF 的 .ass 时，字段名会变成 `Text\r` 匹配失败。
> （目前 `subtitle.py` 用的是 `splitlines()`，本来就正确，所以没出过问题。）

### 第 3 步：在 GitHub 上建一个空仓库

1. 右上角 **`+`** → **New repository**
2. **Repository name** 填 `video-tool-mac`（随便取）
3. 选 **Public**（免费无限额度）或 **Private**（每月约 20~30 次）
4. ⚠️ **不要**勾 "Add a README file"、不要加 .gitignore —— 要**完全空的仓库**，否则推送会冲突
5. 点 **Create repository**
6. 建好后页面会显示一个地址，形如：
   `https://github.com/你的用户名/video-tool-mac.git`
   **复制它，下一步要用。**

### 第 4 步：把 `mac/` 推上去

#### 4.1 用向导推（推荐，一键）

回到 `video-tool` 文件夹，**双击 `推送mac到github.bat`**。

它做三件事：

| 阶段 | 做什么 |
|---|---|
| **体检** | git 装了没 / 身份配了没 / 行尾对不对 / `.gitignore` 挡没挡住产物 |
| **问答** | 让你粘贴仓库地址（会校验格式，网址写错会提醒重填） |
| **推送** | `init` → `add -A` → `commit` → `remote` → `push` |

体检长这样：

```
============================================================
  把 mac/ 推送到 GitHub
============================================================
  源目录：D:\video-tool\mac

============================================================
  [1/3] 查找 git
------------------------------------------------------------
    OK   C:\Program Files\Git\cmd\git.exe
         git version 2.55.0.windows.3

============================================================
  [体检 1/4] git 身份配置
------------------------------------------------------------
    OK   user.name      = yu
    OK   user.email     = you@example.com
    OK   core.autocrlf  = input   （推送时 CRLF→LF）

============================================================
  [体检 2/4] 行尾检查（Mac 的 .sh / .command 必须是 LF）
------------------------------------------------------------
    !!   打包mac.sh  有 317 处 CR（CRLF）
    !!   一键打包.command  有 75 处 CR（CRLF）

    这两个文件在 Mac 上会报 bad interpreter: /bin/bash^M 直接跑不起来。
    ✅ 不用手动改 —— 只要 core.autocrlf=input，推送时 git 会自动转成 LF。

============================================================
  [体检 3/4] .gitignore 是否挡住了不该传的东西
------------------------------------------------------------
    OK   挡住 __pycache__/         (Python 字节码缓存)
    OK   挡住 build/               (PyInstaller 产物)
    OK   挡住 dist/                (PyInstaller 产物)
    OK   挡住 _bundled_ffmpeg/     (临时下的 ffmpeg（约 93MB）)
    OK   挡住 *.zip                (成品包)
    OK   挡住 .DS_Store            (macOS 系统文件)

============================================================
  [体检 4/4] 待推送内容预览
------------------------------------------------------------
    文件数：31 个
    总体积：569.3 KB
```

> **体检有警告时它会停下来问你**，不会闷头推。
> 比如 `user.name` 没配，它会提示你先去双击 `配置git.bat`。

#### 4.2 或者手动敲（想自己来的话）

打开 Git Bash，依次跑（**注意：只推 `mac/` 这一个目录，不是整个 video-tool**）：

```bash
cd /d/video-tool/mac

git init
git add -A
git commit -m "Mac 版打包工程"

# 换成你第 3 步复制的地址
git remote add origin https://github.com/你的用户名/video-tool-mac.git
git branch -M main
git push -u origin main
```

**第一次 push 会弹窗要你登录 GitHub**：
- 弹浏览器 → 点授权即可（推荐，最简单）
- 或者要 **token**：去 GitHub → Settings → Developer settings →
  Personal access tokens → Tokens(classic) → Generate new token → 勾 `repo` → 复制那串码当密码用

> **为什么只推 `mac/`？**
> ① `mac/` 是**独立副本**，自己就是个完整工程，能单独跑；
> ② 整个 `video-tool` 里有 `dist/`（几百 MB 的成品）、`_test_media/`（真实素材），
>    推上去又慢又没必要。
>
> 我的 workflow 两种布局都兼容（`mac/` 当仓库根、或 `video-tool` 当仓库根），
> 你以后想改推整个目录也能直接跑。

### 第 5 步：确认 `.gitignore` 挡住了不该传的东西

`mac/.gitignore` 我已经配好了，会排掉 `__pycache__`、`build`、`dist`、`_bundled_ffmpeg`。
（第 4.1 的向导会自动帮你查这一步；手动推的话可以自己看一眼。）

```bash
git status --short | head -40
```

**看到这些就要警惕**（不该出现在列表里）：
`__pycache__/`、`build/`、`dist/`、`_bundled_ffmpeg/`、`*.pyc`

### 第 6 步：在 GitHub 上点一次打包

1. 打开你的仓库页 → 顶部 **Actions** 标签

   ⚠️ **左边列表里有两个 workflow，别点错：**

   | 点哪个 | 名字 | 有表单吗 | 耗时 | 你什么时候用它 |
   |---|---|---|---|---|
   | ✅ **点这个** | **Mac 打包（产出 .app 成品 ← 要这个）** | **有**：`run_tests` + `make_dmg` | 6~10 分钟 | **要 .app 成品** |
   | ⬜ 别点 | Mac 冒烟测试（字幕+字体，不管打包） | 有：`burn_sample` | 3~5 分钟 | 只想快验一下字幕/字体 |

   > **怎么快速确认自己点对了**：点 **Run workflow** 之后应该**弹出一个表单**。
   > 如果看到 `run_tests` 这个勾选项 → **点对了**。
   > 如果看到的是 `burn_sample` → 你点的是冒烟测试，那个**不打包 .app**。

   > **看不到 `run_tests` 的两种可能**：
   > ① **点错 workflow 了**（最常见）—— 表单里有 `burn_sample` 而不是 `run_tests`，
   >    说明你选的是「Mac 冒烟测试」。回左侧列表换选带「**产出 .app**」的那个。
   > ② 列表里**只有冒烟测试、没有「Mac 打包」** → 说明 `mac-package.yml` 没推上去。
   >    本地确认：`git ls-tree -r --name-only origin/main | findstr workflows`
   >    应该看到两个文件；少一个就重新推。

2. 选好 workflow 后，右侧点 **Run workflow** 按钮 → 弹出的表单里：
   - `run_tests` 保持勾选（顺带跑字体检查 + 烧一条中文字幕）
   - `make_dmg` 不用勾（zip 已经够用）
3. 点绿色的 **Run workflow** 确认
4. 等 **6~10 分钟**（页面会自动刷新，也能看到每步的实时日志）

### 第 7 步：下载成品

跑完后点进那次运行（绿色 ✓），页面**最下方**有 **Artifacts** 区：

| Artifact 名字 | 里面是什么 |
|---|---|
| **视频工具-macOS-arm64** | **← 你要的**：`视频工具.app` + 外置 `ffmpeg/`，一个 zip |
| 中文字幕烧录样例 | 一段真机烧的中文字幕视频，**用肉眼看字形对不对** |
| 打包日志 | 出问题时的完整日志，可以发我看 |

⚠️ **Artifact 只保留 14 天**（我设的），过期自动删，记得及时下载。

---

## 四、拿到 `.app` 之后怎么验

1. **先看「中文字幕烧录样例」那段视频** —— 这是最能说明问题的：
   如果中文字形正常，说明 Mac 上的字体解析 + ffmpeg 烧录整条链路是通的。
2. `.app` 本身是 **arm64**，只能在 **M 系列 Mac** 上跑。
   你要是有 Apple 芯片的 Mac（或同事有），拷过去双击试试。
3. 如果对方是 Intel Mac → 这份 `.app` **跑不起来**（会闪退或提示格式不对）。
   两个办法：
   - 把 workflow 里 `runs-on: macos-latest` 改成 `macos-13`（Intel），再跑一次；
   - 或者用 `--both` 模式打包时那份 `_alt/` 备胎换掉旁边的 ffmpeg（见打包指南）。

---

## 五、这个 CI 替你验了什么、没验什么

**能验（这就是它的价值）：**

| 项目 | 说明 |
|---|---|
| PyInstaller 在真 Mac 上能不能跑通 | 打包流程本身 |
| 依赖版本对不对 | PySide6 3.12 的 wheel 能不能装上 |
| `.app` 结构完不完整 | 内置 ffmpeg 有没有进去、可执行位对不对 |
| `.app` 能不能启动 | 跑 `--selftest` 看结论 |
| **Mac 上字体解析对不对** | 探测到哪些中文字体、路径对不对 |
| **中文字幕真能烧出来** | 真跑 ffmpeg，成品视频能下载回来看 |
| 成品体积 | 和预估的 193 MB 对不对得上 |

**不能验（CI 的先天限制）：**

- ❌ **图形界面** —— CI 没有显示器，窗口点不了。
  所以「界面排版好不好看、按钮点起来顺不顺手、字幕拖动预览对不对」**验不了**。
- ❌ 高分屏字糊不糊、菜单中文会不会被截断 —— 这些必须**真人看**。
- ❌ 暂停/恢复按钮的实际手感。

> 也就是说：CI 能帮你把「**能不能跑起来 / 能不能干活**」这类问题挡掉，
> 但「**界面好不好用**」还是得等真 Mac。

---

## 六、常见问题

**Q：`Run workflow` 按钮是灰的 / 找不到我的 workflow？**
A：① 确认推送成功（刷新仓库页能看到 `.github/workflows/mac-package.yml`）；
② 只有**默认分支**（`main`）上的 workflow 才会出现在列表里；
③ 刚推完可能要等十几秒才刷新出来。

**Q：点了 Run workflow 但表单里没有 `run_tests`？**
A：**你点错 workflow 了。** 左边列表有两个，`run_tests` 在带「**产出 .app 成品**」
字样的那个（`mac-package.yml`）里。另一个「Mac 冒烟测试」的表单只有 `burn_sample`，
而且它**不打包 .app**。换一个再点。

**Q：左边列表里根本没有「Mac 打包」这一项？**
A：说明 `mac-package.yml` 没推上去。在 `mac/` 目录里确认：
```bash
git ls-tree -r --name-only origin/main | grep workflows
```
应该列出**两个**文件。只列出一个或者为空 → 重新推一次：
```bash
git add -A && git commit -m "补 workflow" && git push origin main
```
推完回 Actions 页面按 F5 刷新。

**Q：跑失败了怎么办？**
A：点进失败的那次运行 → 看哪个步骤红了 → 展开日志。
最省事的是把 **「打包日志」Artifact** 下载下来发我。

**Q：日志看不了 / 只有一句「Process completed with exit code 1」？**
A：GitHub 的作业日志要登录才能下载（匿名 API 会返回 403）。
如果 Artifact 里也没有「打包日志」，说明**失败发生在打包步骤之前**
（日志文件是打包时才生成的）。这种情况看**步骤名**就能定位：
展开失败的那一步，一般会打印出 xx 项 PASS / 几条 FAIL，
把 **FAIL 那几行**截图发我就够了。

**Q：第 5 步「字体解析 + 交付物格式检查」秒挂（exit 1）？**
A：这一坑踩过一次，记下来：自检脚本里有一批断言查的是
**仓库外**的 Windows 侧文件（`配置git.bat` / `_setup_git.py` /
`推送mac到github.bat` / `_push_mac.py` / `_package_mac_src.py`）。
这些文件在 `D:\video-tool\` 下 —— 但 GitHub 仓库的根**就是 `mac/`**，
所以 runner 上一个都找不到 → 断言失败 → 整个脚本 `exit 1`。
**本地永远测不出来**（本地这些文件都在），所以在 Mac 上遇到
「本地全过、CI 秒挂」时，先怀疑这一类。

现在已经修好并且上了双保险：
- 这类断言统一走 `check_win_side()`：**找不到就「跳过」，不判失败**
- `_verify_runner_layout.py` 会**把 `mac/` 拷到临时目录假装成仓库根**，
  在那份「runner 同款布局」里把三个自检脚本再跑一遍 —— 专门抓这种 bug
- `_verify_mac_format.py` 的 `[6]` 组是**元检查**：用 AST 扫源码，
  发现「没受保护的上游目录引用」直接报错

想在本机自查有没有这类问题（Windows 上也能跑）：
```bash
cd mac
python _dev_tests/_verify_runner_layout.py
```
输出 `通过 3 个脚本，失败 0 个` 就说明 CI 那关能过。

**Q：报 403 / 权限错误？**
A：仓库 Settings → Actions → General → Workflow permissions →
选 **Read and write permissions** → Save。

**Q：额度用完了会怎样？**
A：私有仓库额度耗尽后，那个月剩下的日子 macOS job 会**直接失败**
（不会偷偷扣钱，除非你主动加了付费方式并超支）。
下个月 1 号自动重置。公开仓库没有这个问题。

**Q：能不能让它 push 就自动跑？**
A：可以，但**不建议**（每推一次烧一次额度）。
真要开的话，把 workflow 里 `on:` 部分改成：
```yaml
on:
  workflow_dispatch:
  push:
    branches: ['**']
```
公开仓库可以这么干，私有仓库别。

**Q：我想测 Intel 版怎么办？**
A：把 `runs-on: macos-latest` 改成 `runs-on: macos-13`，再手动跑一次。
注意 Intel runner 排队时间通常更长。

---

## 七、文件清单（我这次给你加的）

| 文件 | 位置 | 作用 |
|---|---|---|
| `.github/workflows/mac-package.yml` | `mac/` | **打包 workflow**（本文主角）：真机打包 → 自检 → 烧字幕 → 传 Artifact |
| `.github/workflows/mac-smoke-test.yml` | `mac/` | 轻量冒烟测试（原本就有，我把「push 自动跑」关掉了改成手动） |
| `.gitignore` | `mac/` | 挡住 `__pycache__` / `build` / `dist` / `_bundled_ffmpeg` |
| `_dev_tests/_verify_mac_fonts.py` | `mac/` | 字体跨平台解析自检（41 项） |
| `_dev_tests/_verify_mac_format.py` | `mac/` | 交付物格式 + workflow + 仓库布局自检（171 项） |
| `_dev_tests/_verify_mac_platform.py` | `mac/` | 平台分支逻辑自检（56 项） |
| **`_dev_tests/_verify_runner_layout.py`** | `mac/` | **精简布局复跑**：把 `mac/` 拷到临时目录当仓库根，复现 runner 布局再跑一遍三个自检 —— 专抓「只在本地能过」的断言 |
| `README-在免费Mac上测试.md` | `mac/` | 本文 |
| **`配置git.bat`** | **`video-tool/`** | **双击即配好 git（第 2 步用）** |
| **`_setup_git.py`** | **`video-tool/`** | 上面那个 bat 的实际逻辑 |
| **`推送mac到github.bat`** | **`video-tool/`** | **双击即推送（第 4 步用）：体检 + 问答 + 推送** |
| **`_push_mac.py`** | **`video-tool/`** | 上面那个 bat 的实际逻辑 |
