from torch.utils.data import DataLoader
from geoseg.losses import *
from geoseg.datasets.loveda_dataset import *
from geoseg.models.UNetFormer import UNetFormer, QuantizedUNetFormer, calibrate
from catalyst.contrib.nn import Lookahead
from catalyst import utils
import torch.quantization as quantization
import torch
import torch.nn.functional as F
import numpy as np
import albumentations as albu
from albumentations.pytorch import ToTensorV2
from tqdm import tqdm
import os

# training hparam
max_epoch = 30
ignore_index = len(CLASSES)
train_batch_size = 16
val_batch_size = 16
lr = 6e-4
weight_decay = 0.01
backbone_lr = 6e-5
backbone_weight_decay = 0.01
num_classes = len(CLASSES)
classes = CLASSES

weights_name = "unetformer-r18-512crop-ms-epoch30-rep"
weights_path = "model_weights/loveda/{}".format(weights_name)
test_weights_name = "last"
log_name = 'loveda/{}'.format(weights_name)
monitor = 'val_mIoU'
monitor_mode = 'max'
save_top_k = 1
save_last = True
check_val_every_n_epoch = 1
pretrained_ckpt_path = None # the path for the pretrained model weight
gpus = 'auto'  # default or gpu ids:[0] or gpu nums: 2, more setting can refer to pytorch_lightning
resume_ckpt_path = None  # whether continue training with the checkpoint, default None

#  define the network, loss + dataloader
net = QuantizedUNetFormer(num_classes=num_classes)

loss = UnetFormerLoss(ignore_index=ignore_index)
use_aux_loss = True

def get_training_transform():
    train_transform = [
        albu.HorizontalFlip(p=0.5),
        albu.Normalize()
    ]
    return albu.Compose(train_transform)


def train_aug(img, mask):
    crop_aug = Compose([RandomScale(scale_list=[0.75, 1.0, 1.25, 1.5], mode='value'),
                        SmartCropV1(crop_size=512, max_ratio=0.75, ignore_index=ignore_index, nopad=False)])
    img, mask = crop_aug(img, mask)
    img, mask = np.array(img), np.array(mask)
    aug = get_training_transform()(image=img.copy(), mask=mask.copy())
    img, mask = aug['image'], aug['mask']
    return img, mask


train_dataset = LoveDATrainDataset(transform=train_aug, data_root='data/LoveDA/Train')

val_dataset = loveda_val_dataset

test_dataset = LoveDATestDataset()

train_loader = DataLoader(dataset=train_dataset,
                          batch_size=train_batch_size,
                          num_workers=4,
                          pin_memory=True,
                          shuffle=True,
                          drop_last=True)

val_loader = DataLoader(dataset=val_dataset,
                        batch_size=val_batch_size,
                        num_workers=4,
                        shuffle=False,
                        pin_memory=True,
                        drop_last=False)

# Quantization
net.qconfig = torch.quantization.get_default_qconfig('fbgemm')
torch.quantization.prepare(net, inplace=True)

# Calibration (with the training data loader)
calibrate(net, train_loader)

# Convert to a quantized model
torch.quantization.convert(net, inplace=True)

# define the optimizer
layerwise_params = {"backbone.*": dict(lr=backbone_lr, weight_decay=backbone_weight_decay)}
net_params = utils.process_model_params(net, layerwise_params=layerwise_params)
base_optimizer = torch.optim.AdamW(net_params, lr=lr, weight_decay=weight_decay)
optimizer = Lookahead(base_optimizer)
lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max_epoch, eta_min=1e-6)

# Training loop, evaluation, and checkpointing (PLZ make this work)
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
net.to(device)

best_miou = 0

for epoch in range(max_epoch):
    net.train()
    train_loss = 0
    train_loader = tqdm(train_loader, desc=f"Epoch {epoch+1}/{max_epoch}")
    
    for batch in train_loader:
        images, masks = batch
        images, masks = images.to(device), masks.to(device)

        optimizer.zero_grad()
        outputs = net(images)
        if use_aux_loss:
            outputs, aux = outputs
            loss_value = loss(outputs, masks) + 0.4 * loss(aux, masks)
        else:
            loss_value = loss(outputs, masks)

        loss_value.backward()
        optimizer.step()
        train_loss += loss_value.item()

    lr_scheduler.step()

    net.eval()
    val_loss = 0
    miou = 0

    with torch.no_grad():
        for batch in val_loader:
            images, masks = batch
            images, masks = images.to(device), masks.to(device)

            outputs = net(images)
            if use_aux_loss:
                outputs, aux = outputs
                loss_value = loss(outputs, masks) + 0.4 * loss(aux, masks)
            else:
                loss_value = loss(outputs, masks)

            val_loss += loss_value.item()
            miou += compute_miou(outputs, masks, num_classes, ignore_index)  # Compute mIoU, assuming you have a function for this

    val_loss /= len(val_loader)
    miou /= len(val_loader)

    print(f"Epoch {epoch+1}/{max_epoch}, Train Loss: {train_loss/len(train_loader)}, Val Loss: {val_loss}, Val mIoU: {miou}")

    if miou > best_miou:
        best_miou = miou
        torch.save(net.state_dict(), os.path.join(weights_path, f"{weights_name}_best.pth"))

    if save_last:
        torch.save(net.state_dict(), os.path.join(weights_path, f"{weights_name}_last.pth"))
