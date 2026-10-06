# InstaCrop 相纸扫描裁剪

从混合尺寸的相纸扫描图中提取每张照片的独立 Windows 工作台。所有识别、裁剪与导出均在本机执行。

## 功能

- 批量导入 JPG、PNG、TIFF（含多页）、BMP、WebP 扫描图；普通静态照片额外支持 HEIC/HEIF。
- 自动识别相纸外框与内部画面，透视拉正，保留原图分辨率。
- 手动补框、拖动四角、原扫描局部放大镜、边缘吸附与方向键微调。
- 90° 旋转、0.1° 微旋转、水平/垂直透视、四边独立收边、朝向与边缘复核。
- 批量参数、选择与筛选、撤销；文件夹或 ZIP 导出带白边、照片画面与背景成图。
- 保存/恢复静态照片 `.polascan` 项目，后台批量任务支持暂停、取消和重试。
- 保留裁剪配套的可选白边除尘、污点保护画笔与调色；内部画面自动修复默认关闭。

本版本已移除 Live 图制作、动态插画、Codex AI 调用、Apple Live 配对以及视频封装。无需 FFmpeg、ExifTool 或 Live Photo Box。旧版含 Live 成果的项目请先导出静态照片再导入。

## 使用

Windows 10/11 x64 需要 Microsoft Edge WebView2 Runtime。发行包解压后运行 `InstaCrop.exe`，保留 `_internal` 目录。

源码运行需要 Python 3.10+：

```powershell
./start.ps1
```

命令行批量裁剪：

```powershell
python scanner.py "扫描图文件夹" --output outputs --trim 2
python scanner.py "扫描图.png" --review "outputs/扫描图_page1/manifest.json" --output outputs
```

识别结果需检查，尤其是低对比度边缘、相纸重叠、缺角与不明确朝向。原素材不改写，重复导出自动使用新目录。工作区默认位于 `%LOCALAPPDATA%/PolaScanCrop/workspace`，可用 `POLASCAN_DATA_DIR` 指定目录。

## 开发与验证

```powershell
python -m pip install -r requirements-desktop.txt
python -m unittest discover -s tests -v
python desktop.py --self-test outputs/self-test.json
./build.ps1
```

`build.ps1` 生成 Windows 便携包与 SHA256。GitHub Actions 自动验证裁剪测试，并在推送 `v*` 标签时生成发行包。
