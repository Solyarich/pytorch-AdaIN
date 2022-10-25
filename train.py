import argparse
import os
import random
from pathlib import Path

import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.utils.data as data
from PIL import Image, ImageFile
from torch.utils.tensorboard import SummaryWriter
from torchvision import transforms
from tqdm import tqdm

import net
from sampler import InfiniteSamplerWrapper

cudnn.benchmark = True
Image.MAX_IMAGE_PIXELS = None  # Disable DecompressionBombError
# Disable OSError: image file is truncated
ImageFile.LOAD_TRUNCATED_IMAGES = True


def seed_everything(seed=42):
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.set_printoptions(precision=10)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True


def train_transform():
    transform_list = [
        transforms.Resize(size=(512, 512)),
        # transforms.RandomCrop(256),
        transforms.ToTensor(),
    ]
    return transforms.Compose(transform_list)


def val_transform():
    transform_list = [
        transforms.Resize(size=(512, 512)),
        # transforms.RandomCrop(256),
        transforms.ToTensor(),
    ]
    return transforms.Compose(transform_list)


class FlatFolderDataset(data.Dataset):
    def __init__(self, root, transform):
        super(FlatFolderDataset, self).__init__()
        self.root = root
        self.paths = list(Path(self.root).glob("*"))
        self.transform = transform

    def __getitem__(self, index):
        path = self.paths[index]
        img = Image.open(str(path)).convert("RGB")
        img = self.transform(img)
        return img

    def __len__(self):
        return len(self.paths)

    def name(self):
        return "FlatFolderDataset"


def adjust_learning_rate(optimizer, iteration_count):
    """Imitating the original implementation"""
    lr = args.lr / (1.0 + args.lr_decay * iteration_count)
    for param_group in optimizer.param_groups:
        param_group["lr"] = lr


parser = argparse.ArgumentParser()
# Basic options
parser.add_argument(
    "--train_content_dir",
    type=str,
    required=True,
    help="Directory path to a batch of train content images",
)
parser.add_argument(
    "--val_content_dir",
    type=str,
    required=True,
    help="Directory path to a batch of validation content images",
)
parser.add_argument(
    "--style_dir",
    type=str,
    required=True,
    help="Directory path to a batch of style images",
)
parser.add_argument("--vgg", type=str, default="models/vgg_normalised.pth")

# training options
parser.add_argument(
    "--save_dir", default="./experiments", help="Directory to save the model"
)
parser.add_argument("--log_dir", default="./logs", help="Directory to save the log")
parser.add_argument("--lr", type=float, default=1e-4)
parser.add_argument("--lr_decay", type=float, default=1e-5)
parser.add_argument("--max_iter", type=int, default=160000)
parser.add_argument("--batch_size", type=int, default=8)
parser.add_argument("--style_weight", type=float, default=1.0)
parser.add_argument("--content_weight", type=float, default=1.0)
parser.add_argument("--n_threads", type=int, default=16)
parser.add_argument("--save_model_interval", type=int, default=10000)
parser.add_argument("--loss_print_interval", type=int, default=100)
args = parser.parse_args()

seed_everything()
device = torch.device("cuda")
save_dir = Path(args.save_dir)
save_dir.mkdir(exist_ok=True, parents=True)
log_dir = Path(args.log_dir)
log_dir.mkdir(exist_ok=True, parents=True)
writer = SummaryWriter(log_dir=str(log_dir))

decoder = net.decoder
vgg = net.vgg

vgg.load_state_dict(torch.load(args.vgg))
vgg = nn.Sequential(*list(vgg.children())[:31])
network = net.Net(vgg, decoder)
network.to(device)

train_content_tf = train_transform()
val_content_tf = val_transform()
style_tf = train_transform()

train_content_dataset = FlatFolderDataset(args.train_content_dir, train_content_tf)
val_content_dataset = FlatFolderDataset(args.val_content_dir, val_content_tf)
style_dataset = FlatFolderDataset(args.style_dir, style_tf)

train_content_iter = iter(
    data.DataLoader(
        train_content_dataset,
        batch_size=args.batch_size,
        sampler=InfiniteSamplerWrapper(train_content_dataset),
        num_workers=args.n_threads,
    )
)
val_content_iter = iter(
    data.DataLoader(
        val_content_dataset,
        batch_size=args.batch_size,
        sampler=InfiniteSamplerWrapper(val_content_dataset),
        num_workers=args.n_threads,
    )
)

style_iter = iter(
    data.DataLoader(
        style_dataset,
        batch_size=args.batch_size,
        sampler=InfiniteSamplerWrapper(style_dataset),
        num_workers=args.n_threads,
    )
)

optimizer = torch.optim.Adam(network.decoder.parameters(), lr=args.lr)

for i in tqdm(range(args.max_iter)):
    adjust_learning_rate(optimizer, iteration_count=i)
    cur_lr = optimizer.param_groups[0]["lr"]
    writer.add_scalar("Learning rate", cur_lr, i + 1)

    style_images = next(style_iter).to(device)

    # Train part
    network.train()
    train_content_images = next(train_content_iter).to(device)
    train_loss_c, train_loss_s = network(train_content_images, style_images)
    train_loss_c = args.content_weight * train_loss_c
    train_loss_s = args.style_weight * train_loss_s
    train_loss = train_loss_c + train_loss_s

    optimizer.zero_grad()
    train_loss.backward()
    optimizer.step()

    writer.add_scalar("train loss_content", train_loss_c.item(), i + 1)
    writer.add_scalar("train loss_style", train_loss_s.item(), i + 1)

    # Val part
    network.eval()
    with torch.no_grad():
        val_content_images = next(val_content_iter).to(device)
        val_loss_c, val_loss_s = network(val_content_images, style_images)
        val_loss_c = args.content_weight * val_loss_c
        val_loss_s = args.style_weight * val_loss_s
        val_loss = val_loss_c + val_loss_s

    writer.add_scalar("val loss_content", val_loss_c.item(), i + 1)
    writer.add_scalar("val loss_style", val_loss_s.item(), i + 1)

    if (i + 1) % args.loss_print_interval == 0:
        print(
            f"Learning rate = {cur_lr}; Train Loss = {train_loss.item()}; Val Loss = {val_loss.item()}"
        )

    if (i + 1) % args.save_model_interval == 0 or (i + 1) == args.max_iter:
        state_dict = net.decoder.state_dict()
        for key in state_dict.keys():
            state_dict[key] = state_dict[key].to(torch.device("cpu"))
        torch.save(state_dict, save_dir / "decoder_iter_{:d}.pth.tar".format(i + 1))
writer.close()
