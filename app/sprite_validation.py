"""Pure PNG validation and fixed-layout character template splitting."""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import struct
import warnings

from PIL import Image


STATES = ("idle", "thinking", "happy", "mail", "encourage")
TEMPLATE_COLUMNS = 8
TEMPLATE_FRAME_SIZE = 64
_STATIONARY_STATES = frozenset({"idle", "thinking", "encourage"})
_GROUND_ROW = TEMPLATE_FRAME_SIZE - 5  # last visible row; keeps the 4px transparent margin below
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_DECODE_PIXELS = 512 * 512 * 120
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_APNG_CHUNKS = {b"acTL", b"fcTL"}


class _ValidationFailure(ValueError):
    """A safe, already-localized validation failure."""


def _fail(message: str) -> None:
    raise _ValidationFailure(message)


def _validate_int(value: object, name: str, lower: int, upper: int) -> None:
    if type(value) is not int:
        _fail(f"{name} 必須是整數。")
    if not lower <= value <= upper:
        _fail(f"{name} 必須介於 {lower}～{upper}。")


def _contains_apng_chunk(data: bytes) -> bool:
    """Detect APNG control chunks without attempting to decode PNG data."""
    if not data.startswith(_PNG_SIGNATURE):
        return False
    offset = len(_PNG_SIGNATURE)
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            return False
        if chunk_type in _APNG_CHUNKS:
            return True
        offset = chunk_end
        if chunk_type == b"IEND":
            break
    return False


def _check_shape(image: Image.Image, expected_size: tuple[int, int] | None) -> None:
    width, height = image.size
    if expected_size is not None and image.size != expected_size:
        _fail("PNG 圖片尺寸不符。")
    if width * height > MAX_DECODE_PIXELS:
        _fail("PNG 像素數超過解碼上限。")


def _check_single_frame(image: Image.Image) -> None:
    if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
        _fail("不支援 APNG 或多幀 PNG。")


def _decode_png(data: bytes, expected_size: tuple[int, int] | None = None) -> Image.Image:
    """Verify before decoding; consume bytes only, without changing Pillow limits."""
    if not isinstance(data, bytes):
        _fail("PNG 資料必須是 bytes。")
    if len(data) > MAX_FILE_BYTES:
        _fail("PNG 檔案不可超過 5 MiB。")
    if not data.startswith(_PNG_SIGNATURE):
        _fail("PNG 格式不符。")
    if _contains_apng_chunk(data):
        _fail("不支援 APNG 或多幀 PNG。")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=["PNG"]) as image:
                if image.format != "PNG":
                    _fail("PNG 格式不符。")
                _check_single_frame(image)
                _check_shape(image, expected_size)
                image.verify()

            with Image.open(BytesIO(data), formats=["PNG"]) as image:
                if image.format != "PNG":
                    _fail("PNG 格式不符。")
                _check_single_frame(image)
                _check_shape(image, expected_size)
                image.load()
                return image.copy()
    except _ValidationFailure:
        raise
    except Image.DecompressionBombError:
        raise ValueError("PNG 解碼資源超過上限。") from None
    except Image.DecompressionBombWarning:
        raise ValueError("PNG 解碼資源超過上限。") from None
    except Exception:
        raise ValueError("PNG 內容無法解碼。") from None


def validate_png(data: bytes, frame_width: int, frame_height: int, frame_count: int, fps: int) -> dict[str, object]:
    """Validate and describe one horizontal PNG sprite sheet."""
    _validate_int(frame_width, "影格寬度", 1, 512)
    _validate_int(frame_height, "影格高度", 1, 512)
    _validate_int(frame_count, "影格數量", 1, 120)
    _validate_int(fps, "播放速度", 1, 30)
    with _decode_png(data, (frame_width * frame_count, frame_height)) as image:
        width, height = image.size
        mode = image.mode
    return {
        "format": "PNG",
        "width": width,
        "height": height,
        "size": len(data),
        "sha256": sha256(data).hexdigest(),
        "mode": mode,
    }


def _clear_grid_edge_fragment(frame: Image.Image, previous: Image.Image) -> None:
    """RepoNPC's conservative cross-row spill check; preserve unsupported artwork."""
    side = frame.width
    alpha = frame.getchannel("A").tobytes()
    prior_edge = previous.getchannel("A").tobytes()[-side:]
    supported = {neighbor for x, value in enumerate(prior_edge) if value >= 64
                 for neighbor in (x - 1, x, x + 1) if 0 <= neighbor < side}
    if not supported:
        return
    max_height, gap = max(1, side // 16), max(1, side // 64)
    height = 0
    while height <= max_height and any(value >= 64 for value in alpha[height * side:(height + 1) * side]):
        height += 1
    if not 0 < height <= max_height or height + gap >= side:
        return
    if any(value >= 64 for value in alpha[height * side:(height + gap) * side]):
        return
    if not any(value >= 64 for value in alpha[(height + gap) * side:]):
        return
    if any(value >= 64 and x not in supported for y in range(height)
           for x, value in enumerate(alpha[y * side:(y + 1) * side])):
        return
    for y in range(height):
        for x in supported:
            if alpha[y * side + x]:
                frame.putpixel((x, y), (0, 0, 0, 0))


def _frame_profile(frame: Image.Image) -> tuple[list[int], list[int], float] | None:
    """Return per-column counts, per-row counts and centroid x of pixels with alpha >= 64, or None if empty.

    Faint alpha (compression noise, soft shadows) is ignored so it cannot pin a frame in place.
    """
    side = frame.width
    mask = frame.getchannel("A").point(lambda value: 255 if value >= 64 else 0).tobytes()
    columns, rows = [0] * side, [0] * side
    for y in range(side):
        for x, value in enumerate(mask[y * side:(y + 1) * side]):
            if value:
                columns[x] += 1
                rows[y] += 1
    total = sum(columns)
    if not total:
        return None
    return columns, rows, sum(x * count for x, count in enumerate(columns)) / total


def _clamp_shift(shift: int, counts: list[int]) -> int:
    """Limit a shift so it pushes out at most 1/16 of the visible pixels (stray spill from neighbouring cells)."""
    budget, lost, moved = sum(counts) // 16, 0, 0
    while moved < abs(shift):
        edge = counts[len(counts) - 1 - moved] if shift > 0 else counts[moved]
        if lost + edge > budget:
            break
        lost, moved = lost + edge, moved + 1
    return moved if shift > 0 else -moved


def _align_frames(frames: list[Image.Image], state: str) -> list[Image.Image]:
    """Pin each frame's body to the cell centre and the shared ground line.

    AI-drawn templates drift a few pixels per cell, which plays back as a sliding sprite.
    Horizontal centroid is aligned for every state; stationary states also put each frame's lowest
    row on the ground, while happy/mail share one vertical shift so jumps keep their height.
    Shifts never push out more than a sliver of stray pixels, so real artwork is not cropped.
    """
    side = TEMPLATE_FRAME_SIZE
    profiles = [_frame_profile(frame) for frame in frames]
    bottoms = [max(y for y, count in enumerate(p[1]) if count) for p in profiles if p]
    if not bottoms:
        return frames
    row_wanted = _GROUND_ROW - max(bottoms)
    row_shift = None
    if state not in _STATIONARY_STATES:
        shifts = [_clamp_shift(row_wanted, p[1]) for p in profiles if p]
        row_shift = min(shifts, key=abs)
    aligned = []
    for frame, profile in zip(frames, profiles):
        if not profile:
            aligned.append(frame)
            continue
        columns, rows, centroid = profile
        dx = _clamp_shift(round((side - 1) / 2 - centroid), columns)
        if row_shift is None:
            dy = _clamp_shift(_GROUND_ROW - max(y for y, count in enumerate(rows) if count), rows)
        else:
            dy = row_shift
        if not dx and not dy:
            aligned.append(frame)
            continue
        shifted = Image.new("RGBA", frame.size)
        shifted.paste(frame, (dx, dy))
        frame.close()
        aligned.append(shifted)
    return aligned


def split_template(data: bytes, fps: int, columns: int = TEMPLATE_COLUMNS, clean_edges: bool = False,
                   align: bool = True) -> dict[str, bytes]:
    """Map explicit frame columns and five state rows to horizontal sheets."""
    _validate_int(fps, "播放速度", 1, 30)
    _validate_int(columns, "每個動作影格數", 4, 12)
    if type(clean_edges) is not bool:
        _fail("邊緣清理設定必須是布林值。")
    with _decode_png(data) as image:
        width, height = image.size
        rows = len(STATES)
        if not (columns <= width <= columns * 512 and rows <= height <= 2560):
            _fail(f"模板尺寸須介於 {columns}×5 與 {columns * 512}×2560 px。")
        # AI exports can be off by a few pixels; allow 1% aspect error, without guessing a layout.
        if abs(width * rows / (height * columns) - 1) > .01:
            _fail(f"模板須為 {columns} 欄×5 列正方形影格，寬高比需接近 {columns}:5；請核對影格數並移除外框與格間空隙。")
        rgba = image.convert("RGBA")
        try:
            if rgba.getchannel("A").getextrema()[0] == 255:
                _fail("模板需有真正的透明背景；請先去背，不能使用白底或畫出的棋盤格。")
            result = {}
            for row, state in enumerate(STATES):
                frames = []
                for column in range(columns):
                    box = (round(column * width / columns), round(row * height / rows),
                           round((column + 1) * width / columns), round((row + 1) * height / rows))
                    with rgba.crop(box) as cell:
                        frame = cell.resize((TEMPLATE_FRAME_SIZE, TEMPLATE_FRAME_SIZE), Image.Resampling.NEAREST)
                    if clean_edges and row > 0:
                        previous_box = (box[0], round((row - 1) * height / rows), box[2], box[1])
                        with rgba.crop(previous_box) as previous_cell:
                            with previous_cell.resize((TEMPLATE_FRAME_SIZE, TEMPLATE_FRAME_SIZE), Image.Resampling.NEAREST) as previous:
                                _clear_grid_edge_fragment(frame, previous)
                    frames.append(frame)
                if align:
                    frames = _align_frames(frames, state)
                with Image.new("RGBA", (columns * TEMPLATE_FRAME_SIZE, TEMPLATE_FRAME_SIZE)) as strip:
                    for column, frame in enumerate(frames):
                        strip.paste(frame, (column * TEMPLATE_FRAME_SIZE, 0))
                        frame.close()
                    output = BytesIO()
                    strip.save(output, format="PNG")
                    result[state] = output.getvalue()
            return result
        finally:
            rgba.close()
