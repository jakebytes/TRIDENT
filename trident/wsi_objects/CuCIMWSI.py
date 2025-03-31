from __future__ import annotations
import numpy as np
from PIL import Image
from typing import List, Tuple, Optional, Union
import geopandas as gpd
import torch 

from trident.wsi_objects.WSI import WSI

try:
    import cupy as cp
except:
    print('Couldnt import cupy. Please install cupy.')
    exit()


class CuCIMWSI(WSI):

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

    def _lazy_initialize(self) -> None:
        """
        Perform lazy initialization by loading the WSI file and its metadata using CuCIM.

        Raises:
        -------
        FileNotFoundError:
            If the WSI file or tissue segmentation mask cannot be found.
        Exception:
            If there is an error while initializing the WSI.

        Notes:
        ------
        This method sets the following attributes after initialization:
        - `width` and `height` of the WSI.
        - `mpp` (microns per pixel) and `mag` (magnification level).
        - `gdf_contours` if a tissue segmentation mask is provided.
        """

        from cucim import CuImage

        if not self.lazy_init:
            try:
                self.img = CuImage(self.slide_path)
                self.dimensions = (self.img.size()[0], self.img.size()[1])  
                self.width, self.height = self.dimensions
                self.level_count = self.img.resolutions['level_count']
                self.level_downsamples = self.img.resolutions['level_downsamples']
                self.level_dimensions = self.img.resolutions['level_dimensions']
                self.properties = self.img.metadata

                if self.mpp is None:
                    self.mpp = self._fetch_mpp(self.custom_mpp_keys)
                self.mag = self._fetch_magnification(self.custom_mpp_keys)
                self.lazy_init = True

                if self.tissue_seg_path is not None:
                    try:
                        self.gdf_contours = gpd.read_file(self.tissue_seg_path)
                    except FileNotFoundError:
                        raise FileNotFoundError(f"Tissue segmentation file not found: {self.tissue_seg_path}")
            except Exception as e:
                raise Exception(f"Error initializing WSI: {e}")

    def _fetch_mpp(self, custom_keys: dict = None) -> Optional[float]:
        """
        Fetch the microns per pixel (MPP) from CuImage metadata.

        Parameters
        ----------
        custom_keys : dict, optional
            Optional dictionary with keys for 'mpp_x' and 'mpp_y' metadata fields to check first.

        Returns
        -------
        float or None
            Average MPP if found, else None.
        """
        import json

        def try_parse(val):
            try:
                return float(val)
            except:
                return None

        # CuCIM metadata can be a JSON string
        metadata = self.img.metadata
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        # Flatten nested CuCIM metadata for convenience
        flat_meta = {}
        def flatten(d, parent_key=''):
            for k, v in d.items():
                key = f"{parent_key}.{k}" if parent_key else k
                if isinstance(v, dict):
                    flatten(v, key)
                else:
                    flat_meta[key.lower()] = v
        flatten(metadata)

        # Check custom keys first if provided
        mpp_x = mpp_y = None
        if custom_keys:
            if 'mpp_x' in custom_keys:
                mpp_x = try_parse(flat_meta.get(custom_keys['mpp_x'].lower()))
            if 'mpp_y' in custom_keys:
                mpp_y = try_parse(flat_meta.get(custom_keys['mpp_y'].lower()))

        # Standard fallback keys used in SVS, NDPI, MRXS, etc.
        fallback_keys = [
            'openslide.mpp-x', 'openslide.mpp-y',
            'tiff.resolution-x', 'tiff.resolution-y',
            'mpp', 'spacing', 'microns_per_pixel',
            'aperio.mpp', 'hamamatsu.mpp',
            'metadata.resolutions.level[0].spacing',
            'metadata.resolutions.level[0].physical_size.0',
        ]

        for key in fallback_keys:
            if mpp_x is None and key in flat_meta:
                mpp_x = try_parse(flat_meta[key])
            elif mpp_y is None and key in flat_meta:
                mpp_y = try_parse(flat_meta[key])
            if mpp_x is not None and mpp_y is not None:
                break

        # Use same value for both axes if only one was found
        if mpp_x is not None and mpp_y is None:
            mpp_y = mpp_x
        if mpp_y is not None and mpp_x is None:
            mpp_x = mpp_y

        if mpp_x is not None and mpp_y is not None:
            return float((mpp_x + mpp_y) / 2)

        return None

    def get_thumbnail(self, size: tuple[int, int]) -> Image.Image:
        """
        Generate a thumbnail image of the WSI.

        Args:
        -----
        size : tuple[int, int]
            A tuple specifying the desired width and height of the thumbnail.

        Returns:
        --------
        Image.Image:
            The thumbnail as a PIL Image in RGB format.
        """
        target_width, target_height = size

        # Compute desired downsample factor and level
        downsample_x = self.width / target_width
        downsample_y = self.height / target_height
        desired_downsample = max(downsample_x, downsample_y)
        level, _ = self.get_best_level_and_custom_downsample(desired_downsample)

        # Compute the size to read at that level
        level_width, level_height = self.level_dimensions[level]

        # Read region at (0, 0) in target level
        region = self.read_region(
            location=(0, 0),
            size=(level_width, level_height),
            level=level
        ).convert("RGB")
        region = region.resize((size[1], size[0]), resample=Image.BILINEAR)

        return region

    def read_region(
        self, 
        location: Tuple[int, int], 
        level: int, 
        size: Tuple[int, int],
        device: str = 'cpu',
        read_as: str = 'pil',
    ) -> Union[np.ndarray, torch.Tensor, Image.Image]:
        """
        Extract a specific region from the whole-slide image (WSI) using CuCIM, with output as NumPy array,
        Torch tensor, or PIL image.

        Args:
        -----
        location : Tuple[int, int]
            (x, y) coordinates of the top-left corner of the region to extract.
        level : int
            Pyramid level to read from.
        size : Tuple[int, int]
            (width, height) of the region to extract.
        device : str, optional
            Device used to perform the read. Can be 'cpu' or 'cuda:0', etc. Defaults to 'cpu'.
        read_as : str, optional
            Format to return the region in. Options are:
            - 'numpy': returns a NumPy array
            - 'torch': returns a Torch tensor (on GPU)
            - 'pil': returns a PIL Image object (default)

        Returns:
        --------
        Union[np.ndarray, torch.Tensor, PIL.Image.Image]
            The extracted region in the specified format.

        Example:
        --------
        >>> region = wsi.read_region((0, 0), level=0, size=(512, 512), device='cuda:0', read_as='torch')
        >>> print(region.shape)
        torch.Size([512, 512, 3])
        """
        region = self.img.read_region(location=location, level=level, size=size, device=device)

        if read_as == 'torch':
            if 'cuda' in device:
                return torch.as_tensor(region, device=device)
            else:
                return torch.from_numpy(cp.asnumpy(region))
        elif read_as == 'numpy':
            return cp.asnumpy(region)
        elif read_as == 'pil':
            return Image.fromarray(cp.asnumpy(region)).convert("RGB")

        raise ValueError(f"Unsupported read_as value: {read_as}")

    def get_dimensions(self) -> Tuple[int, int]:
        """
        The `get_dimensions` function from the class `OpenSlideWSI` Retrieve the dimensions of the WSI.

        Returns:
        --------
        Tuple[int, int]:
            A tuple containing the width and height of the WSI in pixels.

        Example:
        --------
        >>> wsi.get_dimensions()
        (100000, 80000)
        """
        return self.dimensions
