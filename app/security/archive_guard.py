"""ZIP reader that never extracts entries to the filesystem."""

from __future__ import annotations

import io
import zipfile
from pathlib import PurePosixPath

from app.config.defaults import (
    ZIP_MAX_COMPRESSION_RATIO,
    ZIP_MAX_FILES,
    ZIP_MAX_SINGLE_FILE_MB,
    ZIP_MAX_TOTAL_UNCOMPRESSED_MB,
)
from app.errors import ErrorCode, SearchError


def read_safe_zip(payload: bytes) -> dict[str, bytes]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        raise SearchError(ErrorCode.DOCUMENT_PARSE_FAILED, "원문 응답이 안전한 ZIP 형식이 아닙니다.") from exc
    infos = archive.infolist()
    if len(infos) > ZIP_MAX_FILES:
        raise SearchError(ErrorCode.DOCUMENT_PARSE_FAILED, "ZIP 내부 파일 수 제한을 초과했습니다.")
    total = 0
    output: dict[str, bytes] = {}
    for info in infos:
        path = PurePosixPath(info.filename.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise SearchError(ErrorCode.DOCUMENT_PARSE_FAILED, "ZIP 경로 이탈 항목을 차단했습니다.")
        if info.is_dir():
            continue
        if info.file_size > ZIP_MAX_SINGLE_FILE_MB * 1024 * 1024:
            raise SearchError(ErrorCode.DOCUMENT_PARSE_FAILED, "ZIP 단일 파일 크기 제한을 초과했습니다.")
        total += info.file_size
        if total > ZIP_MAX_TOTAL_UNCOMPRESSED_MB * 1024 * 1024:
            raise SearchError(ErrorCode.DOCUMENT_PARSE_FAILED, "ZIP 총 압축해제 크기 제한을 초과했습니다.")
        ratio = info.file_size / max(1, info.compress_size)
        if ratio > ZIP_MAX_COMPRESSION_RATIO:
            raise SearchError(ErrorCode.DOCUMENT_PARSE_FAILED, "비정상적인 ZIP 압축률을 차단했습니다.")
        output[str(path)] = archive.read(info)
    return output
