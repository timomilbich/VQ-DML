"""
The network architectures and weights are adapted and used from the great https://github.com/Cadene/pretrained-models.pytorch.
"""
import torch, torch.nn as nn
import pretrainedmodels as ptm
from architectures.VQ import VectorQuantizer
import argparse

"""============================================================="""
class Network(torch.nn.Module):
    def __init__(self, arch, pretraining, embed_dim, VQ, n_e=1000, beta=0.25, e_dim=1024, e_init='uniform'):
        super(Network, self).__init__()

        self.arch = arch
        self.embed_dim = embed_dim
        self.model = ptm.__dict__['resnet50'](num_classes=1000, pretrained=pretraining if pretraining=='imagenet' else None)
        self.name = self.arch
        self.VQ = VQ
        self.n_e = n_e
        self.beta = beta
        self.e_dim = e_dim
        self.e_init = e_init

        if self.VQ:
            self.VectorQuantizer = VectorQuantizer(self.n_e, self.e_dim, self.beta, self.e_init)

        if 'frozen' in self.arch:
            for module in filter(lambda m: type(m) == nn.BatchNorm2d, self.model.modules()):
                module.eval()
                module.train = lambda _: None

        self.model.last_linear = torch.nn.Linear(self.model.last_linear.in_features, embed_dim)
        self.layer_blocks = nn.ModuleList([self.model.layer1, self.model.layer2, self.model.layer3, self.model.layer4])

        self.pool_base = torch.nn.AdaptiveAvgPool2d(1)
        self.pool_aux  = torch.nn.AdaptiveMaxPool2d(1) if 'double' in self.arch else None

        print(f'ARCHITECTURE:\ntype: {self.arch}\nembed_dims: {self.embed_dim}\n')


    def forward(self, x, warmup=False, quantize=True, **kwargs):
        # quantize argument is needed to turn off quantization for initial features
        # extracting before clustering initialization

        x = self.model.maxpool(self.model.relu(self.model.bn1(self.model.conv1(x))))
        for k, layerblock in enumerate(self.layer_blocks):
            x = layerblock(x)
        prepool_y = x

        # VQ features #########
        if self.VQ:
            if quantize:
                x, vq_loss = self.VectorQuantizer(x)
            else:
                vq_loss = 0
        ##########

        if self.pool_aux is not None:
            y = self.pool_aux(x) + self.pool_base(x)
        else:
            y = self.pool_base(x)
        y = y.view(y.size(0),-1)

        if warmup:
            x,y,prepool_y = x.detach(), y.detach(), prepool_y.detach()

        z = self.model.last_linear(y)

        if 'normalize' in self.arch:
            z = torch.nn.functional.normalize(z, dim=-1)

        if self.VQ:
            return {'embeds': z, 'avg_features': y, 'features': x, 'extra_embeds': prepool_y, 'vq_loss': vq_loss}
        else:
            return {'embeds': z, 'avg_features': y, 'features': x, 'extra_embeds': prepool_y}
