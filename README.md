# VLM PDF to Markdown

Windows 桌面工具，使用本地 MinerU VLM 将文件夹中的 PDF 批量转换为 Markdown。选择文件夹、确认输出位置后即可开始，无需逐篇输入命令。

## 功能

- 默认只处理当前文件夹，可选择包含子文件夹。
- 默认输出到每份 PDF 所在目录，也可指定其他目录。
- 生成同名 `.md` 与 `_assets` 文件夹，重写配图引用。
- 显示逐文件进度、完成结果与错误信息。
- 已有完整结果会跳过；失败文件仅在勾选重试后重新解析。
- 保护用户创建或修改过的同名 Markdown 与配图。
- 发布中断后可继续完成保存，不需要重新推理；中断后用户修改的文件仍受保护。
- 停止时先完成当前 PDF，再停止后续文件。

## 安装

建议使用 Windows 10/11 与 Python 3.12。Python 安装时保留 Tk 支持。模型推理所需的内存、驱动和加速能力取决于设备与 MinerU 引擎；见 [MinerU 官方安装说明](https://opendatalab.github.io/MinerU/quick_start/)。

下载仓库，在项目目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe launcher.py
```

默认配置为 `standard` 档、ONNX 小模型和 llama.cpp VLM，使用强制 OCR 与全部页面。首次使用可能需要联网下载模型；下载模型不代表向服务上传 PDF。若需要预先准备模型，可执行：

```powershell
& .\.venv\Scripts\mineru-kit.exe models download --tier standard --small-backend onnx --vlm-engine llama-cpp --source modelscope
```

## 创建桌面入口

安装完成后，在 PowerShell 中执行：

```powershell
& .\Create-DesktopShortcut.ps1
```

脚本根据当前项目位置和 Python 环境生成快捷方式，不含开发者的电脑路径。以后双击桌面「VLM PDF转MD」即可打开。

## 使用

1. 选择包含 PDF 的文件夹。
2. 保留「输出到源文件目录」，或取消勾选后另选输出位置。
3. 点击「开始转换」，等待文件列表显示完成。
4. 点击「打开结果目录」查看结果。

例如，`report.pdf` 会生成 `report.md` 和 `report_assets/`。配图目录只包含图片与 Markdown 引用资源，不复制解析器的中间 JSON 或运行日志。移动或分享 Markdown 时须一起保留配图目录。指定输出目录并包含子文件夹时，程序保留原目录层级。

输出目录中的 `.vlm-work/` 保存解析缓存、日志与发布记录。保留它可继续处理并跳过完成结果。删除缓存后，程序无法确认已有同名输出的归属，会拒绝覆盖；需要重新解析时可另选空的输出目录。

## 使用已有 MinerU 环境

默认自动寻找项目 `.venv` 中的 Python 和 `mineru-kit`。也可设置以下环境变量：

| 变量 | 用途 |
| --- | --- |
| `VLM_MINERU_PYTHON` | 运行解析脚本的 Python 完整路径，环境中须有 `pypdf` |
| `VLM_MINERU_CLI` | `mineru-kit` 可执行文件完整路径 |
| `VLM_MINERU_CONFIG` | MinerU YAML 配置文件路径 |
| `VLM_APP_DATA` | 本工具设置、日志和缓存的保存目录 |

也可将 `config.example.json` 复制到 `%LOCALAPPDATA%\VlmPdfToMarkdown\config.json`，填写已有环境的路径。配置优先级为环境变量、用户配置文件、自动探测。留空的字段使用自动探测结果。

个人目录选择记录、日志、模型缓存默认保存在 `%LOCALAPPDATA%\VlmPdfToMarkdown`，不写入仓库。程序不要求 GitHub 凭据或云端解析 API Key。

## 测试

```powershell
py -3.12 -m unittest -v test_backend test_ui test_runtime
```

测试使用临时生成的文件和模拟解析结果，不包含真实论文或其他个人文档，不启动模型推理。

## 限制与许可证

此仓库提供源码和桌面快捷方式生成脚本，不包含独立 EXE、MinerU 安装包或模型权重。转换结果中的公式、参考文献和表格数值需对照原 PDF 核对。模型或解析器配置改变后，旧缓存可能不兼容，可选择新的输出目录。

本仓库代码使用 [MIT 许可证](LICENSE)。第三方组件遵守各自许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
