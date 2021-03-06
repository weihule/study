import torch
import torch.nn as nn
from collections import OrderedDict
from torch import Tensor, FloatTensor


class SELayer(nn.Module):
    def __init__(self, in_channel, reduction=16):
        super(SELayer, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Sequential(
            nn.Linear(in_channel, in_channel // reduction, bias=True),
            nn.ReLU(True),
            nn.Linear(in_channel // reduction, in_channel, bias=True),
            nn.Sigmoid()
        )

    def forward(self, x: Tensor):
        b, c, _, _ = x.shape
        y = self.avg_pool(x)
        y = torch.flatten(y, start_dim=1)
        y = self.fc(y).view(b, c, 1, 1)

        return x * y.expand_as(x)


class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, in_channel, out_channel, stride=1, downsample=None, use_se=False, **kwargs):
        super(BasicBlock, self).__init__()
        self.conv1 = nn.Conv2d(in_channel, out_channel,
                               kernel_size=3, stride=stride,
                               padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(out_channel)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = nn.Conv2d(out_channel, out_channel,
                               kernel_size=3, stride=1,
                               padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(out_channel)
        self.downsample = downsample
        self.use_se = use_se
        if self.use_se:
            self.se = SELayer(out_channel)

    def forward(self, x):
        identity = x
        if self.downsample is not None:
            identity = self.downsample(x)

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)

        out += identity
        out = self.relu(out)
        if self.use_se:
            out = self.se(out)

        return out


class Bottleneck(nn.Module):
    """
    注意：原论文中, 在虚线残差结构的主分支上, 第一个1x1卷积层的步距是2, 第二个3x3卷积层步距是1。
    但在pytorch官方实现过程中是第一个1x1卷积层的步距是1, 第二个3x3卷积层步距是2, 
    这么做的好处是能够在top1上提升大概0.5%的准确率。
    可参考Resnet v1.5 https://ngc.nvidia.com/catalog/model-scripts/nvidia:resnet_50_v1_5_for_pytorch
    """
    expansion = 4

    def __init__(self, in_channel, out_channel, stride=1, downsample=None,
                 groups=1, width_per_group=64):
        super(Bottleneck, self).__init__()

        # # default : width = out_channel
        # width = int(out_channel * (width_per_group / 64.)) * groups

        self.conv1 = nn.Conv2d(in_channels=in_channel, out_channels=out_channel,
                               kernel_size=1, stride=1, bias=False)
        self.bn1 = nn.BatchNorm2d(out_channel)
        # -----------------------------------------
        self.conv2 = nn.Conv2d(in_channels=out_channel, out_channels=out_channel,
                               kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(out_channel)
        # -----------------------------------------
        self.conv3 = nn.Conv2d(in_channels=out_channel, out_channels=out_channel * self.expansion,
                               kernel_size=1, stride=1, bias=False)
        self.bn3 = nn.BatchNorm2d(out_channel * self.expansion)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample

    def forward(self, x):
        identity = x
        if self.downsample is not None:
            identity = self.downsample(x)

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        out += identity
        out = self.relu(out)

        return out


class ResNet(nn.Module):
    def __init__(self,
                 block,
                 blocks_num,
                 num_classes=1000,
                 include_top=True,
                 groups=1,
                 width_per_group=64):
        super(ResNet, self).__init__()
        self.include_top = include_top
        self.in_channel = 64

        self.groups = groups
        self.width_per_group = width_per_group

        self.conv1 = nn.Conv2d(in_channels=3, out_channels=self.in_channel,
                               kernel_size=7, stride=2, padding=3, bias=False)
        self.bn1 = nn.BatchNorm2d(self.in_channel)
        self.relu = nn.ReLU(inplace=True)

        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.layer1 = self._make_layer(block, 64, blocks_num[0], stride=1)
        self.layer2 = self._make_layer(block, 128, blocks_num[1], stride=2)
        self.layer3 = self._make_layer(block, 256, blocks_num[2], stride=2)
        self.layer4 = self._make_layer(block, 512, blocks_num[3], stride=2)

        if self.include_top:
            self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
            self.fc = nn.Linear(512 * block.expansion, num_classes)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode="fan_out", nonlinearity="relu")
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                nn.init.xavier_normal_(m.weight)
                nn.init.constant_(m.bias, 0)

    def _make_layer(self, block, channel, block_num, stride=1):
        downsample = None
        """ 
       对于layer2, 3, 4来说, stride == 2时, 即第一层需要进行下采样
        对于layer1来说, 所有层都不需要进行下采样, 但是其第一层
        需要进行卷积操作, stride == 1, 但是self.in_channel == 64, 
        但是 channel * block.expansion == 256, 这两者不相等，但是
        仍然有shortcut连接, 所以仍然需要右边进行一个卷积操作来调整channel

        另外, 这里的stride是为了layer的第一层设置的, 传到了block里
        其余层的stride都是固定的, 已经在block中设置了
        """
        if stride != 1 or self.in_channel != channel * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.in_channel, channel * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(channel * block.expansion)
            )
        layers = []

        # 先把第一层单独添加进去
        layers.append(block(self.in_channel,
                            channel,
                            stride=stride,
                            downsample=downsample,
                            groups=self.groups,
                            width_per_group=self.width_per_group,
                            )
                      )

        # 每个layer的第一层之后, self.in_channel就变成channel的倍数了
        self.in_channel = channel * block.expansion

        for _ in range(1, block_num):
            layers.append(block(self.in_channel,
                                channel,
                                groups=self.groups,
                                width_per_group=self.width_per_group

                                ))
        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        if self.include_top:
            x = self.avgpool(x)
            x = torch.flatten(x, start_dim=1)
            x = self.fc(x)

        return x


class ResNetCIFAR(nn.Module):
    """
    为CIFAR10 (32*32) 的数据集做了改造
    """

    def __init__(self,
                 block,
                 blocks_num: list,
                 num_classes: int = 1000,
                 include_top: bool = True,
                 groups: int = 1,
                 width_per_group: int = 64):
        super(ResNetCIFAR, self).__init__()
        self.include_top = include_top
        self.in_channel = 64
        self.groups = groups
        self.conv1 = nn.Conv2d(in_channels=3, out_channels=self.in_channel,
                               kernel_size=3, stride=1, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(self.in_channel)
        self.relu = nn.ReLU(inplace=True)
        # 这里改变刚开始的 conv1，抛弃 Maxpool 层，直接开始各个 residual block
        self.layer1 = self._make_layer_cifar(block, 64, blocks_num[0], stride=1)  # 32*32, 64
        self.layer2 = self._make_layer_cifar(block, 128, blocks_num[1], stride=2)  # 16*16, 128
        self.layer3 = self._make_layer_cifar(block, 256, blocks_num[2], stride=2)  # 8*8, 256
        self.layer4 = self._make_layer_cifar(block, 512, blocks_num[3], stride=2)  # 4*4, 512

        if self.include_top:
            self.avg_pool = nn.AdaptiveAvgPool2d(1)
            self.fc = nn.Linear(512 * block.expansion, num_classes)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode="fan_out", nonlinearity="relu")
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                nn.init.xavier_normal_(m.weight)
                nn.init.constant_(m.bias, 0)

    def _make_layer_cifar(self, block, channel, block_num, stride=1):
        downsample = None
        if stride != 1 or self.in_channel != channel * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.in_channel, channel * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(channel * block.expansion)
            )
        layers = OrderedDict()
        layers.update({'block1': block(self.in_channel, channel, stride=stride, downsample=downsample, use_se=True)})
        self.in_channel = channel * block.expansion

        for i in range(block_num - 1):
            key = 'block' + str(i + 2)
            layers.update({key: block(self.in_channel, channel, stride=1, use_se=True)})

        return nn.Sequential(layers)

    def forward(self, x: Tensor):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        if self.include_top:
            x = self.avg_pool(x)
            x = torch.flatten(x, start_dim=1)
            x = self.fc(x)

        return x


def resnet50(num_classes=1000, include_top=True):
    return ResNet(Bottleneck, [3, 4, 6, 3], num_classes=num_classes, include_top=include_top)


def resnet18(num_classes=1000, include_top=True):
    return ResNetCIFAR(BasicBlock, [2, 2, 2, 2], num_classes=num_classes, include_top=include_top)


if __name__ == "__main__":
    model = resnet18(num_classes=5)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = model.to(device)
    input_data = torch.randn(size=(64, 3, 32, 32))
    pred = model(input_data)
    print(pred.shape)
