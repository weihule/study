import os
import sys
import torch
import torch.nn as nn
from network_files.anchor import YoloV3Anchors

BASE_DIR = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
sys.path.append(BASE_DIR)

from torch_detection.utils.iou_methos import IoUMethod


class YoloV5Loss(nn.Module):
    def __init__(self,
                 anchor_sizes=None,
                 strides=None,
                 per_level_num_anchors=3,
                 obj_layer_weight=None,
                 obj_loss_weight=1.,
                 box_loss_weight=0.05,
                 cls_loss_weight=0.5,
                 box_loss_iou_type='CIoU',
                 filter_anchor_threshold=4.):
        super(YoloV5Loss, self).__init__()
        assert box_loss_iou_type in ['IoU, DIoU, CIoU'], 'Wrong IoU type'
        if anchor_sizes is None:
            self.anchor_sizes = [[10, 13], [16, 30], [33, 23], [30, 61],
                            [62, 45], [59, 119], [116, 90], [156, 198],
                            [373, 326]]
        if strides is None:
            self.strides = [8, 16, 32]
        if obj_layer_weight is None:
            self.obj_layer_weight = [4.0, 1.0, 0.4]
        self.anchors = YoloV3Anchors(
            anchor_sizes=self.anchor_sizes,
            strides=self.strides,
            per_level_num_anchors=per_level_num_anchors
        )
        self.obj_loss_weight = obj_loss_weight
        self.box_loss_weight = box_loss_weight
        self.cls_loss_weight = cls_loss_weight
