import os
import re
import hashlib
import time
from typing import Optional, Tuple, Dict
from urllib.parse import urljoin, urlparse
from uuid import uuid4
from fastapi import UploadFile
from core.config import settings

import httpx

from .file_utils import get_project_base_directory


def _sanitize_filename(name: str) -> str:
    """
    将任意字符串转换为安全的文件名。
    仅保留字母数字、点、下划线和连字符，并截断过长名称。
    """
    name = name.strip().replace(" ", "_")
    name = re.sub(r"[^A-Za-z0-9._-]", "", name)
    return name[:128] if len(name) > 128 else name


class FileStorageUtil:
    """
    简单的文件存储工具：
    - 提供基于知识库的存储目录
    - 从 URL 下载 PDF 并计算 SHA256
    """

    @staticmethod
    def get_kb_storage_dir(kb_id: int) -> str:
        base = get_project_base_directory("storage", "file", f"kb_{kb_id}")
        os.makedirs(base, exist_ok=True)
        return base

    @staticmethod
    def get_session_tmp_dir(user_id: int | str, session_id: str) -> str:
        base = get_project_base_directory("storage", "file", "tmp_session", str(user_id), session_id)
        os.makedirs(base, exist_ok=True)
        return base

    @staticmethod
    def sanitize_filename(name: str) -> str:
        return _sanitize_filename(name)

    @staticmethod
    def save_upload_temp(file: UploadFile, kb_id: int) -> Dict[str, str]:
        """
        将上传文件保存为临时文件，同时计算 sha256。
        返回 {temp_path, sha256, original_name, extension, size}
        """
        storage_dir = FileStorageUtil.get_kb_storage_dir(kb_id)
        tmp_dir = os.path.join(storage_dir, "tmp")
        os.makedirs(tmp_dir, exist_ok=True)

        original_name = file.filename or "uploaded"
        _, ext = os.path.splitext(original_name)
        temp_name = f"tmp_{uuid4().hex}{ext}"
        temp_path = os.path.join(tmp_dir, temp_name)

        hasher = hashlib.sha256()
        size = 0
        max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        with open(temp_path, "wb") as f:
            # 读取首块做简易 MIME 嗅探
            first_chunk = file.file.read(8192)
            if first_chunk:
                size += len(first_chunk)
                hasher.update(first_chunk)
                f.write(first_chunk)
                # 简易嗅探：PDF/DOCX
                sniff = first_chunk[:8]
                if ext.lower() == ".pdf":
                    if not sniff.startswith(b"%PDF-"):
                        try:
                            f.close()
                        except Exception:
                            pass
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass
                        raise ValueError("Uploaded file is not a valid PDF (magic header mismatch)")
                if ext.lower() == ".docx":
                    if not sniff.startswith(b"PK"):
                        try:
                            f.close()
                        except Exception:
                            pass
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass
                        raise ValueError("Uploaded file is not a valid DOCX (zip signature missing)")
            # 继续写入剩余内容
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    # 删除已写入的临时文件并报错
                    try:
                        f.close()
                    except Exception:
                        pass
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                    raise ValueError(f"Uploaded file exceeds limit: {settings.MAX_UPLOAD_SIZE_MB} MB")
                hasher.update(chunk)
                f.write(chunk)

        # 重置文件指针，避免上层复用时报错
        try:
            file.file.seek(0)
        except Exception:
            pass

        return {
            "temp_path": temp_path,
            "sha256": hasher.hexdigest(),
            "original_name": original_name,
            "extension": ext.lower(),
            "size": str(size),
        }

    @staticmethod
    def save_upload_temp_session(file: UploadFile, user_id: int | str, session_id: str) -> Dict[str, str]:
        """
        保存上传文件到会话级临时目录 tmp_session/{user}/{session}/，并计算 sha256。
        返回字段与 save_upload_temp 保持一致，便于 Job 重用。
        """
        storage_dir = FileStorageUtil.get_session_tmp_dir(user_id, session_id)
        original_name = file.filename or "uploaded"
        _, ext = os.path.splitext(original_name)
        temp_name = f"tmp_{uuid4().hex}{ext}"
        temp_path = os.path.join(storage_dir, temp_name)

        hasher = hashlib.sha256()
        size = 0
        max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        with open(temp_path, "wb") as f:
            first_chunk = file.file.read(8192)
            if first_chunk:
                size += len(first_chunk)
                hasher.update(first_chunk)
                f.write(first_chunk)
                sniff = first_chunk[:8]
                if ext.lower() == ".pdf" and not sniff.startswith(b"%PDF-"):
                    try:
                        f.close()
                    except Exception:
                        pass
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                    raise ValueError("Uploaded file is not a valid PDF (magic header mismatch)")
                if ext.lower() == ".docx" and not sniff.startswith(b"PK"):
                    try:
                        f.close()
                    except Exception:
                        pass
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                    raise ValueError("Uploaded file is not a valid DOCX (zip signature missing)")
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    try:
                        f.close()
                    except Exception:
                        pass
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                    raise ValueError(f"Uploaded file exceeds limit: {settings.MAX_UPLOAD_SIZE_MB} MB")
                hasher.update(chunk)
                f.write(chunk)

        try:
            file.file.seek(0)
        except Exception:
            pass

        return {
            "temp_path": temp_path,
            "sha256": hasher.hexdigest(),
            "original_name": original_name,
            "extension": ext.lower(),
            "size": str(size),
        }

    @staticmethod
    def remove_session_tmp_dir(user_id: int | str, session_id: str) -> None:
        """删除会话级临时目录，忽略错误。"""
        try:
            d = FileStorageUtil.get_session_tmp_dir(user_id, session_id)
            if os.path.isdir(d):
                # 安全起见，逐文件删除
                for name in os.listdir(d):
                    p = os.path.join(d, name)
                    try:
                        if os.path.isfile(p):
                            os.remove(p)
                    except Exception:
                        continue
                try:
                    os.rmdir(d)
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def move_temp_to_final(temp_path: str, kb_id: int, doc_id: int, original_name: str) -> str:
        """
        将临时文件移动到最终文档路径，命名为 {doc_id}_{sanitized_original_name}
        返回最终路径。
        """
        storage_dir = FileStorageUtil.get_kb_storage_dir(kb_id)
        safe_name = FileStorageUtil.sanitize_filename(original_name)

        # 保留原始扩展名（避免过长截断掉 .pdf/.docx/.txt 等）
        base_ext = os.path.splitext(original_name)
        ext = base_ext[1].lower() if base_ext else ""
        if ext:
            # 若截断导致丢失扩展名，则补回；并确保整体长度不超限制
            if not safe_name.lower().endswith(ext):
                max_len = 128
                if len(safe_name) > max_len - len(ext):
                    safe_name = safe_name[: max(1, max_len - len(ext))] + ext
                else:
                    safe_name = safe_name + ext

        final_path = os.path.join(storage_dir, f"{doc_id}_{safe_name}")
        os.replace(temp_path, final_path)
        return final_path

    @staticmethod
    def clean_tmp_dir(kb_id: int, max_age_seconds: int = 24 * 3600) -> None:
        """
        清理超时的临时文件，避免堆积。非强约束，尽力而为。
        """
        tmp_dir = os.path.join(FileStorageUtil.get_kb_storage_dir(kb_id), "tmp")
        if not os.path.isdir(tmp_dir):
            return
        now = int(os.path.getmtime(tmp_dir)) if os.path.exists(tmp_dir) else 0
        try:
            for name in os.listdir(tmp_dir):
                p = os.path.join(tmp_dir, name)
                try:
                    mtime = os.path.getmtime(p)
                    if now - mtime > max_age_seconds:
                        os.remove(p)
                except Exception:
                    continue
        except Exception:
            pass

    @staticmethod
    def download_pdf(
        url: str,
        kb_id: int,
        preferred_name: Optional[str] = None,
        timeout: int = 30,
        retries: int = 2,
        backoff: float = 1.5,
    ) -> Tuple[str, str]:
        """
        下载 PDF 文件到 KB 目录，返回 (local_path, sha256)。
        若服务端未返回 application/pdf，但 URL 显示为 .pdf，也尝试保存。
        """
        storage_dir = FileStorageUtil.get_kb_storage_dir(kb_id)

        filename = preferred_name or "document.pdf"
        filename = _sanitize_filename(filename)
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"

        local_path = os.path.join(storage_dir, filename)

        # Smallest plausible PDF; anything below this is almost certainly a
        # placeholder / error page returned in disguise.
        MIN_PDF_BYTES = 1024
        PDF_MAGIC = b"%PDF-"

        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                # 下载前尝试解析网页源为 PDF 直链
                try:
                    url_to_fetch = FileStorageUtil.resolve_pdf_url(url, timeout=timeout)
                except Exception:
                    url_to_fetch = url

                # follow_redirects=True is critical: hosts like figshare,
                # researchgate and IEEE issue 302/303 to a CDN before serving
                # bytes. Without it httpx returns the redirect itself and
                # downstream code happily writes an empty body.
                with httpx.stream(
                    "GET",
                    url_to_fetch,
                    timeout=timeout,
                    follow_redirects=True,
                ) as resp:
                    # 202 Accepted is figshare's "file is being prepared" code:
                    # raise_for_status() lets it pass, but the body is empty,
                    # so we MUST treat it as a failure here.
                    if resp.status_code == 202:
                        raise httpx.HTTPStatusError(
                            "Remote returned 202 Accepted (file not ready, retry later or download manually)",
                            request=resp.request,
                            response=resp,
                        )
                    resp.raise_for_status()

                    content_type = resp.headers.get("content-type", "").lower()
                    if "application/pdf" not in content_type and not str(resp.url).lower().endswith(".pdf"):
                        raise ValueError(f"URL does not seem to be a PDF: content-type={content_type}")

                    hasher = hashlib.sha256()
                    size = 0
                    head = b""
                    with open(local_path, "wb") as f:
                        for chunk in resp.iter_bytes():
                            if not chunk:
                                continue
                            if len(head) < len(PDF_MAGIC):
                                head += chunk[: len(PDF_MAGIC) - len(head)]
                            hasher.update(chunk)
                            f.write(chunk)
                            size += len(chunk)

                # Post-download integrity check: drop placeholder/HTML payloads
                # that slipped past the content-type check (e.g. 202 prep page,
                # CDN error HTML, captive portals).
                if size < MIN_PDF_BYTES or not head.startswith(PDF_MAGIC):
                    try:
                        os.remove(local_path)
                    except OSError:
                        pass
                    raise ValueError(
                        f"Downloaded payload is not a valid PDF (size={size}B, "
                        f"head={head[:8]!r}); remote likely returned a placeholder"
                    )

                return local_path, hasher.hexdigest()
            except httpx.HTTPStatusError as e:
                # Persistent client-side states aren't worth retrying;
                # surface them to the handler for "需手动下载" routing.
                if e.response is not None and e.response.status_code in {202, 403, 404}:
                    raise
                last_exc = e
                if attempt < retries:
                    time.sleep(backoff ** attempt)
                else:
                    raise last_exc
            except Exception as e:
                last_exc = e
                if attempt < retries:
                    time.sleep(backoff ** attempt)
                else:
                    raise last_exc

    @staticmethod
    def resolve_pdf_url(url: str, timeout: int = 15) -> str:
        """
        将网页源 URL 解析为 PDF 直链（尽力而为）：
        - 若已是 PDF（content-type 或后缀），直接返回
        - 若是 HTML，扫描页面中的 .pdf 链接或 meta refresh 重定向
        - 常见站点通过简单规则即可奏效（如 arXiv、机构仓储）
        解析失败则返回原始 URL
        """
        try:
            with httpx.Client(follow_redirects=True, timeout=timeout) as client:
                r = client.get(url)
                ct = (r.headers.get("content-type") or "").lower()
                if "application/pdf" in ct or str(r.url).lower().endswith(".pdf"):
                    return str(r.url)

                text = r.text or ""

                # 1) 直接查找 .pdf 的 href 链接
                for m in re.findall(r"href=[\"']([^\"']+\.pdf)(?:[\"']|$)", text, flags=re.IGNORECASE):
                    candidate = urljoin(str(r.url), m)
                    try:
                        h = client.head(candidate)
                        cth = (h.headers.get("content-type") or "").lower()
                        if "application/pdf" in cth or candidate.lower().endswith(".pdf"):
                            return candidate
                    except Exception:
                        try:
                            g = client.get(candidate)
                            cth = (g.headers.get("content-type") or "").lower()
                            if "application/pdf" in cth or candidate.lower().endswith(".pdf"):
                                return candidate
                        except Exception:
                            continue

                # 2) 处理 meta refresh 重定向
                m = re.search(r"<meta[^>]+http-equiv=\"refresh\"[^>]+content=\"\d+;\s*url=([^\"]+)\"", text, flags=re.IGNORECASE)
                if m:
                    candidate = urljoin(str(r.url), m.group(1))
                    try:
                        g = client.get(candidate)
                        cth = (g.headers.get("content-type") or "").lower()
                        if "application/pdf" in cth or str(g.url).lower().endswith(".pdf"):
                            return str(g.url)
                    except Exception:
                        pass

                # 3) 常见站点：arXiv 等
                parsed = urlparse(str(r.url))
                if "arxiv.org" in parsed.netloc and "/pdf/" not in parsed.path:
                    m2 = re.search(r"href=[\"'](/pdf/[^\"']+\.pdf)[\"']", text, flags=re.IGNORECASE)
                    if m2:
                        return urljoin(str(r.url), m2.group(1))
        except Exception:
            pass
        return url



