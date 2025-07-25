from typing import List
import numpy as np
import torch
from torch import nn
import torch.nn.functional as F

from detectron2.modeling.meta_arch.build import META_ARCH_REGISTRY
from detectron2.modeling import build_backbone
from detectron2.modeling.postprocessing import detector_postprocess as d2_postprocesss
from detectron2.structures import ImageList, Instances

from adet.layers.pos_encoding import PositionalEncoding2D
from adet.modeling.sd21.losses import SetCriterion
from adet.modeling.sd21.matcher import build_matcher
from adet.modeling.sd21.models import TESTR_SD21
from adet.utils.misc import NestedTensor, box_xyxy_to_cxcywh

from torchvision.utils import save_image 


def detector_postprocess(results, output_height, output_width, mask_threshold=0.5):
    """
    In addition to the post processing of detectron2, we add scalign for 
    bezier control points.
    """
    scale_x, scale_y = (output_width / results.image_size[1], output_height / results.image_size[0])
    # results = d2_postprocesss(results, output_height, output_width, mask_threshold)

    # scale bezier points
    if results.has("beziers"):
        beziers = results.beziers
        # scale and clip in place
        h, w = results.image_size
        beziers[:, 0].clamp_(min=0, max=w)
        beziers[:, 1].clamp_(min=0, max=h)
        beziers[:, 6].clamp_(min=0, max=w)
        beziers[:, 7].clamp_(min=0, max=h)
        beziers[:, 8].clamp_(min=0, max=w)
        beziers[:, 9].clamp_(min=0, max=h)
        beziers[:, 14].clamp_(min=0, max=w)
        beziers[:, 15].clamp_(min=0, max=h)
        beziers[:, 0::2] *= scale_x
        beziers[:, 1::2] *= scale_y

    if results.has("polygons"):
        polygons = results.polygons
        polygons[:, 0::2] *= scale_x
        polygons[:, 1::2] *= scale_y

    return results


@META_ARCH_REGISTRY.register()
class TransformerDetectorSD21(nn.Module):
    """
    Same as :class:`detectron2.modeling.ProposalNetwork`.
    Use one stage detector and a second stage for instance-wise prediction.
    """
    def __init__(self, cfg):
        super().__init__()
        self.device = torch.device(cfg.MODEL.DEVICE)
        self.cfg = cfg 
        
        # PHO 
        if self.cfg.MODEL.DIFFUSION.BACKBONE == 'SD21':
            
            from diffusers.models.unets.unet_2d_condition import UNet2DConditionModel
            from diffusers.models import AutoencoderKL
            from transformers import CLIPTextModel, CLIPTokenizer
            
            self.diffusion_model = UNet2DConditionModel.from_pretrained("stabilityai/stable-diffusion-2-1", subfolder='unet')
            self.vae = AutoencoderKL.from_pretrained("stabilityai/stable-diffusion-2-1", subfolder="vae")
            self.tokenizer = CLIPTokenizer.from_pretrained("stabilityai/stable-diffusion-2-1", subfolder="tokenizer")
            self.text_encoder = CLIPTextModel.from_pretrained("stabilityai/stable-diffusion-2-1", subfolder="text_encoder")
            self.text_encoder.eval()
        
        elif self.cfg.MODEL.DIFFUSION.BACKBONE == 'DiT':
            pass 
        
        
        self.test_score_threshold = cfg.MODEL.TRANSFORMER.INFERENCE_TH_TEST
        self.use_polygon = cfg.MODEL.TRANSFORMER.USE_POLYGON
        self.testr = TESTR_SD21(cfg)

        box_matcher, point_matcher = build_matcher(cfg)
        
        loss_cfg = cfg.MODEL.TRANSFORMER.LOSS
        weight_dict = {'loss_ce': loss_cfg.POINT_CLASS_WEIGHT, 'loss_ctrl_points': loss_cfg.POINT_COORD_WEIGHT, 'loss_texts': loss_cfg.POINT_TEXT_WEIGHT}
        enc_weight_dict = {'loss_bbox': loss_cfg.BOX_COORD_WEIGHT, 'loss_giou': loss_cfg.BOX_GIOU_WEIGHT, 'loss_ce': loss_cfg.BOX_CLASS_WEIGHT}
        if loss_cfg.AUX_LOSS:
            aux_weight_dict = {}
            # decoder aux loss
            for i in range(cfg.MODEL.TRANSFORMER.DEC_LAYERS - 1):
                aux_weight_dict.update(
                    {k + f'_{i}': v for k, v in weight_dict.items()})
            # encoder aux loss
            aux_weight_dict.update(
                {k + f'_enc': v for k, v in enc_weight_dict.items()})
            weight_dict.update(aux_weight_dict)

        enc_losses = ['labels', 'boxes']
        dec_losses = ['labels', 'ctrl_points', 'texts']

        self.criterion = SetCriterion(self.testr.num_classes, box_matcher, point_matcher,
                                      weight_dict, enc_losses, dec_losses, self.testr.num_ctrl_points, 
                                      focal_alpha=loss_cfg.FOCAL_ALPHA, focal_gamma=loss_cfg.FOCAL_GAMMA)

        pixel_mean = torch.Tensor(cfg.MODEL.PIXEL_MEAN).to(self.device).view(3, 1, 1)
        pixel_std = torch.Tensor(cfg.MODEL.PIXEL_STD).to(self.device).view(3, 1, 1)
        self.normalizer = lambda x: (x - pixel_mean) / pixel_std
        self.to(self.device)

    def preprocess_image(self, batched_inputs):
        """
        Normalize, pad and batch the input images.
        """
        # images = [self.normalizer(x["image"].to(self.device)) for x in batched_inputs]
        # images = ImageList.from_tensors(images)
        # return images
    
        resized_images = []
        for x in batched_inputs:
            image = x["image"].to(self.device)
            # Convert from Byte to Float and normalize to [0, 1]
            if image.dtype == torch.uint8:
                image = image.float()
            image = F.interpolate(image.unsqueeze(0), size=(512, 512), mode="bilinear", align_corners=False).squeeze(0)
            image = self.normalizer(image)
            resized_images.append(image)

        images = ImageList.from_tensors(resized_images)
        return images

    def forward(self, batched_inputs):
        """
        Args:
            batched_inputs: a list, batched outputs of :class:`DatasetMapper` .
                Each item in the list contains the inputs for one image.
                For now, each item in the list is a dict that contains:

                * image: Tensor, image in (C, H, W) format.
                * instances (optional): groundtruth :class:`Instances`
                * proposals (optional): :class:`Instances`, precomputed proposals.

                Other information that's included in the original dicts, such as:

                * "height", "width" (int): the output resolution of the model, used in inference.
                  See :meth:`postprocess` for details.

        Returns:
            list[dict]:
                Each dict is the output for one input image.
                The dict contains one key "instances" whose value is a :class:`Instances`.
                The :class:`Instances` object has the following keys:
                "pred_boxes", "pred_classes", "scores", "pred_masks", "pred_keypoints"
        """
        batched_imgs = self.preprocess_image(batched_inputs)  # normalized to [-1,1]
        images = batched_imgs.tensor                          # b 3 h w, where h=w=512 for sd2.1 input 
        bs = images.shape[0]

        # PHO         
        t = torch.tensor([0], device=self.device).expand(bs)
        prompt = [""] * bs
        with torch.no_grad():
            latent_dist = self.vae.encode(images).latent_dist
            latents = latent_dist.mode() * self.vae.config.scaling_factor
            text_inputs = self.tokenizer(prompt, padding="max_length", max_length=77, truncation=True, return_tensors="pt").to(self.device)
            text_embeddings = self.text_encoder(input_ids=text_inputs.input_ids).last_hidden_state  # (b, 77, 768)
            model_out = self.diffusion_model(
                sample=latents,
                timestep=t,
                encoder_hidden_states=text_embeddings,
                model_cfg=self.cfg.MODEL,
            )
        output = self.testr(model_out['unet_feat'])
        
        if self.training:
            gt_instances = [x["instances"].to(self.device) for x in batched_inputs]
            targets = self.prepare_targets(gt_instances)
            loss_dict = self.criterion(output, targets)
            weight_dict = self.criterion.weight_dict
            for k in loss_dict.keys():
                if k in weight_dict:
                    loss_dict[k] *= weight_dict[k]

            ctrl_point_cls = output["pred_logits"]
            ctrl_point_coord = output["pred_ctrl_points"]
            text_pred = output["pred_texts"]
            results = self.inference(ctrl_point_cls, ctrl_point_coord, text_pred, batched_imgs.image_sizes)
            processed_results = []
            for results_per_image, input_per_image, image_size in zip(results, batched_inputs, batched_imgs.image_sizes):
                height = input_per_image.get("height", image_size[0])
                width = input_per_image.get("width", image_size[1])
                r = detector_postprocess(results_per_image, height, width)
                processed_results.append({"instances": r})
            return loss_dict, processed_results
        else:
            ctrl_point_cls = output["pred_logits"]
            ctrl_point_coord = output["pred_ctrl_points"]
            text_pred = output["pred_texts"]
            results = self.inference(ctrl_point_cls, ctrl_point_coord, text_pred, batched_imgs.image_sizes)
            processed_results = []
            for results_per_image, input_per_image, image_size in zip(results, batched_inputs, batched_imgs.image_sizes):
                height = input_per_image.get("height", image_size[0])
                width = input_per_image.get("width", image_size[1])
                r = detector_postprocess(results_per_image, height, width)
                processed_results.append({"instances": r})
            return processed_results
        
        
        # if self.training:
        #     gt_instances = [x["instances"].to(self.device) for x in batched_inputs]
        #     targets = self.prepare_targets(gt_instances)
        #     loss_dict = self.criterion(output, targets)
        #     weight_dict = self.criterion.weight_dict
        #     for k in loss_dict.keys():
        #         if k in weight_dict:
        #             loss_dict[k] *= weight_dict[k]
        #     return loss_dict
        # else:
        #     ctrl_point_cls = output["pred_logits"]
        #     ctrl_point_coord = output["pred_ctrl_points"]
        #     text_pred = output["pred_texts"]
        #     results = self.inference(ctrl_point_cls, ctrl_point_coord, text_pred, images.image_sizes)
        #     processed_results = []
        #     for results_per_image, input_per_image, image_size in zip(results, batched_inputs, images.image_sizes):
        #         height = input_per_image.get("height", image_size[0])
        #         width = input_per_image.get("width", image_size[1])
        #         r = detector_postprocess(results_per_image, height, width)
        #         processed_results.append({"instances": r})
        #     return processed_results

    def prepare_targets(self, targets):
        new_targets = []
        for targets_per_image in targets:
            h, w = targets_per_image.image_size
            image_size_xyxy = torch.as_tensor([w, h, w, h], dtype=torch.float, device=self.device)
            gt_classes = targets_per_image.gt_classes
            gt_boxes = targets_per_image.gt_boxes.tensor / image_size_xyxy
            gt_boxes = box_xyxy_to_cxcywh(gt_boxes)
            raw_ctrl_points = targets_per_image.polygons if self.use_polygon else targets_per_image.beziers
            gt_ctrl_points = raw_ctrl_points.reshape(-1, self.testr.num_ctrl_points, 2) / torch.as_tensor([w, h], dtype=torch.float, device=self.device)[None, None, :]
            gt_text = targets_per_image.text
            new_targets.append({"labels": gt_classes, "boxes": gt_boxes, "ctrl_points": gt_ctrl_points, "texts": gt_text})
        return new_targets

    def inference(self, ctrl_point_cls, ctrl_point_coord, text_pred, image_sizes):
        assert len(ctrl_point_cls) == len(image_sizes)
        results = []

        text_pred = torch.softmax(text_pred, dim=-1)
        prob = ctrl_point_cls.mean(-2).sigmoid()
        scores, labels = prob.max(-1)

        for scores_per_image, labels_per_image, ctrl_point_per_image, text_per_image, image_size in zip(
            scores, labels, ctrl_point_coord, text_pred, image_sizes
        ):
            selector = scores_per_image >= self.test_score_threshold
            scores_per_image = scores_per_image[selector]
            labels_per_image = labels_per_image[selector]
            ctrl_point_per_image = ctrl_point_per_image[selector]
            text_per_image = text_per_image[selector]
            result = Instances(image_size)
            result.scores = scores_per_image
            result.pred_classes = labels_per_image
            result.rec_scores = text_per_image
            ctrl_point_per_image[..., 0] *= image_size[1]
            ctrl_point_per_image[..., 1] *= image_size[0]
            if self.use_polygon:
                result.polygons = ctrl_point_per_image.flatten(1)
            else:
                result.beziers = ctrl_point_per_image.flatten(1)
            _, topi = text_per_image.topk(1)
            result.recs = topi.squeeze(-1)
            results.append(result)
        return results
