#!/usr/bin/env python3
"""
HyperOS / MIUI 便签提取器

读取小米「本地备份」生成的笔记备份包，解析其中的 protobuf 数据，
导出为 Markdown + 图片/录音附件 + JSON。

不依赖 root，不需要小米账号。

准备备份（手机上操作一次）：
    设置 → 更多设置 → 备份与恢复 → 手机备份恢复 → 备份
    只勾选「其他系统应用数据 → 笔记」，取消其他所有分类，点「立即备份」
    完成后备份位于 MIUI/backup/AllBackup/<时间>/ 与同名 .zip

用法:
    python3 hyperos_notes_extract.py                    # 自动拉取手机上最新的备份并导出
    python3 hyperos_notes_extract.py -o ./my_notes      # 指定输出目录
    python3 hyperos_notes_extract.py --from-backup x.zip  # 解析已有备份（zip 或 .bak）
    python3 hyperos_notes_extract.py --keep-backup      # 保留拉取到本地的备份包
    python3 hyperos_notes_extract.py --delete-device-backup  # 导出后删掉手机上的备份
    python3 hyperos_notes_extract.py --debug            # 打印备份内部结构
"""

import argparse
import datetime
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
import zlib
from html.parser import HTMLParser

__version__ = "0.1.0"

# ---------------------------------------------------------------- protobuf

def _read_varint(buf, pos):
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def pb_parse(buf):
    """通用 protobuf 解析，返回 {field_number: [value, ...]}。"""
    fields = {}
    pos = 0
    n = len(buf)
    while pos < n:
        key, pos = _read_varint(buf, pos)
        field_no = key >> 3
        wire_type = key & 0x7
        if wire_type == 0:
            value, pos = _read_varint(buf, pos)
        elif wire_type == 1:
            value = buf[pos:pos + 8]
            pos += 8
        elif wire_type == 2:
            length, pos = _read_varint(buf, pos)
            value = buf[pos:pos + length]
            pos += length
        elif wire_type == 5:
            value = buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError("unsupported wire type %d" % wire_type)
        fields.setdefault(field_no, []).append(value)
    return fields


def pb_str(fields, no, default=""):
    if no not in fields:
        return default
    return fields[no][0].decode("utf-8", "replace")


def pb_int(fields, no, default=0):
    if no not in fields:
        return default
    return int(fields[no][0])


def pb_msg(fields, no):
    return [pb_parse(v) for v in fields.get(no, [])]


# ---------------------------------------------------------------- schema

SYNC_NOTE = 6
SYNC_TODO = 8

NOTES_FOLDER = 1
NOTES_NOTE = 2

NE_GUID = 1
NE_ALERT_DATE = 3
NE_BG_COLOR = 4
NE_CREATED = 5
NE_MODIFIED = 6
NE_HAS_ATTACHMENT = 7
NE_SNIPPET = 8
NE_DATA = 9
NE_FOLDER_TITLE = 10
NE_STICK_DATE = 12
NE_TITLE = 14
NE_CONTENT_TYPE = 15
NE_MIND_TEXT = 17
NE_IMAGES = 19
NE_IS_EMPTY = 22

ND_MIME = 1
ND_CREATED = 2
ND_MODIFIED = 3
ND_CONTENT = 4

NF_GUID = 1
NF_SUBJECT = 9
NF_CREATED = 5
NF_MODIFIED = 6

TEXT_NOTE_MIME = "vnd.android.cursor.item/text_note"
CALL_NOTE_MIME = "vnd.android.cursor.item/call_note"

MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "audio/mp3": ".mp3",
    "audio/mpeg": ".mp3",
    "audio/amr": ".amr",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
}

# "☺ <sha1>" 是便签里的图片占位符
IMAGE_MARK = re.compile(r"☺\s*([0-9a-f]{40})")
SOUND_TAG = re.compile(r'<sound\b[^>]*fileid="([^"]+)"[^>]*/?>', re.IGNORECASE)
IMG_TAG = re.compile(r'<img\b[^>]*src="([^"]+)"[^>]*/?>', re.IGNORECASE)


# ---------------------------------------------------------------- backup container

def sniff_ext(data):
    if not data:
        return ""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"#!AMR"):
        return ".amr"
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return ".mp3"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"mif1"):
            return ".heic"
        if brand in (b"M4A ", b"M4B "):
            return ".m4a"
        return ".mp4"
    if data.startswith(b"PK\x03\x04"):
        return ".zip"
    return ""


def find_backup_file(path):
    if os.path.isdir(path):
        candidates = []
        candidates += glob.glob(os.path.join(path, "*.zip"))
        candidates += glob.glob(os.path.join(path, "*.bak"))
        if not candidates:
            raise ValueError("目录里没有找到 .zip / .bak 备份文件: %s" % path)
        candidates.sort(key=os.path.getmtime, reverse=True)
        return candidates[0]
    return path


def read_backup_bytes(path):
    path = find_backup_file(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            baks = [n for n in z.namelist() if n.lower().endswith(".bak")]
            if not baks:
                raise ValueError("备份 zip 里没有 .bak 文件")
            # 优先选便签的备份
            baks.sort(key=lambda n: ("notes" not in n, n))
            return z.read(baks[0])
    with open(path, "rb") as f:
        return f.read()


def open_backup_tar(blob):
    """剥掉 MIUI 头，再按 Android backup 格式解出 tar。"""
    idx = blob.find(b"ANDROID BACKUP\n")
    if idx < 0:
        raise ValueError("不是有效的 MIUI / Android 备份文件")
    pos = idx + len(b"ANDROID BACKUP\n")

    def read_line():
        nonlocal pos
        end = blob.find(b"\n", pos)
        if end < 0:
            raise ValueError("备份文件头损坏")
        line = blob[pos:end].decode("utf-8", "replace").strip()
        pos = end + 1
        return line

    read_line()  # version
    compressed = read_line() == "1"
    encryption = read_line()
    if encryption not in ("none", ""):
        raise ValueError("备份已加密（%s），无法解析" % encryption)

    payload = blob[pos:]
    if compressed:
        payload = zlib.decompress(payload)
    return tarfile.open(fileobj=io.BytesIO(payload), mode="r:")


# ---------------------------------------------------------------- html -> markdown

class NoteHtmlToMarkdown(HTMLParser):
    BLOCK_TAGS = {
        "p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "pre", "li",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []
        self.list_depth = 0
        self.list_type = []
        self.in_pre = False
        self.link_href = None

    def _newline(self, count=1):
        text = "".join(self.out)
        if not text.endswith("\n" * count):
            self.out.append("\n" * count)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.stack.append(tag)
        if tag == "br":
            self.out.append("  \n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._newline(2)
            self.out.append("#" * int(tag[1]) + " ")
        elif tag in ("p", "div"):
            self._newline(2)
        elif tag == "text":
            self._newline(1)
        elif tag == "size":
            self._newline(2)
        elif tag == "blockquote":
            self._newline(1)
            self.out.append("> ")
        elif tag in ("ul", "ol"):
            self._newline(1)
            self.list_depth += 1
            self.list_type.append(tag)
        elif tag == "li":
            self._newline(1)
            indent = "  " * max(0, self.list_depth - 1)
            marker = "- " if not self.list_type or self.list_type[-1] == "ul" else "1. "
            self.out.append(indent + marker)
        elif tag in ("b", "strong"):
            self.out.append("**")
        elif tag in ("i", "em"):
            self.out.append("*")
        elif tag == "u":
            self.out.append("<u>")
        elif tag in ("s", "strike", "del"):
            self.out.append("~~")
        elif tag == "code":
            self.out.append("`")
        elif tag == "pre":
            self._newline(2)
            self.out.append("```\n")
            self.in_pre = True
        elif tag == "a":
            self.link_href = attrs.get("href")
            self.out.append("[")
        elif tag == "img":
            src = attrs.get("src") or ""
            self.out.append("![](%s)" % src)
        elif tag == "hr":
            self._newline(2)
            self.out.append("---\n")
        elif tag == "input" and (attrs.get("type") or "").lower() == "checkbox":
            checked = "checked" in attrs or attrs.get("checked") == "true"
            self.out.append("[x] " if checked else "[ ] ")

    def handle_endtag(self, tag):
        while self.stack:
            current = self.stack.pop()
            if current == tag:
                break
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._newline(2)
        elif tag in ("p", "div"):
            self._newline(2)
        elif tag == "text":
            self._newline(1)
        elif tag == "size":
            self._newline(2)
        elif tag == "blockquote":
            self._newline(2)
        elif tag in ("ul", "ol"):
            self.list_depth = max(0, self.list_depth - 1)
            if self.list_type:
                self.list_type.pop()
            self._newline(1)
        elif tag == "li":
            self._newline(1)
        elif tag in ("b", "strong"):
            self.out.append("**")
        elif tag in ("i", "em"):
            self.out.append("*")
        elif tag == "u":
            self.out.append("</u>")
        elif tag in ("s", "strike", "del"):
            self.out.append("~~")
        elif tag == "code":
            self.out.append("`")
        elif tag == "pre":
            self.out.append("\n```\n\n")
            self.in_pre = False
        elif tag == "a":
            self.out.append("](%s)" % (self.link_href or ""))
            self.link_href = None

    def handle_data(self, data):
        if self.in_pre:
            self.out.append(data)
        else:
            self.out.append(re.sub(r"[ \t]+", " ", data))


def html_to_markdown(content):
    content = (content or "").strip()
    if not content:
        return ""
    parser = NoteHtmlToMarkdown()
    try:
        parser.feed(content)
        parser.close()
        text = "".join(parser.out)
    except Exception:
        text = re.sub(r"<[^>]+>", "", content)
    text = re.sub(r"<0\s*/>", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_html(content):
    text = SOUND_TAG.sub("", content or "")
    text = IMG_TAG.sub("", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = IMAGE_MARK.sub("", text)
    return re.sub(r"[ \t\r\n]+", " ", text).strip()


def first_line(content):
    text = SOUND_TAG.sub("", content or "")
    text = IMG_TAG.sub("", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = IMAGE_MARK.sub("", text)
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:50]
    return ""


# ---------------------------------------------------------------- export

INVALID_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]')


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


def collect_attachment_blobs(tar, debug=False):
    blobs = {}
    for member in tar.getmembers():
        if not member.isfile():
            continue
        name = os.path.basename(member.name)
        if "/miui_att/" not in member.name and not member.name.startswith("miui_att/"):
            if debug:
                print("  entry: %-72s %8d bytes" % (member.name, member.size))
            continue
        if member.size == 0:
            continue
        f = tar.extractfile(member)
        if f is None:
            continue
        blobs[name] = f.read()
    return blobs


def find_root_payload(tar, debug=False):
    best = None
    for member in tar.getmembers():
        if not member.isfile() or member.size == 0:
            continue
        if not (member.name.endswith("_tmp_bak") or member.name.endswith(".bak")):
            continue
        f = tar.extractfile(member)
        if f is None:
            continue
        data = f.read()
        try:
            fields = pb_parse(data)
        except Exception:
            continue
        if SYNC_NOTE in fields and (best is None or len(data) > len(best[1])):
            best = (member.name, data)
    if best is None:
        raise ValueError("备份里没有找到便签数据")
    if debug:
        print("  数据: %s (%d bytes)" % (best[0], len(best[1])))
    return best[1]


def export(root_payload, workdir, blobs, debug=False):
    root = pb_parse(root_payload)
    notes_msg_list = pb_msg(root, SYNC_NOTE)
    if not notes_msg_list:
        raise ValueError("便签数据为空")
    notes_msg = notes_msg_list[0]

    folders = {}
    for folder in pb_msg(notes_msg, NOTES_FOLDER):
        guid = pb_str(folder, NF_GUID)
        folders[guid] = {
            "guid": guid,
            "title": pb_str(folder, NF_SUBJECT),
            "created": pb_int(folder, NF_CREATED),
            "modified": pb_int(folder, NF_MODIFIED),
        }

    # 先解析所有笔记，收集 hash -> mime
    parsed = []
    hash_mime = {}
    for entity in pb_msg(notes_msg, NOTES_NOTE):
        title = pb_str(entity, NE_TITLE)
        folder_title = pb_str(entity, NE_FOLDER_TITLE)
        created = pb_int(entity, NE_CREATED)
        modified = pb_int(entity, NE_MODIFIED)

        texts = []
        attachments = []
        for data in pb_msg(entity, NE_DATA):
            mime = pb_str(data, ND_MIME)
            content = pb_str(data, ND_CONTENT)
            if mime == TEXT_NOTE_MIME:
                texts.append(content)
            elif mime == CALL_NOTE_MIME:
                continue
            elif content:
                name = os.path.basename(content)
                base = re.sub(r"\.[A-Za-z0-9]+$", "", name)
                hash_mime[base] = mime
                attachments.append({"mime": mime, "name": name, "base": base})

        body = "\n".join(texts)
        mind_text = pb_str(entity, NE_MIND_TEXT)
        if not strip_html(body) and mind_text:
            body = mind_text

        if not title:
            title = first_line(body)

        parsed.append({
            "guid": pb_str(entity, NE_GUID),
            "title": title,
            "title_explicit": bool(pb_str(entity, NE_TITLE)),
            "folder": folder_title,
            "created": created,
            "modified": modified,
            "body": body,
            "attachments": attachments,
            "content_type": pb_str(entity, NE_CONTENT_TYPE),
            "snippet": strip_html(pb_str(entity, NE_SNIPPET)),
        })

    # 落附件
    assets_dir = os.path.join(workdir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    used_asset_names = {}
    for name, data in blobs.items():
        stem = re.sub(r"\.[A-Za-z0-9]+$", "", name)
        mime = hash_mime.get(stem) or hash_mime.get(name, "")
        ext = MIME_EXT.get(mime) or sniff_ext(data) or ".bin"
        filename = name if name.lower().endswith(ext) else name + ext
        with open(os.path.join(assets_dir, filename), "wb") as f:
            f.write(data)
        used_asset_names[name] = filename
        used_asset_names[stem] = filename
    if debug:
        print("  附件: %d 个" % len(set(used_asset_names.values())))

    # 写 Markdown
    notes_dir = os.path.join(workdir, "notes")
    os.makedirs(notes_dir, exist_ok=True)
    used_paths = set()
    written = 0
    exported_notes = []

    for note in parsed:
        body = note["body"]
        has_content = bool(strip_html(body)) or note["attachments"]

        def image_repl(m):
            base = m.group(1)
            filename = used_asset_names.get(base)
            if not filename:
                return m.group(0)
            return "![](%s)" % os.path.join("..", "assets", filename)

        def sound_repl(m):
            raw = m.group(1)
            base = re.sub(r"\.[A-Za-z0-9]+$", "", os.path.basename(raw))
            filename = used_asset_names.get(base)
            if not filename:
                return ""
            return "[🔊 录音](%s)" % os.path.join("..", "assets", filename)

        body = IMAGE_MARK.sub(image_repl, body)
        body = SOUND_TAG.sub(sound_repl, body)
        body = IMG_TAG.sub(lambda m: "![](%s)" % m.group(1), body)
        markdown = html_to_markdown(body)

        if not has_content and not note["title"]:
            continue

        folder = safe_name(note["folder"], "") if note["folder"] else ""
        target_dir = os.path.join(notes_dir, folder) if folder else notes_dir
        os.makedirs(target_dir, exist_ok=True)

        base_name = safe_name(note["title"], date_prefix(note["created"]))
        filename = base_name + ".md"
        i = 2
        while os.path.join(target_dir, filename) in used_paths:
            filename = "%s (%d).md" % (base_name, i)
            i += 1
        used_paths.add(os.path.join(target_dir, filename))
        path = os.path.join(target_dir, filename)

        meta = [
            "---",
            "title: %s" % json.dumps(note["title"], ensure_ascii=False),
            "created: %s" % format_date(note["created"]),
            "modified: %s" % format_date(note["modified"]),
        ]
        if note["folder"]:
            meta.append("folder: %s" % json.dumps(note["folder"], ensure_ascii=False))
        if note["guid"]:
            meta.append("guid: %s" % note["guid"])
        meta.append("---")
        header = "\n".join(meta) + "\n\n"
        if note["title"] and note["title_explicit"]:
            header += "# " + note["title"] + "\n\n"

        with open(path, "w", encoding="utf-8") as f:
            f.write(header + markdown + "\n")
        written += 1

        exported_notes.append({
            "guid": note["guid"],
            "title": note["title"],
            "folder": note["folder"],
            "created": note["created"],
            "modified": note["modified"],
            "file": os.path.relpath(path, workdir),
            "attachments": [
                used_asset_names.get(a["base"], a["name"]) for a in note["attachments"]
            ],
            "markdown": markdown,
        })

    asset_count = len(set(used_asset_names.values()))
    with open(os.path.join(workdir, "notes.json"), "w", encoding="utf-8") as f:
        json.dump({
            "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "note_count": len(exported_notes),
            "attachment_count": asset_count,
            "folders": list(folders.values()),
            "notes": exported_notes,
        }, f, ensure_ascii=False, indent=2)

    return written, len(parsed), asset_count


# ---------------------------------------------------------------- adb

def adb(args, check=True):
    proc = subprocess.run(["adb"] + args, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "adb 执行失败")
    return proc


def latest_device_backup():
    proc = adb(["shell", "ls", "-t", "/sdcard/MIUI/backup/AllBackup"], check=False)
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    zips = [line for line in lines if line.endswith(".zip")]
    if not zips:
        return None
    return "/sdcard/MIUI/backup/AllBackup/" + zips[0]


def pull_latest_backup(dest_dir):
    path = latest_device_backup()
    if not path:
        raise RuntimeError(
            "手机上没找到本地备份。请先按说明在手机上备份一次「笔记」。"
        )
    local = os.path.join(dest_dir, os.path.basename(path))
    print("正在拉取: %s" % path)
    adb(["pull", path, local])
    return local, path


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        prog="hyperos-notes",
        description="HyperOS / MIUI 便签提取器（解析小米本地备份，无需 root）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "手机端准备（只需做一次）:\n"
            "  设置 → 更多设置 → 备份与恢复 → 手机备份恢复 → 备份\n"
            "  取消所有已勾选分类，只勾选「其他系统应用数据 → 笔记」\n"
            "  点「立即备份」，并在进度页确认队列里只有「笔记」\n"
            "\n"
            "更多说明: https://github.com/MoYu2008Fish/hyperos-notes-extractor\n"
        ),
    )
    ap.add_argument("-V", "--version", action="version", version="%(prog)s " + __version__)
    ap.add_argument("-o", "--output", default="notes_export", help="输出目录")
    ap.add_argument("--from-backup", help="解析已有的备份 zip 或 .bak")
    ap.add_argument("--keep-backup", action="store_true", help="保留拉取到本地的备份包")
    ap.add_argument("--delete-device-backup", action="store_true",
                    help="导出后删除手机上的这份备份（含附件，不可恢复）")
    ap.add_argument("--debug", action="store_true", help="打印备份内部结构")
    args = ap.parse_args()

    workdir = os.path.abspath(args.output)
    os.makedirs(workdir, exist_ok=True)

    backup_path = args.from_backup
    tmp_dir = None
    device_backup = None
    if not backup_path:
        if not shutil.which("adb"):
            raise RuntimeError("找不到 adb；也可以手动指定 --from-backup")
        devices = adb(["devices"]).stdout
        if "\tdevice" not in devices:
            raise RuntimeError("没有已授权的 USB 设备，请先连接手机并允许 USB 调试")
        tmp_dir = tempfile.mkdtemp(prefix="miui_notes_")
        backup_path, device_backup = pull_latest_backup(tmp_dir)

    print("正在解析: %s" % backup_path)
    blob = read_backup_bytes(backup_path)
    tar = open_backup_tar(blob)

    blobs = collect_attachment_blobs(tar, debug=args.debug)
    payload = find_root_payload(tar, debug=args.debug)
    written, total, attachments = export(payload, workdir, blobs, debug=args.debug)

    if args.delete_device_backup and device_backup:
        adb(["shell", "rm", "-f", device_backup], check=False)
        adb(["shell", "rm", "-rf", device_backup[:-4]], check=False)
        print("已删除手机上的备份: %s" % device_backup)

    if tmp_dir and not args.keep_backup:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print("")
    print("完成: 导出 %d/%d 条笔记，%d 个附件" % (written, total, attachments))
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
