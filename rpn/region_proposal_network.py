from typing import Tuple

import torch
from torch import FloatTensor
from torch import nn
from torch.autograd import Variable
from torch.nn import functional as F

from bbox import BBox
from nms.nms import NMS


class RegionProposalNetwork(nn.Module):

    def __init__(self) -> None:
        super().__init__()

        self._features = nn.Sequential(
            nn.Conv2d(in_channels=512, out_channels=512, kernel_size=3, padding=1),
            nn.ReLU()
        )

        self._objectness = nn.Conv2d(in_channels=512, out_channels=18, kernel_size=1)
        self._transformer = nn.Conv2d(in_channels=512, out_channels=36, kernel_size=1)

    def forward(self, features, image_width: int, image_height: int):
        anchor_bboxes = BBox.generate_anchors(max_x=image_width, max_y=image_height, stride=16).cuda()

        features = self._features(features)
        objectnesses = self._objectness(features)
        transformers = self._transformer(features)

        objectnesses = objectnesses.permute(0, 2, 3, 1).contiguous().view(-1, 2)
        transformers = transformers.permute(0, 2, 3, 1).contiguous().view(-1, 4)

        proposal_score = objectnesses.data[:, 1]
        _, sorted_indices = torch.sort(proposal_score, dim=0, descending=True)

        sorted_transformers = transformers.data[sorted_indices]
        sorted_anchor_bboxes = anchor_bboxes[sorted_indices]

        proposal_bboxes = BBox.apply_transformer(sorted_anchor_bboxes, sorted_transformers)
        proposal_bboxes = BBox.clip(proposal_bboxes, 0, 0, image_width, image_height)

        area_threshold = 16
        non_small_area_indices = ((proposal_bboxes[:, 2] - proposal_bboxes[:, 0] >= area_threshold) &
                                  (proposal_bboxes[:, 3] - proposal_bboxes[:, 1] >= area_threshold)).nonzero().squeeze()
        proposal_bboxes = proposal_bboxes[non_small_area_indices]

        proposal_bboxes = proposal_bboxes[:12000 if self.training else 6000]
        keep_indices = NMS.suppress(proposal_bboxes, threshold=0.7)
        proposal_bboxes = proposal_bboxes[keep_indices]
        proposal_bboxes = proposal_bboxes[:2000 if self.training else 300]

        return anchor_bboxes, objectnesses, transformers, proposal_bboxes

    def sample(self, anchor_bboxes, anchor_objectnesses, anchor_transformers, gt_bboxes, image_width: int, image_height: int):
        anchor_bboxes = anchor_bboxes.cpu()
        gt_bboxes = gt_bboxes.cpu()

        # remove cross-boundary
        boundary = FloatTensor(BBox(0, 0, image_width, image_height).tolist())
        inside_indices = BBox.inside(anchor_bboxes, boundary.unsqueeze(dim=0)).squeeze().nonzero().squeeze()

        anchor_bboxes = anchor_bboxes[inside_indices]
        anchor_objectnesses = anchor_objectnesses[inside_indices.cuda()]
        anchor_transformers = anchor_transformers[inside_indices.cuda()]

        # find labels for each `anchor_bboxes`
        labels = torch.ones(len(anchor_bboxes)).long() * -1
        ious = BBox.iou(anchor_bboxes, gt_bboxes)
        anchor_max_ious, anchor_assignments = ious.max(dim=1)
        gt_max_ious, gt_assignments = ious.max(dim=0)
        anchor_additions = (ious == gt_max_ious).nonzero()[:, 0]
        labels[anchor_max_ious < 0.3] = 0
        labels[anchor_additions] = 1
        labels[anchor_max_ious >= 0.7] = 1

        # select 256 samples
        fg_indices = (labels == 1).nonzero().squeeze()
        bg_indices = (labels == 0).nonzero().squeeze()
        if len(fg_indices) > 0:
            fg_indices = fg_indices[torch.randperm(len(fg_indices))[:min(len(fg_indices), 128)]]
        if len(bg_indices) > 0:
            bg_indices = bg_indices[torch.randperm(len(bg_indices))[:256 - len(fg_indices)]]
        select_indices = torch.cat([fg_indices, bg_indices])
        select_indices = select_indices[torch.randperm(len(select_indices))]

        gt_anchor_objectnesses = labels[select_indices]
        gt_bboxes = gt_bboxes[anchor_assignments[fg_indices]]
        anchor_bboxes = anchor_bboxes[fg_indices]
        gt_anchor_transformers = BBox.calc_transformer(anchor_bboxes, gt_bboxes)

        gt_anchor_objectnesses = Variable(gt_anchor_objectnesses).cuda()
        gt_anchor_transformers = Variable(gt_anchor_transformers).cuda()

        anchor_objectnesses = anchor_objectnesses[select_indices.cuda()]
        anchor_transformers = anchor_transformers[fg_indices.cuda()]

        return anchor_objectnesses, anchor_transformers, gt_anchor_objectnesses, gt_anchor_transformers

    def loss(self, anchor_objectnesses, anchor_transformers, gt_anchor_objectnesses, gt_anchor_transformers):
        cross_entropy = F.cross_entropy(input=anchor_objectnesses, target=gt_anchor_objectnesses)

        # NOTE: The default of `size_average` is `True`, which is divided by N x 4 (number of all elements), here we replaced by N for better performance
        smooth_l1_loss = F.smooth_l1_loss(input=anchor_transformers, target=gt_anchor_transformers, size_average=False)
        smooth_l1_loss /= len(gt_anchor_transformers)

        return cross_entropy, smooth_l1_loss