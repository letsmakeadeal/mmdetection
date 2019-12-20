import math

import torch.nn as nn
import torch.nn.functional as F

from .base_backbone import BaseBackbone, filter_by_out_idices

__all__ = ['ScarletA', 'ScarletB', 'ScarletC']

from ..utils.activations import Mish


def stem(inp, oup, stride):
    return nn.Sequential(
        nn.Conv2d(inp, oup, 3, stride, 1, bias=False),
        nn.BatchNorm2d(oup),
        Mish()
    )


def separable_conv(inp, oup):
    return nn.Sequential(
        nn.Conv2d(inp, inp, 3, 1, 1, groups=inp, bias=False),
        nn.BatchNorm2d(inp),
        Mish(),
        nn.Conv2d(inp, oup, 1, 1, 0, bias=False),
        nn.BatchNorm2d(oup),
    )


def conv_before_pooling(inp, oup):
    return nn.Sequential(
        nn.Conv2d(inp, oup, 1, 1, 0, bias=False),
        nn.BatchNorm2d(oup),
        Mish()
    )


class Identity(nn.Module):
    def __init__(self):
        super(Identity, self).__init__()

    def forward(self, inputs):
        return inputs


class HSwish(nn.Module):
    def __init__(self, inplace=True):
        super(HSwish, self).__init__()
        self.inplace = inplace

    def forward(self, x):
        out = x * F.relu6(x + 3, inplace=self.inplace) / 6
        return out


class HSigmoid(nn.Module):
    def __init__(self, inplace=True):
        super(HSigmoid, self).__init__()
        self.inplace = inplace

    def forward(self, x):
        out = F.relu6(x + 3, inplace=self.inplace) / 6
        return out


class SqueezeExcite(nn.Module):
    def __init__(self, in_channel,
                 reduction=4,
                 squeeze_act=nn.ReLU(inplace=True),
                 excite_act=HSigmoid(inplace=True)):
        super(SqueezeExcite, self).__init__()
        self.global_pooling = nn.AdaptiveAvgPool2d(1)
        self.squeeze_conv = nn.Conv2d(in_channels=in_channel,
                                      out_channels=in_channel // reduction,
                                      kernel_size=1,
                                      bias=True)
        self.squeeze_act = squeeze_act
        self.excite_conv = nn.Conv2d(in_channels=in_channel // reduction,
                                     out_channels=in_channel,
                                     kernel_size=1,
                                     bias=True)
        self.excite_act = excite_act

    def forward(self, inputs):
        feature_pooling = self.global_pooling(inputs)
        feature_squeeze_conv = self.squeeze_conv(feature_pooling)
        feature_squeeze_act = self.squeeze_act(feature_squeeze_conv)
        feature_excite_conv = self.excite_conv(feature_squeeze_act)
        feature_excite_act = self.excite_act(feature_excite_conv)
        return inputs * feature_excite_act


class InvertedResidual(nn.Module):
    def __init__(self, inp, oup, kernel_size, stride, expand_ratio, is_use_se):
        super(InvertedResidual, self).__init__()
        assert stride in [1, 2]
        self.stride = stride
        self.is_use_se = is_use_se
        padding = kernel_size // 2
        hidden_dim = round(inp * expand_ratio)
        self.use_res_connect = self.stride == 1 and inp == oup
        self.conv1 = nn.Conv2d(inp, hidden_dim, 1, 1, 0, bias=False)
        self.bn1 = nn.BatchNorm2d(hidden_dim)
        self.act1 = HSwish(inplace=True)
        self.conv2 = nn.Conv2d(hidden_dim, hidden_dim, kernel_size, stride, padding, groups=hidden_dim, bias=False)
        self.bn2 = nn.BatchNorm2d(hidden_dim)
        self.act2 = HSwish(inplace=True)
        if self.is_use_se is True:
            self.mid_se = SqueezeExcite(hidden_dim)
        self.conv3 = nn.Conv2d(hidden_dim, oup, 1, 1, 0, bias=False)
        self.bn3 = nn.BatchNorm2d(oup)

    def forward(self, x):
        inputs = x
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.act1(x)
        x = self.conv2(x)
        x = self.bn2(x)
        x = self.act2(x)
        if self.is_use_se is True:
            x = self.mid_se(x)
        x = self.conv3(x)
        x = self.bn3(x)
        if self.use_res_connect:
            return inputs + x
        else:
            return x


class ScarletBase(BaseBackbone):
    def __init__(self, mb_config: list, input_channel: int, last_channel: int):
        super(ScarletBase, self).__init__()
        self.last_channel = last_channel
        self.stem = stem(3, 32, 2)
        self.separable_conv = separable_conv(32, 16)
        self.block_names = []
        for idx, each_config in enumerate(mb_config):
            if each_config == "identity":
                self.__setattr__(f'identity_{idx}', Identity())
                self.block_names.append(f'identity_{idx}')
                continue
            t, c, k, s, e = each_config
            output_channel = c
            block_name = f'block_{idx}' if s == 1 else f'strided_block_{idx}'
            self.__setattr__(
                block_name,
                InvertedResidual(input_channel, output_channel, k, s, expand_ratio=t, is_use_se=e))
            self.block_names.append(block_name)
            input_channel = output_channel
        self.conv_before_pooling = conv_before_pooling(input_channel, self.last_channel)
        self._initialize_weights()

    @filter_by_out_idices
    def forward(self, x):
        skips = []
        x = self.stem(x)
        x = self.separable_conv(x)

        for block_name in self.block_names:
            if block_name.startswith('strided'):
                skips.append(x)
            x = self.__getattr__(block_name)(x)

        x = self.conv_before_pooling(x)
        skips.append(x)
        return skips

    def _initialize_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()


class ScarletA(ScarletBase):
    def __init__(self):
        super(ScarletA, self).__init__(
            mb_config=[
                # expansion, out_channel, kernel_size, stride, se
                [3, 32, 7, 2, False],
                [6, 32, 5, 1, False],
                [3, 40, 5, 2, False],
                [3, 40, 7, 1, True],
                [6, 40, 3, 1, True],
                [3, 40, 5, 1, True],
                [6, 80, 3, 2, True],
                [3, 80, 3, 1, False],
                [3, 80, 7, 1, True],
                [3, 80, 7, 1, False],
                [3, 96, 5, 1, False],
                [3, 96, 7, 1, True],
                [3, 96, 3, 1, False],
                [3, 96, 7, 1, True],
                [6, 192, 3, 2, True],
                [6, 192, 5, 1, True],
                [3, 192, 3, 1, True],
                [6, 192, 3, 1, True],
                [6, 320, 7, 1, True],
            ],
            input_channel=16,
            last_channel=1280)


class ScarletB(ScarletBase):
    def __init__(self):
        super(ScarletB, self).__init__(
            mb_config=[
                # expansion, out_channel, kernel_size, stride, se
                [3, 32, 3, 2, True],
                [3, 32, 5, 1, True],
                [3, 40, 3, 2, True],
                [6, 40, 7, 1, True],
                [3, 40, 3, 1, False],
                [3, 40, 5, 1, False],
                [6, 80, 7, 2, True],
                [3, 80, 3, 1, True],
                "identity",
                [3, 80, 5, 1, False],
                [3, 96, 3, 1, True],
                [3, 96, 3, 1, True],
                [6, 96, 7, 1, True],
                [3, 96, 3, 1, True],
                [6, 192, 5, 2, True],
                [6, 192, 5, 1, True],
                "identity",
                [6, 192, 7, 1, True],
                [6, 320, 5, 1, True],
            ],
            input_channel=16,
            last_channel=1280)


class ScarletC(ScarletBase):
    def __init__(self):
        super(ScarletC, self).__init__(
            mb_config=[
                # expansion, out_channel, kernel_size, stride, se
                [3, 32, 5, 2, True],
                [3, 32, 3, 1, True],
                [3, 40, 5, 2, True],
                "identity",
                "identity",
                [3, 40, 3, 1, False],
                [6, 80, 7, 2, True],
                [3, 80, 3, 1, True],
                [3, 80, 3, 1, True],
                [3, 80, 5, 1, False],
                [3, 96, 7, 1, True],
                [3, 96, 7, 1, False],
                [3, 96, 3, 1, True],
                [3, 96, 7, 1, True],
                [3, 192, 3, 2, True],
                "identity",
                [6, 192, 3, 1, True],
                [6, 192, 7, 1, True],
                [6, 320, 5, 1, True],
            ],
            input_channel=16,
            last_channel=1280)
