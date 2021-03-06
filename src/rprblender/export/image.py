#**********************************************************************
# Copyright 2020 Advanced Micro Devices, Inc
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#********************************************************************
import numpy as np
import os
from pathlib import Path

import bpy
import bpy_extras

from rprblender import utils

import pyrpr

from rprblender.utils import logging
log = logging.Log(tag='export.image')


UNSUPPORTED_IMAGES = ('.tiff', '.tif', '.exr')

# image format conversion for packed pixel/generated images
IMAGE_FORMATS = {
    'OPEN_EXR_MULTILAYER': ('OPEN_EXR', 'exr'),
    'OPEN_EXR': ('OPEN_EXR', 'exr'),
    'HDR': ('HDR', 'hdr'),
    # 'TIFF': ('TIFF', 'tiff'), # Seems tiff is not working properly in RPR
    'TARGA': ('TARGA', 'tga'),
    'TARGA_RAW': ('TARGA', 'tga'),
    # everything else will be stored as PNG
}
DEFAULT_FORMAT = ('PNG', 'png')


def key(image: bpy.types.Image):
    """ Generate image key for RPR """
    return image.name


def sync(rpr_context, image: bpy.types.Image):
    """ Creates pyrpr.Image from bpy.types.Image """

    if image.size[0] * image.size[1] * image.channels == 0:
        log.warn("Image has no data", image)
        return None

    image_key = key(image)

    if image_key in rpr_context.images:
        return rpr_context.images[image_key]

    log("sync", image)

    pixels = image.pixels
    if hasattr(pixels, 'foreach_get'):
        data = utils.get_prop_array_data(pixels)
        data = np.flipud(data.reshape(image.size[1], image.size[0], image.channels))
        rpr_image = rpr_context.create_image_data(image_key, np.ascontiguousarray(data))

    elif image.source in ('FILE', 'GENERATED'):
        file_path = cache_image_file(image, rpr_context.blender_data['depsgraph'])
        rpr_image = rpr_context.create_image_file(image_key, file_path)

    else:
        # loading image by pixels
        data = np.fromiter(pixels, dtype=np.float32,
                           count=image.size[0] * image.size[1] * image.channels)
        data = np.flipud(data.reshape(image.size[1], image.size[0], image.channels))
        rpr_image = rpr_context.create_image_data(image_key, np.ascontiguousarray(data))

    rpr_image.set_name(image_key)

    # TODO: implement more correct support of image color space types
    if image.colorspace_settings.name in ('sRGB', 'BD16', 'Filmic Log'):
        rpr_image.set_gamma(2.2)
    else:
        if image.colorspace_settings.name not in ('Non-Color', 'Raw', 'Linear'):
            log.warn("Ignoring unsupported image color space type",
                     image.colorspace_settings.name, image)

    return rpr_image


class ImagePixels:
    """
    This class stores source image pixels clipped to region size.
    Exports full and sub-region pixels
    """
    def __init__(self, image: bpy.types.Image, region: tuple):
        self.pixels = None
        self.size = (0, 0)
        self.channels = image.channels
        self.name = image.name
        self.color_space = image.colorspace_settings.name

        if image.size[0] * image.size[1] * image.channels == 0:
            log.warn("Image has no data", image)
            return

        self.pixels = self.extract_pixels(image, *region)

        self.size = (region[2] - region[0] + 1, region[3] - region[1] + 1)

    def is_empty(self) -> bool:
        return self.pixels is None

    def extract_pixels(self, image: bpy.types.Image, x1, y1, x2, y2) -> np.array:
        """ Store source image pixels cropped to region coordinates """
        # extract pixels
        data = np.fromiter(image.pixels, dtype=np.float32,
                           count=image.size[0] * image.size[1] * image.channels)
        pixels = data.reshape(image.size[1], image.size[0], image.channels)

        return self.extract_pixels_region(pixels, x1, y1, x2, y2)

    def extract_pixels_region(self, pixels: np.array, x1, y1, x2, y2) -> np.array:
        """ Crop pixels to region size """
        region_pixels = np.array(pixels[y1:y2+1, x1:x2+1, :], dtype=np.float32)

        return region_pixels

    def export_full(self, rpr_context) -> (pyrpr.Image, None):
        """ Export the full image pixels as RPR image"""
        if self.is_empty():
            return None

        image_key = f"{self.name}@{self.color_space}"

        if image_key in rpr_context.images:
            return rpr_context.images[image_key]

        pixels = np.ascontiguousarray(np.flipud(self.pixels))
        rpr_image = rpr_context.create_image_data(image_key, pixels)
        rpr_image.set_name(image_key)

        if self.color_space in ('sRGB', 'BD16', 'Filmic Log'):
            rpr_image.set_gamma(2.2)

        return rpr_image

    def export_region(self, rpr_context, x1, y1, x2, y2) -> (pyrpr.Image, None):
        """ Export pixels cropped to sub-region coordinates as RPR image """
        if self.is_empty():
            return None

        # check sub-region boundaries, just in case something went terribly wrong
        if x1 == x2 or y1 == y2 or x1 >= self.size[0] or x2 < 0 or y1 >= self.size[1] or y2 < 0:
            log.warn(f"Image region ({x1}; {y1})-({x2}; {y2}) has no data", self.name)
            return None

        image_key = f"{self.name}({x1}, {y1})-({x2}, {y2})@{self.color_space}"

        if image_key in rpr_context.images:
            return rpr_context.images[image_key]

        # get pixels region
        pixels = self.extract_pixels_region(self.pixels, x1, y1, x2, y2)
        pixels = np.flipud(pixels)

        rpr_image = rpr_context.create_image_data(image_key, np.ascontiguousarray(pixels))

        rpr_image.set_name(image_key)

        if self.color_space in ('sRGB', 'BD16', 'Filmic Log'):
            rpr_image.set_gamma(2.2)

        return rpr_image


def _get_temp_image_path(image: [bpy.types.Image, str], extension="png") -> str:
    h = abs(hash(image if isinstance(image, str) else image.name))
    return str(utils.get_temp_pid_dir() / f"{h}.{extension}")


def _save_temp_image(image, target_format, temp_path, depsgraph):
    scene = depsgraph.scene_eval

    # set desired output image file format
    scene.render.image_settings.file_format = target_format
    image.save_render(temp_path, scene=scene)


def cache_image_file(image: bpy.types.Image, depsgraph) -> str:
    """
    See if image is a file, cache image pixels to temporary folder if not.
    Return image file path.
    """
    if image.source != 'FILE':
        temp_path = _get_temp_image_path(image)
        if image.is_dirty or not os.path.isfile(temp_path):
            image.save_render(temp_path)

        return temp_path

    file_path = image.filepath_from_user()

    if file_path.lower().endswith('.ies'):
        if os.path.isfile(file_path):
            return file_path

        if not image.packed_file:
            log.warn("Can't load image", image, file_path)
            return None

        temp_path = _get_temp_image_path(image, "ies")
        if not os.path.isfile(temp_path):
            # save data of packed file
            Path(temp_path).write_bytes(image.packed_file.data)

        return temp_path

    if image.is_dirty or not os.path.isfile(file_path) \
            or file_path.lower().endswith(UNSUPPORTED_IMAGES):
        target_format, target_extension = IMAGE_FORMATS.get(image.file_format, DEFAULT_FORMAT)

        # getting file path from image cache and if such file not exist saving image to cache
        temp_path = _get_temp_image_path(image, target_extension)
        if image.is_dirty or not os.path.isfile(temp_path):
            _save_temp_image(image, target_format, temp_path, depsgraph)

        return temp_path

    return file_path


def cache_image_file_path(file_path: str, depsgraph) -> str:
    """ Cache Blender integrated and user-defined LookDev IBL files """
    if not file_path.lower().endswith(UNSUPPORTED_IMAGES):
        return file_path

    if file_path.lower().endswith('.exr'):
        target_format, target_extension = IMAGE_FORMATS['OPEN_EXR']
    else:
        target_format, target_extension = IMAGE_FORMATS['TIFF']

    temp_path = _get_temp_image_path(file_path, target_extension)
    if os.path.isfile(temp_path):
        return temp_path

    image = bpy_extras.image_utils.load_image(file_path)
    try:
        _save_temp_image(image, target_format, temp_path, depsgraph)
    finally:
        bpy.data.images.remove(image)

    return temp_path
