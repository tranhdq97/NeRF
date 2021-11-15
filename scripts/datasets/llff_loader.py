import os
import torch
import numpy as np
from glob import glob
from PIL import Image
from torchvision import transforms as T
from torch.utils.data import Dataset
from ..utils.llff_utils import *
from ..utils.ray_utils import get_rays, get_ray_dirs, get_ndc_rays


class LLFFDataset(Dataset):
    """LLFF dataset class.

    Args:
        root_dir (str): dataset directory
        split (str | train/val): training set or validation test
        img_wh (tuple): Scaling width, height
        transforms (object): transformer
        spheric_poses (bool): If True, the images are taken in a spheric
            inward-facing manner. Otherwise, in forward-facing one
        val_step (int): validation item getting step
        res_factor (int): downscale image resolution
    """
    def __init__(self, root_dir, split='train', img_wh=(504, 378),
                 spheric_poses=False, val_num=1, transforms=None,
                 res_factor=1):
        self.root_dir = root_dir
        self.split = split
        self.img_wh = img_wh
        self.spheric_poses = spheric_poses
        self.val_num = [max(1, val_num)]
        self.transforms = transforms if transforms else T.ToTensor()
        self.sfx = '' if res_factor == 1 else f'_{res_factor}'
        self.read_meta()

    def __len__(self):
        if self.split == 'train':
            return len(self.all_rays)
        elif self.split == 'val':
            return len(self.val_num)
        else:
            return len(self.poses_test)

    def __getitem__(self, idx):
        if self.split == 'train':
            sample = {
                'rays': self.all_rays[idx],
                'rgbs': self.all_rgbs[idx]
            }
        else:
            if self.split == 'val':
                c2w = torch.FloatTensor(self.c2w_val)
            else:
                c2w = torch.FloatTensor(self.poses_test[idx])

            rays_o, rays_d = get_rays(self.directions, c2w)
            if not self.spheric_poses:
                near, far = 0, 1
                rays_o, rays_d = get_ndc_rays(self.img_wh[1], self.img_wh[0],
                                              self.focal, 1., rays_o, rays_d)
            else:
                near = self.bounds.min()
                far = min(8 * near, self.bounds.max())

            rays = self.to_rays(rays_o, rays_d, near, far)
            sample = {
                'rays': rays,
                'c2w': c2w
            }
            if self.split == 'val':
                img = Image.open(self.image_path_val).convert('RGB')
                if img.size != self.img_wh:
                    img = img.resize(self.img_wh, Image.LANCZOS)

                img = self.transforms(img)
                img = img.view(3, -1).permute(1, 0)
                sample['rgbs'] = img

        return sample

    def read_meta(self):
        poses_bounds = np.load(os.path.join(self.root_dir, 'poses_bounds.npy'))
        self.image_paths = sorted(
            glob(os.path.join(self.root_dir, f'images{self.sfx}/*'))
        )
        if self.split in ['train', 'val']:
            assert len(poses_bounds) == len(self.image_paths)

        poses = poses_bounds[:, :15].reshape(-1, 3, 5)  # (N_images, 3, 5)
        self.bounds = poses_bounds[:, -2:]  # (N_images, 2)
        # Step 1: Rescale focal length according to resolution
        H, W, self.focal = poses[0, :, -1]  # Original intrinsics
        print(poses[0, :, -1])
        print(self.img_wh)
        assert H * self.img_wh[0] == W * self.img_wh[1], "Must same ratio"
        self.focal *= self.img_wh[0] / W
        # Step 2: Correct poses
        # down right back -> right up back
        poses = np.concatenate(
            [poses[..., 1:2], -poses[..., 0:1], poses[..., 2:4]], -1
        )  # (N_images, 3, 4) exclude H, W, focal
        self.poses, self.pose_avg = center_poses(poses)
        distance_from_center = np.linalg.norm(self.poses[..., 3], axis=1)
        val_idx = np.argmin(distance_from_center)  # TODO why chose the nearest one
        # Step 3: Correct scale so that the nearest depth is at a little more
        # than 1.0
        near_original = self.bounds.min()
        scale_factor = near_original * 0.75
        self.bounds /= scale_factor
        self.poses[..., 3] /= scale_factor
        self.directions = get_ray_dirs(
            self.img_wh[1], self.img_wh[0], self.focal
        )
        if self.split == 'train':
            self.all_rays = []
            self.all_rgbs = []
            for i, image_path in enumerate(self.image_paths):
                if i == val_idx:
                    continue
                c2w = torch.FloatTensor(self.poses[i])
                img = Image.open(image_path).convert('RGB')
                assert img.size[1] * self.img_wh[0] == \
                       img.size[0] * self.img_wh[1]
                if img.size != self.img_wh:
                    img = img.resize(self.img_wh, Image.LANCZOS)

                img = self.transforms(img)
                img = img.view(3, -1).permute(1, 0)
                self.all_rgbs.append(img)
                rays_o, rays_d = get_rays(self.directions, c2w)
                if not self.spheric_poses:
                    near, far = 0, 1
                    rays_o, rays_d = get_ndc_rays(self.img_wh[1],
                                                  self.img_wh[0],
                                                  self.focal,
                                                  1.0,
                                                  rays_o,
                                                  rays_d)
                else:
                    near = self.bounds.min()
                    far = min(8 * near, self.bounds.max())

                self.all_rays.append(self.to_rays(rays_o, rays_d, near, far))
            self.all_rays = torch.cat(self.all_rays, 0)
            self.all_rgbs = torch.cat(self.all_rgbs, 0)

        elif self.split == 'val':
            self.c2w_val = self.poses[val_idx]
            self.image_path_val = self.image_paths[val_idx]

        else:  # For testing
            if self.split.endswith('train'):
                self.poses_test = self.poses
            elif not self.spheric_poses:
                focus_depth = 3.5  # hardcoded
                radii = np.percentile(np.abs(self.poses[..., 3]), 90, axis=0)
                self.poses_test = create_spiral_poses(radii, focus_depth)
            else:
                radius = 1.1 * self.bounds.min()
                self.poses_test = create_spheric_poses(radius,
                                                       phi=-36,
                                                       theta=(0, 360))

    @staticmethod
    def to_rays(rays_o, rays_d, near, far):
        """Form rays"""
        return torch.cat([
            rays_o,
            rays_d,
            near * torch.ones_like(rays_o[:, :1]),
            far * torch.ones_like(rays_o[:, :1]),
        ], 1)