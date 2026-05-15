import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import io
from PIL import Image

class ResidualDenseBlock(nn.Module):

    def __init__(self, num_feat=64, num_grow_ch=32):
        super().__init__()
        self.conv1 = nn.Conv2d(num_feat, num_grow_ch, 3, 1, 1)
        self.conv2 = nn.Conv2d(num_feat + num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv3 = nn.Conv2d(num_feat + 2 * num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv4 = nn.Conv2d(num_feat + 3 * num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv5 = nn.Conv2d(num_feat + 4 * num_grow_ch, num_feat, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x):
        x1 = self.lrelu(self.conv1(x))
        x2 = self.lrelu(self.conv2(torch.cat([x, x1], 1)))
        x3 = self.lrelu(self.conv3(torch.cat([x, x1, x2], 1)))
        x4 = self.lrelu(self.conv4(torch.cat([x, x1, x2, x3], 1)))
        return self.conv5(torch.cat([x, x1, x2, x3, x4], 1)) * 0.2 + x

class RRDB(nn.Module):

    def __init__(self, num_feat=64, num_grow_ch=32):
        super().__init__()
        self.rdb1 = ResidualDenseBlock(num_feat, num_grow_ch)
        self.rdb2 = ResidualDenseBlock(num_feat, num_grow_ch)
        self.rdb3 = ResidualDenseBlock(num_feat, num_grow_ch)

    def forward(self, x):
        return self.rdb3(self.rdb2(self.rdb1(x))) * 0.2 + x

class RRDBNet(nn.Module):

    def __init__(self, num_in_ch=3, num_out_ch=3, num_feat=64, num_block=6, num_grow_ch=32):
        super().__init__()
        self.conv_first = nn.Conv2d(num_in_ch, num_feat, 3, 1, 1)
        self.body = nn.Sequential(*[RRDB(num_feat, num_grow_ch) for _ in range(num_block)])
        self.conv_body = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_up1 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_up2 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_hr = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_last = nn.Conv2d(num_feat, num_out_ch, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x):
        feat = self.conv_first(x)
        feat = feat + self.conv_body(self.body(feat))
        feat = self.lrelu(self.conv_up1(F.interpolate(feat, scale_factor=2, mode='nearest')))
        feat = self.lrelu(self.conv_up2(F.interpolate(feat, scale_factor=2, mode='nearest')))
        return self.conv_last(self.lrelu(self.conv_hr(feat)))
_model = None
_MODEL_PATH = '/root/Projects/NanoHatani/models/anime.pth'
_TILE = 256
_TILE_PAD = 10

def _load_model():
    global _model
    if _model is not None:
        return _model
    m = RRDBNet()
    st = torch.load(_MODEL_PATH, map_location='cpu', weights_only=False)
    p = st.get('params_ema', st.get('params', st))
    p = {k.replace('module.', ''): v for (k, v) in p.items()}
    m.load_state_dict(p)
    m.eval()
    _model = m
    return _model

def upscale_anime(image_bytes: bytes) -> bytes:
    model = _load_model()
    img = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    (w, h) = img.size
    img_np = np.array(img).astype(np.float32) / 255.0
    img_t = torch.from_numpy(img_np.transpose(2, 0, 1)).unsqueeze(0)
    (_, _, H, W) = img_t.shape
    (out_h, out_w) = (H * 4, W * 4)
    output = torch.zeros(1, 3, out_h, out_w)
    for row in range(0, H, _TILE):
        for col in range(0, W, _TILE):
            r0 = max(row - _TILE_PAD, 0)
            r1 = min(row + _TILE + _TILE_PAD, H)
            c0 = max(col - _TILE_PAD, 0)
            c1 = min(col + _TILE + _TILE_PAD, W)
            tile = img_t[:, :, r0:r1, c0:c1]
            with torch.no_grad():
                tile_out = model(tile)
            out_r0 = (row - r0) * 4
            out_r1 = out_r0 + (min(row + _TILE, H) - row) * 4
            out_c0 = (col - c0) * 4
            out_c1 = out_c0 + (min(col + _TILE, W) - col) * 4
            dest_r0 = row * 4
            dest_r1 = min(row + _TILE, H) * 4
            dest_c0 = col * 4
            dest_c1 = min(col + _TILE, W) * 4
            output[:, :, dest_r0:dest_r1, dest_c0:dest_c1] = tile_out[:, :, out_r0:out_r1, out_c0:out_c1]
    out_np = output.squeeze(0).clamp(0, 1).numpy().transpose(1, 2, 0)
    out_img = Image.fromarray((out_np * 255).astype(np.uint8))
    buf = io.BytesIO()
    out_img.save(buf, format='PNG')
    return buf.getvalue()