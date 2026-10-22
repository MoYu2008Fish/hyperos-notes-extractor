#!/usr/bin/env python3
"""
把 HyperOS / MIUI 便签导出结果导入 ColorOS / realme UI 便签。

原理：把 notes_export（Markdown + assets + notes.json）转换成一份
ColorOS 本地备份，再用 ColorOS 自带的「本地备份 → 恢复」导入。

手机端要求：
    ColorOS / realme UI，已开启 USB 调试
    手机上至少存在一份「便签」本地备份（用作模板）

用法:
    python3 hyperos_to_coloros_import.py \
        --export ./notes_export \
        --template /path/to/coloros/backup/Data/<时间>/Note

    # 生成后直接推到手机并自动恢复
    python3 hyperos_to_coloros_import.py --export ./notes_export --push --restore

    # 只生成备份，不推送
    python3 hyperos_to_coloros_import.py --export ./notes_export --out ./coloros_import_backup
"""

import argparse
import datetime
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import uuid
import zipfile

__version__ = "0.2.0"

BACKUP_ROOT = "/sdcard/Android/data/com.coloros.backuprestore/Backup"


# ---------------------------------------------------------------- utils

def safe_name(name, fallback="untitled", max_len=120):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (name or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or fallback)[:max_len]


def image_size(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            i += 2
            if marker in (0xD8, 0xD9):
                continue
            if i + 2 > len(data):
                break
            length = struct.unpack(">H", data[i:i + 2])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 3:i + 7])
                return w, h
            i += length
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        try:
            return struct.unpack("<HH", data[26:30])[0:2]
        except Exception:
            pass
    return 1000, 1000


# ---------------------------------------------------------------- markdown -> coloros html

def inline_html(text):
    text = html_lib.escape(text, quote=False)
    text = text.replace("&lt;u&gt;", "<u>").replace("&lt;/u&gt;", "</u>")
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
                  r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*",
                  r'<span class="text-weight-bold">\1</span>', text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"`([^`]+)`", r"<span>\1</span>", text)
    return text


def markdown_to_coloros_html(markdown, image_links):
    """image_links: asset filename -> attachment UUID"""
    lines = markdown.splitlines()
    out = []
    list_open = None

    def close_list():
        nonlocal list_open
        if list_open:
            out.append("</%s>" % list_open)
            list_open = None

    def open_list(tag):
        nonlocal list_open
        if list_open != tag:
            close_list()
            out.append("<%s>" % tag)
            list_open = tag

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            continue

        # 图片：单独一行或行内
        def img_repl(m):
            path = m.group(1)
            name = os.path.basename(path)
            aid = image_links.get(name)
            if not aid:
                return ""
            token = "%%IMG%d%%" % len(img_tokens)
            img_tokens[token] = '<img src="%s">' % aid
            return token

        img_tokens = {}
        line = re.sub(r"!\[[^\]]*\]\(([^)]+)\)", img_repl, line)

        def finalize(text):
            for token, tag in img_tokens.items():
                text = text.replace(token, tag)
            return text

        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            close_list()
            level = min(len(m.group(1)), 3)
            out.append('<div class="h%d">%s</div>' % (level, finalize(inline_html(m.group(2).strip()))))
            continue

        m = re.match(r"^[-*]\s+\[([ xX])\]\s+(.*)$", line)
        if m:
            open_list("ul")
            cls = "checked" if m.group(1).lower() == "x" else "unchecked"
            out.append('<li class="%s">%s</li>' % (cls, finalize(inline_html(m.group(2).strip()))))
            continue

        m = re.match(r"^[-*]\s+(.*)$", line)
        if m:
            open_list("ul")
            out.append('<li class="unchecked">%s</li>' % finalize(inline_html(m.group(1).strip())))
            continue

        m = re.match(r"^\d+\.\s+(.*)$", line)
        if m:
            open_list("ol")
            out.append("<li>%s</li>" % finalize(inline_html(m.group(1).strip())))
            continue

        if line.strip() == "---":
            close_list()
            out.append("<div>———</div>")
            continue

        if line.startswith(">"):
            close_list()
            out.append("<div>%s</div>" % finalize(inline_html(line.lstrip("> ").strip())))
            continue

        close_list()
        out.append("<div>%s</div>" % finalize(inline_html(line.strip())))

    close_list()
    return '<head><meta name="feature_list" content=""></head>' + "".join(out)


def markdown_to_plain(markdown):
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[#*`>_]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------- template

def adb(args, check=True):
    proc = subprocess.run(["adb"] + args, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "adb 执行失败")
    return proc


def pull_template(dest):
    proc = adb(["shell", "ls", "-t", BACKUP_ROOT + "/Data"], check=False)
    names = [x.strip() for x in proc.stdout.splitlines() if x.strip()]
    if not names:
        raise RuntimeError("手机上没有 ColorOS 便签备份，请先手动备份一次便签作为模板")
    ts = names[0]
    remote = BACKUP_ROOT + "/Data/" + ts
    os.makedirs(dest, exist_ok=True)
    for name in ("backup_config.db", "backup_config_new.db"):
        adb(["pull", remote + "/" + name, os.path.join(dest, name)])
    note_remote = remote + "/Note"
    note_local = os.path.join(dest, "Note")
    os.makedirs(note_local, exist_ok=True)
    for name in ("rich_note", "folder", "data_source_info", "todo", "todo_attr",
                 "skin", "encrypt_folder", "encrypt_rich_note",
                 "no_folder_rich_note", "paragraph_style"):
        adb(["pull", note_remote + "/" + name, os.path.join(note_local, name)], check=False)
    adb(["pull", note_remote + "/share_preference", os.path.join(note_local, "share_preference")], check=False)
    os.makedirs(os.path.join(note_local, "paint_note"), exist_ok=True)
    adb(["pull", note_remote + "/paint_note/file_list.json",
         os.path.join(note_local, "paint_note", "file_list.json")], check=False)
    adb(["pull", BACKUP_ROOT + "/.Preview/" + ts + ".preview",
         os.path.join(dest, "backup.preview")], check=False)
    return dest


def resolve_template(path):
    path = os.path.abspath(path)
    if os.path.isfile(os.path.join(path, "Note", "rich_note")):
        return path, os.path.join(path, "Note")
    if os.path.isfile(os.path.join(path, "rich_note")):
        return path, path
    raise ValueError("模板目录里找不到 rich_note: %s" % path)


def load_template_note(template_note_dir):
    with open(os.path.join(template_note_dir, "rich_note"), encoding="utf-8") as f:
        notes = json.load(f)
    for item in notes:
        r = item.get("richNote", {})
        if not item.get("attachments") and r.get("rawTitle") and r.get("rawText"):
            return json.loads(json.dumps(item, ensure_ascii=False))
    raise ValueError("模板里没有可用的文本笔记")


# ---------------------------------------------------------------- build backup

def load_export(export_dir):
    path = os.path.join(export_dir, "notes.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data, os.path.join(export_dir, "assets")


def build_note(item, asset_dir, template_note, zipf, ts_now, debug=False):
    note = json.loads(json.dumps(template_note, ensure_ascii=False))
    r = note["richNote"]

    title = (item.get("title") or "").strip() or "无标题"
    title = title.replace("\n", " ")[:200]
    markdown = item.get("markdown") or ""
    # ColorOS 的录音附件格式与小米不同，录音不导入；去掉无效的本地链接
    if re.search(r"\[[^\]]*录音[^\]]*\]\([^)]*\)", markdown):
        markdown = re.sub(r"\[[^\]]*录音[^\]]*\]\([^)]*\)", "", markdown)
        markdown += "\n\n（原 HyperOS 录音未导入）"
    created = item.get("created") or ts_now
    modified = item.get("modified") or created

    local_id = str(uuid.uuid4())
    image_links = {}
    attachments = []
    extras = []

    # 收集图片并去重（同一笔记内同名图片只建一个附件）
    for m in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", markdown):
        asset_name = os.path.basename(m.group(1))
        if asset_name in image_links:
            continue
        asset_path = os.path.join(asset_dir, asset_name)
        if not os.path.isfile(asset_path):
            continue
        data = open(asset_path, "rb").read()
        w, h = image_size(data)
        aid = str(uuid.uuid4())
        image_links[asset_name] = aid
        zipf.writestr("files/%s/%s_thumb.png" % (local_id, aid), data)
        attachments.append({
            "attachmentId": aid,
            "cloudMetaData": {"addToNoteTime": 0, "audioDuration": 0, "type": 0},
            "extra": {"audioDuration": 0, "createTime": 0},
            "isShowAttachment": False,
            "picture": {"height": h, "width": w},
            "richNoteId": local_id,
            "state": 0,
            "type": 0,
        })
        extras.append({
            "fileHash": hashlib.md5(data).hexdigest(),
            "fileMediaType": "0",
            "fileMetaData": {
                "addToNoteTime": 0, "audioDuration": 0, "type": 0,
                "id": aid, "imgHeight": h, "imgWidth": w,
            },
            "filePath": "/data/user/0/com.coloros.note/files/%s/%s_thumb.png" % (local_id, aid),
            "fileSize": len(data),
            "oosId": "",
            "url": "",
        })

    raw_text = markdown_to_coloros_html(markdown, image_links)
    plain = markdown_to_plain(markdown)

    r.update({
        "localId": local_id,
        "globalId": str(uuid.uuid4()),
        "rawTitle": "<div>%s</div>" % html_lib.escape(title, quote=False),
        "title": title,
        "rawText": raw_text,
        "text": plain,
        "createTime": int(created),
        "updateTime": int(modified),
        "timestamp": int(time.time() * 1000),
        "state": 1,
        "version": 0,
        "sysVersion": 0,
        "sysProtocolVersion": 0,
        "encryptSysVersion": 0,
        "encrypted": 0,
        "encryptedPre": 0,
        "deleted": False,
        "isPreset": False,
        "isPaintPic": False,
        "noteTypeFlag": 0,
        "folderGuid": "00000000_0000_0000_0000_000000000000",
        "topTime": 0,
        "alarmTime": 0,
        "alarmTimePre": 0,
        "recycleTime": 0,
        "recycleTimePre": 0,
        "allAttachmentFileName": "",
        "attachmentExtra": {"attachmentExtra": extras},
        "summaryAttachment": "",
        "summaryContent": "",
        "summaryTitle": "",
        "summaryVersion": 3,
        "summaryAttachmentSize": len(attachments),
        "extra": {
            "encryptStatus": -1,
            "featureList": [],
            "moveOutFromPaintFolder": 0,
            "pageResults": [],
            "coverPaints": [],
            "coverPictures": [],
            "paints": [],
            "voices": [],
            "speechLogExtra": {"appInfo": "", "audioInfo": "", "entityGroup": "", "newLrc": False},
        },
    })
    note["attachments"] = attachments

    if debug:
        print("  %-46s %5d 字  %2d 图" % (title[:46], len(plain), len(attachments)))
    return note


def build_backup(export_dir, template_root, template_note_dir, out_dir, limit=None, skip=0, debug=False):
    data, asset_dir = load_export(export_dir)
    template_note = load_template_note(template_note_dir)
    notes = data.get("notes", [])
    if skip:
        notes = notes[skip:]
    if limit:
        notes = notes[:limit]

    ts = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    root = os.path.join(out_dir, "Data", ts)
    note_dir = os.path.join(root, "Note")
    os.makedirs(os.path.join(note_dir, "files"), exist_ok=True)

    # 复制模板里除 rich_note / files 以外的小文件
    for name in ("folder", "data_source_info", "todo", "todo_attr", "skin",
                 "encrypt_folder", "encrypt_rich_note", "no_folder_rich_note",
                 "paragraph_style"):
        src = os.path.join(template_note_dir, name)
        if os.path.isfile(src):
            shutil.copy(src, os.path.join(note_dir, name))
    src = os.path.join(template_note_dir, "share_preference")
    if os.path.isdir(src):
        shutil.copytree(src, os.path.join(note_dir, "share_preference"), dirs_exist_ok=True)
    src = os.path.join(template_note_dir, "paint_note")
    if os.path.isdir(src):
        shutil.copytree(src, os.path.join(note_dir, "paint_note"), dirs_exist_ok=True)
    else:
        os.makedirs(os.path.join(note_dir, "paint_note"), exist_ok=True)

    for name in ("backup_config.db", "backup_config_new.db"):
        src = os.path.join(template_root, name)
        if os.path.isfile(src):
            shutil.copy(src, os.path.join(root, name))

    ts_now = int(time.time() * 1000)
    result = []
    zip_path = os.path.join(note_dir, "files", "file_backup.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for i, item in enumerate(notes, 1):
            result.append(build_note(item, asset_dir, template_note, zipf, ts_now, debug=debug))
            if debug and i % 20 == 0:
                print("  ... %d/%d" % (i, len(notes)))

    with open(os.path.join(note_dir, "rich_note"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)

    preview_src = os.path.join(template_root, "backup.preview")
    if os.path.isfile(preview_src):
        os.makedirs(os.path.join(out_dir, ".Preview"), exist_ok=True)
        shutil.copy(preview_src, os.path.join(out_dir, ".Preview", ts + ".preview"))

    report = {
        "timestamp": ts,
        "note_count": len(result),
        "image_count": sum(len(n.get("attachments", [])) for n in result),
        "backup_dir": root,
        "zip_size": os.path.getsize(zip_path),
    }
    with open(os.path.join(out_dir, "import_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return ts, root, report


# ---------------------------------------------------------------- push / restore

def push_backup(out_dir, ts):
    remote_root = BACKUP_ROOT + "/Data/" + ts
    adb(["shell", "mkdir", "-p", remote_root])
    adb(["push", os.path.join(out_dir, "Data", ts, "."), remote_root + "/"])
    preview = os.path.join(out_dir, ".Preview", ts + ".preview")
    if os.path.isfile(preview):
        adb(["shell", "mkdir", "-p", BACKUP_ROOT + "/.Preview"])
        adb(["push", preview, BACKUP_ROOT + "/.Preview/" + ts + ".preview"])
    print("已推送到手机: %s" % remote_root)
    return remote_root


def _dump():
    subprocess.run(["adb", "shell", "uiautomator", "dump", "/sdcard/ui.xml"],
                   capture_output=True)
    import xml.etree.ElementTree as ET
    xml = subprocess.run(["adb", "shell", "cat", "/sdcard/ui.xml"],
                         capture_output=True, text=True).stdout
    return ET.fromstring(xml)


def _tap_text(text, wait=1.5):
    import re as _re
    for n in _dump().iter("node"):
        if n.get("text") == text or n.get("content-desc") == text:
            m = _re.findall(r"\d+", n.get("bounds"))
            x = (int(m[0]) + int(m[2])) // 2
            y = (int(m[1]) + int(m[3])) // 2
            subprocess.run(["adb", "shell", "input", "tap", str(x), str(y)])
            time.sleep(wait)
            print("  tap:", text)
            return True
    print("  未找到:", text)
    return False


def _texts():
    return [n.get("text") for n in _dump().iter("node") if n.get("text")]


def restore_backup(ts):
    dt = datetime.datetime.strptime(ts, "%Y-%m-%d-%H%M%S")
    label = "%d月%d日 %02d:%02d" % (dt.month, dt.day, dt.hour, dt.minute)
    print("开始恢复 %s ..." % label)
    subprocess.run(["adb", "shell", "am", "start", "-a", "android.settings.SETTINGS"],
                   capture_output=True)
    time.sleep(2)
    for _ in range(5):
        texts = _texts()
        if "账号与备份" in texts:
            break
        if "完成" in texts:
            _tap_text("完成")
        else:
            subprocess.run(["adb", "shell", "input", "keyevent", "4"])
            time.sleep(1)
    if "完成" in _texts():
        _tap_text("完成")
    _tap_text("账号与备份")
    _tap_text("备份与迁移")
    time.sleep(1)
    if "完成" in _texts():
        _tap_text("完成")
    _tap_text("本地备份", 2)
    if not _tap_text(label, 2):
        raise RuntimeError("备份列表里没有找到 %s" % label)
    if not _tap_text("开始恢复", 3):
        raise RuntimeError("没找到「开始恢复」")
    # 等待恢复完成
    for _ in range(300):
        texts = " ".join(t for t in _texts() if t)
        if "恢复成功" in texts:
            print("恢复成功")
            return True
        if "恢复失败" in texts:
            raise RuntimeError("恢复失败")
        time.sleep(2)
    raise RuntimeError("等待恢复完成超时")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        prog="hyperos-to-coloros",
        description="把 HyperOS / MIUI 便签导出结果导入 ColorOS / realme UI 便签",
    )
    ap.add_argument("-V", "--version", action="version", version="%(prog)s " + __version__)
    ap.add_argument("--export", required=True, help="HyperOS 导出目录（含 notes.json 和 assets/）")
    ap.add_argument("--template", help="ColorOS 便签备份模板目录（包含 Note/rich_note）")
    ap.add_argument("--out", default="coloros_import_backup", help="生成的备份目录")
    ap.add_argument("--limit", type=int, help="只导入前 N 条（用于测试）")
    ap.add_argument("--skip", type=int, default=0, help="跳过前 N 条（断点续导）")
    ap.add_argument("--push", action="store_true", help="生成后推送到手机")
    ap.add_argument("--restore", action="store_true", help="推送后自动执行恢复")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    template_dir = args.template
    if not template_dir:
        tmp = os.path.join(out_dir, "_template")
        print("正在从手机拉取 ColorOS 备份模板 ...")
        template_dir = pull_template(tmp)
    template_root, template_note_dir = resolve_template(template_dir)

    print("正在生成 ColorOS 备份 ...")
    ts, root, report = build_backup(args.export, template_root, template_note_dir,
                                    out_dir, limit=args.limit, skip=args.skip, debug=args.debug)
    print("完成: %d 条笔记，%d 张图片，zip %.1f MB"
          % (report["note_count"], report["image_count"], report["zip_size"] / 1024 / 1024))
    print("备份目录: %s" % root)

    if args.push or args.restore:
        push_backup(out_dir, ts)
    if args.restore:
        restore_backup(ts)
        print("")
        print("恢复完成。打开便签 App 检查内容。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)
    except Exception as e:
        print("错误: %s" % e, file=sys.stderr)
        sys.exit(1)
