import numpy as np
import cv2
from typing import Tuple, List

from .base import register_textdetectors, TextDetectorBase, TextBlock, DEFAULT_DEVICE, DEVICE_SELECTOR, ProjImgTrans
from .ctd import CTDModel

CTD_ONNX_PATH = 'data/models/comictextdetector.pt.onnx'
CTD_TORCH_PATH = 'data/models/comictextdetector.pt'

def load_ctd_model(model_path, device, detect_size=1024) -> CTDModel:
    model = CTDModel(model_path, detect_size=detect_size, device=device)
    
    return model

@register_textdetectors('ctd')
class ComicTextDetector(TextDetectorBase):

    params = {
        'detect_size': {
            'type': 'selector',
            'options': [896, 1024, 1152, 1280], 
            'value': 1280
        }, 
        'det_rearrange_max_batches': {
            'type': 'selector',
            'options': [1, 2, 4, 6, 8, 12, 16, 24, 32], 
            'value': 4
        },
        'device': DEVICE_SELECTOR(),
        'description': 'ComicTextDetector',
        'font size multiplier': 1.,
        'font size max': -1,
        'font size min': -1,
        'mask dilate size': 2,
        'distance_tolerance': {
            'type': 'line_editor',
            'value': 1.2,
            'description': 'Max distance factor (relative to font size) to merge text blocks. Higher = merge further blocks.',
            'display_name': 'Merge Distance Tolerance'
        },
        'font_size_tolerance': {
            'type': 'line_editor',
            'value': 1.7,
            'description': 'Max font size ratio difference to merge text blocks.',
            'display_name': 'Merge Font Size Tolerance'
        },
        'block_expansion_ratio': {
            'type': 'line_editor',
            'value': 0.3,
            'description': 'Expand text block bounding box by this ratio (0.3 = 30%). Set to 0 to disable.',
            'display_name': 'Block Expansion Ratio'
        }
    }
    _load_model_keys = {'model'}
    download_file_list = [{
        'url': 'https://github.com/zyddnys/manga-image-translator/releases/download/beta-0.3/',
        'files': ['data/models/comictextdetector.pt', 'data/models/comictextdetector.pt.onnx'],
        'sha256_pre_calculated': ['1f90fa60aeeb1eb82e2ac1167a66bf139a8a61b8780acd351ead55268540cccb', '1a86ace74961413cbd650002e7bb4dcec4980ffa21b2f19b86933372071d718f'],
        'concatenate_url_filename': 2,
    }]

    device = DEFAULT_DEVICE
    detect_size = 1024
    def __init__(self, **params) -> None:
        super().__init__(**params)
        self.model: CTDModel = None

    @property
    def device(self):
        return self.params['device']['value']
    
    @property
    def detect_size(self):
        return int(self.params['detect_size']['value'])

    def _load_model(self):
        if self.device != 'cpu':
            self.model = load_ctd_model(CTD_TORCH_PATH, self.device, self.detect_size)
        else:
            self.model = load_ctd_model(CTD_ONNX_PATH, self.device, self.detect_size)

    def _detect(self, img: np.ndarray, proj: ProjImgTrans) -> Tuple[np.ndarray, List[TextBlock]]:
        dist_tol = float(self.params['distance_tolerance']['value'])
        fnt_tol = float(self.params['font_size_tolerance']['value'])
        _, mask, blk_list = self.model(img, distance_tolerance=dist_tol, font_size_tolerance=fnt_tol)
        
        fnt_rsz = self.get_param_value('font size multiplier')
        fnt_max = self.get_param_value('font size max')
        fnt_min = self.get_param_value('font size min')
        for blk in blk_list:
            sz = blk._detected_font_size * fnt_rsz
            if fnt_max > 0:
                sz = min(fnt_max, sz)
            if fnt_min > 0:
                sz = max(fnt_min, sz)
            blk.font_size = sz
            blk._detected_font_size = sz

        ksize = self.get_param_value('mask dilate size')
        if ksize > 0:
            element = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * ksize + 1, 2 * ksize + 1),(ksize, ksize))
            mask = cv2.dilate(mask, element)

        # Expand text block bounding boxes from center
        expansion_ratio = float(self.params['block_expansion_ratio']['value'])
        if expansion_ratio > 0:
            im_h, im_w = img.shape[:2]
            for blk in blk_list:
                x1, y1, x2, y2 = blk.xyxy
                w = x2 - x1
                h = y2 - y1
                cx = (x1 + x2) / 2
                cy = (y1 + y2) / 2
                new_w = w * (1 + expansion_ratio)
                new_h = h * (1 + expansion_ratio)
                new_x1 = int(max(0, cx - new_w / 2))
                new_y1 = int(max(0, cy - new_h / 2))
                new_x2 = int(min(im_w, cx + new_w / 2))
                new_y2 = int(min(im_h, cy + new_h / 2))
                blk.xyxy = [new_x1, new_y1, new_x2, new_y2]
                # Set _bounding_rect as [x, y, w, h] for UI rendering
                blk._bounding_rect = [new_x1, new_y1, new_x2 - new_x1, new_y2 - new_y1]

        return mask, blk_list

    def updateParam(self, param_key: str, param_content):
        super().updateParam(param_key, param_content)
        device = self.device
        if self.model is not None:
            if self.model.device != device:
                self.model.device = device
                if device != 'cpu':
                    self.model.load_model(CTD_TORCH_PATH)
                else:
                    self.model.load_model(CTD_ONNX_PATH)
            self.model.detect_size = self.detect_size