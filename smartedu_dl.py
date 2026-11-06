#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国家中小学智慧教育平台（basic.smartedu.cn）教材 PDF 下载工具
================================================================

功能：
  - 获取平台全部电子教材目录（约 3500+ 册，数据来自官方 CDN 公开接口）
  - 按 学段 / 学科 / 版本 / 年级 / 册次 筛选，或按关键词搜索
  - 自动完成访客登录（无需账号），生成 x-nd-auth 签名下载 PDF
  - 支持批量下载、断点续传、失败重试、多线程并发

用法示例：
  python smartedu_dl.py filters                         # 查看所有筛选项
  python smartedu_dl.py search 道德与法治                # 按关键词搜索
  python smartedu_dl.py list --stage 小学 --subject 语文  # 按条件列出教材
  python smartedu_dl.py info <contentId 或 详情页URL>     # 查看教材详情
  python smartedu_dl.py download --id <contentId>        # 下载单本
  python smartedu_dl.py download --keyword 道法 --stage 小学   # 按条件批量下载
  python smartedu_dl.py download --stage 初中 --subject 数学 --version 人教版 -o D:/教材

仅依赖 Python 3.8+ 标准库，无需安装第三方包。
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from des_pure import des_ecb_decrypt, des_ecb_encrypt

# ============================================================
# 常量与接口配置（均来自 basic.smartedu.cn 前端公开接口）
# ============================================================

APP_ID = "e8906b1f-786a-4cc1-9afc-17e39abd099d"      # UC 应用 ID
SDP_APP_ID = "8fb42234-efb6-4ef3-bdb6-82d7f2a06246"  # sdp-app-id
UC_GATEWAY = "https://uc-gateway.ykt.eduyun.cn"       # 用户中心网关
CDN1 = "https://s-file-1.ykt.cbern.com.cn"            # 目录 CDN 1
CDN2 = "https://s-file-2.ykt.cbern.com.cn"            # 目录 CDN 2
PRIVATE_HOSTS = [                                     # PDF 私有 CDN（按优先级）
    "https://r1-ndr-private.ykt.cbern.com.cn",
    "https://r2-ndr-private.ykt.cbern.com.cn",
    "https://r3-ndr-private.ykt.cbern.com.cn",
]
TAG_VERSION_URL = CDN2 + "/zxx/ndrs/resources/tch_material/version/data_version.json"
TAG_DETAIL_URL = CDN2 + "/zxx/ndrv2/resources/tch_material/details/{id}.json"
THEMATIC_LIST_URL = CDN1 + "/zxx/ndrs/special_edu/thematic_course/{id}/resources/list.json"

# 标签维度 → 中文名
TAG_DIMS = {
    "zxxxd": "stage",     # 学段
    "zxxxk": "subject",   # 学科
    "zxxbb": "version",   # 版本
    "zxxnj": "grade",     # 年级
    "zxxcc": "term",      # 册次
}
DIM_NAMES = {"stage": "学段", "subject": "学科", "version": "版本",
             "grade": "年级", "term": "册次"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_CACHE = os.path.join(TOOL_DIR, ".token.json")
CATALOG_CACHE = os.path.join(TOOL_DIR, ".catalog.json")

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}GB"


def sanitize_filename(name, maxlen=120):
    """清理为合法文件名"""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if len(name) > maxlen:
        name = name[:maxlen].strip()
    return name or "untitled"


# ============================================================
# HTTP 封装（urllib）
# ============================================================

def http_get(url, headers=None, timeout=30):
    h = {"User-Agent": UA, "Referer": "https://basic.smartedu.cn/"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_get_json(url, headers=None, timeout=30):
    return json.loads(http_get(url, headers=headers, timeout=timeout).decode("utf-8"))


def http_post_json(url, data, headers=None, timeout=30):
    h = {"User-Agent": UA, "Referer": "https://basic.smartedu.cn/",
         "Content-Type": "application/json", "Origin": "https://basic.smartedu.cn"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"),
                                 headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"raw": body[:300]}


# ============================================================
# 访客认证（device_id → session → guest token → x-nd-auth）
# ============================================================

# 设备 ID 校验密钥（来自前端 UC SDK，md5 加盐后取 base64 第 3 位）
_DEVICE_KEY = b"\xa3\xac\xa1\xa3fdjf,jkgfkl"


def _device_checksum(payload):
    md5hex = hashlib.md5(payload.encode("latin-1") + _DEVICE_KEY).hexdigest()
    return base64.b64encode(md5hex.encode("ascii")).decode("ascii")[2]


def gen_device_id(browser="Chrome131", platform="Win10"):
    """生成合法 device_id：<校验符>w<平台>/<浏览器>/<UUID>"""
    payload = f"{platform}/{browser}/{uuid4()}"
    return _device_checksum(payload) + "w" + payload


def uuid4():
    h = "0123456789abcdef"
    s = []
    for c in "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx":
        if c in "xy":
            r = random.randrange(16)
            v = r if c == "x" else (r & 0x3) | 0x8
            s.append(h[v])
        else:
            s.append(c)
    return "".join(s)


class NdAuth:
    """管理访客令牌与 x-nd-auth 签名头（线程安全）"""

    def __init__(self):
        self._lock = threading.Lock()
        self._token = None
        self._session_key = None

    # ---------- 令牌获取 / 缓存 ----------
    def _load_cache(self):
        try:
            with open(TOKEN_CACHE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _save_cache(self, data):
        try:
            with open(TOKEN_CACHE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    def _login(self):
        """完整访客登录流程，返回 (token_dict, session_key)"""
        device_id = gen_device_id()
        st, sess = http_post_json(UC_GATEWAY + "/v1.1/sessions", {"device_id": device_id})
        if st != 201 or not sess.get("session_key"):
            raise RuntimeError(f"创建会话失败: HTTP {st} {sess}")
        session_id = sess["session_id"]
        session_key = sess["session_key"]
        enc_dev = base64.b64encode(
            des_ecb_encrypt(session_key.encode("utf-8"), device_id.encode("utf-8"))
        ).decode("ascii")
        st, tok = http_post_json(UC_GATEWAY + "/v1.1/guest_tokens",
                                 {"session_id": session_id, "device_id": enc_dev})
        if st != 201 or not tok.get("access_token"):
            raise RuntimeError(f"访客登录失败: HTTP {st} {tok}")
        # 解密 mac_key
        mac_key = None
        if tok.get("mac_key"):
            try:
                mac_key = des_ecb_decrypt(session_key.encode("utf-8"),
                                          base64.b64decode(tok["mac_key"])).decode("utf-8")
            except Exception:
                mac_key = tok["mac_key"]
        tok["mac_key_plain"] = mac_key or tok.get("mac_key")
        # diff = 服务器时间 - 本地时间（毫秒）；server_time 可能是 ISO 字符串或毫秒数
        server_ms = _to_epoch_ms(tok.get("server_time"))
        tok["diff"] = server_ms - int(time.time() * 1000) if server_ms else 800
        # 过期时间统一转为毫秒时间戳
        exp = _to_epoch_ms(tok.get("expires_at"))
        if exp:
            tok["expires_at"] = exp
        tok["_login_time"] = int(time.time())
        return tok, session_key

    def _refresh(self, token):
        st, tok = http_post_json(
            UC_GATEWAY + f"/v1.1/tokens/{token['refresh_token']}/actions/refresh", {})
        if st != 201 or not tok.get("access_token"):
            return None
        return tok

    def get_token(self, force=False):
        """获取有效令牌（带缓存；过期自动刷新/重新登录）"""
        with self._lock:
            if self._token and not force:
                return self._token
            cached = self._load_cache()
            if cached and not force:
                expires_at = cached.get("expires_at") or 0
                # 剩余有效期不足 10 分钟则刷新
                if expires_at - time.time() * 1000 > 10 * 60 * 1000:
                    self._token = cached
                    return cached
                # 尝试刷新
                if cached.get("refresh_token"):
                    try:
                        new = self._refresh(cached)
                        if new:
                            new["mac_key_plain"] = cached.get("mac_key_plain")
                            new["diff"] = cached.get("diff", 800)
                            new["_login_time"] = int(time.time())
                            self._token = new
                            self._save_cache(new)
                            return new
                    except Exception:
                        pass
            # 重新登录
            tok, _ = self._login()
            self._token = tok
            self._save_cache(tok)
            return tok

    def invalidate(self):
        with self._lock:
            self._token = None
            try:
                os.remove(TOKEN_CACHE)
            except OSError:
                pass

    # ---------- 签名头 ----------
    def auth_header(self, url):
        """为指定 URL 生成 x-nd-auth 头"""
        token = self.get_token()
        access_token = token["access_token"]
        mac_key = token.get("mac_key_plain") or token.get("mac_key")
        diff = int(token.get("diff") or 800)
        nonce = f"{int(time.time() * 1000) + diff}:{rand_str(8)}"
        p = urllib.parse.urlsplit(url)
        sig = f"{nonce}\nGET\n{p.path}\n{p.netloc}\n"
        mac = base64.b64encode(
            hmac.new(mac_key.encode("utf-8"), sig.encode("utf-8"),
                     hashlib.sha256).digest()).decode("ascii")
        return f'MAC id="{access_token}",nonce="{nonce}",mac="{mac}"'


def rand_str(n):
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "".join(random.choice(chars) for _ in range(n))


def _to_epoch_ms(value):
    """把 ISO8601 字符串或毫秒/秒时间戳统一转为毫秒时间戳"""
    if not value:
        return 0
    if isinstance(value, (int, float)):
        v = int(value)
        return v if v > 10 ** 12 else v * 1000  # 秒 → 毫秒
    s = str(value).strip()
    if s.isdigit():
        v = int(s)
        return v if v > 10 ** 12 else v * 1000
    try:
        return int(datetime_from_iso(s) * 1000)
    except Exception:
        return 0


def datetime_from_iso(s):
    """解析 ISO8601 时间为 Unix 秒"""
    import datetime as _dt
    s = s.strip()
    # 处理时区 +0800 / Z
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
                 r"(Z|[+-]\d{2}:?\d{2})?$", s)
    if not m:
        raise ValueError(f"bad datetime: {s}")
    y, mo, d, h, mi, se, frac, tz = m.groups()
    micro = int((frac or "0").ljust(6, "0")[:6])
    dt = _dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(se), micro,
                      tzinfo=_dt.timezone.utc)
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        tz = tz[1:].replace(":", "")
        offset = _dt.timedelta(hours=int(tz[:2]), minutes=int(tz[2:4]))
        dt = dt.replace(tzinfo=_dt.timezone(sign * offset))
    return dt.timestamp()


# ============================================================
# 教材目录
# ============================================================

class Catalog:
    """教材目录：获取、缓存、筛选、搜索"""

    def __init__(self):
        self.items = []
        self._load_or_fetch()

    def _load_or_fetch(self):
        # 读取本地缓存
        try:
            with open(CATALOG_CACHE, encoding="utf-8") as f:
                data = json.load(f)
            self.items = data.get("items", [])
            log(f"已加载本地目录缓存：{len(self.items)} 册教材"
                f"（版本 {data.get('module_version')}，缓存时间 {data.get('fetch_time')}）")
            return
        except Exception:
            pass
        self.refresh()

    def refresh(self):
        log("正在获取教材目录版本信息 ...")
        ver = http_get_json(TAG_VERSION_URL)
        urls = [u.strip() for u in ver.get("urls", "").split(",") if u.strip()]
        log(f"目录共 {len(urls)} 个分卷，开始下载 ...")
        items = []
        for i, u in enumerate(urls, 1):
            log(f"  [{i}/{len(urls)}] {u.split('/')[-1]} ...")
            part = http_get_json(u, timeout=120)
            items.extend(part)
            log(f"       {len(part)} 册（累计 {len(items)}）")
        self.items = items
        try:
            with open(CATALOG_CACHE, "w", encoding="utf-8") as f:
                json.dump({"module_version": ver.get("module_version"),
                           "fetch_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "items": items}, f, ensure_ascii=False)
        except Exception:
            pass
        log(f"目录获取完成，共 {len(items)} 册教材。")

    # ---------- 条目解析 ----------
    @staticmethod
    def _entry(it):
        """提取目录条目的关键字段"""
        tags = {}
        for t in it.get("tag_list", []):
            dim = TAG_DIMS.get(t.get("tag_dimension_id"))
            if dim and t.get("tag_name"):
                tags[dim] = t["tag_name"]
        return {
            "id": it.get("id", ""),
            "title": (it.get("global_title") or {}).get("zh-CN", ""),
            "type": it.get("resource_type_code", ""),
            "provider": "/".join(p.get("name", "") for p in it.get("provider_list") or []),
            "update_time": it.get("update_time", ""),
            "tags": tags,
        }

    def all_entries(self):
        return [self._entry(it) for it in self.items]

    def filter(self, stage=None, subject=None, version=None, grade=None,
               term=None, keyword=None):
        """按条件筛选，返回条目列表"""
        result = []
        kw = (keyword or "").lower()
        for it in self.items:
            e = self._entry(it)
            tags = e["tags"]
            if stage and tags.get("stage") != stage:
                continue
            if subject and tags.get("subject") != subject:
                continue
            if version and tags.get("version") != version:
                continue
            if grade and tags.get("grade") != grade:
                continue
            if term and tags.get("term") != term:
                continue
            if kw and kw not in e["title"].lower():
                continue
            result.append(e)
        return result

    def filter_values(self):
        """统计各维度可选值"""
        values = {k: set() for k in DIM_NAMES}
        for it in self.items:
            e = self._entry(it)
            for dim, name in e["tags"].items():
                values[dim].add(name)
        return {k: sorted(v) for k, v in values.items() if v}

    def get(self, content_id):
        for it in self.items:
            if it.get("id") == content_id:
                return self._entry(it)
        return None


# ============================================================
# PDF 地址解析与下载
# ============================================================

class Downloader:
    def __init__(self, auth, output_dir, workers=4, timeout=120, retries=3):
        self.auth = auth
        self.output_dir = output_dir
        self.workers = workers
        self.timeout = timeout
        self.retries = retries

    # ---------- PDF 地址 ----------
    def _pdf_items_from_details(self, content_id):
        """普通教材：从详情 JSON 提取 PDF 相对路径（去重，不同主机视为同一文件）"""
        data = http_get_json(TAG_DETAIL_URL.format(id=content_id))
        title = (data.get("global_title") or {}).get("zh-CN", "") or content_id
        rels, seen = [], set()
        for ti in data.get("ti_items", []):
            if ti.get("ti_format") != "pdf":
                continue
            candidates = []
            storage = ti.get("ti_storage") or ""
            if storage.startswith("cs_path:${ref-path}"):
                candidates.append(storage[len("cs_path:${ref-path}"):])
            for s in ti.get("ti_storages", []):
                for h in PRIVATE_HOSTS:
                    if s.startswith(h):
                        candidates.append(s[len(h):])
                        break
                else:
                    if s.startswith("http"):
                        candidates.append(urllib.parse.urlsplit(s).path)
            for rel in candidates:
                if rel not in seen:
                    seen.add(rel)
                    rels.append(rel)
        return title, rels

    def _pdf_items_from_thematic(self, content_id):
        """资源包教材：从资源列表提取所有 PDF"""
        data = http_get_json(THEMATIC_LIST_URL.format(id=content_id))
        items = []
        for r in data:
            title = (r.get("global_title") or {}).get("zh-CN", "") or r.get("id", "")
            fmt = (r.get("custom_properties") or {}).get("format", "")
            if fmt != "pdf":
                continue
            for ti in r.get("ti_items", []):
                if ti.get("ti_format") == "pdf":
                    storage = ti.get("ti_storage") or ""
                    if storage.startswith("cs_path:${ref-path}"):
                        items.append((title, storage[len("cs_path:${ref-path}"):]))
        return items

    def get_pdf_targets(self, entry):
        """返回 [(保存文件名, 相对路径)] 列表"""
        cid = entry["id"]
        if entry["type"] == "thematic_course":
            try:
                raw = self._pdf_items_from_thematic(cid)
            except Exception as e:
                log(f"  [警告] 获取资源包列表失败 {entry['title']}: {e}")
                return []
            targets = []
            used = set()
            for title, rel in raw:
                name = sanitize_filename(f"{title}.pdf")
                if name in used:
                    name = sanitize_filename(f"{title}_{cid[:8]}.pdf")
                used.add(name)
                targets.append((name, rel))
            return targets
        else:
            try:
                title, rels = self._pdf_items_from_details(cid)
            except Exception as e:
                log(f"  [警告] 获取教材详情失败 {entry['title']}: {e}")
                return []
            if not rels:
                return []
            base = sanitize_filename(entry["title"] or title)
            targets = []
            if len(rels) == 1:
                targets.append((f"{base}.pdf", rels[0]))
            else:
                for i, rel in enumerate(rels, 1):
                    targets.append((f"{base}_第{i}部分.pdf", rel))
            return targets

    # ---------- 下载 ----------
    def _book_dir(self, entry):
        tags = entry.get("tags", {})
        parts = [self.output_dir]
        for dim in ("stage", "subject", "version", "grade", "term"):
            if tags.get(dim):
                parts.append(sanitize_filename(tags[dim]))
        if entry.get("type") == "thematic_course":
            parts.append(sanitize_filename(entry["title"]))
        d = os.path.join(*parts)
        os.makedirs(d, exist_ok=True)
        return d

    def download_file(self, rel_path, filepath, label="", show_progress=True):
        """下载单个文件：多主机故障转移 + 断点续传 + 重试"""
        # 断点续传：检查已有大小
        existing = 0
        if os.path.exists(filepath):
            existing = os.path.getsize(filepath)
        for attempt in range(1, self.retries + 1):
            host = PRIVATE_HOSTS[(attempt - 1) % len(PRIVATE_HOSTS)]
            url = host + urllib.parse.quote(rel_path)
            headers = {"x-nd-auth": self.auth.auth_header(url)}
            if existing:
                headers["Range"] = f"bytes={existing}-"
            req = urllib.request.Request(url, headers={**headers, "User-Agent": UA,
                                                       "Referer": "https://basic.smartedu.cn/"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    total = int(r.headers.get("Content-Length", 0)) + existing
                    if r.status == 206:  # 续传
                        mode = "ab"
                    elif r.status == 200:
                        mode, existing = "wb", 0
                        total = int(r.headers.get("Content-Length", 0))
                    else:
                        raise IOError(f"HTTP {r.status}")
                    downloaded = existing
                    t0 = time.time()
                    last_print = 0.0
                    with open(filepath, mode) as f:
                        while True:
                            chunk = r.read(256 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            now = time.time()
                            if show_progress and (now - last_print >= 0.5
                                                  or downloaded >= total):
                                last_print = now
                                self._progress(label, downloaded, total,
                                               t0, existing)
                    if show_progress:
                        self._progress_done(label, filepath)
                    else:
                        log(f"  ✓ {label}  {human_size(os.path.getsize(filepath))}")
                    return True
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and attempt == 1:
                    # 令牌可能过期，刷新后重试
                    try:
                        self.auth.invalidate()
                        self.auth.get_token(force=True)
                    except Exception:
                        pass
                    continue
                if e.code == 404:
                    log(f"  [失败] {label}: 资源不存在（404，可能已下架）")
                    return False
                if e.code == 416:  # Range 越界 = 文件已完整
                    self._progress_done(label, filepath)
                    return True
                if attempt == self.retries:
                    log(f"  [失败] {label}: HTTP {e.code}"
                        + (f"（{human_size(os.path.getsize(filepath))} 已下载，可重试续传）"
                           if os.path.exists(filepath) else ""))
                    return False
                time.sleep(1.5 * attempt)
            except Exception as e:
                if attempt == self.retries:
                    log(f"  [失败] {label}: {e}")
                    return False
                time.sleep(1.5 * attempt)
        return False

    def _progress(self, label, done, total, t0, base=0):
        pct = done / total * 100 if total else 0
        elapsed = max(time.time() - t0, 0.001)
        speed = (done - base) / elapsed
        with _print_lock:
            sys.stdout.write(f"\r  {label[:36]:<38} {pct:5.1f}% "
                             f"{human_size(done):>9}/{human_size(total):<9} "
                             f"{human_size(speed)}/s   ")
            sys.stdout.flush()

    def _progress_done(self, label, filepath):
        size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
        with _print_lock:
            sys.stdout.write(f"\r  {label[:38]:<40} 100.0% {human_size(size):>10}  ✓\n")
            sys.stdout.flush()

    def download_entry(self, entry, index=None, total=None):
        """下载一本教材（可能含多个 PDF）"""
        targets = self.get_pdf_targets(entry)
        if not targets:
            log(f"  [跳过] {entry['title']}：未找到 PDF 资源")
            return 0, 1
        book_dir = self._book_dir(entry)
        ok, fail = 0, 0
        # 仅单文件下载时显示实时进度条，批量并发时只显示完成行
        show_progress = (self.workers == 1 and (total or 1) == 1
                         and len(targets) == 1)
        for name, rel in targets:
            filepath = os.path.join(book_dir, name)
            prefix = f"[{index}/{total}] " if index and (total or 0) > 1 else ""
            label = prefix + (name if len(targets) == 1 else f"{entry['title']}/{name}")
            if self.download_file(rel, filepath, label, show_progress):
                ok += 1
            else:
                fail += 1
        return ok, fail

    def download_entries(self, entries):
        """批量下载（多线程）"""
        total = len(entries)
        log(f"\n开始下载 {total} 册教材 → {os.path.abspath(self.output_dir)}"
            f"（并发 {self.workers}）\n")
        done = ok_cnt = fail_cnt = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {}
            for i, e in enumerate(entries, 1):
                futures[pool.submit(self.download_entry, e, i, total)] = e
            for fut in as_completed(futures):
                e = futures[fut]
                try:
                    ok, fail = fut.result()
                    ok_cnt += ok
                    fail_cnt += fail
                except Exception as ex:
                    log(f"  [错误] {e['title']}: {ex}")
                    fail_cnt += 1
                done += 1
        log(f"\n下载完成：成功 {ok_cnt} 个文件，失败 {fail_cnt} 个。"
            f"输出目录：{os.path.abspath(self.output_dir)}")
        return ok_cnt, fail_cnt


# ============================================================
# CLI
# ============================================================

def parse_content_id(text):
    """从 contentId 或详情页 URL 中提取 contentId"""
    text = text.strip()
    m = re.search(r"contentId=([0-9a-fA-F-]{36})", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[0-9a-fA-F-]{36}", text):
        return text
    return None


def cmd_filters(args, catalog):
    values = catalog.filter_values()
    log(f"\n教材目录共 {len(catalog.items)} 册，可选筛选值：\n")
    for dim in ("stage", "subject", "version", "grade", "term"):
        vals = values.get(dim, [])
        log(f"【{DIM_NAMES[dim]}】({len(vals)}) {' | '.join(vals)}")
    log("")


def cmd_list(args, catalog):
    entries = catalog.filter(stage=args.stage, subject=args.subject,
                             version=args.version, grade=args.grade,
                             term=args.term, keyword=args.keyword)
    if not entries:
        log("没有符合条件的教材。")
        return
    log(f"\n共 {len(entries)} 册：\n")
    for i, e in enumerate(entries, 1):
        tags = e["tags"]
        tag_str = "/".join(tags.get(d, "") for d in
                           ("stage", "subject", "version", "grade", "term"))
        log(f"{i:>4}. {e['title']}")
        log(f"      ID: {e['id']}  类型: {e['type']}")
        log(f"      分类: {tag_str}")
    log("")


def cmd_search(args, catalog):
    entries = catalog.filter(keyword=args.keyword)
    if not entries:
        log(f"未找到包含「{args.keyword}」的教材。")
        return
    log(f"\n找到 {len(entries)} 册包含「{args.keyword}」的教材：\n")
    for i, e in enumerate(entries[:100], 1):
        tags = e["tags"]
        tag_str = "/".join(tags.get(d, "") for d in
                           ("stage", "subject", "version", "grade", "term"))
        log(f"{i:>4}. {e['title']}    [{tag_str}]")
        log(f"      ID: {e['id']}")
    if len(entries) > 100:
        log(f"\n…… 其余 {len(entries) - 100} 册未显示，可用 --stage/--subject 等条件缩小范围。")
    log("")


def cmd_info(args, catalog):
    cid = parse_content_id(args.target)
    if not cid:
        log("无法识别 contentId，请传入 36 位 UUID 或 basic.smartedu.cn 教材详情页 URL。")
        return
    entry = catalog.get(cid)
    if entry:
        tags = entry["tags"]
        log(f"\n标题：{entry['title']}")
        log(f"ID：{entry['id']}    类型：{entry['type']}")
        log(f"分类：{'/'.join(tags.get(d, '') for d in ('stage', 'subject', 'version', 'grade', 'term'))}")
        log(f"提供商：{entry['provider'] or '智慧中小学'}    更新：{entry['update_time']}")
        log(f"详情页：https://basic.smartedu.cn/tchMaterial/detail?contentType={entry['type']}"
            f"&contentId={entry['id']}&catalogType=tchMaterial&subCatalog=tchMaterial")
    else:
        log(f"\n目录中未找到 {cid}，仍尝试直接获取详情 ...")
    auth = NdAuth()
    dl = Downloader(auth, args.output)
    entry = entry or {"id": cid, "title": cid, "type": "assets_document", "tags": {}}
    targets = dl.get_pdf_targets(entry)
    if not targets:
        log("未找到 PDF 资源。")
        return
    log(f"\n包含 {len(targets)} 个 PDF：")
    for name, rel in targets:
        log(f"  - {name}")
    log(f"\n下载命令：python {os.path.basename(__file__)} download --id {cid}")


def cmd_download(args, catalog):
    # 确定下载目标
    entries = []
    if args.id:
        cid = parse_content_id(args.id)
        if not cid:
            log("无效的 contentId 或 URL。")
            sys.exit(1)
        entry = catalog.get(cid)
        if entry:
            entries = [entry]
        else:
            log(f"目录中无此 ID，仍尝试直接下载 {cid} ...")
            entries = [{"id": cid, "title": cid, "type": "assets_document", "tags": {}}]
    else:
        if not any([args.stage, args.subject, args.version, args.grade,
                    args.term, args.keyword, args.all]):
            log("请指定下载条件：--id / --keyword / --stage / --subject / --version "
                "/ --grade / --term，或使用 --all 下载全部（约 3500 册，体积巨大！）。")
            sys.exit(1)
        entries = catalog.filter(stage=args.stage, subject=args.subject,
                                 version=args.version, grade=args.grade,
                                 term=args.term, keyword=args.keyword)
        if not entries:
            log("没有符合条件的教材。")
            sys.exit(1)
        if args.all or len(entries) > 50:
            if not args.yes:
                ans = input(f"即将下载 {len(entries)} 册教材，确认？[y/N] ")
                if ans.strip().lower() != "y":
                    log("已取消。")
                    sys.exit(0)
    auth = NdAuth()
    dl = Downloader(auth, args.output, workers=args.workers)
    dl.download_entries(entries)


def cmd_refresh(args, catalog):
    try:
        os.remove(CATALOG_CACHE)
    except OSError:
        pass
    catalog.refresh()


def main():
    parser = argparse.ArgumentParser(
        description="国家中小学智慧教育平台教材 PDF 下载工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python smartedu_dl.py filters
  python smartedu_dl.py search 道德与法治
  python smartedu_dl.py list --stage 小学 --subject 语文 --version 统编版
  python smartedu_dl.py download --id bdc00134-465d-454b-a541-dcd0cec4d86e
  python smartedu_dl.py download --stage 初中 --subject 数学 --version 人教版 -o D:/教材
""")
    parser.add_argument("-o", "--output", default="downloads", help="输出目录（默认 downloads）")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("filters", help="查看所有筛选项（学段/学科/版本/年级/册次）")

    p = sub.add_parser("list", help="按条件列出教材")
    p.add_argument("--stage", help="学段，如 小学/初中/高中")
    p.add_argument("--subject", help="学科，如 语文/数学/英语")
    p.add_argument("--version", help="版本，如 统编版/人教版")
    p.add_argument("--grade", help="年级，如 一年级/七年级")
    p.add_argument("--term", help="册次，如 上册/下册/全一册")
    p.add_argument("--keyword", help="标题关键词")

    p = sub.add_parser("search", help="按关键词搜索教材")
    p.add_argument("keyword", help="关键词，如：道德与法治")

    p = sub.add_parser("info", help="查看教材详情与 PDF 清单")
    p.add_argument("target", help="contentId 或教材详情页 URL")

    p = sub.add_parser("download", help="下载教材 PDF")
    p.add_argument("--id", help="contentId 或教材详情页 URL")
    p.add_argument("--stage"), p.add_argument("--subject")
    p.add_argument("--version"), p.add_argument("--grade"), p.add_argument("--term")
    p.add_argument("--keyword", help="标题关键词")
    p.add_argument("--all", action="store_true", help="下载全部教材（约 3500 册，慎用！）")
    p.add_argument("--workers", type=int, default=4, help="并发下载数（默认 4）")
    p.add_argument("-y", "--yes", action="store_true", help="跳过确认提示")

    p = sub.add_parser("refresh", help="强制刷新教材目录缓存")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return

    if args.cmd == "refresh":
        catalog = Catalog.__new__(Catalog)
        catalog.items = []
        cmd_refresh(args, catalog)
        return

    log("正在加载教材目录 ...")
    catalog = Catalog()

    {"filters": cmd_filters, "list": cmd_list, "search": cmd_search,
     "info": cmd_info, "download": cmd_download}[args.cmd](args, catalog)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n已中断。")
