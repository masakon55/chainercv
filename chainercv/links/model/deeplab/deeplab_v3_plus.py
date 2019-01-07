import numpy as np
import chainer
import chainer.links as L
import chainer.functions as F
from chainercv.links import PickableSequentialChain
from chainercv.links import Conv2DBNActiv
from chainercv.links import SeparableConv2DBNActiv


class XceptionBlock(chainer.Chain):
    def __init__(self, in_channels, depthlist, stride=1, dilate=1,
                 skip_type='conv', activ_first=True, bn_kwargs={},
                 dw_activ_list=[None, None, None],
                 pw_activ_list=[F.relu, F.relu, None]):
        super(XceptionBlock, self).__init__()
        self.skip_type = skip_type
        self.activ_first = activ_first
        self.separable2_activ = pw_activ_list[1]

        with self.init_scope():
            self.separable1 = SeparableConv2DBNActiv(
                in_channels, depthlist[0], 3, 1, dilate, dilate, bn_kwargs=bn_kwargs,
                depthwise_activ=dw_activ_list[0], pointwise_activ=pw_activ_list[0])
            self.separable2 = SeparableConv2DBNActiv(
                depthlist[0], depthlist[1], 3, 1, dilate, dilate, bn_kwargs=bn_kwargs,
                depthwise_activ=dw_activ_list[1], pointwise_activ=None)
            self.separable3 = SeparableConv2DBNActiv(
                depthlist[1], depthlist[2], 3, stride, dilate, dilate, bn_kwargs=bn_kwargs,
                depthwise_activ=dw_activ_list[2], pointwise_activ=pw_activ_list[2])
            if skip_type == 'conv':
                self.conv = Conv2DBNActiv(
                    in_channels, depthlist[2], 1, activ=None,
                    nobias=True, stride=stride, bn_kwargs=bn_kwargs)

    def __call__(self, x):
        if self.activ_first:
            h = F.relu(x)
        else:
            h = x

        h = self.separable1(h)
        h = self.separable2(h)
        separable2 = h
        if self.separable2_activ is not None:
            h = self.separable2_activ(h)
        h = self.separable3(h)

        if self.skip_type == 'conv':
            skip = self.conv(x)
            h = h + skip
        elif self.skip_type == 'sum':
            h = h + x
        elif self.skip_type == 'none':
            pass

        if not self.activ_first:
            h = F.relu(h)

        return h, separable2


class Xception65(chainer.Chain):
    mean_pixel = [127.5, 127.5, 127.5]

    def __init__(self, bn_kwargs={}):
        super(Xception65, self).__init__()

        with self.init_scope():
            self.entryflow_conv1 = Conv2DBNActiv(
                3, 32, 3, 2, 1, bn_kwargs=bn_kwargs)
            self.entryflow_conv2 = Conv2DBNActiv(
                32, 64, 3, 1, 1, bn_kwargs=bn_kwargs)
            self.entryflow_block1 = XceptionBlock(
                64, [128, 128, 128], stride=2, skip_type='conv', bn_kwargs=bn_kwargs)
            self.entryflow_block2 = XceptionBlock(
                128, [256, 256, 256], stride=2, skip_type='conv', bn_kwargs=bn_kwargs)
            self.entryflow_block3 = XceptionBlock(
                256, [728, 728, 728], stride=1, skip_type='conv', bn_kwargs=bn_kwargs)

            for i in range(1, 17):
                block = XceptionBlock(
                    728, [728, 728, 728], stride=1, dilate=2, skip_type='sum', bn_kwargs=bn_kwargs)
                self.__setattr__('middleflow_block{}'.format(i), block)

            self.exitflow_block1 = XceptionBlock(
                728, [728, 1024, 1024], stride=1, dilate=2, skip_type='conv', bn_kwargs=bn_kwargs)
            self.exitflow_block2 = XceptionBlock(
                1024, [1536, 1536, 2048], stride=1, dilate=4, skip_type='none', bn_kwargs=bn_kwargs,
                activ_first=False, dw_activ_list=[F.relu]*3, pw_activ_list=[F.relu]*3)

    def __call__(self, x):
        h = self.entryflow_conv1(x)
        h = self.entryflow_conv2(h)
        h, _ = self.entryflow_block1(h)
        h, lowlevel = self.entryflow_block2(h)
        h, _ = self.entryflow_block3(h)

        for i in range(1, 17):
            h, _ = self['middleflow_block{}'.format(i)](h)

        h, _ = self.exitflow_block1(h)
        highlevel, _ = self.exitflow_block2(h)

        return lowlevel, highlevel


class SeparableASPP(chainer.Chain):
    def __init__(self, in_channels, out_channels=256, dilate_list=[12, 24, 36], bn_kwargs={}):
        super(SeparableASPP, self).__init__()

        with self.init_scope():
            self.image_pooling_conv = Conv2DBNActiv(
                in_channels, out_channels, 1, bn_kwargs=bn_kwargs)
            self.conv1x1 = Conv2DBNActiv(
                in_channels, out_channels, 1, bn_kwargs=bn_kwargs)
            self.atrous1 = SeparableConv2DBNActiv(
                in_channels, out_channels, 3, 1, dilate_list[0], dilate_list[0],
                depthwise_activ=F.relu, pointwise_activ=F.relu, bn_kwargs=bn_kwargs)
            self.atrous2 = SeparableConv2DBNActiv(
                in_channels, out_channels, 3, 1, dilate_list[1], dilate_list[1],
                depthwise_activ=F.relu, pointwise_activ=F.relu, bn_kwargs=bn_kwargs)
            self.atrous3 = SeparableConv2DBNActiv(
                in_channels, out_channels, 3, 1, dilate_list[2], dilate_list[2],
                depthwise_activ=F.relu, pointwise_activ=F.relu, bn_kwargs=bn_kwargs)
            self.proj = Conv2DBNActiv(
                out_channels * 5, out_channels, 1, bn_kwargs=bn_kwargs)

    def image_pooling(self, x):
        _, _, H, W = x.shape
        x = F.average(x, axis=(2, 3), keepdims=True)
        x = self.image_pooling_conv(x)
        B, C, _, _ = x.shape
        x = F.broadcast_to(x, (B, C, H, W))
        return x

    def __call__(self, x):
        h = []
        h.append(self.image_pooling(x))
        h.append(self.conv1x1(x))
        h.append(self.atrous1(x))
        h.append(self.atrous2(x))
        h.append(self.atrous3(x))
        h = F.concat(h, axis=1)
        h = self.proj(h)
        h = F.dropout(h)

        return h

