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


# yolov4的anchor分配机制和yolov3一致, V4和前两个有所不同
class YoloV4Loss(nn.Module):
    def __init__(self,
                 anchor_sizes=None,
                 strides=None,
                 per_level_num_anchors=3,
                 obj_layer_weight=None,
                 conf_loss_weight=1.,
                 box_loss_weight=1.,
                 cls_loss_weight=1.,
                 box_loss_iou_type='CIoU',
                 iou_ignore_threshold=0.5):
        super(YoloV4Loss, self).__init__()
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
        self.per_level_num_anchors = per_level_num_anchors
        self.conf_loss_weight = conf_loss_weight
        self.box_loss_weight = box_loss_weight
        self.cls_loss_weight = cls_loss_weight
        self.box_loss_iou_type = box_loss_iou_type
        self.iou_ignore_threshold = iou_ignore_threshold
        self.iou_function = IoUMethod(iou_type=self.box_loss_iou_type)

    def forward(self, preds, annotations):
        """
        compute obj loss, reg loss and cls loss in one batch
        :param preds:
        :param annotations: [B, N, 5]
        :return:
        """
        device = annotations.device
        batch_size = annotations.shape[0]

        # if input size:[B,3,416,416]
        # features shape:[[B, 255, 52, 52],[B, 255, 26, 26],[B, 255, 13, 13]]
        # obj_reg_cls_heads shape:[[B, 52, 52, 3, 85],[B, 26, 26, 3, 85],[B, 13, 13, 3, 85]]
        # TODO: 这块需要查一下
        obj_reg_cls_preds = preds[0]

        # feature_size = [[w, h], ...]
        feature_size = [[per_level_cls_head[2], per_level_cls_head[1]] for per_level_cls_head in obj_reg_cls_preds]
        # one_image_anchors shape: [[52, 52, 3, 5], [26, 26, 3, 5], [13, 13, 3, 5]]
        # 5: [grids_x_idx, grids_y_idx, relative_anchor_w, relative_anchor_h, stride] relative feature map
        one_image_anchors = self.anchors(feature_size)

        # batch_anchors shape is [[B, H, W, 3, 5], ...]
        batch_anchors = [
            torch.tensor(per_level_anchor).unsqueeze(0).repeat(
                batch_size, 1, 1, 1, 1) for per_level_anchor in one_image_anchors
        ]

        all_anchors, all_targets = self.get_batch_anchors_targets(batch_anchors, annotations)

    def get_batch_anchors_targets(self, obj_reg_cls_heads, batch_anchors, annotations):
        """
        Assign a ground truth target for each anchor
        :param obj_reg_cls_heads: [[B, h, w, 3, 85], ...]
        :param batch_anchors: [[B,52,52,3,5], [B,26,26,3,5], ...]
               if one feature map shape is [w=3, h=5], this feature map anchor shape is [5, 3, 3, 5]
        :param annotations: [B,N,5]
        :return:
        """
        device = annotations.device

        anchor_sizes = torch.tensor(self.anchor_sizes).float().to(device)
        anchor_sizes = anchor_sizes.view(
            len(anchor_sizes) // self.per_level_num_anchors, -1, 2)  # [3, 3, 2]
        # scale anchor size
        for i in range(anchor_sizes.shape[0]):
            anchor_sizes[i] = anchor_sizes[i] / self.strides[i]
        anchor_sizes = anchor_sizes.view(-1, 2)  # [9, 2]

        # all_strides: [8, 8, 8, 16, 16, ...]
        all_strides = [stride for stride in self.strides for _ in range(self.per_level_num_anchors)]
        all_strides = torch.tensor(all_strides).to(device)

        # grid_inside_ids: [0, 1, 2, 0, 1, 2, 0, 1, 2]
        grid_inside_ids = [i for _ in range(len(batch_anchors)) for i in range(self.per_level_num_anchors)]
        grid_inside_ids = torch.tensor(grid_inside_ids).to(device)

        all_preds, all_anchors, all_targets = [], [], []
        feature_hw = []     # 最终有9个元素, [[52,52], [52,52], ...]
        per_layer_prefix_ids = [0, 0, 0]
        # 分别遍历一个batch中所有图片的三个层级feature map
        for layer_idx, (per_level_heads, per_level_anchors) in enumerate(zip(obj_reg_cls_heads, batch_anchors)):
            B, H, W, _, _ = per_level_anchors.shape
            for _ in range(self.per_level_num_anchors):
                feature_hw.append([H, W])

            # TODO: 这里需要理解一下为什么
            previous_layer_prefix, cur_layer_prefix = 0, 0
            if layer_idx == 0:
                for _ in range(self.per_level_num_anchors):
                    per_layer_prefix_ids.append(H * W * self.per_level_num_anchors)
                previous_layer_prefix = H * W * self.per_level_num_anchors
            # len(batch_anchors) - 1 = 3-1 = 2
            if layer_idx == 1:
                for _ in range(self.per_level_num_anchors):
                    cur_layer_prefix = H * W * self.per_level_num_anchors
                    per_layer_prefix_ids.append(previous_layer_prefix + cur_layer_prefix)
                previous_layer_prefix = previous_layer_prefix + cur_layer_prefix

            # obj target init value=0
            per_level_obj_target = torch.zeros((B, H * W * self.per_level_num_anchors, 1),
                                               dtype=torch.float32,
                                               device=device)
            # noobj target init value=1
            per_level_noobj_target = torch.ones((B, H * W * self.per_level_num_anchors, 1),
                                                dtype=torch.float32,
                                                device=device)
            # box loss scale init value=0
            per_level_box_loss_scale = torch.zeros((B, H * W * self.per_level_num_anchors, 1),
                                                   dtype=torch.float32,
                                                   device=device)
            # reg target init value=0
            per_level_reg_target = torch.zeros((B, H * W * self.per_level_num_anchors, 4),
                                               dtype=torch.float32,
                                               device=device)
            # cls target init value=-1
            per_level_cls_target = torch.ones((B, H * W * self.per_level_num_anchors, 1),
                                              dtype=torch.float32,
                                              device=device) * (-1)
            # per_level_targets shape is [B, H*W*self.per_level_num_anchors, 8]
            # 8: [obj_target, noobj_target, box_loss_scale,
            #       x_offset, y_offset, scaled_gt_w, scaled_gt_h, class_target]
            per_level_targets = torch.cat((
                per_level_obj_target, per_level_noobj_target, per_level_box_loss_scale,
                per_level_reg_target, per_level_cls_target), dim=-1)

            # per level anchor shape: [B, H*W*3, 5]
            # 5: [grids_x_idx, grids_y_idx, relative_anchor_w, relative_anchor_h, stride]
            per_level_anchors = per_level_anchors.view(
                per_level_anchors.shape[0], -1, per_level_anchors.shape[-1])

            # per_level_heads: [B, H*W*3, 85]
            per_level_heads = per_level_heads.view(
                per_level_anchors.shape[0], -1, per_level_anchors.shape[-1])
            per_level_obj_preds = per_level_heads[..., 0]
            per_level_cls_preds = per_level_heads[..., 5:]

            # per_level_heads[..., 1:3] 是相对于某个cell的左上角偏移量, 所以加上per_level_anchors前两列得到中心点
            # per_level_anchors这里anchors里的数值已经相对feature map做了缩小,在anchor.py中
            # TODO: per_level_scaled_xy_ctr, per_level_scaled_wh 就是 bx, by, bw, bh
            per_level_scaled_xy_ctr = per_level_heads[..., 1:3] + per_level_anchors[..., :2]    # [B, H*W*3, 2]
            per_level_scaled_wh = torch.exp(per_level_heads[..., 3:5]) * per_level_heads[..., 2:4]

            per_level_scaled_xymin = per_level_scaled_xy_ctr - per_level_scaled_wh * 0.5
            per_level_scaled_xymax = per_level_scaled_xy_ctr + per_level_scaled_wh * 0.5

            # per reg preds shape: [B, H*W*3, 4]
            # per reg preds format:[scaled_xmin,scaled_ymin,scaled_xmax,scaled_ymax]
            per_level_reg_heads = torch.cat((per_level_scaled_xymin, per_level_scaled_xymax), dim=2)

            per_level_heads = torch.cat((per_level_obj_preds, per_level_reg_heads, per_level_cls_preds), dim=2)

            all_preds.append(per_level_heads)       # [[B, H*W*3, 85], ...]
            all_anchors.append(per_level_anchors)   # [[B, H*W*3, 5], ...]
            all_targets.append(per_level_targets)   # [[B, H*W*3, 8], ...]

        all_preds = torch.cat(all_preds, dim=1)         # [B, H1*W1*3+H2*W2*3+H3*W3*3, 85]
        all_anchors = torch.cat(all_anchors, dim=1)     # [B, H1*W1*3+H2*W2*3+H3*W3*3, 5]
        all_targets = torch.cat(all_targets, dim=1)     # [B, H1*W1*3+H2*W2*3+H3*W3*3, 8]
        per_layer_prefix_ids = torch.tensor(per_layer_prefix_ids).to(device)
        feature_hw = torch.tensor(feature_hw).to(device)

        for img_idx, per_img_annots in enumerate(annotations):
            # traverse each pic in batch
            # drop all index=-1 in annotations  [N, 5]
            one_image_annots = per_img_annots[per_img_annots[:, 4] >= 0]
            # not empty
            if one_image_annots.shape[0] > 0:
                # gt_class index range from 0 to 79
                gt_boxes = one_image_annots[:, :4]
                gt_classes = one_image_annots[:, 4]

                # for 9 anchor of each gt boxes, compute anchor global idx
                # gt_9_boxes_ctr: [gt_num, 2] -> [gt_num, 1, 2] -> [gt_num, 9, 2]
                # all_strides: [9, ] -> [1, 9] -> [1, 9, 1]
                gt_9_boxes_ctr = (
                    (gt_boxes[:, :2] + gt_boxes[:, 2:]) * 0.5).unsqueeze(1) / all_strides.unsqueeze(0).unsqueeze(-1)

                # torch.floor向下取整到离它最近的整数
                # 如[[3.5, 2.1]] -> [[3, 2]], 即中心点为[3.5, 2.1]的gt是属于[3, 2]这个gird cell的
                gt_9_boxes_grid_xy = torch.floor(gt_9_boxes_ctr)
                global_ids = ()

                # assign positive anchor which has max iou with a gt box
                # [gt_num, 9, 2]
                gt_9_boxes_scaled_wh = (gt_boxes[:, 2:] - gt_boxes[:, :2]
                                        ).unsqueeze(1) / all_strides.unsqueeze(0).unsqueeze(-1)

        return all_anchors, all_targets
