import struct
from binascii import crc32
from hashlib import sha256
from io import BytesIO
import unittest
from unittest.mock import patch

from PIL import Image

from app.sprite_validation import (
    MAX_DECODE_PIXELS,
    MAX_FILE_BYTES,
    STATES,
    validate_png,
    split_template,
)


def png_bytes(width, height, color="red"):
    image = Image.new("RGBA", (width, height), color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def apng_bytes(width=2, height=2):
    first = Image.new("RGBA", (width, height), "red")
    second = Image.new("RGBA", (width, height), "blue")
    output = BytesIO()
    first.save(output, format="PNG", save_all=True, append_images=[second], duration=80, loop=0)
    return output.getvalue()


def insert_single_frame_apng_marker(data):
    """Add valid acTL/fcTL chunks to a normal PNG.

    Pillow 12 reports this one-frame APNG as non-animated, so the validator
    must reject the APNG marker before relying on Pillow's frame metadata.
    """
    output = bytearray(data[:8])
    offset = 8

    def chunk(chunk_type, payload):
        return (
            struct.pack(">I", len(payload))
            + chunk_type
            + payload
            + struct.pack(">I", crc32(chunk_type + payload) & 0xFFFFFFFF)
        )

    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        chunk_type = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        if chunk_type == b"IDAT":
            output.extend(chunk(b"acTL", struct.pack(">II", 1, 0)))
            output.extend(chunk(b"fcTL", struct.pack(">IIIIIHHBB", 0, 2, 2, 0, 0, 1, 10, 0, 0)))
        output.extend(data[offset : offset + length + 12])
        offset += length + 12
    return bytes(output)


def corrupt_idat_crc(data):
    result = bytearray(data)
    offset = 8
    while offset + 12 <= len(result):
        length = struct.unpack_from(">I", result, offset)[0]
        chunk_type = bytes(result[offset + 4 : offset + 8])
        if chunk_type == b"IDAT":
            result[offset + 8 + length] ^= 1
            return bytes(result)
        offset += length + 12
    raise AssertionError("test PNG has no IDAT chunk")


class SpriteValidationTests(unittest.TestCase):
    def test_template_preserves_row_column_order_and_alpha_at_other_sizes(self):
        for columns in (4, 8, 12):
            for size in [(columns * 64, 320), (columns * 128 + 2, 642)]:
                with self.subTest(size=size, columns=columns):
                    image = Image.new('RGBA', size)
                    for row in range(5):
                        for column in range(columns):
                            color = (row * 40, column * 20, 123, 160)
                            image.paste(color, (round(column * size[0] / columns), round(row * size[1] / 5),
                                                round((column + 1) * size[0] / columns), round((row + 1) * size[1] / 5)))
                    output = BytesIO(); image.save(output, format='PNG'); image.close()
                    sheets = split_template(output.getvalue(), 8, columns)
                    if columns == 8:
                        self.assertEqual(split_template(output.getvalue(), 8), sheets)
                    self.assertEqual(tuple(sheets), STATES)
                    for row, state in enumerate(STATES):
                        validate_png(sheets[state], 64, 64, columns, 8)
                        with Image.open(BytesIO(sheets[state])) as strip:
                            for column in range(columns):
                                self.assertEqual(strip.getpixel((column * 64 + 32, 32)), (row * 40, column * 20, 123, 160))

    def test_template_edge_cleanup_only_removes_supported_shallow_cross_row_spill(self):
        for columns in (4, 8, 12):
            with self.subTest(columns=columns):
                image = Image.new('RGBA', (columns * 64, 320))
                for column in range(columns):
                    x = column * 64
                    image.paste('black', (x + 20, 63, x + 28, 64))
                    image.paste('red', (x + 18, 80, x + 38, 105))
                image.paste('black', (22, 64, 26, 66))  # supported shallow fragment
                image.paste('blue', (42, 90, 50, 100))  # separate prop must survive
                image.paste('black', (64 + 42, 64, 64 + 48, 66))  # unsupported x
                image.paste('black', (128 + 20, 64, 128 + 28, 81))  # connected to body
                image.paste('black', (192 + 20, 64, 192 + 28, 69))  # deeper than four pixels
                image.paste('black', (22, 0, 26, 2))  # first row has no predecessor
                output = BytesIO(); image.save(output, format='PNG'); image.close()
                # Alignment would move the fixture's pixels; this test only covers the cleanup rule.
                original = split_template(output.getvalue(), 8, columns, align=False)
                cleaned = split_template(output.getvalue(), 8, columns, True, align=False)
                with Image.open(BytesIO(original['thinking'])) as raw, Image.open(BytesIO(cleaned['thinking'])) as result:
                    self.assertEqual(raw.getpixel((23, 0))[3], 255)
                    self.assertEqual(result.getpixel((23, 0))[3], 0)
                    for point in [(25, 25), (45, 30), (64 + 44, 0), (128 + 23, 0), (192 + 23, 0)]:
                        self.assertEqual(result.getpixel(point), raw.getpixel(point))
                self.assertEqual(cleaned['idle'], original['idle'])
        for columns in (3, 13, True):
            with self.assertRaises(ValueError): split_template(output.getvalue(), 8, columns)
        with self.assertRaises(ValueError): split_template(output.getvalue(), 8, 12, 'true')
        with self.assertRaises(ValueError): split_template(png_bytes(256, 320, (1,2,3,0)), 8, 8)

    def test_template_alignment_stops_sideways_drift_but_keeps_jump_height(self):
        columns = 8
        lift = [0, 2, 6, 10, 6, 2, 0, 0]
        image = Image.new('RGBA', (columns * 64, 320))
        for row in range(5):
            for column in range(columns):
                if row == 1 and column == 5:
                    continue  # an empty cell must survive untouched
                left = 4 + 3 * column  # drifts 3px per cell, like an AI-drawn sheet
                bottom = 51 - lift[column] if row == 2 else 46 + column % 3
                image.paste('red', (column * 64 + left, row * 64 + bottom - 20, column * 64 + left + 16, row * 64 + bottom))
        output = BytesIO(); image.save(output, format='PNG'); image.close()
        sheets = split_template(output.getvalue(), 8, columns)
        raw = split_template(output.getvalue(), 8, columns, align=False)
        for row, state in enumerate(STATES):
            with Image.open(BytesIO(sheets[state])) as strip, Image.open(BytesIO(raw[state])) as before:
                bottoms = []
                for column in range(columns):
                    frame = strip.crop((column * 64, 0, column * 64 + 64, 64)).getchannel('A')
                    if (row, column) == (1, 5):
                        self.assertIsNone(frame.getbbox())
                        continue
                    left, top, right, bottom = frame.getbbox()
                    self.assertEqual(right - left, 16, "artwork is moved, never resized or cropped")
                    self.assertEqual(left + right, 64, f"{state} frame {column} must be centred")
                    bottoms.append(bottom - 1)
                    if state in ('idle', 'thinking', 'encourage'):
                        self.assertEqual(bottom - 1, 59, f"{state} frame {column} must stand on the ground line")
                if state == 'happy':
                    original = [51 - value - 1 for value in lift]
                    self.assertEqual(max(bottoms), 59)
                    self.assertEqual([value - max(bottoms) for value in bottoms],
                                     [value - max(original) for value in original], "jump heights are preserved")
        self.assertNotEqual(sheets['idle'], raw['idle'])

    def test_template_alignment_ignores_small_neighbour_spill_but_never_crops_artwork(self):
        image = Image.new('RGBA', (8 * 64, 320))
        image.paste('red', (30, 20, 46, 40))  # body right of centre
        image.paste('black', (2, 10, 4, 12))  # stray spill from the neighbouring cell
        image.paste('red', (64, 20, 64 + 16, 40))  # a tail touching the left edge ...
        image.paste('red', (64 + 40, 20, 64 + 64, 60))  # ... with the body far to the right
        output = BytesIO(); image.save(output, format='PNG'); image.close()
        with Image.open(BytesIO(split_template(output.getvalue(), 8)['idle'])) as strip:
            first = strip.crop((0, 0, 64, 64)).getchannel('A').getbbox()
            self.assertEqual((first[0], first[2]), (24, 40), "the stray pixels are pushed out instead of pinning the frame")
            second = strip.crop((64, 0, 128, 64)).getchannel('A')
            before = 16 * 20 + 24 * 40
            self.assertGreaterEqual(sum(1 for value in second.tobytes() if value), before - before // 16,
                                    "at most a sliver of pixels may be pushed out")
            self.assertEqual(second.getbbox()[2], 60, "the shift stops once real artwork would be cropped")

    def test_template_rejects_wrong_layout_opaque_corrupt_apng_and_limits(self):
        for data in [png_bytes(256, 256), png_bytes(256, 448), png_bytes(256, 320),
                     png_bytes(2052, 2565, (0, 0, 0, 0)), b'bad',
                     corrupt_idat_crc(png_bytes(256, 320, (1, 2, 3, 0))),
                     apng_bytes(), b'x' * (MAX_FILE_BYTES + 1)]:
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                split_template(data, 8)
        with self.assertRaises(ValueError): split_template(png_bytes(256, 320, (1, 2, 3, 0)), 31)

    def test_states_and_valid_single_and_horizontal_sheets(self):
        self.assertEqual(STATES, ("idle", "thinking", "happy", "mail", "encourage"))
        for width, height, count, fps in [(3, 4, 1, 1), (2, 5, 4, 30), (512, 512, 1, 30)]:
            with self.subTest(width=width, height=height, count=count):
                data = png_bytes(width * count, height)
                result = validate_png(data, width, height, count, fps)
                self.assertEqual(result["format"], "PNG")
                self.assertEqual(result["width"], width * count)
                self.assertEqual(result["height"], height)
                self.assertEqual(result["size"], len(data))
                self.assertEqual(result["sha256"], sha256(data).hexdigest())
                self.assertEqual(result["mode"], "RGBA")

    def test_rejects_non_integer_and_out_of_range_parameters(self):
        data = png_bytes(1, 1)
        cases = [
            (True, 1, 1, 1),
            (1, False, 1, 1),
            (1, 1, True, 1),
            (1, 1, 1, False),
            (0, 1, 1, 1),
            (513, 1, 1, 1),
            (1, 0, 1, 1),
            (1, 513, 1, 1),
            (1, 1, 0, 1),
            (1, 1, 121, 1),
            (1, 1, 1, 0),
            (1, 1, 1, 31),
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_png(data, *args)

    def test_rejects_size_mismatch_non_png_crc_and_truncated(self):
        data = png_bytes(4, 3)
        cases = [
            (data, (3, 3, 1, 10)),
            (b"not a PNG", (1, 1, 1, 10)),
            (corrupt_idat_crc(data), (4, 3, 1, 10)),
            (data[:-8], (4, 3, 1, 10)),
        ]
        for content, args in cases:
            with self.subTest(size=len(content)), self.assertRaises(ValueError):
                validate_png(content, *args)

    def test_rejects_oversize_and_resource_shape_before_load(self):
        with self.assertRaises(ValueError):
            validate_png(b"x" * (MAX_FILE_BYTES + 1), 1, 1, 1, 1)
        data = png_bytes(2, 2)
        with patch("app.sprite_validation.MAX_DECODE_PIXELS", 1):
            with self.assertRaises(ValueError):
                validate_png(data, 2, 2, 1, 1)
        self.assertEqual(MAX_DECODE_PIXELS, 512 * 512 * 120)

    def test_rejects_pillow_bomb_warning_without_changing_global_limit(self):
        data = png_bytes(2, 2)
        previous_limit = Image.MAX_IMAGE_PIXELS
        with patch.object(Image, "MAX_IMAGE_PIXELS", 1):
            with self.assertRaises(ValueError):
                validate_png(data, 2, 2, 1, 1)
        self.assertEqual(Image.MAX_IMAGE_PIXELS, previous_limit)

    def test_rejects_multi_frame_and_single_frame_apng(self):
        with self.assertRaises(ValueError):
            validate_png(apng_bytes(), 2, 2, 1, 10)
        with self.assertRaises(ValueError):
            validate_png(insert_single_frame_apng_marker(png_bytes(2, 2)), 2, 2, 1, 10)

    def test_rejects_non_bytes_and_keeps_errors_safe(self):
        with self.assertRaisesRegex(ValueError, "PNG 資料") as raised:
            validate_png(bytearray(png_bytes(1, 1)), 1, 1, 1, 1)
        self.assertNotIn("bytearray", str(raised.exception))
        with self.assertRaises(ValueError) as raised:
            validate_png(b"\x89PNG\r\n\x1a\n" + b"x" * 40, 1, 1, 1, 1)
        self.assertTrue(str(raised.exception).startswith("PNG ") or str(raised.exception).startswith("不支援"))


if __name__ == "__main__":
    unittest.main()
