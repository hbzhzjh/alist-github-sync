#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AList GitHub Sync - 自动化同步引擎
作用: 读取 software.json，监控 GitHub 最新 Release，自动下载、打包、上传至 AList 网盘并轮转清理旧版本
"""

import os
import sys
import json
import time
import re
import fnmatch
import zipfile
import tempfile
import urllib.parse
from datetime import datetime
import requests

# 引擎版本定义
ENGINE_VERSION = "1.7.1"

# ----------------------------------------------------------------------
# 配置与环境变量获取
# ----------------------------------------------------------------------
ALIST_URL = os.environ.get("ALIST_URL", "").rstrip("/")
ALIST_TOKEN = os.environ.get("ALIST_TOKEN", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
SOFTWARE_JSON_PATH = os.environ.get("SOFTWARE_JSON_PATH", "software.json")

# AList 请求头
ALIST_HEADERS = {
    "Authorization": ALIST_TOKEN,
    "User-Agent": "AList-GitHub-Sync-Bot/1.0"
}

# GitHub 请求头
GH_HEADERS = {
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "AList-GitHub-Sync-Bot/1.0"
}
if GITHUB_TOKEN:
    GH_HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"

# ----------------------------------------------------------------------
# AList API 交互封装
# ----------------------------------------------------------------------
def alist_mkdir(path: str) -> bool:
    """创建 AList 目录"""
    url = f"{ALIST_URL}/api/fs/mkdir"
    try:
        resp = requests.post(url, headers=ALIST_HEADERS, json={"path": path}, timeout=20)
        res = resp.json()
        if res.get("code") in [200, 0]:
            return True
        print(f"[AList] 创建目录 {path} 提示: {res.get('message')}")
        return True
    except Exception as e:
        print(f"[AList] 创建目录 {path} 异常: {e}")
        return False

def alist_upload_file(local_path: str, remote_dir: str, file_name: str) -> bool:
    """上传本地文件到 AList 指定目录"""
    # 确保远端目录结构存在
    alist_mkdir(remote_dir)

    # 规范化文件全路径并进行 URL 编码
    clean_dir = remote_dir.rstrip("/")
    remote_full_path = f"{clean_dir}/{file_name}"
    encoded_path = urllib.parse.quote(remote_full_path)

    # AList PUT 流式上传接口
    url = f"{ALIST_URL}/api/fs/put"
    upload_headers = ALIST_HEADERS.copy()
    upload_headers["File-Path"] = encoded_path
    upload_headers["Content-Type"] = "application/octet-stream"

    file_size = os.path.getsize(local_path)
    file_size_mb = file_size / (1024 * 1024)

    # 科学宽容超时：针对国内网盘(百度/夸克/移动)中转落盘耗时设计
    # 基础 90 秒 + 每 10MB 增加 10 秒，封顶 600 秒 (10 分钟)
    # 35MB 文件给 120 秒，300MB 文件给 390 秒，给足网盘后端分片合并与确认落盘时间
    calc_timeout = max(90, min(600, 90 + int(file_size_mb / 10) * 10))
    print(f"[AList] 开始上传: {file_name} ({file_size_mb:.2f} MB) -> {remote_full_path} (超时上限: {calc_timeout}s)")

    # 采用阶梯式智能退避重试 (最多 3 次，间隔 5s -> 10s)
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            with open(local_path, "rb") as f:
                resp = requests.put(url, headers=upload_headers, data=f, timeout=calc_timeout)
            res = resp.json()
            if res.get("code") in [200, 0]:
                print(f"[AList] 上传成功: {file_name}")
                # 上传成功后适度休眠 1.5 秒，给网盘后端留出转存落盘与释放连接的时间，避免高频并发排队限流
                time.sleep(1.5)
                return True
            else:
                print(f"[AList] 上传失败 (第 {attempt} 次): {res.get('message')}")
        except Exception as e:
            print(f"[AList] 上传异常 (第 {attempt} 次): {e}")
        
        # 失败退避休眠：给网盘后端恢复时间
        backoff_delay = attempt * 5
        print(f"[AList] 等待 {backoff_delay} 秒后重试...")
        time.sleep(backoff_delay)

    return False

def alist_list_dirs(remote_dir: str) -> list:
    """列出 AList 目录下所有的子文件夹（按创建/修改时间或排序返回）"""
    url = f"{ALIST_URL}/api/fs/list"
    try:
        resp = requests.post(url, headers=ALIST_HEADERS, json={
            "path": remote_dir,
            "page": 1,
            "per_page": 0,
            "refresh": True
        }, timeout=25)
        res = resp.json()
        if res.get("code") not in [200, 0]:
            return []
        
        content = res.get("data", {}).get("content", [])
        if not content:
            return []
            
        dirs = []
        for item in content:
            if item.get("is_dir"):
                dirs.append({
                    "name": item.get("name"),
                    "modified": item.get("modified", ""),
                    "created": item.get("created", "")
                })
        return dirs
    except Exception as e:
        print(f"[AList] 获取目录列表失败 {remote_dir}: {e}")
        return []

def alist_remove_dir(parent_dir: str, sub_dir_name: str) -> bool:
    """删除 AList 下的指定子文件夹"""
    url = f"{ALIST_URL}/api/fs/remove"
    try:
        resp = requests.post(url, headers=ALIST_HEADERS, json={
            "dir": parent_dir,
            "names": [sub_dir_name]
        }, timeout=30)
        res = resp.json()
        if res.get("code") in [200, 0]:
            print(f"[AList] 成功清理历史版本目录: {parent_dir}/{sub_dir_name}")
            return True
        else:
            print(f"[AList] 清理历史目录失败: {res.get('message')}")
            return False
    except Exception as e:
        print(f"[AList] 清理历史目录异常: {e}")
        return False

# ----------------------------------------------------------------------
# GitHub API 交互封装
# ----------------------------------------------------------------------
def gh_get_latest_release(repo: str, include_prerelease: bool = False) -> dict:
    """获取 GitHub 仓库最新 Release (支持可选包含预发布版 Pre-release / Beta)"""
    clean_repo = repo.replace("https://github.com/", "").strip().strip("/")
    
    if include_prerelease:
        # 允许获取预发布版：拉取 releases 列表，取第一个非 draft 的版本
        url = f"https://api.github.com/repos/{clean_repo}/releases?per_page=5"
        try:
            resp = requests.get(url, headers=GH_HEADERS, timeout=25)
            if resp.status_code == 200:
                releases = resp.json()
                if isinstance(releases, list) and releases:
                    for rel in releases:
                        if not rel.get("draft", False):
                            return rel
                print(f"[GitHub] 仓库 {clean_repo} 发布列表为空")
            elif resp.status_code == 404:
                print(f"[GitHub] 仓库 {clean_repo} 未找到 Release 或发布版本为空")
            else:
                print(f"[GitHub] 请求 Release 列表失败 ({resp.status_code}): {resp.text[:200]}")
        except Exception as e:
            print(f"[GitHub] 获取 Release 列表异常 {clean_repo}: {e}")
        return {}

    # 默认只获取正式稳定版
    url = f"https://api.github.com/repos/{clean_repo}/releases/latest"
    try:
        resp = requests.get(url, headers=GH_HEADERS, timeout=25)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 404:
            print(f"[GitHub] 仓库 {clean_repo} 未找到正式 Release，尝试检查预发布版本兜底...")
            # 兜底：若作者从未发布过正式版(全为 prerelease)，自动降级尝试获取最新发布
            fallback_url = f"https://api.github.com/repos/{clean_repo}/releases?per_page=1"
            f_resp = requests.get(fallback_url, headers=GH_HEADERS, timeout=25)
            if f_resp.status_code == 200:
                f_list = f_resp.json()
                if isinstance(f_list, list) and f_list and not f_list[0].get("draft", False):
                    return f_list[0]
            print(f"[GitHub] 仓库 {clean_repo} 未找到任何可用版本")
        else:
            print(f"[GitHub] 请求 Release 失败 ({resp.status_code}): {resp.text[:200]}")
    except Exception as e:
        print(f"[GitHub] 获取 Release 异常 {clean_repo}: {e}")
    return {}


def gh_download_file(download_url: str, save_path: str) -> bool:
    """下载 GitHub 资产文件（带重试与断点能力）"""
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            with requests.get(download_url, headers=GH_HEADERS, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(save_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
            return True
        except Exception as e:
            print(f"[GitHub] 下载文件失败 (第 {attempt} 次) {download_url}: {e}")
            time.sleep(3)
    return False

# ----------------------------------------------------------------------
# 历史版本轮转清理
# ----------------------------------------------------------------------
def rotate_old_versions(alist_base_path: str, sw_name: str, keep_count: int):
    """根据 keep_count 清理 AList 中超额的历史版本文件夹"""
    if keep_count <= 0:
        return

    sw_root = f"{alist_base_path.rstrip('/')}/{sw_name}"
    existing_dirs = alist_list_dirs(sw_root)
    if not existing_dirs or len(existing_dirs) <= keep_count:
        return

    # 按 modified 时间排序（升序：最早修改的在最前）
    # 若 modified 为空则按名称排序
    sorted_dirs = sorted(existing_dirs, key=lambda x: x.get("modified") or x.get("name"))
    
    # 超过限制的旧目录
    excess_count = len(sorted_dirs) - keep_count
    dirs_to_delete = sorted_dirs[:excess_count]

    print(f"[清理机制] 软件 '{sw_name}' 当前已有 {len(sorted_dirs)} 个版本目录，保留最新 {keep_count} 个，正在清理 {excess_count} 个旧版本...")
    for d in dirs_to_delete:
        alist_remove_dir(sw_root, d["name"])

def create_zip_archive(zip_save_path: str, files_list: list, password: str = "") -> bool:
    """创建 ZIP 压缩包，若传入 password 则使用 AES-256 高强度加密"""
    if not password:
        # 无密码标准普通打包
        with zipfile.ZipFile(zip_save_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname, fpath in files_list:
                zf.write(fpath, arcname=fname)
        return True

    print(f"[加密打包] 正在为压缩包设置解压密码 (AES-256 加密)...")

    # 方式一：尝试使用 pyzipper (纯 Python 标准 AES-256 加密)
    try:
        import pyzipper
        with pyzipper.AESZipFile(
            zip_save_path,
            "w",
            compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES
        ) as zf:
            zf.setpassword(password.encode("utf-8"))
            for fname, fpath in files_list:
                zf.write(fpath, arcname=fname)
        print(f"[加密打包] 使用 pyzipper 成功生成加密 ZIP！")
        return True
    except ImportError:
        pass

    # 方式二：尝试调用系统 7z 命令
    try:
        import subprocess
        # 复制文件到工作目录避免绝对路径层级
        file_paths = [fpath for _, fpath in files_list]
        cmd = ["7z", "a", "-tzip", f"-p{password}", "-mem=AES256", zip_save_path] + file_paths
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0:
            print(f"[加密打包] 使用 7z 命令行成功生成加密 ZIP！")
            return True
    except Exception as e:
        print(f"[加密打包] 7z 调用异常: {e}")

    # 方式三：尝试调用系统 zip 命令
    try:
        import subprocess
        cmd = ["zip", "-P", password, "-j", zip_save_path] + [fpath for _, fpath in files_list]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0:
            print(f"[加密打包] 使用 zip 命令行成功生成加密 ZIP！")
            return True
    except Exception as e:
        print(f"[加密打包] zip 调用异常: {e}")

    # 回退：普通打包
    print(f"[警告] 环境缺少 pyzipper / 7z / zip，降级为普通未加密打包！")
    with zipfile.ZipFile(zip_save_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, fpath in files_list:
            zf.write(fpath, arcname=fname)
    return True

def split_file_to_volumes(file_path: str, max_chunk_size: int = 50 * 1024 * 1024) -> list:
    """将文件切分成分卷 (例如 .zip.001, .zip.002 ...)，若小于分卷大小则强制对半分卷阻断网盘在线解压"""
    file_size = os.path.getsize(file_path)
    if file_size <= max_chunk_size:
        chunk_size = max(1024, file_size // 2)
    else:
        chunk_size = max_chunk_size

    part_num = 1
    part_files = []
    base_dir = os.path.dirname(file_path)
    base_name = os.path.basename(file_path)

    with open(file_path, "rb") as src:
        while True:
            chunk = src.read(chunk_size)
            if not chunk:
                break
            part_name = f"{base_name}.{part_num:03d}"
            part_path = os.path.join(base_dir, part_name)
            with open(part_path, "wb") as dst:
                dst.write(chunk)
            part_files.append((part_name, part_path))
            part_num += 1

    return part_files

# ----------------------------------------------------------------------
# 核心同步流程
# ----------------------------------------------------------------------
def sync_software_item(item: dict, default_remote_dir: str = "", default_disguise_mode: str = "none") -> tuple:
    name = item.get("name", "").strip()
    repo = item.get("repo", "").strip()
    pattern = item.get("pattern", "*").strip()
    category = item.get("category", "").strip()
    # 兼容 remote_dir 与 alist_path 字段，若未指定则继承全局默认网盘路径
    remote_dir = item.get("remote_dir") or item.get("alist_path", "")
    remote_dir = remote_dir.strip()
    if not remote_dir:
        remote_dir = (default_remote_dir or os.environ.get("DEFAULT_REMOTE_DIR", "")).strip()
    pkg_mode = item.get("package_mode", "raw").strip()
    keep_versions = int(item.get("keep_versions", 3))
    last_version = item.get("last_sync_version", "").strip()

    # 读取解压密码：优先单软件独立密码，其次 effective_password，最后全局环境变量
    zip_pwd = item.get("zip_password") or item.get("effective_password") or os.environ.get("DEFAULT_ZIP_PASSWORD", "")
    zip_pwd = zip_pwd.strip()

    if not name or not repo or not remote_dir:
        print(f"[跳过] 软件项配置不完整 (缺失名称/仓库或网盘路径): {name}")
        return False, None

    # 解析多个网盘路径 (支持分号、逗号、换行分隔) 及 {category} 占位符
    raw_dirs = [d.strip() for d in re.split(r'[;\n,]+', remote_dir) if d.strip()]
    if not raw_dirs:
        print(f"[跳过] 未指定有效的网盘存放路径: {name}")
        return False, None

    target_dirs = []
    for d in raw_dirs:
        if "{category}" in d:
            if category:
                d_resolved = d.replace("{category}", category)
            else:
                d_resolved = d.replace("/{category}", "").replace("{category}", "")
        else:
            d_resolved = d
        d_resolved = "/" + d_resolved.strip("/")
        if d_resolved not in target_dirs:
            target_dirs.append(d_resolved)

    include_prerelease = bool(item.get("include_prerelease", False))

    print(f"\n========================================================")
    print(f"正在检查: {name} ({repo})")
    print(f"配置: 分类='{category or '无'}' | 匹配规则='{pattern}' | 打包模式='{pkg_mode}' | 包含预发布={'是' if include_prerelease else '否'} | 加密={'是' if zip_pwd else '否'}")
    print(f"分发目标网盘 ({len(target_dirs)} 个): {target_dirs}")
    print(f"========================================================")

    # 构建驱动器名称映射
    drive_names = []
    for td in target_dirs:
        clean_parts = [p for p in td.strip("/").split("/") if p]
        dname = clean_parts[0] if clean_parts else "AList"
        drive_names.append((dname, td))

    # 检查是否配置为本地服务器直传通道 (若为 local，云端直接跳过，零消耗 Actions 额度)
    sync_channel = item.get("sync_channel", "github")
    if sync_channel == "local":
        print(f"--> 该软件配置为 [🖥️ 本地服务器直传] 通道，跳过云端 GitHub Actions 处理以节省额度。")
        item["status"] = "local_channel (由本地直传)"
        drives_map = {dname: True for dname, _ in drive_names}
        status_info = {
            "version": last_version or "local",
            "last_sync": last_sync_time or "",
            "drives": drives_map
        }
        detail = {"type": "skipped", "name": name, "repo": repo, "reason": "已配置为本地服务器直传通道"}
        return True, (name, status_info), detail

    # 独立检测更新间隔 (冷却期) 判断
    check_interval = int(item.get("check_interval_hours", 0) or 0)
    last_sync_time = item.get("last_sync_time", "")
    if check_interval > 0 and last_sync_time and last_version:
        try:
            last_dt = datetime.strptime(last_sync_time, "%Y-%m-%d %H:%M:%S")
            diff_hours = (datetime.now() - last_dt).total_seconds() / 3600.0
            if diff_hours < check_interval:
                print(f"--> 该软件设置了 {check_interval} 小时独立检测间隔，距离上次检测仅 {diff_hours:.1f} 小时，处于冷却期，快速跳过。")
                item["status"] = f"success (冷却中: 剩 {check_interval - diff_hours:.1f}h)"
                drives_map = {dname: True for dname, _ in drive_names}
                status_info = {
                    "version": last_version,
                    "last_sync": last_sync_time,
                    "drives": drives_map
                }
                detail = {"type": "skipped", "name": name, "repo": repo, "reason": "处于冷却期"}
                return True, (name, status_info), detail
        except Exception as e:
            print(f"[冷却检查跳过] 时间解析异常: {e}")

    release_info = gh_get_latest_release(repo, include_prerelease)
    if not release_info:
        item["status"] = "error: 未能获取 Release"
        detail = {"type": "failed", "name": name, "repo": repo, "error": item["status"]}
        return False, None, detail

    tag_name = release_info.get("tag_name", "").strip()
    if not tag_name:
        print(f"[警告] 仓库 {repo} 最新 Release 中未解析到 tag_name 版本标签")
        item["status"] = "error: 版本标签为空"
        detail = {"type": "failed", "name": name, "repo": repo, "error": item["status"]}
        return False, None, detail

    if tag_name == last_version:
        print(f"--> 当前已是最新版本 ({tag_name})，无需更新。")
        item["status"] = "success (最新)"
        drives_map = {dname: True for dname, _ in drive_names}
        status_info = {
            "version": tag_name,
            "last_sync": item.get("last_sync_time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "drives": drives_map
        }
        detail = {"type": "skipped", "name": name, "repo": repo, "reason": "已是最新版本"}
        return True, (name, status_info), detail

    print(f"发现新版本: {tag_name} (原版本: {last_version or '无'})，开始同步流程...")

    assets = release_info.get("assets", [])
    if not assets:
        print(f"[警告] 该 Release 没有找到可下载的 Assets 附件")
        item["status"] = "warning: 资产列表为空"
        detail = {"type": "failed", "name": name, "repo": repo, "error": item["status"]}
        return False, None, detail

    # 根据通配符规则筛选资产
    patterns = [p.strip() for p in pattern.split(";") if p.strip()]
    if not patterns:
        patterns = ["*"]

    matched_assets = []
    for asset in assets:
        asset_name = asset.get("name", "")
        for p in patterns:
            if fnmatch.fnmatch(asset_name, p):
                matched_assets.append(asset)
                break

    if not matched_assets:
        print(f"[警告] 没有匹配到规则 '{pattern}' 的文件。全部可用资产为: {[a.get('name') for a in assets]}")
        item["status"] = f"warning: 未匹配到资产"
        detail = {"type": "failed", "name": name, "repo": repo, "error": item["status"]}
        return False, None, detail

    print(f"成功匹配到 {len(matched_assets)} 个待同步文件: {[a.get('name') for a in matched_assets]}")

    # 使用临时工作目录下载与打包
    with tempfile.TemporaryDirectory(prefix="alist_sync_") as tmp_dir:
        downloaded_files = []
        for asset in matched_assets:
            file_name = asset.get("name")
            download_url = asset.get("browser_download_url")
            local_save = os.path.join(tmp_dir, file_name)

            print(f"[下载] 正在下载: {file_name} ...")
            if gh_download_file(download_url, local_save):
                downloaded_files.append((file_name, local_save))
            else:
                print(f"[错误] 下载失败: {file_name}")

        if not downloaded_files:
            item["status"] = "error: 资产下载全败"
            detail = {"type": "failed", "name": name, "repo": repo, "error": item["status"]}
            return False, None, detail

        # 准备待上传的文件列表
        files_to_upload = []

        if pkg_mode in ["raw", "both"]:
            for fname, fpath in downloaded_files:
                files_to_upload.append((fname, fpath))

        if pkg_mode in ["both", "zip_only"]:
            # 生成归档 ZIP 文件并根据防在线解压策略处理
            clean_tag = tag_name.replace("/", "_")
            raw_disguise = item.get("disguise_mode") or "default"
            effective_disguise = item.get("effective_disguise_mode") or (default_disguise_mode if raw_disguise == "default" else raw_disguise)
            effective_disguise = (effective_disguise or "none").strip()

            base_zip_name = f"{name}_{clean_tag}.zip"
            base_zip_path = os.path.join(tmp_dir, base_zip_name)
            print(f"[打包] 正在将文件压缩打包为: {base_zip_name} (防在线解压策略: {effective_disguise}) ...")

            create_zip_archive(base_zip_path, downloaded_files, zip_pwd)

            if effective_disguise == "zip1":
                # 策略 1：追加 .zip1 假扩展名 (防网盘识别为压缩包)
                disguise_name = f"{name}_{clean_tag}.zip1"
                disguise_path = os.path.join(tmp_dir, disguise_name)
                os.rename(base_zip_path, disguise_path)
                print(f"[防在线解压] 已附加 .zip1 假扩展名: {disguise_name}")
                files_to_upload.append((disguise_name, disguise_path))
            elif effective_disguise == "zip_txt":
                # 策略 2：带操作提示的假扩展名 .zip.txt
                disguise_name = f"{name}_{clean_tag}[下载后删去末尾txt].zip.txt"
                disguise_path = os.path.join(tmp_dir, disguise_name)
                os.rename(base_zip_path, disguise_path)
                print(f"[防在线解压] 已附加带提示假扩展名: {disguise_name}")
                files_to_upload.append((disguise_name, disguise_path))
            elif effective_disguise == "split":
                # 策略 3：分卷切分保护 (.zip.001 / .zip.002，阻断任何网盘在线解压)
                print(f"[防在线解压] 正在将压缩包切分为分卷 (.zip.001 / .zip.002)...")
                volume_files = split_file_to_volumes(base_zip_path)
                for vname, vpath in volume_files:
                    files_to_upload.append((vname, vpath))
                print(f"[防在线解压] 成功生成 {len(volume_files)} 个分卷文件: {[v[0] for v in volume_files]}")
            else:
                # 默认标准 .zip 格式
                files_to_upload.append((base_zip_name, base_zip_path))

        # 依次多网盘分发上传与独立历史版本清理
        drives_status = {}
        any_success = False

        for dname, target_dir in drive_names:
            remote_target_dir = f"{target_dir.rstrip('/')}/{name}/{tag_name}"
            print(f"\n[多盘分发 ➔ {dname}] 正在上传至: {remote_target_dir} ...")

            upload_success_count = 0
            consecutive_failures = 0 # 记录当前网盘连续失败文件数

            for f_idx, (upload_name, upload_path) in enumerate(files_to_upload):
                if alist_upload_file(upload_path, remote_target_dir, upload_name):
                    upload_success_count += 1
                    consecutive_failures = 0 # 成功则重置连续失败计数
                else:
                    consecutive_failures += 1
                    # 熔断触发：若当前网盘连续 2 个文件上传超时/失败，且该软件仍有后续文件
                    if consecutive_failures >= 2 and len(files_to_upload) > 2:
                        remaining = len(files_to_upload) - f_idx - 1
                        print(f"[熔断保护 ⚠] 网盘 [{dname}] 连续 {consecutive_failures} 个文件上传超时/失败，判定该网盘接口暂时不可达！")
                        if remaining > 0:
                            print(f"[熔断保护 ⚠] 立即跳过网盘 [{dname}] 剩余 {remaining} 个文件的无效重试，保护任务额度！")
                        break

            if upload_success_count > 0:
                print(f"[多盘分发 ➔ {dname}] 上传成功 ({upload_success_count}/{len(files_to_upload)}) 文件！")
                drives_status[dname] = True
                any_success = True

                # 上传成功后，对该网盘独立执行历史版本轮转清理
                try:
                    rotate_old_versions(target_dir, name, keep_versions)
                except Exception as e:
                    print(f"[清理警告] 网盘 [{dname}] 历史版本轮转异常: {e}")
            else:
                print(f"[多盘分发 ➔ {dname}] 全部文件上传失败！")
                drives_status[dname] = False

        if not any_success:
            item["status"] = "error: 所有网盘上传全败"
            detail = {"type": "failed", "name": name, "repo": repo, "error": item["status"]}
            return False, None, detail

        # 更新状态字段
        sync_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        item["last_sync_version"] = tag_name
        item["last_sync_time"] = sync_time_str
        item["status"] = "success"
        print(f"\n[完成] 软件 '{name}' 多网盘同步完毕，各网盘状态: {drives_status}")

        status_info = {
            "version": tag_name,
            "last_sync": sync_time_str,
            "drives": drives_status
        }
        detail = {
            "type": "updated",
            "name": name,
            "repo": repo,
            "version": tag_name,
            "previous_version": last_version or "初次同步",
            "drives": drives_status
        }
        return True, (name, status_info), detail

# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def main():
    start_time = time.time()
    print("====================================================")
    print("      AList GitHub Sync 自动化任务启动")
    print(f"      引擎版本: v{ENGINE_VERSION}")
    print(f"      时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("====================================================")

    if not ALIST_URL or not ALIST_TOKEN:
        print("[错误] 未配置 ALIST_URL 或 ALIST_TOKEN 环境变量，无法执行网盘写入！")
        sys.exit(1)

    if not os.path.exists(SOFTWARE_JSON_PATH):
        print(f"[错误] 未找到同步清单文件: {SOFTWARE_JSON_PATH}")
        sys.exit(1)

    try:
        with open(SOFTWARE_JSON_PATH, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
    except Exception as e:
        print(f"[错误] 解析 {SOFTWARE_JSON_PATH} 失败: {e}")
        sys.exit(1)

    # 兼容 list 或 {"notify": ..., "softwares": [...], "default_remote_dir": ...}
    is_list_format = isinstance(raw_data, list)
    softwares = raw_data if is_list_format else raw_data.get("softwares", [])
    notify_config = {} if is_list_format else raw_data.get("notify", {})
    global_default_remote_dir = "" if is_list_format else raw_data.get("default_remote_dir", "")
    global_default_remote_dir = (global_default_remote_dir or "").strip()
    global_default_disguise_mode = "" if is_list_format else raw_data.get("default_disguise_mode", "none")
    global_default_disguise_mode = (global_default_disguise_mode or "none").strip()

    if not softwares:
        print("[提示] software.json 中软件清单为空，无需同步。")
        sys.exit(0)

    print(f"共发现 {len(softwares)} 个待监控的软件项目。")
    if global_default_remote_dir:
        print(f"[全局默认网盘路径] {global_default_remote_dir}")
    if global_default_disguise_mode and global_default_disguise_mode != "none":
        print(f"[全局默认防解压策略] {global_default_disguise_mode}")

    # 读取现有的 sync_status.json
    status_path = "sync_status.json"
    status_map = {}
    if os.path.exists(status_path):
        try:
            with open(status_path, "r", encoding="utf-8") as sf:
                status_map = json.load(sf)
        except Exception:
            status_map = {}

    updated_items = []
    skipped_items = []
    failed_items = []

    for item in softwares:
        try:
            res, status_tuple, detail = sync_software_item(
                item,
                default_remote_dir=global_default_remote_dir,
                default_disguise_mode=global_default_disguise_mode
            )
            if status_tuple:
                s_name, s_info = status_tuple
                status_map[s_name] = s_info
            
            if detail.get("type") == "updated":
                updated_items.append(detail)
            elif detail.get("type") == "failed":
                failed_items.append(detail)
            else:
                skipped_items.append(detail)
        except Exception as err:
            print(f"[异常] 同步软件 {item.get('name')} 时发生未捕获异常: {err}")
            err_msg = f"error: {str(err)[:50]}"
            item["status"] = err_msg
            failed_items.append({
                "type": "failed",
                "name": item.get("name", "未知"),
                "repo": item.get("repo", ""),
                "error": err_msg
            })

    # 将最新的状态写回 software.json
    try:
        with open(SOFTWARE_JSON_PATH, "w", encoding="utf-8") as f:
            if is_list_format:
                json.dump(softwares, f, ensure_ascii=False, indent=2)
            else:
                raw_data["softwares"] = softwares
                raw_data["last_run_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                json.dump(raw_data, f, ensure_ascii=False, indent=2)
        print(f"\n[写入] 状态已更新至 {SOFTWARE_JSON_PATH}")
    except Exception as e:
        print(f"[写入错误] 无法保存状态到 {SOFTWARE_JSON_PATH}: {e}")

    # 将网盘详细状态写入 sync_status.json
    try:
        with open(status_path, "w", encoding="utf-8") as sf:
            json.dump(status_map, sf, ensure_ascii=False, indent=2)
        print(f"[写入] 详细网盘状态已更新至 {status_path}")
    except Exception as e:
        print(f"[写入错误] 无法保存状态到 {status_path}: {e}")

    elapsed_time = round(time.time() - start_time, 1)
    print(f"\n====================================================")
    print(f"同步检查总结: 总数 {len(softwares)} | 更新 {len(updated_items)} | 跳过 {len(skipped_items)} | 失败 {len(failed_items)}")
    print(f"总耗时: {elapsed_time} 秒")
    print(f"====================================================")

    # ------------------------------------------------------------------
    # 同步结果邮件通知推送 (基于 WordPress REST API 与原生 SMTP)
    # ------------------------------------------------------------------
    if notify_config and notify_config.get("enabled"):
        notify_url = str(notify_config.get("url", "")).strip()
        notify_secret = str(notify_config.get("secret", "")).strip()
        notify_strategy = str(notify_config.get("strategy", "success_only")).strip()

        if not notify_url or not notify_secret:
            print("[通知] 邮件通知配置不全 (缺失 url 或 secret)，已跳过发信。")
        else:
            should_notify = False
            if notify_strategy == "success_only" and len(updated_items) > 0:
                should_notify = True
            elif notify_strategy == "failure_only" and len(failed_items) > 0:
                should_notify = True
            elif notify_strategy == "all_events" and (len(updated_items) > 0 or len(failed_items) > 0):
                should_notify = True
            else:
                print(f"[通知] 本次运行无需发信，根据策略 ({notify_strategy}) 跳过邮件通知。")

            if should_notify:
                print(f"\n[通知] 正在向站点回调接口推送同步报告: {notify_url} ...")
                run_id = os.environ.get("GITHUB_RUN_ID", "")
                gh_repo = os.environ.get("GITHUB_REPOSITORY", "")
                run_url = f"https://github.com/{gh_repo}/actions/runs/{run_id}" if gh_repo and run_id else ""

                notify_payload = {
                    "event": "sync_complete" if not failed_items else "sync_warning",
                    "secret": notify_secret,
                    "run_id": run_id,
                    "run_url": run_url,
                    "repository": gh_repo,
                    "trigger_actor": os.environ.get("GITHUB_ACTOR", ""),
                    "duration_seconds": elapsed_time,
                    "summary": {
                        "total": len(softwares),
                        "updated": len(updated_items),
                        "skipped": len(skipped_items),
                        "failed": len(failed_items)
                    },
                    "updated_details": updated_items,
                    "failed_details": failed_items
                }

                try:
                    headers = {
                        "Content-Type": "application/json",
                        "X-AList-Sync-Secret": notify_secret,
                        "User-Agent": "AList-GitHub-Sync-Action/1.5.0"
                    }
                    resp = requests.post(notify_url, json=notify_payload, headers=headers, timeout=20)
                    if resp.status_code == 200:
                        print(f"[通知] 邮件推送成功！站点返回: {resp.text}")
                    else:
                        print(f"[通知警告] 站点接口响应非 200 (HTTP {resp.status_code}): {resp.text}")
                except Exception as ne:
                    print(f"[通知警告] 发送通知网络异常: {ne}")

    print("\n所有同步任务已处理完成！")


if __name__ == "__main__":
    main()
