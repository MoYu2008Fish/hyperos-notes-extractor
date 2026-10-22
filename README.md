# HyperOS / ColorOS 便签工具箱

在本地提取小米、OPPO 系手机的便签，或在两个系统之间迁移。无需 root、无需登录账号、不经过云端。

| 脚本 | 用途 |
| --- | --- |
| `hyperos_notes_extract.py` | HyperOS / MIUI 便签 → Markdown + 图片 / 录音 |
| `coloros_notes_extract.py` | ColorOS / realme UI 便签 → Markdown + 图片 / 录音 |
| `hyperos_to_coloros_import.py` | 把 HyperOS 导出结果导入 ColorOS 便签 |

> 所有工具只读写本机文件与 adb，不联网、不上传任何数据。

## 环境要求

- Python 3.8+
- `adb`（Android platform-tools）
- 手机开启「USB 调试」，并允许当前电脑

## 一、HyperOS / MIUI 便签 → Markdown

### 为什么不用 `adb backup`

Android 13 起，系统限制非 debuggable 应用使用 `adb backup`，小米便签会直接得到空备份。因此改用小米自带的「本地备份」通道，它调用便签自身的 `NoteBackupAgent`，可以得到完整数据。

### 手机端准备

> 小米备份默认会勾选多个分类，**务必先取消所有分类，只保留「笔记」**。

1. 打开 `设置 → 更多设置 → 备份与恢复 → 手机备份恢复 → 备份`
2. 取消所有已勾选分类，直到每个分类都不再显示已选数量
3. 展开「其他系统应用数据」，只勾选「笔记」，确认显示类似 `500MB 1/25项`
4. 点「立即备份」→「继续备份」→「我知道了」
5. 在进度页确认队列里**只有「笔记」**；如果出现其他 App，立即取消

备份位于：

```
/sdcard/MIUI/backup/AllBackup/<时间>/
/sdcard/MIUI/backup/AllBackup/<时间>.zip
```

### 用法

```bash
# 自动拉取手机上最新的备份并导出
python3 hyperos_notes_extract.py

# 指定输出目录
python3 hyperos_notes_extract.py -o ~/Documents/Notes

# 解析已有备份（zip 或 .bak）
python3 hyperos_notes_extract.py --from-backup ~/Downloads/20261022_145603.zip
```

### 输出结构

```
notes_export/
├── notes/
│   ├── 标题.md
│   └── 文件夹/
│       └── 标题.md
├── assets/
│   ├── <sha1>.jpg
│   └── <sha1>.mp3
└── notes.json
```

Markdown 带 YAML 头（标题、创建时间、修改时间、文件夹），图片和录音已改为本地相对路径。

### 限制

- 私密笔记、双开应用数据不在本地备份范围内
- 待办清单暂未导出
- 附件使用 SHA1 命名
- 完全空白的笔记会被跳过

## 二、ColorOS / realme UI 便签 → Markdown

ColorOS 的本地备份同样是明文数据：`rich_note` 是 JSON，附件在 `files/file_backup.zip`。

### 手机端准备

1. 打开 `设置 → 账号与备份 → 备份与迁移 → 本地备份 → 新建备份`
2. 取消所有分类，只勾选「系统偏好设置 → 便签」
3. 开始备份，确认队列里只有「便签」

备份位于：

```
/sdcard/Android/data/com.coloros.backuprestore/Backup/Data/<时间>/
```

### 用法

```bash
# 自动拉取手机上最新的便签备份并导出
python3 coloros_notes_extract.py

# 解析已拉取的备份目录（包含 Note/）
python3 coloros_notes_extract.py --from-backup <备份目录>

# 指定输出目录
python3 coloros_notes_extract.py -o ~/Documents/ColorOSNotes
```

### 输出结构

```
coloros_notes_export/
├── notes/
│   ├── 标题.md
│   └── 文件夹/
│       └── 标题.md
├── assets/
│   └── <note-id>/
│       ├── <attachment-id>.png
│       └── <attachment-id>.mp3
└── notes.json
```

### 限制

- ColorOS 本地备份只保存图片的 `_thumb.png` 缩略图，不包含原图
- 私密笔记、双开应用数据不在备份范围内
- 录音、手写文件会一并导出

## 三、HyperOS → ColorOS 迁移

把「一」导出的 `notes_export` 转换成一份 ColorOS 本地备份，再用 ColorOS 自带的「本地备份 → 恢复」导入。

### 前置条件

- 已完成 HyperOS 导出（`notes_export/notes.json` + `assets/`）
- 手机上存在一份 ColorOS 便签本地备份，用作模板（工具也会自动拉取最新备份的模板）

### 用法

```bash
python3 hyperos_to_coloros_import.py \
  --export ./notes_export \
  --out ./coloros_import_backup \
  --push --restore
```

参数：

| 参数 | 说明 |
| --- | --- |
| `--export` | HyperOS 导出目录 |
| `--template` | ColorOS 备份模板目录；省略时自动从手机拉取 |
| `--out` | 生成的 ColorOS 备份目录 |
| `--push` | 生成后推送到手机 |
| `--restore` | 推送后自动打开备份界面并执行恢复 |
| `--limit N` | 只导入前 N 条（测试用） |
| `--skip N` | 跳过前 N 条（断点续导） |
| `--debug` | 打印每条笔记的转换信息 |

### 工作方式

1. 读取 `notes_export/notes.json` 和 `assets/`
2. 把 Markdown 转成 ColorOS 的富文本 HTML（标题、粗体、列表、复选框、图片）
3. 按 `localId/attachmentId` 生成 `Note/rich_note` 和 `Note/files/file_backup.zip`
4. 复制 ColorOS 备份的配置文件和模板，生成完整备份目录
5. 通过 adb 推送，并用 ColorOS 的恢复功能写入便签数据库

### 限制

- **录音不导入**：ColorOS 录音附件的格式与小米不同，正文会保留转写文字，并标注「原 HyperOS 录音未导入」
- ColorOS 的修改时间会显示为导入时间，原始创建时间会保留
- 恢复是「追加」操作，重复执行会产生重复笔记；可用 `--skip` 分批，或先在手机上清理
- 大量笔记（数百条、数百张图）会生成数百 MB 的备份，恢复需要几分钟

## 安装为命令

```bash
# 将 <repo-url> 替换为本仓库地址
pipx install git+<repo-url>

hyperos-notes --help
coloros-notes --help
hyperos-to-coloros --help
```

也可以直接下载脚本运行，三个脚本只依赖 Python 标准库。

## 隐私与安全

- 三个脚本都不联网、不上传数据
- 导出的 `notes_export/`、`coloros_notes_export/`、`coloros_import_backup/` 已加入 `.gitignore`，不会被提交
- 小米 / ColorOS 的本地备份都是**未加密**的，备份和导出文件请妥善保管
- 导出完成后建议删除手机上的本地备份，或使用脚本的清理选项
- 仓库中不包含任何个人笔记、账号、手机号或设备标识

## 开发

```bash
python3 -m py_compile hyperos_notes_extract.py coloros_notes_extract.py hyperos_to_coloros_import.py
```

## 相关项目

- [mi-note-export](https://github.com/ceynri/mi-note-export)：走小米云导出（已停止维护）
- [mi-note-cli](https://github.com/ceynri/mi-note-cli)：走小米云，支持双向同步

本项目走本地备份，不依赖账号和云服务，适合把手机里的便签完整搬到本地的场景。

## License

[MIT](LICENSE)
