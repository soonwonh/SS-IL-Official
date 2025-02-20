import torch
import torch.nn as nn
import torch.nn.functional as F
from networks.layers import NormedLinear, SplitNormedLinear, LSCLinear, SplitLSCLinear
from copy import deepcopy

def conv3x3(in_planes, out_planes, stride=1, groups=1, dilation=1):
    """3x3 convolution with padding"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=dilation, groups=groups, bias=False, dilation=dilation)


def conv1x1(in_planes, out_planes, stride=1):
    """1x1 convolution"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=1, stride=stride, bias=False)


class BasicBlock(nn.Module):
    expansion = 1
    __constants__ = ['downsample']

    def __init__(self, inplanes, planes, stride=1, downsample=None, groups=1,
                 base_width=64, dilation=1, norm_layer=None, use_last_relu = True):
        super(BasicBlock, self).__init__()
        if norm_layer is None:
            norm_layer = nn.BatchNorm2d
        if groups != 1 or base_width != 64:
            raise ValueError('BasicBlock only supports groups=1 and base_width=64')
        if dilation > 1:
            raise NotImplementedError("Dilation > 1 not supported in BasicBlock")
        # Both self.conv1 and self.downsample layers downsample the input when stride != 1
        self.conv1 = conv3x3(inplanes, planes, stride)
        self.bn1 = norm_layer(planes)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = conv3x3(planes, planes)
        self.bn2 = norm_layer(planes)
        self.downsample = downsample
        self.stride = stride
        
        self.use_last_relu = use_last_relu

    def forward(self, x):
        identity = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)
        
        out = self.conv2(out)
        out = self.bn2(out)

        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        if self.use_last_relu:
            out = self.relu(out)
        
        return out

class ResNet(nn.Module):

    def __init__(self, block, layers, num_classes, zero_init_residual=False,
                 groups=1, width_per_group=64, replace_stride_with_dilation=None,
                 norm_layer=None, trainer = None):
        super(ResNet, self).__init__()
        
        if norm_layer is None:
            norm_layer = nn.BatchNorm2d
        self._norm_layer = norm_layer
        self.trainer = trainer
        self.inplanes = 64
        self.dilation = 1
        self.block = block
        self.layers = layers
        self.norm_layer = norm_layer
        self.groups = groups
        self.base_width = width_per_group
        self.last_relu = nn.Identity() if trainer =='rebalancing' or trainer == 'podnet' else nn.ReLU(inplace=True)
        self.heads = []
        if replace_stride_with_dilation is None:
            # each element in the tuple indicates if we should replace
            # the 2x2 stride with a dilated convolution instead
            self.replace_stride_with_dilation = [False, False, False]
        if len(self.replace_stride_with_dilation) != 3:
            raise ValueError("replace_stride_with_dilation should be None "
                             "or a 3-element tuple, got {}".format(self.replace_stride_with_dilation))
        
        self.fc = nn.Linear(512 * block.expansion, num_classes)
        if trainer == 'der':
            self.fc = nn.ModuleList()
        if trainer == 'rebalancing':
            self.fc = NormedLinear(512 * block.expansion, num_classes)
        elif trainer == 'podnet':
            self.fc = LSCLinear(512 * block.expansion, num_classes)

        self.encoder = self.init_encoder()

        if trainer == 'der':
            self.encoders = nn.ModuleList()
                
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

        # Zero-initialize the last BN in each residual branch,
        # so that the residual branch starts with zeros, and each residual block behaves like an identity.
        # This improves the model by 0.2~0.3% according to https://arxiv.org/abs/1706.02677
        if zero_init_residual:
            for m in self.modules():
                if isinstance(m, Bottleneck):
                    nn.init.constant_(m.bn3.weight, 0)
                elif isinstance(m, BasicBlock):
                    nn.init.constant_(m.bn2.weight, 0)
    
    def init_encoder(self):
        
        self.conv1 = nn.Conv2d(3, self.inplanes, kernel_size=7, stride=2, padding=3, bias=False)
        self.bn1 = self.norm_layer(self.inplanes)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.layer1 = self._make_layer(self.block, 64, self.layers[0])
        self.layer2 = self._make_layer(self.block, 128, self.layers[1], stride=2,
                                       dilate=self.replace_stride_with_dilation[0])
        self.layer3 = self._make_layer(self.block, 256, self.layers[2], stride=2,
                                       dilate=self.replace_stride_with_dilation[1])
        self.layer4 = self._make_layer(self.block, 512, self.layers[3], stride=2,
                                       dilate=self.replace_stride_with_dilation[2])
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        
        encoder = nn.Sequential(
                self.conv1,
                self.bn1,
                self.relu,
                self.maxpool,
                self.layer1,
                self.relu,
                self.layer2,
                self.relu,
                self.layer3,
                self.relu,
                self.layer4,
                self.last_relu,
                self.avgpool,
                nn.Flatten()
            )
        return encoder
    
    def add_encoder(self):
        if len(self.encoders) == 0:
            self.encoders.append(self.encoder)
        else:
            for encoder in self.encoders:
                encoder.eval()
                for param in encoder.parameters():
                    param.requires_grad = False
            new_encoder = deepcopy(self.encoders[-1])
            self.encoders.append(new_encoder)
            #print("encoders!!", self.encoders)
        
    def add_head(self, num_outputs):
        for head in self.heads:
            head.eval()
            for param in head.parameters():
                param.requires_grad=False
        new_head = nn.Linear(512, num_outputs).cuda()
        self.heads.append(new_head)
        
    
    def _make_layer(self, block, planes, blocks, stride=1, dilate=False):
        norm_layer = self._norm_layer
        downsample = None
        previous_dilation = self.dilation
        if dilate:
            self.dilation *= stride
            stride = 1
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                conv1x1(self.inplanes, planes * block.expansion, stride),
                norm_layer(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample, self.groups,
                            self.base_width, previous_dilation, norm_layer))
        self.inplanes = planes * block.expansion
        for _ in range(1, blocks):
            if _ == blocks-1:
                layers.append(block(self.inplanes, planes, groups=self.groups,
                                    base_width=self.base_width, dilation=self.dilation,
                                    norm_layer=norm_layer, use_last_relu = False))
            else:
                layers.append(block(self.inplanes, planes, groups=self.groups,
                                    base_width=self.base_width, dilation=self.dilation,
                                    norm_layer=norm_layer))

        return nn.Sequential(*layers)

    def forward(self, x, feature_return=False):
        
        if self.trainer =='der':
            """
            # 1) feature concatenation
            features = []
            for encoder in self.encoders:
                features.append(encoder(x))
            print("len(features)",len(features))
            print("features[0].shape",features[0].shape)
            features = torch.cat(features, dim=2)
            print("features.shape",features.shape)
            for head in self.heads:
                output.append(head(features))
            x = torch.cat(output, dim=1)
            """
            # 2) parallel forwarding
            logits = []
            features = []
            for i, encoder in enumerate(self.encoders):
                feature = encoder(x)
                logits.append(self.heads[i](feature))
                features.append(feature)
            x = torch.concat(logits, dim=1)
            feature = torch.concat(features, dim=1)

        elif self.trainer =='podnet':
            x1 = self.layer1(self.maxpool(self.relu(self.bn1(self.conv1(x)))))
            x2 = self.layer2(self.relu(x1))
            x3 = self.layer3(self.relu(x2))
            x4 = self.layer4(self.relu(x3))
            x = torch.flatten(self.avgpool(x4), 1)
            feature = x / torch.norm(x, 2, 1).unsqueeze(1)
            x = self.fc(x)
            
        else:
            x = self.encoder(x)
            feature = x / torch.norm(x, 2, 1).unsqueeze(1)
            x = self.fc(x)

        if feature_return:
            if self.trainer == 'podnet':
                return x, feature, [x1, x2, x3, x4]
            return x, feature
        return x
    

def _resnet(block, layers, num_classes, trainer = None):
    model = ResNet(block, layers, num_classes, trainer = trainer)
    
    return model


def resnet18(num_classes, trainer = None):
    
    return _resnet(BasicBlock, [2, 2, 2, 2], num_classes, trainer = trainer)

