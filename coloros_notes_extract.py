#!/usr/bin/env python3
"""
ColorOS / realme UI 便签提取器

读取 ColorOS「本地备份」里的便签数据，导出为 Markdown + 图片/录音附件 + JSON。

不依赖 root，不需要账号。

手机上准备备份：
    设置 → 账号与备份 → 备份与迁移 → 本地备份 → 新建备份
    只勾选「系统偏好设置 → 便签」，取消其他所有分类，开始备份

用法:
    python3 coloros_notes_extract.py                     # 自动拉取手机上最新的便签备份并导出
    python3 coloros_notes_extract.py -o ./my_notes       # 指定输出目录
    python3 coloros_notes_extract.py --from-backup DIR   # 解析已拉取的备份目录（含 Note/）
    python3 coloros_notes_extract.py --keep-backup       # 保留拉取到本地的备份
    python3 coloros_notes_extract.py --delete-device-backup  # 导出后删除手机上的备份
"""

import argparse
import datetime
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from html.parser import HTMLParser

__version__ = "0.2.0"

BACKUP_ROOT = "/sdcard/Android/data/com.coloros.backuprestore/Backup/Data"
DEFAULT_FOLDER_GUID = "00000000_0000_0000_0000_000000000000"

INVALID_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]')
IMG_TAG = re.compile(r'<img\b[^>]*src="([^"]+)"[^>]*/?>', re.IGNORECASE)
HEAD_BLOCK = re.compile(r"<head>.*?</head>", re.IGNORECASE | re.DOTALL)
H1_BLOCK = re.compile(r'<div[^>]*class="h1"[^>]*>(.*?)</div>', re.IGNORECASE | re.DOTALL)


# ---------------------------------------------------------------- helpers

def safe_name(name, fallback="untitled", max_len=80):
    name = INVALID_FILENAME.sub("_", (name or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = fallback
    return name[:max_len]


def format_date(ms):
    try:
        if ms and ms > 10 ** 12:
            ms = ms / 1000
        return datetime.datetime.fromtimestamp(ms).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def date_prefix(ms):
    try:
        return datetime.datetime.fromtimestamp(ms).strftime("%Y-%m-%d_%H-%M-%S")
    except Exception:
        return "note"


def strip_tags(text):
    text = HEAD_BLOCK.sub("", text or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).replace("\u200b", "").strip()


# ---------------------------------------------------------------- html -> markdown

class ColorOSHtmlToMarkdown(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.list_stack = []
        self.bold_depth = 0
        self.link_href = None
        self.skip = 0

    def _newline(self, count=1):
        text = "".join(self.out)
        if not text.endswith("\n" * count):
            self.out.append("\n" * count)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag in ("head", "meta"):
            self.skip += 1
            return
        if self.skip:
            return

        if tag == "br":
            self.out.append("\n")
        elif tag == "div":
            if "h1" in classes:
                self._newline(2)
                self.out.append("# ")
            elif "h2" in classes:
                self._newline(2)
                self.out.append("## ")
            elif "h3" in classes:
                self._newline(2)
                self.out.append("### ")
            elif "media-attachment" in classes:
                self._newline(1)
            else:
                self._newline(2)
        elif tag in ("ul", "ol"):
            self._newline(1)
            self.list_stack.append({"tag": tag, "indent": 0})
        elif tag == "li":
            self._newline(1)
            indent = 0
            if "ql-indent-1" in classes:
                indent = 1
            elif "ql-indent-2" in classes:
                indent = 2
            marker = "- "
            if "checked" in classes:
                marker = "- [x] "
            elif "unchecked" in classes:
                marker = "- [ ] "
            self.out.append("  " * indent + marker)
        elif tag == "span":
            if "text-weight-bold" in classes:
                self.out.append("**")
                self.bold_depth += 1
        elif tag == "b" or tag == "strong":
            self.out.append("**")
            self.bold_depth += 1
        elif tag == "i" or tag == "em":
            self.out.append("*")
        elif tag == "u":
            self.out.append("<u>")
        elif tag == "a":
            self.link_href = attrs.get("href")
            if self.link_href:
                self.out.append("[")
        elif tag == "hr":
            self._newline(2)
            self.out.append("---\n")

    def handle_endtag(self, tag):
        if tag in ("head", "meta"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "div":
            self._newline(2)
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
            self._newline(1)
        elif tag == "li":
            self._newline(1)
        elif tag == "span":
            if self.bold_depth:
                self.out.append("**")
                self.bold_depth -= 1
        elif tag in ("b", "strong"):
            self.out.append("**")
            if self.bold_depth:
                self.bold_depth -= 1
        elif tag in ("i", "em"):
            self.out.append("*")
        elif tag == "u":
            self.out.append("</u>")
        elif tag == "a":
            if self.link_href:
                self.out.append("](%s)" % self.link_href)
            self.link_href = None

    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(data.replace("\u200b", ""))


def html_to_markdown(content):
    parser = ColorOSHtmlToMarkdown()
    try:
        parser.feed(content or "")
        parser.close()
        text = "".join(parser.out)
    except Exception:
        text = strip_tags(content)
    text = text.replace("\xa0", " ")
    text = re.sub(r"(?m)^#{1,6}[ \t]*$", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"<a[^>]*>", "", text)
    text = re.sub(r"</a>", "", text)
    return text.strip()


# ---------------------------------------------------------------- backup files

def find_note_dir(path):
    path = os.path.abspath(path)
    for candidate in (path, os.path.join(path, "Note")):
        if os.path.isfile(os.path.join(candidate, "rich_note")):
            return candidate
    for root, dirs, files in os.walk(path):
        if "rich_note" in files:
            return root
    raise ValueError("找不到便签备份（缺少 rich_note）: %s" % path)


def adb(args, check=True):
    proc = subprocess.run(["adb"] + args, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "adb 执行失败")
    return proc


def latest_device_backup():
    proc = adb(["shell", "ls", "-t", BACKUP_ROOT], check=False)
    for line in proc.stdout.splitlines():
        name = line.strip()
        if name and not name.startswith("."):
            return BACKUP_ROOT + "/" + name
    return None


def pull_device_backup(dest_dir):
    remote = latest_device_backup()
    if not remote:
        raise RuntimeError("手机上没找到便签本地备份，请先按说明备份一次")
    print("正在拉取: %s" % remote)
    local = os.path.join(dest_dir, "backup")
    os.makedirs(local, exist_ok=True)
    adb(["pull", remote + "/Note", local])
    return os.path.join(local, "Note"), remote


# ---------------------------------------------------------------- export

def load_backup(note_dir):
    with open(os.path.join(note_dir, "rich_note"), encoding="utf-8") as f:
        notes = json.load(f)
    folders = {}
    folder_path = os.path.join(note_dir, "folder")
    if os.path.isfile(folder_path):
        with open(folder_path, encoding="utf-8") as f:
            for folder in json.load(f):
                folders[folder.get("guid")] = folder.get("name", "")
    zip_path = os.path.join(note_dir, "files", "file_backup.zip")
    zf = zipfile.ZipFile(zip_path) if os.path.isfile(zip_path) else None
    return notes, folders, zf


def build_zip_index(zf):
    index = {}
    if zf is None:
        return index
    for name in zf.namelist():
        parts = name.lstrip("/").split("/")
        if len(parts) >= 3 and parts[0] == "files":
            index.setdefault(parts[1], []).append(name)
    return index


def pick_image(entries, attachment_id):
    """优先原图，其次缩略图 / 占位图。"""
    if not entries:
        return None
    for suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        target = attachment_id + suffix
        for name in entries:
            if os.path.basename(name) == target:
                return name
    for tag in ("_thumb", "_placeholder"):
        target = attachment_id + tag
        for name in entries:
            if os.path.basename(name).startswith(target):
                return name
    return None


def export(note_dir, workdir, debug=False):
    notes, folders, zf = load_backup(note_dir)
    index = build_zip_index(zf)

    assets_dir = os.path.join(workdir, "assets")
    notes_dir = os.path.join(workdir, "notes")
    os.makedirs(assets_dir, exist_ok=True)
    os.makedirs(notes_dir, exist_ok=True)

    used_paths = set()
    written = 0
    asset_count = 0
    exported = []

    for item in notes:
        r = item.get("richNote", {})
        if r.get("deleted"):
            continue

        local_id = r.get("localId", "")
        raw_text = r.get("rawText", "") or ""
        raw_title = strip_tags(r.get("rawTitle", ""))
        title_explicit = bool(raw_title)
        if not raw_title:
            m = H1_BLOCK.search(raw_text)
            if m:
                raw_title = strip_tags(m.group(1))
        if not raw_title:
            plain = strip_tags(raw_text)
            for line in plain.splitlines():
                line = line.strip()
                if line:
                    raw_title = line[:50]
                    break

        title = safe_name(raw_title, "图片笔记" if not strip_tags(raw_text) else date_prefix(r.get("createTime")))
        folder_name = folders.get(r.get("folderGuid", ""), "")
        if r.get("folderGuid") in (None, "", DEFAULT_FOLDER_GUID):
            folder_name = ""
        target_dir = os.path.join(notes_dir, safe_name(folder_name, "")) if folder_name else notes_dir
        os.makedirs(target_dir, exist_ok=True)

        base = title
        filename = base + ".md"
        i = 2
        while os.path.join(target_dir, filename) in used_paths:
            filename = "%s (%d).md" % (base, i)
            i += 1
        used_paths.add(os.path.join(target_dir, filename))
        path = os.path.join(target_dir, filename)

        note_asset_dir = os.path.join(assets_dir, local_id or "unknown")
        os.makedirs(note_asset_dir, exist_ok=True)

        def rel_asset(asset_path):
            return os.path.relpath(asset_path, target_dir)

        # 图片：attachmentId -> 相对链接
        entries = index.get(local_id, [])
        image_map = {}
        for att in item.get("attachments", []):
            aid = att.get("attachmentId")
            if not aid or att.get("type") not in (0, None):
                continue
            src = pick_image(entries, aid)
            if not src:
                continue
            ext = os.path.splitext(src)[1].lower() or ".png"
            asset_path = os.path.join(note_asset_dir, aid + ext)
            with zf.open(src) as f, open(asset_path, "wb") as out:
                shutil.copyfileobj(f, out)
            image_map[aid] = rel_asset(asset_path)
            asset_count += 1

        # 其它附件：录音、手写
        extra_links = []
        for name in entries:
            base_name = os.path.basename(name)
            ext = os.path.splitext(base_name)[1].lower()
            if ext not in (".mp3", ".wav", ".m4a", ".amr", ".paint"):
                continue
            asset_path = os.path.join(note_asset_dir, base_name)
            with zf.open(name) as f, open(asset_path, "wb") as out:
                shutil.copyfileobj(f, out)
            asset_count += 1
            label = "🔊 录音" if ext in (".mp3", ".wav", ".m4a", ".amr") else "✍️ 手写"
            extra_links.append("[%s](%s)" % (label, rel_asset(asset_path)))

        def img_repl(m):
            aid = m.group(1)
            link = image_map.get(aid)
            if not link:
                return "\n[图片缺失 %s]\n" % aid[:8]
            return "\n![](%s)\n" % link

        body = HEAD_BLOCK.sub("", raw_text)
        body = IMG_TAG.sub(img_repl, body)
        body = body.replace("<a null>", "").replace("</a>", "")
        markdown = html_to_markdown(body)
        if extra_links:
            markdown += "\n\n## 附件\n\n" + "  \n".join(extra_links)

        meta = [
            "---",
            "title: %s" % json.dumps(raw_title, ensure_ascii=False),
            "created: %s" % format_date(r.get("createTime")),
            "modified: %s" % format_date(r.get("updateTime")),
        ]
        if folder_name:
            meta.append("folder: %s" % json.dumps(folder_name, ensure_ascii=False))
        if local_id:
            meta.append("id: %s" % local_id)
        meta.append("---")
        header = "\n".join(meta) + "\n\n"
        if title_explicit:
            header += "# " + raw_title + "\n\n"

        with open(path, "w", encoding="utf-8") as f:
            f.write(header + markdown + "\n")
        written += 1
        exported.append({
            "id": local_id,
            "title": raw_title,
            "folder": folder_name,
            "created": r.get("createTime"),
            "modified": r.get("updateTime"),
            "file": os.path.relpath(path, workdir),
            "markdown": markdown,
        })

        if debug:
            print("  %-40s %5d 字" % (raw_title or "(无标题)", len(markdown)))

    with open(os.path.join(workdir, "notes.json"), "w", encoding="utf-8") as f:
        json.dump({
            "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "note_count": len(exported),
            "attachment_count": asset_count,
            "folders": [{"guid": k, "name": v} for k, v in folders.items()],
            "notes": exported,
        }, f, ensure_ascii=False, indent=2)

    if zf:
        zf.close()
    return written, len(notes), asset_count


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        prog="coloros-notes",
        description="ColorOS / realme UI 便签提取器（解析本地备份，无需 root）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "手机端准备:\n"
            "  设置 → 账号与备份 → 备份与迁移 → 本地备份 → 新建备份\n"
            "  取消所有分类，只勾选「系统偏好设置 → 便签」\n"
            "  开始备份，并确认队列里只有「便签」\n"
        ),
    )
    ap.add_argument("-V", "--version", action="version", version="%(prog)s " + __version__)
    ap.add_argument("-o", "--output", default="coloros_notes_export", help="输出目录")
    ap.add_argument("--from-backup", help="已拉取的备份目录（包含 Note/）")
    ap.add_argument("--keep-backup", action="store_true", help="保留拉取到本地的备份")
    ap.add_argument("--delete-device-backup", action="store_true",
                    help="导出后删除手机上的这份备份（不可恢复）")
    ap.add_argument("--debug", action="store_true", help="打印每条笔记的导出信息")
    args = ap.parse_args()

    workdir = os.path.abspath(args.output)
    os.makedirs(workdir, exist_ok=True)

    note_dir = args.from_backup
    tmp_dir = None
    device_backup = None
    if note_dir:
        note_dir = find_note_dir(note_dir)
    else:
        if not shutil.which("adb"):
            raise RuntimeError("找不到 adb；也可以手动指定 --from-backup")
        if "\tdevice" not in adb(["devices"]).stdout:
            raise RuntimeError("没有已授权的 USB 设备")
        tmp_dir = tempfile.mkdtemp(prefix="coloros_notes_")
        note_dir, device_backup = pull_device_backup(tmp_dir)

    print("正在解析: %s" % note_dir)
    written, total, assets = export(note_dir, workdir, debug=args.debug)

    if args.delete_device_backup and device_backup:
        adb(["shell", "rm", "-rf", device_backup], check=False)
        adb(["shell", "rm", "-f", device_backup.replace("/Data/", "/.Preview/") + ".preview"], check=False)
        print("已删除手机上的备份: %s" % device_backup)

    if tmp_dir and not args.keep_backup:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print("")
    print("完成: 导出 %d/%d 条笔记，%d 个附件" % (written, total, assets))
    print("输出目录: %s" % workdir)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)
    except Exception as e:
        print("错误: %s" % e, file=sys.stderr)
        sys.exit(1)
