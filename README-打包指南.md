# Mac 版打包指南（给你自己看）

## 一句话流程

把 `mac` 文件夹打成 zip 发给那台 Mac（或自己拿 U 盘拷过去）→ 在 Mac 上双击 **`一键打包.command`** → 拿到 `dist/视频工具.app`。

> **默认就是双架构**（arm64 + Intel 两份 ffmpeg 都备好），不用加参数，拿到手谁的 Mac 都能跑。
> 体积代价见下面「芯片架构」一节（约多 50 MB）。只有**确定对方芯片**时才用 `--one` 省这 50 MB。

### 怎么把 mac 文件夹打成 zip

在 Windows 这边跑（**推荐**，会自动排掉 `__pycache__` 等不该带的东西）：

```
python _package_mac_src.py
```

产物 → `发布/视频工具-Mac工程-YYYYMMDD.zip`（**约 170 KB**）。

想先看看会打包哪些文件：

```
python _package_mac_src.py --dry-run
```

也可以通过 `打包Mac工程.bat` 双击运行。

<details>
<summary>手动压也行，但要注意</summary>

右键 `mac` 文件夹 → 压缩，也能得到 zip。但**手压之前记得删掉 `mac/__pycache__/`** ——
在 Windows 上跑过 `py_compile` 或 `import` 就会生成它，里面是 **Windows 编译的 `.pyc`**，
拷到 Mac 上没用（Mac 会重新编译），还可能被 Python 误加载导致莫名报错。

`_package_mac_src.py` 会自动排掉：`__pycache__` / `*.pyc` / `*.pyo` / `build` / `dist` /
`.venv` / `_bundled_ffmpeg` / `.DS_Store` / 各种日志。
</details>

**Mac 完全认 zip** —— Finder 里右键就能压缩，双击 `.zip` 自动解压，系统自带的「归档实用工具」干这个的。
压成 zip 反而比散着传更好：微信/网盘传一堆散文件容易漏，一个 zip 不会掉文件。


---

## 为什么必须在那台 Mac 上打包

PyInstaller **不支持交叉编译**。Windows 上打不出 macOS 能跑的程序，反过来也一样。
所以「打包」这一步只能在 Mac 上做，我在 Windows 这边做不了。

我能做的：把跨平台源码改好、把打包脚本写好、把坑都标出来。
剩下那一步（大约 5 分钟）得你在 Mac 上敲一条命令。

> **🤔 手头没有 Mac？**
> 可以借 **GitHub 免费的 macOS 机器**替你跑一次打包（是真机，不是模拟器）。
> 完整流程见 **`README-在免费Mac上测试.md`**。
> 简单说：把 `mac/` 推到 GitHub → Actions 里点一下 → 等 6~10 分钟 → 下载 `.app`。
> 公开仓库**额度无限免费**。
>
> ⚠️ 但要提前知道：CI 里**点不了图形界面**，所以它只能验「打包通不通、能不能跑起来、
> 中文字幕烧不烧得出来」，**验不了**界面排版、预览拖动这些需要人眼看的东西。

---

## 具体步骤

### 1. 把工程发到那台 Mac 上

在 Windows 这边先打成 zip（见上面「怎么把 mac 文件夹打成 zip」），然后把
`视频工具-Mac工程-YYYYMMDD.zip` 通过微信/网盘/U 盘传过去。只有 170 KB，微信直接发就行。

对方收到后**双击 zip 就能解压**（Mac 认 zip，不用装任何解压软件），得到一个 `mac` 文件夹。

### 2. 双击打包（不用开终端）

打开解压出来的 `mac` 文件夹，**双击 `一键打包.command`**。

> **第一次双击可能被系统拦下**（提示「无法打开，因为来自身份不明的开发者」）：
> **右键点它 → 选「打开」→ 弹窗里再点一次「打开」**。之后就正常双击了。
>
> 如果双击**完全没反应**，多半是从 Windows 压缩包解压后文件丢了可执行权限。
> 在 `mac` 文件夹里开一次终端跑 `chmod +x 一键打包.command` 即可（只需一次）。

**想用终端的话**也可以（等价）：

```bash
cd 把mac文件夹拖进来
bash 打包mac.sh
```

就这一条，不用加参数 —— **默认已经会把 arm64 和 Intel 两份 ffmpeg 都备上**，
对方是 M 系列还是 Intel 都能跑。

只有当你**已经确定对方的芯片型号**、想省掉那 50 MB 时，才用单份模式：

```
bash 打包mac.sh --one
```

（`--one` 备的是**当前这台机器**的架构；`--arm` / `--intel` 是强制指定，一般用不到。
`bash 打包mac.sh --help` 可以看全部参数。）

### 3. 等它跑完（首次约 2~4 分钟）

脚本会自动：

| 步骤 | 做什么 |
|---|---|
| 1 | 检查 Python3（macOS 自带，一般不用管） |
| 2 | 装 PyInstaller + PySide6 |
| 3 | 准备 ffmpeg（优先用本机已装的 `brew install ffmpeg`，没装就自动下载） |
| 4 | 把 `app_icon.ico` 转成 `.icns` |
| 5 | 跑 PyInstaller 打出 `.app` |
| 6 | 自检，打印「全部正常」就是成了 |

### 4. 拿产物

在 `mac/dist/` 下会看到 **`视频工具.app`**。用「一键打包.command」跑的还会自动
帮你在访达里打开 `dist` 文件夹。

### 5. 发给用的人（这一步别搞混）

`dist/` 里要发的是这些：

```
dist/
├── 视频工具.app        ← 必须整个发（它是个文件夹，别只拽里面的东西）
└── ffmpeg/             ← 连着发，对方将来好换 ffmpeg 版本
```

把上面两样**一起**压成 zip 发给用的人。用 Windows 的话：

```
python _package_mac_src.py --dist
```

⚠️ **`.app` 是个文件夹**（不是单文件），压缩时要把整个 `.app` 一起压，
不能只拽 `Contents` 里面的东西，否则对方解压出来打不开。

---

## 两种「发给别人」别搞混

| | 发**工程**给打包的人 | 发**成品**给用的人 |
|---|---|---|
| 发什么 | `视频工具-Mac工程-*.zip`（约 170 KB） | `视频工具.app` + `ffmpeg/` 压成的 zip（约 196 MB） |
| 怎么来 | `python _package_mac_src.py` | 在那台 Mac 上打完包后 `python _package_mac_src.py --dist` |
| 对方做什么 | 解压 → 双击 `一键打包.command` | 解压 → 双击 `视频工具.app` |
| 为什么 | PyInstaller 不能交叉编译，必须在 Mac 上打 | 已经是成品，零依赖 |

---

## 关键设计（为什么对方什么都不用装）

`ffmpeg` 被放进了 **两份**：

```
dist/
├── 视频工具.app                      ← 单独拿走这个也行
│   └── Contents/Resources/ffmpeg/    ← 内置副本（保证「自带」）
├── ffmpeg/                           ← 外置副本（方便替换）
│   ├── ffmpeg
│   └── ffprobe
├── subtitle_presets/                 ← 字幕预设，可见可改
└── encode_reference.json             ← 编码参考数据
```

程序运行时按这个顺序找 ffmpeg：**外置 → 内置 → Homebrew → 系统 PATH**。

所以：

- 对方拿到**整个文件夹** → 用外置那份，将来他/你能直接换 ffmpeg 版本
- 对方**只拖走 .app** 到「应用程序」→ 用内置那份，照样跑

两种都行，这就是「自带」的含义。

---

## 发出去之前，建议你验一下

打包脚本最后会自检，但你可以自己再确认一次：

1. **把 .app 单独复制到别的地方**（比如桌面新建个文件夹），双击试试能不能开。
   这一步是在测「内置 ffmpeg 兜底」这条路 —— 对方很可能只拿走 .app。
2. 随便丢一个 mp4 进去跑一次拼接或者烧字幕，看有没有成品出来。
   **这一步最重要** —— 自检只测「能不能起来」，不测「能不能干活」。

---

## 已知的坑（都处理好了，说明一下为什么）

| 坑 | 处理方式 |
|---|---|
| 没签名，双击被 Gatekeeper 拦 | 说明书里写了「右键 → 打开」；脚本会自动清 quarantine 属性 |
| 从网盘/微信下载的文件带隔离属性 → 提示「已损坏」 | 脚本 `xattr -dr com.apple.quarantine` 清掉；说明书也给了手动命令 |
| `.app` 内部只读，用户数据写不进去 | `paths.app_dir()` 在 macOS 下**往上退三层**到 `.app` 同级，用户数据写外面 |
| `os.startfile` 是 Windows 独有的 | 换成跨平台 `open_in_explorer()`，macOS 走 `open` 命令 |
| `NtSuspendProcess` 是 Windows 独有的 | 换成 `SIGSTOP`/`SIGCONT`（暂停/恢复功能**保留了**，不用砍） |
| macOS 没有「进程优先级类」 | 整项隐藏（`paths.priority_supported()` 返回 False），设置对话框少一行 |
| `.ico` 图标 macOS 不认 | 转成 `.icns`；失败也无所谓，用默认图标 |
| 高分屏字糊 | `.app` 的 `Info.plist` 里开了 `NSHighResolutionCapable` |
| **字体路径全写的是 `C:/Windows/Fonts/`** | 抽成 `paths.font_candidates()`，按平台给候选；Mac 上落到 PingFang / Songti / STHeiti / Kaiti |
| **`or 微软雅黑` 这种兜底在 Mac 上拿到 `None`** | 换成 `paths.default_font()`，只返回**确实存在**的字体；一个都没有时明确返回 `(None, None)`，不再拿 `None` 当路径去读 |
| `Microsoft YaHei` 这个 Qt 字体族名 macOS 不认 | 加了 `_qt_family()` 映射表（`苹方 → PingFang SC` 等），不在表里的原样返回 |
| **打包时图标静默丢失** | 原脚本只找上一级的 `app_icon.ico`；工程包解压后没有上级 → 现在两处都找，`mac/` 里也放了一份 |
| 从 Windows zip 解压后 `打包mac.sh` 丢了 x 位 | 用 `bash 打包mac.sh` 调用，不依赖可执行位；`一键打包.command` 同理 |
| **推 GitHub 时带上了 Windows 的 `__pycache__`** | `.gitignore` 挡住（里面是 Windows 字节码，Mac 上用不了还可能被误加载） |

---

## 芯片架构：不知道就备份两份

打包脚本会自动识别当前 Mac 的芯片并选对应的 ffmpeg：

- **Apple Silicon（M1/M2/M3/M4）** → `arm64` 版 ffmpeg
- **Intel** → `x86_64` 版 ffmpeg

⚠️ **打出来的包只能在同架构的 Mac 上跑。**
M 系列打的包，Intel Mac 打不开；反之亦然。

### 三种打法，体积对比

| 打法 | 命令 | `.app` 体积 | 压缩发送 | 说明 |
|---|---|---|---|---|
| **两份都备（默认）** | `bash 打包mac.sh` | **约 243 MB** | 约 196 MB | **不用问对方芯片，拿到就能跑** |
| 只备 arm64 | `bash 打包mac.sh --one`（在 M 系列上打） | **约 193 MB** | 约 172 MB | 确定对方是 M 系列时用，最小 |
| 只备 Intel | `bash 打包mac.sh --one`（在 Intel 上打） | **约 200 MB** | 约 175 MB | 确定对方是 Intel 时用 |

**双架构的代价 = 多约 50 MB（解压后）/ 多约 24 MB（压缩传输）**，换来的是不用问对方芯片。

> 参考基准：ffmpeg + ffprobe 两份分别是
> arm64 约 43 MB（zip 各 21.6/21.5 MB）、Intel 约 50 MB（zip 各 24.9 MB）。
> Python + PySide6 + Qt 那部分是固定的约 150 MB。

> ⚠️ `--one` 备的是**当前机器**那一种架构。在 M 系列 Mac 上跑 `--one` 得到的是 arm64 包，
> 拿到 Intel Mac 上依然打不开 —— 所以不确定就别用 `--one`，用默认的就好。

### 双架构是怎么工作的（重要）

脚本**不会**把两份 ffmpeg 都塞进 `.app` —— 那样 `PyInstaller` 只会认一个名字，
而且塞两份会让 `.app` 里出现两个同名的 `ffmpeg`，运行时会撞车。

实际做法：

1. `.app` 内 + `.app` 旁边那份，用的是**当前机器架构**（保证能跑）；
2. **另一架构的一份**放在 `_bundled_ffmpeg/_alt/`，**不参与打包**，只留在你手里当备胎；
3. 打完包后在终端里会打印提示，告诉你备胎在哪、怎么用。

如果打出来的包在对方机器上因为架构不对跑不起来（现象：双击闪一下就没了，
或者提示「不是受支持的格式」）：

```
把 _bundled_ffmpeg/_alt/ 里的 ffmpeg、ffprobe
改名覆盖到「视频工具.app 旁边的 ffmpeg/ 文件夹」即可
```

**不用重新打包** —— 因为 `paths.py` 的查找顺序是「外面那份优先」。
这个排查办法也写进了 `给收包人看-使用说明.txt`，对方自己能照着弄。

### 最省事的做法

默认打就完了（243 MB），谁的 Mac 都能跑。
只有明确知道对方芯片、且很在意那 50 MB 时，才去左上角  →「关于本机」→ 看「芯片」那行，
然后加 `--one`。

---

## 如果打包报错

把终端里的完整输出发我。常见的三种：

**① `externally-managed-environment`**
系统 Python 不让直接装包。用虚拟环境：
```bash
python3 -m venv .venv
source .venv/bin/activate
bash 打包mac.sh
```

**② PySide6 装不上**
多半是 Python 版本太新或太旧。推荐 3.11 / 3.12：
```bash
brew install python@3.12
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
bash 打包mac.sh
```

**③ ffmpeg 下载失败**
手动装一个再重跑：
```bash
brew install ffmpeg
bash 打包mac.sh
```

---

## 我这边验证过什么 / 没验证过什么

**验证过（三组自动检查全过，共 181 项）：**

*平台兼容（56 项）*
- 所有源码在 Mac 分支下语法正确、能编译
- `app_dir()` 对 `.app` 路径的解析（含中文路径）
- `procctl` 的 SIGSTOP/SIGCONT 分支存在且不再 raise
- 所有 `os.startfile` 都在 Windows 分支里，Mac 走 `open`
- 优先级设置在该隐藏时确实隐藏，且**编码参考表的「优先级」列不受影响**（那是数据）
- `.spec` / 打包脚本的关键开关都在

*字体（41 项）*
- 源码里**没有任何一处**硬编码 `C:/Windows/Fonts/` 在会执行到的地方（用 AST 查的，不是查文本）
- 模拟 macOS 文件系统后，字体列表**非空**、路径全在 `/System` 或 `/Library` 下、含 Mac 专属字体
- `default_font()` 在模拟 Mac 上有值（原 bug 就是这里返回 `None`）
- 老字体名（「微软雅黑」）在 Mac 上也能落到真实存在的 Mac 字体
- `subtitle.py` / `subtitle_tab.py` 的对外接口与映射表都连着新的 `paths` 表

*打包格式 / 双架构 / CI（84 项）*
- 打包脚本 bash 语法通过、无 BOM / 无 CRLF（这两个会让脚本直接跑不起来）
- 默认是双架构、`--one` 能关掉、两份下载地址都没写错
- 双架构时 `.app` 内的主份路径**唯一**（不会两个同名 ffmpeg 撞车）
- 内置 ffmpeg 会显式 `chmod 755`（丢可执行位是个隐蔽坑）
- `一键打包.command`：LF / 无 BOM / 先 cd / 用 bash 调用 / 有失败提示 / 结尾停住窗口
- GitHub workflow：**只手动触发**（不会 push 就烧额度）/ 有超时 / 调的是同一份 `打包mac.sh` / 用 `ditto` 压 `.app`
- `.gitignore` **实测**挡住了 `__pycache__`/`build`/`dist`/`_bundled_ffmpeg`，且没误挡源码

**没验证过（没法验，需要真 Mac）：**
- 实际打包能否成功 ← **可以靠 GitHub 那份 workflow 验**（见 `README-在免费Mac上测试.md`）
- `.app` 在那台 Mac 上能否真正启动 ← 同上，CI 会跑 `--selftest`
- **Mac 上真的能渲染出中文字幕** ← CI 会烧一段样例视频给你肉眼看
- 暂停/恢复在真机上是否好用
- ffmpeg 处理中文路径文件名是否会出问题
- **界面相关的一切**：排版 / 预览拖动 / 高分屏是否糊 / 菜单中文会不会被截断
  ← 这些 CI 永远验不了（没显示器），只能等真人开窗口看

---

## 隔离性保证

**Windows 版一个字节都没动。** 我核对过 MD5：

| 文件 | 状态 |
|---|---|
| app.py / paths.py / procctl.py / utils.py / subtitle_tab.py / subtitle.py | Mac 版已改（都加了平台分支） |
| concat.py / reencode.py / probe.py / config.py / stats.py / batch_dialog.py | **MD5 相同，字节级一致** |

> `subtitle.py` 的改动**只在内核之外**：字体表从写死的 Windows 路径改成问 `paths` 要，
> 烧字幕的算法、参数、`.ass` 解析逻辑一个字节没动 —— 所以 Windows 版的成片效果不受任何影响。

Mac 版放在 `D:\video-tool\mac\`，是独立副本。
`D:\video-tool\` 根目录下你原来那些文件没有任何改动，继续用、继续发 Windows 包都不受影响。
