"""Bounded local image I/O for the existing Runtime. No OCR, model or scheduler."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any
import uuid
import warnings

from PIL import Image, UnidentifiedImageError
import runtime

MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_DELIVERY_BYTES = 6 * 1024 * 1024
MAX_VISUAL_DISK_BYTES = 256 * 1024 * 1024
_SUPPORTED = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif", "BMP": "image/bmp"}
_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif", "image/bmp": ".bmp"}


def owned_image_path(path: str | Path, lease: dict[str, Any], attachment_dir: Path | None = None) -> Path:
    resolved = Path(path).expanduser().resolve(strict=True)
    if resolved.is_relative_to(runtime.MEDIA_ROOT.resolve(strict=False)):
        return resolved
    lease_id = str(lease["lease_id"])
    special_roots = [runtime.VISUAL_DIR]
    if attachment_dir is not None:
        special_roots.append(attachment_dir)
    for root in special_roots:
        base = root.resolve(strict=False)
        if resolved.is_relative_to(base):
            if not resolved.is_relative_to((base / lease_id).resolve(strict=False)):
                raise runtime.RuntimePolicyError("图像越界", "不能读取其他任务的参考资料或观察附件", error_type="IMAGE_SCOPE_DENIED")
            return resolved
    scope = Path(str(lease.get("scope_root") or lease["workspace"])).resolve(strict=False)
    if not resolved.is_relative_to(scope):
        raise runtime.RuntimePolicyError("图像越界", "图像需在当前项目或本任务附件目录内", error_type="IMAGE_SCOPE_DENIED")
    return resolved


def decode_image(data: bytes) -> tuple[dict[str, Any], bytes, str]:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图像为空或超过 25 MB 限制")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                width, height = source.size
                image_format = source.format
                if image_format not in _SUPPORTED or width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("仅支持 PNG/JPEG/WebP/GIF/BMP 位图（最多 1600 万像素）")
                source.verify()
            with Image.open(io.BytesIO(data)) as source:
                source.seek(0)
                source.load()
                image = source.convert("RGBA" if "A" in source.getbands() else "RGB")
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, OSError) as exc:
        raise ValueError("文件不是可解码的受支持图像") from exc
    source_mime = _SUPPORTED[image_format]
    delivered, delivery_mime = data, source_mime
    if image_format in {"BMP", "GIF"}:
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        delivered, delivery_mime = stream.getvalue(), "image/png"
    if len(delivered) > MAX_DELIVERY_BYTES:
        image.thumbnail((2048, 2048))
        if image.mode == "RGBA":
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            image = background
        stream = io.BytesIO()
        image.convert("RGB").save(stream, format="JPEG", quality=88)
        delivered, delivery_mime = stream.getvalue(), "image/jpeg"
    if len(delivered) > MAX_DELIVERY_BYTES:
        raise ValueError("压缩后的视觉输入仍过大；请使用更小的截图或参考图")
    meta = {"mime": source_mime, "size": len(data), "width": width, "height": height,
            "sha256": hashlib.sha256(data).hexdigest(), "delivery_mime": delivery_mime,
            "delivery_size": len(delivered), "delivery_width": image.width, "delivery_height": image.height,
            "delivery_transformed": delivered != data}
    return meta, delivered, delivery_mime


def read_image(path: Path) -> tuple[dict[str, Any], bytes, str]:
    with path.open("rb") as handle:
        return decode_image(handle.read(MAX_IMAGE_BYTES + 1))


def _png_bytes(image: Image.Image) -> bytes:
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _physical_window_bounds(hwnd: int, user32: Any, dwmapi: Any, ctypes_module: Any, wintypes_module: Any) -> tuple[tuple[int, int, int, int], str]:
    """Return active-window bounds in physical screen pixels.

    ``GetWindowRect`` is DPI-virtualized for DPI-unaware callers while Pillow's
    Windows ImageGrab operates on physical desktop pixels. Prefer DWM's
    extended frame bounds because Microsoft defines them as screen-space
    coordinates that are not DPI-adjusted. If DWM is unavailable, temporarily
    make only the current thread per-monitor-v2 aware while querying the rect.
    """
    rect_type = wintypes_module.RECT
    dwm_get = getattr(dwmapi, "DwmGetWindowAttribute", None) if dwmapi is not None else None
    if dwm_get is not None:
        try:
            dwm_get.argtypes = [wintypes_module.HWND, wintypes_module.DWORD, ctypes_module.c_void_p, wintypes_module.DWORD]
            dwm_get.restype = ctypes_module.c_long
            rect = rect_type()
            # DWMWA_EXTENDED_FRAME_BOUNDS = 9. These coordinates are physical
            # screen pixels and are not subject to caller DPI virtualization.
            result = dwm_get(hwnd, 9, ctypes_module.byref(rect), ctypes_module.sizeof(rect))
            if result == 0 and rect.right > rect.left and rect.bottom > rect.top:
                return (rect.left, rect.top, rect.right, rect.bottom), "dwm_extended_frame_bounds"
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    set_context = getattr(user32, "SetThreadDpiAwarenessContext", None)
    previous_context = None
    if set_context is not None:
        try:
            set_context.argtypes = [wintypes_module.HANDLE]
            set_context.restype = wintypes_module.HANDLE
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (HANDLE)-4.
            previous_context = set_context(ctypes_module.c_void_p(-4))
        except (AttributeError, OSError, TypeError, ValueError):
            previous_context = None
    try:
        rect = rect_type()
        if not user32.GetWindowRect(hwnd, ctypes_module.byref(rect)):
            raise RuntimeError("无法读取活动窗口边界")
        if rect.right <= rect.left or rect.bottom <= rect.top:
            raise RuntimeError("活动窗口边界无效")
        source = "get_window_rect_per_monitor_v2" if previous_context else "get_window_rect"
        return (rect.left, rect.top, rect.right, rect.bottom), source
    finally:
        if previous_context and set_context is not None:
            try:
                set_context(previous_context)
            except (OSError, TypeError, ValueError):
                pass


def capture_screenshot(
    target: str,
    *,
    browser_url: str | None = None,
    width: int | None = None,
    height: int | None = None,
    full_page: bool = False,
    timeout_ms: int = 20_000,
) -> tuple[bytes, dict[str, Any]]:
    """Capture one real bitmap without creating another Runtime/Job/Agent.

    ``browser`` launches a short-lived headless Edge page at the requested
    viewport. ``active_window`` and ``desktop`` read the interactive Windows
    session directly through Pillow. The caller owns persistence and activity
    publication so this primitive cannot accidentally become Console input.
    """
    clean_target = str(target or "").strip().lower()
    if clean_target not in {"browser", "active_window", "desktop"}:
        raise ValueError("target 仅支持 browser、active_window、desktop")
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1_000 <= timeout_ms <= 60_000:
        raise ValueError("timeout_ms 必须为 1000..60000 的整数")

    if clean_target == "browser":
        from playwright.sync_api import sync_playwright

        url = str(browser_url or "").strip()
        if not url or len(url) > 4096 or not url.lower().startswith(("http://", "https://", "file://")):
            raise ValueError("browser target 需要有效的 http/https/file browser_url")
        viewport_width = 1440 if width is None else width
        viewport_height = 900 if height is None else height
        if any(isinstance(value, bool) or not isinstance(value, int) or not 200 <= value <= 4096
               for value in (viewport_width, viewport_height)):
            raise ValueError("browser width/height 必须为 200..4096 的整数")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                context = browser.new_context(
                    viewport={"width": viewport_width, "height": viewport_height},
                    device_scale_factor=1,
                    reduced_motion="reduce",
                )
                try:
                    page = context.new_page()
                    response = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                    data = page.screenshot(type="png", full_page=bool(full_page), animations="disabled")
                    capture = {
                        "capture_type": "browser",
                        "browser_url": page.url,
                        "page_title": page.title()[:240],
                        "http_status": response.status if response is not None else None,
                        "viewport_width": viewport_width,
                        "viewport_height": viewport_height,
                        "full_page": bool(full_page),
                    }
                finally:
                    context.close()
            finally:
                browser.close()
        return data, capture

    from PIL import ImageGrab

    if clean_target == "desktop":
        image = ImageGrab.grab(all_screens=True)
        return _png_bytes(image), {"capture_type": "desktop", "all_screens": True}

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    dwmapi = getattr(ctypes.windll, "dwmapi", None)
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        raise RuntimeError("当前没有可抓取的活动窗口")
    title_length = user32.GetWindowTextLengthW(hwnd)
    title_buffer = ctypes.create_unicode_buffer(title_length + 1)
    user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
    bbox, bounds_source = _physical_window_bounds(hwnd, user32, dwmapi, ctypes, wintypes)
    window_dpi = None
    get_dpi = getattr(user32, "GetDpiForWindow", None)
    if get_dpi is not None:
        try:
            get_dpi.argtypes = [wintypes.HWND]
            get_dpi.restype = wintypes.UINT
            value = int(get_dpi(hwnd))
            window_dpi = value if value > 0 else None
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    image = ImageGrab.grab(bbox=bbox, all_screens=True)
    return _png_bytes(image), {
        "capture_type": "active_window",
        "window_handle": int(hwnd),
        "window_title": title_buffer.value[:240],
        "window_bounds": {"left": bbox[0], "top": bbox[1], "right": bbox[2], "bottom": bbox[3]},
        "window_bounds_source": bounds_source,
        "window_dpi": window_dpi,
    }


def persist_media_image(directory: Path, data: bytes, *, prefix: str = "media", label: str = "") -> dict[str, Any]:
    directory = directory.resolve(strict=False)
    if not directory.is_relative_to(runtime.MEDIA_ROOT.resolve(strict=False)):
        raise runtime.RuntimePolicyError("媒体目录越界", f"免租约媒体文件只能写入 {runtime.MEDIA_ROOT}", error_type="MEDIA_SCOPE_DENIED")
    metadata, _delivery, _mime = decode_image(data)
    directory.mkdir(parents=True, exist_ok=True)
    identity = prefix.strip()[:32] + "-" + uuid.uuid4().hex
    stamp = runtime.utc_now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"{stamp}-{identity}{_EXTENSIONS[metadata['mime']]}"
    with path.open("xb") as handle:
        handle.write(data)
    return {"id": identity, "kind": "image", "path": str(path), "name": label.strip()[:240] or path.name,
            "label": label.strip()[:240], "created_at": runtime.utc_iso(), **metadata}


def _visual_directory_bytes(directory: Path) -> int:
    if not directory.exists():
        return 0
    total = 0
    for path in directory.iterdir():
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _prune_observation_storage(directory: Path, *, keep: Path) -> dict[str, int]:
    """Keep Visual loops unbounded while bounding retained screenshot storage."""
    files: list[tuple[int, str, Path, int]] = []
    total = 0
    if directory.exists():
        for path in directory.iterdir():
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            size = int(stat.st_size)
            total += size
            files.append((int(stat.st_mtime_ns), path.name, path, size))
    removed = 0
    removed_bytes = 0
    for _mtime, _name, path, size in sorted(files):
        if total <= MAX_VISUAL_DISK_BYTES:
            break
        if path == keep:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed += 1
        removed_bytes += size
    return {"retained_bytes": total, "pruned_files": removed, "pruned_bytes": removed_bytes}


def persist_image(lease_id: str, kind: str, data: bytes, *, label: str = "") -> dict[str, Any]:
    if kind not in {"references", "observations"}:
        raise ValueError("图片类型必须为 references 或 observations")
    runtime.require_execute_lease(lease_id, [])
    metadata, _delivery, _mime = decode_image(data)
    visual_root = runtime.VISUAL_DIR / lease_id
    reference_root = runtime.MEDIA_REFERENCE_DIR / lease_id
    identity = ("ref-" if kind == "references" else "obs-") + uuid.uuid4().hex
    directory = reference_root if kind == "references" else visual_root / "observations"
    if kind == "references" and _visual_directory_bytes(reference_root) + len(data) > MAX_VISUAL_DISK_BYTES:
        raise ValueError("该任务参考图片已达到 256 MB 保留上限；请移除不再需要的参考图")
    path = directory / (identity + _EXTENSIONS[metadata["mime"]])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
    retention = _prune_observation_storage(directory, keep=path) if kind == "observations" else {}
    return {"id": identity, "kind": "image", "path": str(path), "name": label.strip()[:240] or path.name,
            "label": label.strip()[:240], "created_at": runtime.utc_iso(), **metadata, **retention}


def mount_references(lease: dict[str, Any], references: list[dict[str, Any]], attachment_dir: Path | None = None) -> list[dict[str, Any]]:
    if len(references) > 16:
        raise ValueError("最多挂载 16 项参考资料")
    existing = {item["id"]: item for item in runtime.get_visual_context(lease["lease_id"])["visual_mode"].get("references", [])}
    mounted = []
    for reference in references:
        if not isinstance(reference, dict):
            raise ValueError("参考资料必须是对象")
        identity = reference.get("id")
        if identity is not None:
            if identity not in existing:
                raise ValueError("引用的参考资料不属于本任务")
            mounted.append(existing[identity])
        elif reference.get("text") is not None:
            text = reference["text"]
            if not isinstance(text, str) or not text.strip() or len(text) > 8000:
                raise ValueError("文本参考需为 1 至 8000 字符")
            mounted.append({"id": "ref-" + uuid.uuid4().hex, "kind": "text", "text": text,
                            "name": str(reference.get("label") or "文字参考")[:240]})
        else:
            path = owned_image_path(str(reference.get("path") or ""), lease, attachment_dir)
            with path.open("rb") as handle:
                data = handle.read(MAX_IMAGE_BYTES + 1)
            mounted.append(persist_image(lease["lease_id"], "references", data, label=str(reference.get("label") or path.name)))
    return mounted
