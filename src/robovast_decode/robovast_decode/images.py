# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""One image message as pixels.

An image is bulk data (:data:`~robovast_decode.registry.BULK_TYPES`): it never becomes rows,
and what analysis wants of it is the array the camera produced, in the encoding's own type
-- ``uint8`` for ``rgb8``, ``uint16`` for a depth camera's ``16UC1``, ``float32`` for
``32FC1`` -- with the channels in the order the encoding names. :func:`decode` gives that
array for a ``sensor_msgs/msg/Image`` or a ``sensor_msgs/msg/CompressedImage``;
:func:`to_pil` turns such an array into a picture a person can look at, scaling a depth
image by its range, which is what the frame route and the run view show.
"""

from __future__ import annotations

import io
from typing import Tuple

import numpy as np
from PIL import Image as PILImage

IMAGE_TYPES = frozenset({"sensor_msgs/msg/CompressedImage", "sensor_msgs/msg/Image"})

#: A raw ``sensor_msgs/msg/Image`` encoding -> (numpy dtype code, channels). The names are
#: ``sensor_msgs/image_encodings.hpp``'s; an encoding not here is refused by name.
ENCODINGS = {
    "rgb8": ("u1", 3), "bgr8": ("u1", 3), "rgba8": ("u1", 4), "bgra8": ("u1", 4),
    "mono8": ("u1", 1), "8UC1": ("u1", 1), "8UC2": ("u1", 2), "8UC3": ("u1", 3), "8UC4": ("u1", 4),
    "8SC1": ("i1", 1), "8SC3": ("i1", 3),
    "rgb16": ("u2", 3), "bgr16": ("u2", 3), "rgba16": ("u2", 4), "bgra16": ("u2", 4),
    "mono16": ("u2", 1), "16UC1": ("u2", 1), "16UC3": ("u2", 3), "16SC1": ("i2", 1),
    "32SC1": ("i4", 1), "32FC1": ("f4", 1), "32FC3": ("f4", 3), "64FC1": ("f8", 1),
}

#: The channel order Pillow reads a three- or four-channel encoding in.
_PIL_RAW_MODES = {
    "rgb8": ("RGB", "RGB"), "bgr8": ("RGB", "BGR"), "8UC3": ("RGB", "BGR"),
    "rgba8": ("RGBA", "RGBA"), "bgra8": ("RGBA", "BGRA"), "8UC4": ("RGBA", "BGRA"),
}


def decode(msg, typename: str) -> Tuple[np.ndarray, str]:
    """``(pixels, encoding)`` of one image message.

    A raw ``Image`` is its buffer viewed in the encoding's dtype, ``(height, width)`` for one
    channel and ``(height, width, channels)`` otherwise, the row padding ``step`` declares
    dropped; nothing is copied but what the view needs. A ``CompressedImage`` is decoded by
    Pillow and reported in the encoding its mode amounts to (``rgb8``, ``rgba8``, ``mono8``,
    or ``16UC1`` for a 16-bit PNG, which is how a compressed depth image travels).
    ``ValueError`` names an encoding this cannot decode; there is no blank frame.
    """
    if typename == "sensor_msgs/msg/CompressedImage":
        return decode_compressed(bytes(msg.data))
    if typename != "sensor_msgs/msg/Image":
        raise ValueError(f"{typename} is not an image type")
    encoding = msg.encoding
    if encoding not in ENCODINGS:
        raise ValueError(f"cannot decode a sensor_msgs/msg/Image in encoding {encoding!r}")
    code, channels = ENCODINGS[encoding]
    dtype = np.dtype((">" if msg.is_bigendian else "<") + code)
    width, height, step = int(msg.width), int(msg.height), int(msg.step)
    data = msg.data if isinstance(msg.data, np.ndarray) else np.frombuffer(bytes(msg.data), np.uint8)
    rows = np.frombuffer(data.tobytes() if data.dtype != np.uint8 else data, dtype=np.uint8,
                         count=height * step).reshape(height, step)
    pixels = rows[:, :width * channels * dtype.itemsize].copy().view(dtype)
    if pixels.dtype.byteorder == ">":
        pixels = pixels.astype(pixels.dtype.newbyteorder("="))
    return (pixels.reshape(height, width) if channels == 1
            else pixels.reshape(height, width, channels)), encoding


def decode_compressed(data: bytes) -> Tuple[np.ndarray, str]:
    """``(pixels, encoding)`` of an encoded image (JPEG, PNG, ...), as :func:`decode` reports
    a ``CompressedImage``."""
    image = PILImage.open(io.BytesIO(data))
    image.load()
    if image.mode in ("I;16", "I;16B", "I;16L", "I"):
        return np.asarray(image, dtype=np.uint16), "16UC1"
    if image.mode == "L":
        return np.asarray(image), "mono8"
    if image.mode == "RGBA":
        return np.asarray(image), "rgba8"
    return np.asarray(image.convert("RGB")), "rgb8"


def to_pil(pixels: np.ndarray, encoding: str) -> PILImage.Image:
    """*pixels* as a picture: colour channels in their named order, a single 8-bit channel
    as grey, and a deeper or floating single channel scaled to 8 bits by its finite range."""
    if encoding in _PIL_RAW_MODES:
        mode, raw_mode = _PIL_RAW_MODES[encoding]
        height, width = pixels.shape[:2]
        return PILImage.frombuffer(mode, (width, height), np.ascontiguousarray(pixels).tobytes(),
                                   "raw", raw_mode, 0, 1)
    if pixels.ndim == 3:
        raise ValueError(f"cannot render a {pixels.shape[2]}-channel image in encoding "
                         f"{encoding!r}")
    if pixels.dtype == np.uint8:
        return PILImage.fromarray(pixels, "L")
    values = pixels.astype(np.float64)
    finite = np.isfinite(values)
    top = float(values[finite].max()) if finite.any() else 0.0
    scaled = np.where(finite, values / top if top > 0 else 0.0, 0.0)
    return PILImage.fromarray((scaled * 255).astype(np.uint8), "L")


__all__ = ["ENCODINGS", "IMAGE_TYPES", "decode", "decode_compressed", "to_pil"]
