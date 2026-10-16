from __future__ import annotations

import html
import io
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.parse import parse_qs, urlparse

import requests
from PIL import Image, ImageDraw, ImageOps
from fastapi import HTTPException, UploadFile

MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_ICON_BYTES = 4 * 1024 * 1024
MAX_UNPACKED_BYTES = 250 * 1024 * 1024
ANDROID_NS = "http://schemas.android.com/apk/res/android"


def parse_play_store_url(value: str) -> str:
    """Return the package ID from a canonical Google Play details URL."""
    try:
        parsed = urlparse(value.strip())
    except ValueError as exc:
        raise ValueError("الرابط غير صالح.") from exc

    if parsed.scheme != "https" or parsed.hostname not in {"play.google.com", "www.play.google.com"}:
        raise ValueError("استخدم رابطًا رسميًا يبدأ بـ https://play.google.com/store/apps/details?id=...")
    if not parsed.path.startswith("/store/apps/details"):
        raise ValueError("الرابط ليس صفحة تفاصيل تطبيق على Google Play.")

    package_id = parse_qs(parsed.query).get("id", [""])[0]
    if not valid_package_id(package_id):
        raise ValueError("لم أجد معرّف تطبيق صالحًا داخل الرابط.")
    return package_id


def valid_package_id(value: str) -> bool:
    if not isinstance(value, str) or len(value) > 200:
        return False
    parts = value.split(".")
    return len(parts) >= 2 and all(re.fullmatch(r"[a-z][a-z0-9_]*", part) for part in parts)


def fetch_play_store_metadata(url: str) -> dict[str, str | None]:
    """Fetch public listing metadata only; never downloads the app binary."""
    package_id = parse_play_store_url(url)
    title: str | None = None
    description: str | None = None
    try:
        response = requests.get(
            url,
            timeout=(3.05, 7),
            headers={"User-Agent": "Mozilla/5.0 (compatible; AppForge/0.1; +https://github.com/)"},
        )
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        page = response.text[:2_000_000]
        match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)', page, re.I)
        if not match:
            match = re.search(r'<title[^>]*>(.*?)</title>', page, re.I | re.S)
        if match:
            title = html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()[:240] or None
        desc_match = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)', page, re.I)
        if desc_match:
            description = html.unescape(desc_match.group(1)).strip()[:700] or None
    except requests.RequestException:
        # Play Store can rate-limit automated requests; package ID is still useful.
        pass
    return {"package_id": package_id, "title": title, "description": description}


def _safe_extract(zf: zipfile.ZipFile, destination: Path) -> None:
    total_size = 0
    for info in zf.infolist():
        raw_name = info.filename.replace("\\", "/")
        member = PurePosixPath(raw_name)
        if member.is_absolute() or ".." in member.parts or not raw_name:
            raise ValueError("ملف ZIP يحتوي على مسار غير آمن.")
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if (unix_mode & 0o170000) == 0o120000:
            raise ValueError("الملفات التي تحتوي على روابط رمزية غير مدعومة.")
        total_size += info.file_size
        if total_size > MAX_UNPACKED_BYTES:
            raise ValueError("حجم الملفات بعد فك الضغط أكبر من الحد المسموح (250 ميغابايت).")
        target = (destination / Path(*member.parts)).resolve()
        if destination.resolve() not in target.parents and target != destination.resolve():
            raise ValueError("ملف ZIP يحاول الكتابة خارج مجلد المشروع.")
    zf.extractall(destination)


def _find_manifest(root: Path) -> Path:
    candidates = list(root.rglob("AndroidManifest.xml"))
    candidates = [p for p in candidates if p.as_posix().endswith("/src/main/AndroidManifest.xml")]
    if not candidates:
        raise ValueError("لم أجد AndroidManifest.xml داخل src/main. ارفع مشروع Android Gradle كاملًا، وليس APK فقط.")
    candidates.sort(key=lambda p: (0 if p.parent.parent.parent.name == "app" else 1, len(p.parts)))
    return candidates[0]


def _xml_escape(value: str) -> str:
    return html.escape(value, quote=False)


def _update_strings(manifest: Path, app_name: str) -> list[str]:
    warnings: list[str] = []
    text = manifest.read_text(encoding="utf-8", errors="replace")
    label_match = re.search(r'android:label\s*=\s*["\']@string/([^"\']+)["\']', text)
    key = label_match.group(1) if label_match else "app_name"
    module_dir = manifest.parents[2]
    res_dir = module_dir / "src" / "main" / "res"
    values_files = list(res_dir.glob("values*/strings.xml")) if res_dir.exists() else []
    updated = False

    for strings_file in values_files:
        content = strings_file.read_text(encoding="utf-8", errors="replace")
        pattern = re.compile(rf'(<string\s+name=["\']{re.escape(key)}["\'][^>]*>).*?(</string>)', re.S)
        if pattern.search(content):
            content = pattern.sub(lambda m: m.group(1) + _xml_escape(app_name) + m.group(2), content, count=1)
            strings_file.write_text(content, encoding="utf-8")
            updated = True

    default_strings = res_dir / "values" / "strings.xml"
    if not updated:
        default_strings.parent.mkdir(parents=True, exist_ok=True)
        if default_strings.exists():
            content = default_strings.read_text(encoding="utf-8", errors="replace")
            entry = f'<string name="{key}">{_xml_escape(app_name)}</string>'
            if re.search(rf'<string\s+name=["\']{re.escape(key)}["\']', content):
                content = re.sub(
                    rf'<string\s+name=["\']{re.escape(key)}["\'][^>]*>.*?</string>',
                    entry,
                    content,
                    count=1,
                    flags=re.S,
                )
            elif "</resources>" in content:
                content = content.replace("</resources>", f"    {entry}\n</resources>")
            else:
                content = f'<resources>\n    {entry}\n</resources>\n'
        else:
            content = f'<?xml version="1.0" encoding="utf-8"?>\n<resources>\n    <string name="{key}">{_xml_escape(app_name)}</string>\n</resources>\n'
        default_strings.write_text(content, encoding="utf-8")

    # If the manifest uses a literal label, replace only the application label attribute.
    if not label_match:
        application = re.search(r"<application\b[^>]*>", text, re.S)
        if application:
            app_tag = application.group(0)
            if re.search(r"android:label\s*=", app_tag):
                app_tag = re.sub(r'android:label\s*=\s*["\'][^"\']*["\']', 'android:label="@string/app_name"', app_tag, count=1)
            else:
                app_tag = app_tag[:-1] + ' android:label="@string/app_name">'
            text = text[:application.start()] + app_tag + text[application.end():]
            manifest.write_text(text, encoding="utf-8")
        else:
            warnings.append("لم أتمكن من العثور على عنصر application لتعيين اسم العرض.")
    return warnings


def _update_application_id(module_dir: Path, package_id: str) -> tuple[bool, str | None]:
    gradle_candidates = [module_dir / "build.gradle.kts", module_dir / "build.gradle"]
    gradle_file = next((p for p in gradle_candidates if p.is_file()), None)
    if gradle_file is None:
        return False, "لم أجد build.gradle أو build.gradle.kts لوحدة التطبيق؛ لم أغيّر معرّف الحزمة."
    content = gradle_file.read_text(encoding="utf-8", errors="replace")
    # Replace only applicationId (install identity); keep namespace/source packages unchanged.
    pattern = re.compile(r'(applicationId\s*(?:=\s*)?)["\'][^"\']+["\']')
    if pattern.search(content):
        content = pattern.sub(lambda m: f'{m.group(1)}"{package_id}"', content, count=1)
    else:
        default_config = re.search(r"defaultConfig\s*\{", content)
        if not default_config:
            return False, "لم أجد applicationId أو defaultConfig؛ لم أغيّر معرّف الحزمة لتجنب إفساد إعدادات البناء."
        line = '        applicationId = "' + package_id + '"\n' if gradle_file.suffix == ".kts" else '        applicationId "' + package_id + '"\n'
        insert_at = default_config.end()
        content = content[:insert_at] + "\n" + line + content[insert_at:]
    gradle_file.write_text(content, encoding="utf-8")
    return True, None


def _update_manifest_icon(manifest: Path) -> None:
    text = manifest.read_text(encoding="utf-8", errors="replace")
    application = re.search(r"<application\b[^>]*>", text, re.S)
    if not application:
        return
    tag = application.group(0)
    for attribute, value in (("icon", "@mipmap/ic_launcher"), ("roundIcon", "@mipmap/ic_launcher_round")):
        attr_pattern = re.compile(rf'android:{attribute}\s*=\s*(["\'])[^"\']*(["\'])')
        if attr_pattern.search(tag):
            tag = attr_pattern.sub(f'android:{attribute}="{value}"', tag, count=1)
        else:
            tag = tag[:-1] + f' android:{attribute}="{value}">'
    manifest.write_text(text[:application.start()] + tag + text[application.end():], encoding="utf-8")


def _apply_icon(module_dir: Path, icon_data: bytes) -> None:
    try:
        with Image.open(io.BytesIO(icon_data)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGBA")
    except Exception as exc:
        raise ValueError("ملف الأيقونة ليس صورة صالحة.") from exc
    if image.width < 64 or image.height < 64:
        raise ValueError("الأيقونة صغيرة جدًا؛ استخدم صورة لا تقل عن 64×64 بكسل.")
    if image.width > 8000 or image.height > 8000:
        raise ValueError("أبعاد الأيقونة كبيرة جدًا.")

    res = module_dir / "src" / "main" / "res"
    densities = {"mipmap-mdpi": 48, "mipmap-hdpi": 72, "mipmap-xhdpi": 96, "mipmap-xxhdpi": 144, "mipmap-xxxhdpi": 192}
    for directory, size in densities.items():
        target_dir = res / directory
        target_dir.mkdir(parents=True, exist_ok=True)
        resized = ImageOps.fit(image, (size, size), method=Image.Resampling.LANCZOS)
        resized.save(target_dir / "ic_launcher.png", format="PNG", optimize=True)
        round_icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        round_icon.paste(resized, (0, 0), resized)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        round_icon.putalpha(mask)
        round_icon.save(target_dir / "ic_launcher_round.png", format="PNG", optimize=True)

    # Prefer the generated legacy bitmap icons over source adaptive icon XML for these names.
    anydpi = res / "mipmap-anydpi-v26"
    for name in ("ic_launcher.xml", "ic_launcher_round.xml"):
        file = anydpi / name
        if file.exists():
            file.unlink()


def prepare_android_source(
    source_data: bytes,
    app_name: str,
    package_id: str,
    icon_data: bytes | None = None,
) -> tuple[Path, tempfile.TemporaryDirectory[str], list[str]]:
    """Customize a user-provided Android Gradle source project and return a prepared ZIP path."""
    app_name = app_name.strip()
    if not app_name or len(app_name) > 60 or any(ord(ch) < 32 for ch in app_name):
        raise ValueError("اسم التطبيق مطلوب ويجب ألا يتجاوز 60 حرفًا.")
    if not valid_package_id(package_id):
        raise ValueError("معرّف الحزمة غير صالح. مثال: com.example.myapp (أحرف صغيرة فقط).")
    if len(source_data) > MAX_SOURCE_BYTES:
        raise ValueError("حجم المصدر المضغوط أكبر من 50 ميغابايت.")
    if icon_data is not None and len(icon_data) > MAX_ICON_BYTES:
        raise ValueError("حجم الأيقونة يجب ألا يتجاوز 4 ميغابايت.")

    work = tempfile.TemporaryDirectory(prefix="appforge-")
    root = Path(work.name)
    source_zip = root / "source.zip"
    source_zip.write_bytes(source_data)
    project_dir = root / "project"
    project_dir.mkdir()
    warnings: list[str] = []

    try:
        with zipfile.ZipFile(source_zip) as zf:
            _safe_extract(zf, project_dir)
    except zipfile.BadZipFile as exc:
        work.cleanup()
        raise ValueError("الملف المرفوع ليس ZIP صالحًا.") from exc
    except Exception:
        work.cleanup()
        raise

    manifest = _find_manifest(project_dir)
    module_dir = manifest.parents[2]
    warnings.extend(_update_strings(manifest, app_name))
    changed, warning = _update_application_id(module_dir, package_id)
    if warning:
        warnings.append(warning)
    if icon_data:
        _apply_icon(module_dir, icon_data)
        _update_manifest_icon(manifest)
    if not changed:
        warnings.append("اسم العرض تم تجهيزه، لكن معرّف الحزمة قد يحتاج تعديلًا يدويًا قبل بناء التطبيق.")

    # Keep the original archive's top-level layout when possible; generated ZIP always contains source files only.
    output_zip = root / "appforge-source.zip"
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for path in project_dir.rglob("*"):
            if not path.is_file():
                continue
            if any(part in {".git", ".gradle", "build", "__pycache__"} for part in path.parts):
                continue
            out.write(path, path.relative_to(project_dir).as_posix())
    return output_zip, work, warnings
