"""
    Vector Quantizer taken from the VQ-GAN github.
    see https://github.com/CompVis/taming-transformers/blob/31216490efe8ae3604efbf9f1531ff5c70bd446a/taming/modules/vqvae/quantize.py
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from torch import einsum
from einops import rearrange
import faiss
from copy import  deepcopy


class VectorQuantizer(nn.Module):
    """
    Improved version over VectorQuantizer, can be used as a drop-in replacement. Mostly
    avoids costly matrix multiplications and allows for post-hoc remapping of indices.
    """

    # NOTE: due to a bug the beta term was applied to the wrong term. for
    # backwards compatibility we use the buggy version by default, but you can
    # specify legacy=False to fix it.
    def __init__(self, n_e, e_dim, beta, e_init='random_uniform', block_to_quantize=-1, remap=None, unknown_index="random",
                 sane_index_shape=False, legacy=True):
        super().__init__()
        self.n_e = n_e
        self.e_dim = e_dim
        self.beta = beta
        self.legacy = legacy
        self.e_init = e_init
        self.block_to_quantize = block_to_quantize

        self.embedding = nn.Embedding(self.n_e, self.e_dim)
        if self.e_init == 'random_uniform':
            self.embedding.weight.data.uniform_(-100.0 / self.n_e, 100.0 / self.n_e)

        self.remap = remap
        if self.remap is not None:
            self.register_buffer("used", torch.tensor(np.load(self.remap)))
            self.re_embed = self.used.shape[0]
            self.unknown_index = unknown_index  # "random" or "extra" or integer
            if self.unknown_index == "extra":
                self.unknown_index = self.re_embed
                self.re_embed = self.re_embed + 1
            print(f"Remapping {self.n_e} indices to {self.re_embed} indices. "
                  f"Using {self.unknown_index} for unknown indices.")
        else:
            self.re_embed = n_e

        self.sane_index_shape = sane_index_shape

        print(f'Initializeing VQ [VectorQuantization]')
        print(f'*** n_e = [{self.n_e}]')
        print(f'*** e_dim = [{self.e_dim}]')
        print(f'*** e_init = [{self.e_init}]')
        print(f'*** block_to_quantize = [{self.block_to_quantize}]')
        print(f'*** beta = [{self.beta}]\n')

    def remap_to_used(self, inds):
        ishape = inds.shape
        assert len(ishape) > 1
        inds = inds.reshape(ishape[0], -1)
        used = self.used.to(inds)
        match = (inds[:, :, None] == used[None, None, ...]).long()
        new = match.argmax(-1)
        unknown = match.sum(2) < 1
        if self.unknown_index == "random":
            new[unknown] = torch.randint(0, self.re_embed, size=new[unknown].shape).to(device=new.device)
        else:
            new[unknown] = self.unknown_index
        return new.reshape(ishape)

    def unmap_to_all(self, inds):
        ishape = inds.shape
        assert len(ishape) > 1
        inds = inds.reshape(ishape[0], -1)
        used = self.used.to(inds)
        if self.re_embed > self.used.shape[0]:  # extra token
            inds[inds >= self.used.shape[0]] = 0  # simply set to zero
        back = torch.gather(used[None, :][inds.shape[0] * [0], :], 1, inds)
        return back.reshape(ishape)

    def forward(self, z, temp=None, rescale_logits=False, return_logits=False):
        assert temp is None or temp == 1.0, "Only for interface compatible with Gumbel"
        assert rescale_logits == False, "Only for interface compatible with Gumbel"
        assert return_logits == False, "Only for interface compatible with Gumbel"
        # reshape z -> (batch, height, width, channel) and flatten
        z = rearrange(z, 'b c h w -> b h w c').contiguous()
        z_flattened = z.view(-1, self.e_dim)
        # distances from z to embeddings e_j (z - e)^2 = z^2 + e^2 - 2 e * z

        d = torch.sum(z_flattened ** 2, dim=1, keepdim=True) + \
            torch.sum(self.embedding.weight ** 2, dim=1) - 2 * \
            torch.einsum('bd,dn->bn', z_flattened, rearrange(self.embedding.weight, 'n d -> d n'))

        min_encoding_indices = torch.argmin(d, dim=1)
        z_q = self.embedding(min_encoding_indices).view(z.shape)

        # compute loss for embedding
        if not self.legacy:
            loss = self.beta * torch.mean((z_q.detach() - z) ** 2) + \
                   torch.mean((z_q - z.detach()) ** 2)
        else:
            loss = torch.mean((z_q.detach() - z) ** 2) + self.beta * \
                   torch.mean((z_q - z.detach()) ** 2)

        # preserve gradients
        z_q = z + (z_q - z).detach()

        # reshape back to match original input shape
        z_q = rearrange(z_q, 'b h w c -> b c h w').contiguous()

        if self.remap is not None:
            min_encoding_indices = min_encoding_indices.reshape(z.shape[0], -1)  # add batch axis
            min_encoding_indices = self.remap_to_used(min_encoding_indices)
            min_encoding_indices = min_encoding_indices.reshape(-1, 1)  # flatten

        if self.sane_index_shape:
            min_encoding_indices = min_encoding_indices.reshape(z_q.shape[0], z_q.shape[2], z_q.shape[3])

        perplexity, cluster_use = self.measure_perplexity(min_encoding_indices, self.n_e)

        return z_q, loss, perplexity, cluster_use

    # return z_q, loss, (perplexity, min_encodings, min_encoding_indices)

    def get_codebook_entry(self, indices, shape):
        # shape specifying (batch, height, width, channel)
        if self.remap is not None:
            indices = indices.reshape(shape[0], -1)  # add batch axis
            indices = self.unmap_to_all(indices)
            indices = indices.reshape(-1)  # flatten again

        # get quantized latent vectors
        z_q = self.embedding(indices)

        if shape is not None:
            z_q = z_q.view(shape)
            # reshape back to match original input shape
            z_q = z_q.permute(0, 3, 1, 2).contiguous()

        return z_q

    def init_codebook_by_clustering(self, features, evaluate_on_gpu=True, n_max=100000):

        ### Prepare features
        features = features.astype(np.float32)

        ### select samples to use
        idx_to_use = np.random.choice(features.shape[0], np.min([features.shape[0], n_max]), replace=False)
        features = features[idx_to_use, :]

        ### Init faiss
        faiss.omp_set_num_threads(20)
        res = None
        torch.cuda.empty_cache()
        if evaluate_on_gpu:
            res = faiss.StandardGpuResources()

        ### Set CPU Cluster index
        cluster_idx = faiss.IndexFlatL2(features.shape[-1])
        if res is not None: cluster_idx = faiss.index_cpu_to_gpu(res, 0, cluster_idx)
        kmeans = faiss.Clustering(features.shape[-1], self.n_e)
        kmeans.niter = 20
        kmeans.min_points_per_centroid = 1
        kmeans.max_points_per_centroid = 1000000000

        ### Train Kmeans
        kmeans.train(features, cluster_idx)
        centroids = faiss.vector_float_to_array(kmeans.centroids).reshape(self.n_e, features.shape[-1])

        ### Init codebook
        self.embedding = nn.Embedding.from_pretrained(torch.from_numpy(deepcopy(centroids)).float(), freeze=False)

        ### empty cache on GPU
        torch.cuda.empty_cache()

    def measure_perplexity(self, predicted_indices, n_embed):  # eval cluster perplexity. when perplexity == num_embeddings then all clusters are used exactly equally
        encodings = F.one_hot(predicted_indices, n_embed).float().reshape(-1, n_embed)
        avg_probs = encodings.mean(0)
        perplexity = (-(avg_probs * torch.log(avg_probs + 1e-10)).sum()).exp()
        cluster_use = torch.sum(avg_probs > 0)
        return perplexity, cluster_use


class MultiHeadVectorQuantizer(nn.Module):
    """
    Improved version over VectorQuantizer, can be used as a drop-in replacement. Mostly
    avoids costly matrix multiplications and allows for post-hoc remapping of indices.
    """

    # NOTE: due to a bug the beta term was applied to the wrong term. for
    # backwards compatibility we use the buggy version by default, but you can
    # specify legacy=False to fix it.
    def __init__(self, n_e, k_e, e_dim, beta, e_init='random_uniform', block_to_quantize=-1, remap=None, unknown_index="random",
                 sane_index_shape=False, legacy=True):
        super().__init__()
        self.n_e = n_e
        self.k_e = k_e
        self.e_dim = e_dim

        self.e_dim_seg = self.e_dim / self.k_e
        assert self.e_dim % self.k_e  == 0, "Assert feature dim is dividable by codeword dim."
        self.e_dim_seg = int(self.e_dim_seg)

        self.beta = beta
        self.legacy = legacy
        self.e_init = e_init
        self.block_to_quantize = block_to_quantize

        self.embedding = nn.Embedding(self.n_e, self.e_dim_seg)
        if self.e_init == 'random_uniform':
            self.embedding.weight.data.uniform_(-100.0 / self.n_e, 100.0 / self.n_e)

        self.remap = remap
        if self.remap is not None:
            self.register_buffer("used", torch.tensor(np.load(self.remap)))
            self.re_embed = self.used.shape[0]
            self.unknown_index = unknown_index  # "random" or "extra" or integer
            if self.unknown_index == "extra":
                self.unknown_index = self.re_embed
                self.re_embed = self.re_embed + 1
            print(f"Remapping {self.n_e} indices to {self.re_embed} indices. "
                  f"Using {self.unknown_index} for unknown indices.")
        else:
            self.re_embed = n_e

        self.sane_index_shape = sane_index_shape

        print(f'Initializeing VQ [MultiHeadVectorQuantization]')
        print(f'*** n_e = [{self.n_e}]')
        print(f'*** e_dim = [{self.e_dim}]')
        print(f'*** k_e = [{self.k_e}]')
        print(f'*** e_dim_seg = [{self.e_dim_seg}]')
        print(f'*** e_init = [{self.e_init}]')
        print(f'*** block_to_quantize = [{self.block_to_quantize}]')
        print(f'*** beta = [{self.beta}]\n')

    def remap_to_used(self, inds):
        ishape = inds.shape
        assert len(ishape) > 1
        inds = inds.reshape(ishape[0], -1)
        used = self.used.to(inds)
        match = (inds[:, :, None] == used[None, None, ...]).long()
        new = match.argmax(-1)
        unknown = match.sum(2) < 1
        if self.unknown_index == "random":
            new[unknown] = torch.randint(0, self.re_embed, size=new[unknown].shape).to(device=new.device)
        else:
            new[unknown] = self.unknown_index
        return new.reshape(ishape)

    def unmap_to_all(self, inds):
        ishape = inds.shape
        assert len(ishape) > 1
        inds = inds.reshape(ishape[0], -1)
        used = self.used.to(inds)
        if self.re_embed > self.used.shape[0]:  # extra token
            inds[inds >= self.used.shape[0]] = 0  # simply set to zero
        back = torch.gather(used[None, :][inds.shape[0] * [0], :], 1, inds)
        return back.reshape(ishape)


    def forward(self, z, temp=None, rescale_logits=False, return_logits=False):
        assert temp is None or temp == 1.0, "Only for interface compatible with Gumbel"
        assert rescale_logits == False, "Only for interface compatible with Gumbel"
        assert return_logits == False, "Only for interface compatible with Gumbel"

        # reshape z -> (batch, height, width, channel) and flatten
        z = rearrange(z, 'b c h w -> b h w c').contiguous()
        z_tmp = torch.reshape(z, (z.shape[0], z.shape[1], z.shape[2], self.k_e, self.e_dim_seg))
        z_tmp = z_tmp.view(-1, self.e_dim_seg)
        # distances from z to embeddings e_j (z - e)^2 = z^2 + e^2 - 2 e * z

        d = torch.sum(z_tmp ** 2, dim=1, keepdim=True) + \
            torch.sum(self.embedding.weight ** 2, dim=1) - 2 * \
            torch.einsum('bd,dn->bn', z_tmp, rearrange(self.embedding.weight, 'n d -> d n'))

        min_encoding_indices = torch.argmin(d, dim=1)
        z_q = self.embedding(min_encoding_indices).view(z_tmp.shape)
        z_q = z_q.view(z.shape[0], z.shape[1], z.shape[2], -1)

        # compute loss for embedding
        if not self.legacy:
            loss = self.beta * torch.mean((z_q.detach() - z) ** 2) + \
                   torch.mean((z_q - z.detach()) ** 2)
        else:
            loss = torch.mean((z_q.detach() - z) ** 2) + self.beta * \
                   torch.mean((z_q - z.detach()) ** 2)

        # preserve gradients
        z_q = z + (z_q - z).detach()
        z_q = rearrange(z_q, 'b h w c -> b c h w').contiguous()

        if self.remap is not None:
            min_encoding_indices = min_encoding_indices.reshape(z.shape[0], -1)  # add batch axis
            min_encoding_indices = self.remap_to_used(min_encoding_indices)
            min_encoding_indices = min_encoding_indices.reshape(-1, 1)  # flatten

        if self.sane_index_shape:
            min_encoding_indices = min_encoding_indices.reshape(z_q.shape[0], z_q.shape[2], z_q.shape[3])

        perplexity, cluster_use = self.measure_perplexity(min_encoding_indices, self.n_e)

        return z_q, loss, perplexity, cluster_use, min_encoding_indices

    def get_codebook_entry(self, indices, shape):
        # shape specifying (batch, height, width, channel)
        if self.remap is not None:
            indices = indices.reshape(shape[0], -1)  # add batch axis
            indices = self.unmap_to_all(indices)
            indices = indices.reshape(-1)  # flatten again

        # get quantized latent vectors
        z_q = self.embedding(indices)

        if shape is not None:
            z_q = z_q.view(shape)
            # reshape back to match original input shape
            z_q = z_q.permute(0, 3, 1, 2).contiguous()

        return z_q

    def init_codebook_by_clustering(self, features, evaluate_on_gpu=True, n_max=100000):

        n_feat_tot = features.shape[0]

        ### Prepare features
        features = features.astype(np.float32)

        ### select samples to use
        idx_to_use = np.random.choice(features.shape[0], np.min([features.shape[0], n_max]), replace=False)
        features = features[idx_to_use, :]

        print(f'Kmeans clustering: [n_feat={features.shape[0]}] [n_feat_tot={n_feat_tot}] [n_centroids={self.n_e}] [dim_centroid={features.shape[-1]}]')

        ### Init faiss
        faiss.omp_set_num_threads(20)
        res = None
        torch.cuda.empty_cache()
        if evaluate_on_gpu:
            res = faiss.StandardGpuResources()

        ### Set CPU Cluster index
        cluster_idx = faiss.IndexFlatL2(features.shape[-1])
        if res is not None: cluster_idx = faiss.index_cpu_to_gpu(res, 0, cluster_idx)
        kmeans = faiss.Clustering(features.shape[-1], self.n_e)
        kmeans.niter = 20
        kmeans.min_points_per_centroid = 1
        kmeans.max_points_per_centroid = 1000000000

        ### Train Kmeans
        kmeans.train(features, cluster_idx)
        centroids = faiss.vector_float_to_array(kmeans.centroids).reshape(self.n_e, features.shape[-1])

        ### Init codebook
        self.embedding = nn.Embedding.from_pretrained(torch.from_numpy(deepcopy(centroids)).float(), freeze=False)

        ### empty cache on GPU
        torch.cuda.empty_cache()

    def measure_perplexity(self, predicted_indices, n_embed):  # eval cluster perplexity. when perplexity == num_embeddings then all clusters are used exactly equally
        encodings = F.one_hot(predicted_indices, n_embed).float().reshape(-1, n_embed)
        avg_probs = encodings.mean(0)
        perplexity = (-(avg_probs * torch.log(avg_probs + 1e-10)).sum()).exp()
        cluster_use = torch.sum(avg_probs > 0)
        return perplexity, cluster_use


# class VectorQuantizer_old(nn.Module):
#     """
#     see https://github.com/MishaLaskin/vqvae/blob/d761a999e2267766400dc646d82d3ac3657771d4/models/quantizer.py
#     ____________________________________________
#     Discretization bottleneck part of the VQ-VAE.
#     Inputs:
#     - n_e : number of embeddings
#     - e_dim : dimension of embedding
#     - beta : commitment cost used in loss term, beta * ||z_e(x)-sg[e]||^2
#     _____________________________________________
#     """
#
#     # NOTE: this class contains a bug regarding beta; see VectorQuantizer2 for
#     # a fix and use legacy=False to apply that fix. VectorQuantizer2 can be
#     # used wherever VectorQuantizer has been used before and is additionally
#     # more efficient.
#     def __init__(self, n_e, e_dim, beta):
#         super(VectorQuantizer, self).__init__()
#         self.n_e = n_e
#         self.e_dim = e_dim
#         self.beta = beta
#
#         self.embedding = nn.Embedding(self.n_e, self.e_dim)
#         self.embedding.weight.data.uniform_(-1.0 / self.n_e, 1.0 / self.n_e)
#
#     def forward(self, z):
#         """
#         Inputs the output of the encoder network z and maps it to a discrete
#         one-hot vector that is the index of the closest embedding vector e_j
#         z (continuous) -> z_q (discrete)
#         z.shape = (batch, channel, height, width)
#         quantization pipeline:
#             1. get encoder input (B,C,H,W)
#             2. flatten input to (B*H*W,C)
#         """
#         # reshape z -> (batch, height, width, channel) and flatten
#         z = z.permute(0, 2, 3, 1).contiguous()
#         z_flattened = z.view(-1, self.e_dim)
#         # distances from z to embeddings e_j (z - e)^2 = z^2 + e^2 - 2 e * z
#
#         d = torch.sum(z_flattened ** 2, dim=1, keepdim=True) + \
#             torch.sum(self.embedding.weight**2, dim=1) - 2 * \
#             torch.matmul(z_flattened, self.embedding.weight.t())
#
#         ## could possible replace this here
#         # #\start...
#         # find closest encodings
#         min_encoding_indices = torch.argmin(d, dim=1).unsqueeze(1)
#
#         min_encodings = torch.zeros(
#             min_encoding_indices.shape[0], self.n_e).to(z)
#         min_encodings.scatter_(1, min_encoding_indices, 1)
#
#         # dtype min encodings: torch.float32
#         # min_encodings shape: torch.Size([2048, 512])
#         # min_encoding_indices.shape: torch.Size([2048, 1])
#
#         # get quantized latent vectors
#         z_q = torch.matmul(min_encodings, self.embedding.weight).view(z.shape)
#         #.........\end
#
#         # with:
#         # .........\start
#         #min_encoding_indices = torch.argmin(d, dim=1)
#         #z_q = self.embedding(min_encoding_indices)
#         # ......\end......... (TODO)
#
#         # compute loss for embedding
#         loss = torch.mean((z_q.detach()-z)**2) + self.beta * \
#             torch.mean((z_q - z.detach()) ** 2)
#
#         # preserve gradients
#         z_q = z + (z_q - z).detach()
#
#         # perplexity
#         e_mean = torch.mean(min_encodings, dim=0)
#         perplexity = torch.exp(-torch.sum(e_mean * torch.log(e_mean + 1e-10)))
#
#         # reshape back to match original input shape
#         z_q = z_q.permute(0, 3, 1, 2).contiguous()
#
#         return z_q, loss, (perplexity, min_encodings, min_encoding_indices)
#
#     def get_codebook_entry(self, indices, shape):
#         # shape specifying (batch, height, width, channel)
#         # TODO: check for more easy handling with nn.Embedding
#         min_encodings = torch.zeros(indices.shape[0], self.n_e).to(indices)
#         min_encodings.scatter_(1, indices[:,None], 1)
#
#         # get quantized latent vectors
#         z_q = torch.matmul(min_encodings.float(), self.embedding.weight)
#
#         if shape is not None:
#             z_q = z_q.view(shape)
#
#             # reshape back to match original input shape
#             z_q = z_q.permute(0, 3, 1, 2).contiguous()
#
#         return z_q
#
#
# class GumbelQuantize(nn.Module):
#     """
#     credit to @karpathy: https://github.com/karpathy/deep-vector-quantization/blob/main/model.py (thanks!)
#     Gumbel Softmax trick quantizer
#     Categorical Reparameterization with Gumbel-Softmax, Jang et al. 2016
#     https://arxiv.org/abs/1611.01144
#     """
#     def __init__(self, num_hiddens, embedding_dim, n_embed, straight_through=True,
#                  kl_weight=5e-4, temp_init=1.0, use_vqinterface=True,
#                  remap=None, unknown_index="random"):
#         super().__init__()
#
#         self.embedding_dim = embedding_dim
#         self.n_embed = n_embed
#
#         self.straight_through = straight_through
#         self.temperature = temp_init
#         self.kl_weight = kl_weight
#
#         self.proj = nn.Conv2d(num_hiddens, n_embed, 1)
#         self.embed = nn.Embedding(n_embed, embedding_dim)
#
#         self.use_vqinterface = use_vqinterface
#
#         self.remap = remap
#         if self.remap is not None:
#             self.register_buffer("used", torch.tensor(np.load(self.remap)))
#             self.re_embed = self.used.shape[0]
#             self.unknown_index = unknown_index # "random" or "extra" or integer
#             if self.unknown_index == "extra":
#                 self.unknown_index = self.re_embed
#                 self.re_embed = self.re_embed+1
#             print(f"Remapping {self.n_embed} indices to {self.re_embed} indices. "
#                   f"Using {self.unknown_index} for unknown indices.")
#         else:
#             self.re_embed = n_embed
#
#     def remap_to_used(self, inds):
#         ishape = inds.shape
#         assert len(ishape)>1
#         inds = inds.reshape(ishape[0],-1)
#         used = self.used.to(inds)
#         match = (inds[:,:,None]==used[None,None,...]).long()
#         new = match.argmax(-1)
#         unknown = match.sum(2)<1
#         if self.unknown_index == "random":
#             new[unknown]=torch.randint(0,self.re_embed,size=new[unknown].shape).to(device=new.device)
#         else:
#             new[unknown] = self.unknown_index
#         return new.reshape(ishape)
#
#     def unmap_to_all(self, inds):
#         ishape = inds.shape
#         assert len(ishape)>1
#         inds = inds.reshape(ishape[0],-1)
#         used = self.used.to(inds)
#         if self.re_embed > self.used.shape[0]: # extra token
#             inds[inds>=self.used.shape[0]] = 0 # simply set to zero
#         back=torch.gather(used[None,:][inds.shape[0]*[0],:], 1, inds)
#         return back.reshape(ishape)
#
#     def forward(self, z, temp=None, return_logits=False):
#         # force hard = True when we are in eval mode, as we must quantize. actually, always true seems to work
#         hard = self.straight_through if self.training else True
#         temp = self.temperature if temp is None else temp
#
#         logits = self.proj(z)
#         if self.remap is not None:
#             # continue only with used logits
#             full_zeros = torch.zeros_like(logits)
#             logits = logits[:,self.used,...]
#
#         soft_one_hot = F.gumbel_softmax(logits, tau=temp, dim=1, hard=hard)
#         if self.remap is not None:
#             # go back to all entries but unused set to zero
#             full_zeros[:,self.used,...] = soft_one_hot
#             soft_one_hot = full_zeros
#         z_q = einsum('b n h w, n d -> b d h w', soft_one_hot, self.embed.weight)
#
#         # + kl divergence to the prior loss
#         qy = F.softmax(logits, dim=1)
#         diff = self.kl_weight * torch.sum(qy * torch.log(qy * self.n_embed + 1e-10), dim=1).mean()
#
#         ind = soft_one_hot.argmax(dim=1)
#         if self.remap is not None:
#             ind = self.remap_to_used(ind)
#         if self.use_vqinterface:
#             if return_logits:
#                 return z_q, diff, (None, None, ind), logits
#             return z_q, diff, (None, None, ind)
#         return z_q, diff, ind
#
#     def get_codebook_entry(self, indices, shape):
#         b, h, w, c = shape
#         assert b*h*w == indices.shape[0]
#         indices = rearrange(indices, '(b h w) -> b h w', b=b, h=h, w=w)
#         if self.remap is not None:
#             indices = self.unmap_to_all(indices)
#         one_hot = F.one_hot(indices, num_classes=self.n_embed).permute(0, 3, 1, 2).float()
#         z_q = einsum('b n h w, n d -> b d h w', one_hot, self.embed.weight)
#         return z_q
#
# class EmbeddingEMA(nn.Module):
#     def __init__(self, num_tokens, codebook_dim, decay=0.99, eps=1e-5):
#         super().__init__()
#         self.decay = decay
#         self.eps = eps
#         weight = torch.randn(num_tokens, codebook_dim)
#         self.weight = nn.Parameter(weight, requires_grad = False)
#         self.cluster_size = nn.Parameter(torch.zeros(num_tokens), requires_grad = False)
#         self.embed_avg = nn.Parameter(weight.clone(), requires_grad = False)
#         self.update = True
#
#     def forward(self, embed_id):
#         return F.embedding(embed_id, self.weight)
#
#     def cluster_size_ema_update(self, new_cluster_size):
#         self.cluster_size.data.mul_(self.decay).add_(new_cluster_size, alpha=1 - self.decay)
#
#     def embed_avg_ema_update(self, new_embed_avg):
#         self.embed_avg.data.mul_(self.decay).add_(new_embed_avg, alpha=1 - self.decay)
#
#     def weight_update(self, num_tokens):
#         n = self.cluster_size.sum()
#         smoothed_cluster_size = (
#                 (self.cluster_size + self.eps) / (n + num_tokens * self.eps) * n
#             )
#         #normalize embedding average with smoothed cluster size
#         embed_normalized = self.embed_avg / smoothed_cluster_size.unsqueeze(1)
#         self.weight.data.copy_(embed_normalized)
#
#
# class EMAVectorQuantizer(nn.Module):
#     def __init__(self, n_embed, embedding_dim, beta, decay=0.99, eps=1e-5,
#                 remap=None, unknown_index="random"):
#         super().__init__()
#         self.codebook_dim = codebook_dim
#         self.num_tokens = num_tokens
#         self.beta = beta
#         self.embedding = EmbeddingEMA(self.num_tokens, self.codebook_dim, decay, eps)
#         self.remap = remap
#         if self.remap is not None:
#             self.register_buffer("used", torch.tensor(np.load(self.remap)))
#             self.re_embed = self.used.shape[0]
#             self.unknown_index = unknown_index # "random" or "extra" or integer
#             if self.unknown_index == "extra":
#                 self.unknown_index = self.re_embed
#                 self.re_embed = self.re_embed+1
#
#             print(f"Remapping {self.n_embed} indices to {self.re_embed} indices. "
#                   f"Using {self.unknown_index} for unknown indices.")
#         else:
#             self.re_embed = n_embed
#
#     def remap_to_used(self, inds):
#         ishape = inds.shape
#         assert len(ishape)>1
#         inds = inds.reshape(ishape[0],-1)
#         used = self.used.to(inds)
#         match = (inds[:,:,None]==used[None,None,...]).long()
#         new = match.argmax(-1)
#         unknown = match.sum(2)<1
#         if self.unknown_index == "random":
#             new[unknown]=torch.randint(0,self.re_embed,size=new[unknown].shape).to(device=new.device)
#         else:
#             new[unknown] = self.unknown_index
#         return new.reshape(ishape)
#
#     def unmap_to_all(self, inds):
#         ishape = inds.shape
#         assert len(ishape)>1
#         inds = inds.reshape(ishape[0],-1)
#         used = self.used.to(inds)
#         if self.re_embed > self.used.shape[0]: # extra token
#             inds[inds>=self.used.shape[0]] = 0 # simply set to zero
#         back=torch.gather(used[None,:][inds.shape[0]*[0],:], 1, inds)
#         return back.reshape(ishape)
#
#     def forward(self, z):
#         # reshape z -> (batch, height, width, channel) and flatten
#         #z, 'b c h w -> b h w c'
#         z = rearrange(z, 'b c h w -> b h w c')
#         z_flattened = z.reshape(-1, self.codebook_dim)
#
#         # distances from z to embeddings e_j (z - e)^2 = z^2 + e^2 - 2 e * z
#         d = z_flattened.pow(2).sum(dim=1, keepdim=True) + \
#             self.embedding.weight.pow(2).sum(dim=1) - 2 * \
#             torch.einsum('bd,nd->bn', z_flattened, self.embedding.weight) # 'n d -> d n'
#
#
#         encoding_indices = torch.argmin(d, dim=1)
#
#         z_q = self.embedding(encoding_indices).view(z.shape)
#         encodings = F.one_hot(encoding_indices, self.num_tokens).type(z.dtype)
#         avg_probs = torch.mean(encodings, dim=0)
#         perplexity = torch.exp(-torch.sum(avg_probs * torch.log(avg_probs + 1e-10)))
#
#         if self.training and self.embedding.update:
#             #EMA cluster size
#             encodings_sum = encodings.sum(0)
#             self.embedding.cluster_size_ema_update(encodings_sum)
#             #EMA embedding average
#             embed_sum = encodings.transpose(0,1) @ z_flattened
#             self.embedding.embed_avg_ema_update(embed_sum)
#             #normalize embed_avg and update weight
#             self.embedding.weight_update(self.num_tokens)
#
#         # compute loss for embedding
#         loss = self.beta * F.mse_loss(z_q.detach(), z)
#
#         # preserve gradients
#         z_q = z + (z_q - z).detach()
#
#         # reshape back to match original input shape
#         #z_q, 'b h w c -> b c h w'
#         z_q = rearrange(z_q, 'b h w c -> b c h w')
#         return z_q, loss, (perplexity, encodings, encoding_indices)