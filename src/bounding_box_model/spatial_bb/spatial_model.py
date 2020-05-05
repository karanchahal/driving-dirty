import random
import numpy as np
import torch
random.seed(0)
np.random.seed(0)
torch.manual_seed(0)

from argparse import ArgumentParser, Namespace

import torchvision
from  torchvision  import transforms
from torch import nn
from torch.nn import functional as F
from torch.utils.data import DataLoader

from pytorch_lightning import LightningModule, Trainer
from test_tube import HyperOptArgumentParser

from src.utils import convert_map_to_lane_map
from src.utils.data_helper import LabeledDataset
from src.utils.helper import collate_fn, boxes_to_binary_map


from src.autoencoder.autoencoder import BasicAE
from src.bounding_box_model.spatial_bb.components import SpatialMappingCNN, BoxesMergingCNN

from src.utils.helper import compute_ts_road_map


class RoadMap(LightningModule):

    def __init__(self, hparams):
        super().__init__()
        self.hparams = hparams
        self.output_dim = 800 * 800
        #self.kernel_size = 4

        # TODO: add pretrained weight path
        # TODO: remove this to train models again
        d = dict(
            latent_dim = 64,
            hidden_dim = 128,
            batch_size = 16
        )
        hparams2 = Namespace(**d)

        # BasicAE.load_from_checkpoint(self.hparams.pretrained_path)
        # pretrained feature extractor - using our own trained Encoder
        self.ae = BasicAE(hparams2)
        self.frozen = True
        self.ae.freeze()
        self.ae.decoder = None

        self.space_map_cnn = SpatialMappingCNN()

        self.box_merge = BoxesMergingCNN()

    def wide_stitch_six_images(self, x):
        # change from tuple len([6 x 3 x H x W]) = b --> tensor [b x 6 x 3 x H x W]
        #x = torch.stack(sample, dim=0)

        # reorder order of 6 images (in first dimension) to become 180 degree view
        x = x[:, [0, 1, 2, 5, 4, 3]]

        # rearrange axes and reshape to wide format
        b, num_imgs, c, h, w = x.size()
        x = x.permute(0, 2, 3, 1, 4).reshape(b, c, h, -1)
        #assert x.size(-1) == 6 * 306
        return x

    def forward(self, x):
        # change from tuple len([6 x 3 x H x W]) = b --> tensor [b x 6 x 3 x H x W]
        x = torch.stack(x, dim=0)

        # spatial representation
        spacial_rep = self.space_map_cnn(x)

        # selfsupervised representation
        x = self.wide_stitch_six_images(x)
        ssr = self.ae.encoder(x, c3_only=True)

        # combine two -> [b, 800, 800]
        yhat = self.box_merge(ssr, spacial_rep)
        yhat = yhat.squeeze(1)

        return yhat

    def bb_coord_to_map(self, target):
        # target is tuple with len b
        results = []
        for i, sample in enumerate(target):
            # tuple of len 2 -> [num_boxes, 2, 4]
            sample = sample['bounding_box']
            map = boxes_to_binary_map(sample)
            results.append(map)

        results = torch.tensor(results)
        return results

    def _run_step(self, batch, batch_idx, step_name):
        sample, target, road_image = batch

        # change target from dict of bounding box coords --> [b, 800, 800]
        target_bb_img = self.bb_coord_to_map(target)
        target_bb_img = target_bb_img.type_as(sample[0])

        # forward pass to find predicted roadmap
        pred_bb_img = self(sample)

        # every 10 epochs we look at inputs + predictions
        if True:
        #if batch_idx % self.hparams.output_img_freq == 0:
            x0 = sample[0]
            target_bb_img0 = target_bb_img[0]
            pred_bb_img0 = pred_bb_img[0]

            self._log_rm_images(x0, target_bb_img0, pred_bb_img0, step_name)

        # calculate mse loss between pixels
        loss = F.mse_loss(target_bb_img, pred_bb_img)

        return loss, target_bb_img, pred_bb_img

    def _log_rm_images(self, x, target, pred, step_name, limit=1):

        input_images = torchvision.utils.make_grid(x)
        target = torchvision.utils.make_grid(target)
        pred = torchvision.utils.make_grid(pred)

        self.logger.experiment.add_image(f'{step_name}_input_images', input_images, self.trainer.global_step)
        self.logger.experiment.add_image(f'{step_name}_target_bbs', target, self.trainer.global_step)
        self.logger.experiment.add_image(f'{step_name}_pred_bbs', pred, self.trainer.global_step)

    def training_step(self, batch, batch_idx):

        if self.current_epoch >= 30 and self.frozen:
            self.frozen=False
            self.ae.unfreeze()

        train_loss, _, _ = self._run_step(batch, batch_idx, step_name='train')
        train_tensorboard_logs = {'train_loss': train_loss}
        return {'loss': train_loss, 'log': train_tensorboard_logs}

    def validation_step(self, batch, batch_idx):
        val_loss, target_rm, pred_rm = self._run_step(batch, batch_idx, step_name='valid')

        return {'val_loss': val_loss}

    def validation_epoch_end(self, outputs):
        avg_val_loss = torch.stack([x['val_loss'] for x in outputs]).mean()
        val_tensorboard_logs = {'avg_val_loss': avg_val_loss}
        return {'val_loss': avg_val_loss, 'log': val_tensorboard_logs}

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.hparams.learning_rate)

    def prepare_data(self):
        image_folder = self.hparams.link
        annotation_csv = self.hparams.link + '/annotation.csv'

        transform = transforms.Compose(
            [
                torchvision.transforms.ToTensor()
            ]
        )

        labeled_dataset = LabeledDataset(image_folder=image_folder,
                                         annotation_file=annotation_csv,
                                         scene_index=np.arange(106, 134),
                                         transform=transform,
                                         extra_info=False)

        trainset_size = round(0.8 * len(labeled_dataset))
        validset_size = round(0.2 * len(labeled_dataset))

        # split train + valid at the sample level (ie 6 image collections) not scene/video level
        self.trainset, self.validset = torch.utils.data.random_split(labeled_dataset,
                                                                     lengths = [trainset_size, validset_size])

    def train_dataloader(self):
        loader = DataLoader(self.trainset,
                            batch_size=self.hparams.batch_size,
                            shuffle=True,
                            num_workers=4,
                            collate_fn=collate_fn)
        return loader

    def val_dataloader(self):
        # don't shuffle validation batches
        loader = DataLoader(self.validset,
                            batch_size=self.hparams.batch_size,
                            shuffle=False,
                            num_workers=4,
                            collate_fn=collate_fn)
        return loader

    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = HyperOptArgumentParser(parents=[parent_parser], add_help=False)

        # want to optimize this parameter
        #parser.opt_list('--batch_size', type=int, default=16, options=[16, 10, 8], tunable=False)
        parser.opt_list('--learning_rate', type=float, default=0.005, options=[1e-1, 1e-2, 1e-3, 1e-4, 1e-5], tunable=True)
        parser.add_argument('--batch_size', type=int, default=16)
        # fixed arguments
        parser.add_argument('--link', type=str, default='/Users/annika/Developer/driving-dirty/data')
        parser.add_argument('--pretrained_path', type=str, default='/Users/annika/Developer/driving-dirty/lightning_logs/version_3/checkpoints/epoch=4.ckpt')
        parser.add_argument('--output_img_freq', type=int, default=1000)
        return parser


if __name__ == '__main__':
    parser = ArgumentParser()
    parser = Trainer.add_argparse_args(parser)
    parser = RoadMap.add_model_specific_args(parser)
    args = parser.parse_args()

    model = RoadMap(args)
    trainer = Trainer.from_argparse_args(args)
    trainer.fit(model)