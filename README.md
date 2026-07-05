# iOS 审核状态监控

一个独立的 macOS / Windows 可视化桌面工具，用来监控 App Store Connect 后台的 iOS 审核状态，包括提交 App 的审核状态和产品页面优化审核状态。

## 功能

- 可视化配置 App Store Connect API Key。
- `.p8` 私钥通过文件选择器选择，不需要手动输入路径。
- 自动读取一个或多个 App ID 的最终审核状态，不再区分显示 App 提审和产品页面优化。
- 内部会同时检查 App 提审和产品页面优化，优先显示正在审核，其次显示等待审核。
- 等待审核不播放提示音。
- 进入正在审核播放特殊提示音。
- 审核完成、等待发布、正在上架处理或 Ready for Sale 播放完成提示音。
- 表格中每个 App 只显示一行最终状态。
- 支持多个 Apple 账号，每个账号像浏览器标签页一样独立配置和监控。
- 点击“开始监控”时会先执行配置自检，检查 API Key、Issuer ID、`.p8`、每个 App ID 是否存在以及状态接口是否可用。
- 日志会显示每次状态来源，方便判断是从 App 版本、审核提交项还是 PPO 实验状态读取。
- 支持演示模式，方便先测试界面和提示音。
- 提供 macOS / Windows 打包脚本和 GitHub Actions。

## 本地运行

```bash
cd iOSReviewMonitor
python3 -m pip install -r requirements.txt
python3 main.py
```

Windows:

```powershell
cd iOSReviewMonitor
python -m pip install -r requirements.txt
python main.py
```

## 配置说明

在 App Store Connect 创建 API Key 后，在界面中填写：

- `Key ID`
- `Issuer ID`
- `.p8 私钥文件`：点击“选择文件...”选择下载的 `.p8`
- `App ID`：可填写一个或多个，多个 App ID 用逗号、空格或换行分隔
- `检查间隔`：建议 300 秒以上

配置会保存到系统应用数据目录。程序只保存 `.p8` 文件路径，不保存私钥内容。

## 多账号

顶部标签页区域可以管理多个 Apple 账号：

- 点击 `+ 新账号` 新增一个账号标签。
- 每个标签页单独填写 Key ID、Issuer ID、`.p8` 文件和 App ID。
- 每个账号可以独立点击“开始监控”和“停止”。
- 标签名后出现 `*` 表示该账号正在监控。
- 点击“保存全部配置”会保存所有账号；旧版本的单账号配置会自动迁移成第一个账号标签。

示例：

```text
1234567890
9876543210
```

或：

```text
1234567890,9876543210
```

## 打包

macOS:

```bash
cd iOSReviewMonitor
chmod +x build_tools/build_macos.sh
./build_tools/build_macos.sh
```

Windows PowerShell:

```powershell
cd iOSReviewMonitor
.\build_tools\build_windows.ps1
```

产物在 `dist/` 目录。

## GitHub 自动构建

推送到 GitHub 后，仓库里的 `.github/workflows/build.yml` 会自动运行。

也可以在 GitHub 仓库页面打开：

```text
Actions -> Build iOS Review Monitor -> Run workflow
```

构建完成后，在本次 workflow run 的 `Artifacts` 区域下载：

- `iOSReviewMonitor-macOS-AppleSilicon`：M1 / M2 / M3 / M4 等 M 系列 Mac 下载这个
- `iOSReviewMonitor-macOS-Intel`：Intel Mac 下载这个
- `iOSReviewMonitor-Windows`

macOS 下载后如果提示 Apple 无法验证，先解压对应的 zip，再右键 App 选择“打开”。如果仍然打不开，可以在终端执行：

```bash
chmod +x "/你的路径/iOS审核状态监控.app/Contents/MacOS/iOS审核状态监控"
xattr -dr com.apple.quarantine "/你的路径/iOS审核状态监控.app"
open "/你的路径/iOS审核状态监控.app"
```

没有 Apple Developer ID 公证的免费构建都会有这个提示；这只代表未公证，不等于包含恶意软件。
