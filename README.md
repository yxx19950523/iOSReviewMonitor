# iOS 审核状态监控

一个独立的 macOS / Windows 可视化桌面工具，用来监控 App Store Connect 后台的 iOS 审核状态。

## 功能

- 可视化配置 App Store Connect API Key。
- `.p8` 私钥通过文件选择器选择，不需要手动输入路径。
- 自动读取最新 iOS App Store 版本状态。
- 等待审核不播放提示音。
- 进入正在审核播放特殊提示音。
- 审核完成、等待发布、正在上架处理或 Ready for Sale 播放完成提示音。
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
- `App ID` 或 `Bundle ID`：二选一即可
- `检查间隔`：建议 300 秒以上

配置会保存到系统应用数据目录。程序只保存 `.p8` 文件路径，不保存私钥内容。

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

- `iOSReviewMonitor-macOS`
- `iOSReviewMonitor-Windows`
