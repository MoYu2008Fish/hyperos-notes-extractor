# HyperOS / MIUI 便签提取器

把小米 HyperOS / MIUI 便签（`com.miui.notes`）导出为 Markdown，包含正文、图片、录音和创建/修改时间。

无需 root、无需登录小米账号、不经过云端，全程只读手机上的本地备份。

> Export Xiaomi HyperOS / MIUI Notes to Markdown with images and voice recordings — no root, no Mi Cloud, no account.

## 特性

- 解析小米「本地备份」生成的笔记备份包（zip / `.bak`）
- 输出 Markdown：标题、正文、时间、文件夹信息
- 图片、录音自动落地为本地文件，并重写 Markdown 引用
- 保留小米便签的粗体、下划线、列表等基础格式
- 额外输出 `notes.json`，方便二次处理
- 单文件、纯 Python 标准库，无第三方依赖
- 支持通过 adb 自动拉取手机上最新的备份

## 为什么不用 `adb backup`？

Android 13 起，系统对 `adb backup` 增加了限制（`RESTRICT_ADB_BACKUP`）：非 debuggable 应用执行 `adb backup` 只会得到一个空备份，日志里会显示：

```
Package com.miui.notes is not eligible for backup, removing.
```

因此本项目改用小米系统自带的「本地备份」通道。它调用便签自身的 `NoteBackupAgent`，可以拿到完整数据：

```
MIUI BACKUP
2
com.miui.notes 笔记
...
ANDROID BACKUP
5
0
none
└── tar
    ├── apps/com.miui.notes/miui_bak/_tmp_bak    # protobuf：笔记正文、标题、时间
    └── apps/com.miui.notes/miui_att/<sha1>      # 图片 / 录音附件
```

脚本会解开这三层容器，解析便签的 protobuf，并按 SHA1 把附件和正文里的图片/录音引用对应起来。

## 环境要求

- Python 3.8+
- 已安装 `adb`（Android platform-tools），并能在终端中直接执行
- 手机开启「USB 调试」，并在弹窗中允许当前电脑

## 手机上创建备份

> 这一步需要手动操作。小米备份默认会勾选多个分类，**务必先取消所有分类，只保留「笔记」**，否则会把其他 App 的数据一起备份。

1. 打开：`设置 → 更多设置 → 备份与恢复 → 手机备份恢复 → 备份`
2. 把已勾选的分类逐个取消（点击整行或右侧圆圈），直到每个分类都不再显示已选数量（例如显示 `0B 21392项`，而不是 `2.67MB 2668/2668项`）
3. 展开「其他系统应用数据」，只勾选「笔记」，确认显示类似 `500MB 1/25项`
4. 点「立即备份」→「继续备份」→「我知道了」
5. 在进度页确认队列里**只有「笔记」**。如果出现其他 App，立即点「取消」

备份会保存在：

```
/sdcard/MIUI/backup/AllBackup/<时间>/
/sdcard/MIUI/backup/AllBackup/<时间>.zip
```

> 小米备份不支持私密笔记和双开应用数据，这部分不会出现在备份中。

## 安装与使用

### 直接运行

```bash
git clone https://github.com/MoYu2008Fish/hyperos-notes-extractor.git
cd hyperos-notes-extractor

# 自动拉取手机上最新的备份并导出到 ./notes_export
python3 hyperos_notes_extract.py
```

### 用 pipx 安装为命令

```bash
pipx install git+https://github.com/MoYu2008Fish/hyperos-notes-extractor.git
hyperos-notes
```

### 解析已有备份

```bash
# zip 或 .bak 都可以
python3 hyperos_notes_extract.py --from-backup ~/Downloads/20261022_145603.zip

# 指定输出目录
python3 hyperos_notes_extract.py -o ~/Documents/Notes

# 保留拉取到本地的备份文件，并打印内部结构
python3 hyperos_notes_extract.py --keep-backup --debug

# 导出后删除手机上的备份（不可恢复）
python3 hyperos_notes_extract.py --delete-device-backup
```

### 参数

| 参数 | 说明 |
| --- | --- |
| `-o, --output` | 输出目录，默认 `notes_export` |
| `--from-backup` | 解析已有的 `.zip` 或 `.bak`，不连接手机 |
| `--keep-backup` | 保留拉取到系统临时目录的备份包 |
| `--delete-device-backup` | 导出成功后删除手机上的这份备份 |
| `--debug` | 打印备份内部结构，便于排查问题 |
| `-V, --version` | 显示版本 |

## 输出结构

```
notes_export/
├── notes/
│   ├── 标题.md
│   └── 文件夹/
│       └── 标题.md
├── assets/
│   ├── <sha1>.jpg
│   ├── <sha1>.png
│   └── <sha1>.mp3
└── notes.json
```

Markdown 示例：

```markdown
---
title: "示例笔记"
created: 2026-06-24 00:27:31
modified: 2026-06-24 21:00:41
---

# 示例笔记

正文段落。

![](../assets/<sha1>.jpg)

[🔊 录音](../assets/<sha1>.mp3)
```

`notes.json` 包含每条笔记的标题、时间、Markdown 正文、附件文件名等字段。

## 已知限制

- **私密笔记不支持**：小米备份本身不会包含「设为私密」的数据
- **双开应用数据不支持**：小米备份的固有限制
- **待办清单暂未导出**：当前版本只处理普通笔记
- 思维导图类笔记只能导出其中的纯文本
- 附件使用 SHA1 命名，与原文件名无关
- 完全空白的笔记会被跳过
- 需要手机端能完成一次「本地备份」；不同 HyperOS 版本的菜单文案可能略有差异

已在 HyperOS 3.0.305 / 便签 2.4.0.9 上验证。其他 HyperOS / MIUI 版本欢迎反馈。

## 隐私与安全

- 小米本地备份是**未加密**的，备份期间和备份完成后请注意设备安全
- `notes_export/` 内含你的全部笔记和图片，`notes.json` 也包含正文
- 仓库的 `.gitignore` 已排除 `notes_export/`、`*.zip`、`*.bak`，避免误提交
- 脚本不联网、不上传任何数据
- 导出完成后建议执行 `--delete-device-backup`，或手动删除手机上的备份

## 开发

项目只有一个文件，无第三方依赖：

```bash
python3 -m py_compile hyperos_notes_extract.py
python3 hyperos_notes_extract.py --help
```

欢迎提交 Issue / PR，尤其是不同机型、不同便签版本的备份样本结构（请先脱敏）。

## 相关项目

- [mi-note-export](https://github.com/ceynri/mi-note-export)：走小米云 `i.mi.com` 导出（已停止维护）
- [mi-note-cli](https://github.com/ceynri/mi-note-cli)：走小米云，支持双向同步

本项目走本地备份，不依赖小米账号和云服务，适合只想把手机里的笔记搬到本地的场景。

## License

[MIT](LICENSE)
