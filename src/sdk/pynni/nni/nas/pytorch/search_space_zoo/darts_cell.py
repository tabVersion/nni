# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from collections import OrderedDict

import torch
import torch.nn as nn
from nni.nas.pytorch import mutables

import ops


class Node(nn.Module):
    def __init__(self, node_id, num_prev_nodes, channels, num_downsample_connect):
        """
        builtin Darts Node structure

        Attributes
        ---
        node_id: str
        num_prev_nodes: int
            the number of previous nodes in this cell
        channels: int
            output channels
        num_downsample_connect: int
        """
        super().__init__()
        self.ops = nn.ModuleList()
        choice_keys = []
        for i in range(num_prev_nodes):
            stride = 2 if i < num_downsample_connect else 1
            choice_keys.append("{}_p{}".format(node_id, i))
            self.ops.append(
                mutables.LayerChoice(OrderedDict([
                    ("maxpool", ops.PoolBN('max', channels, 3, stride, 1, affine=False)),
                    ("avgpool", ops.PoolBN('avg', channels, 3, stride, 1, affine=False)),
                    ("skipconnect",
                     nn.Identity() if stride == 1 else ops.FactorizedReduce(channels, channels, affine=False)),
                    ("sepconv3x3", ops.SepConv(channels, channels, 3, stride, 1, affine=False)),
                    ("sepconv5x5", ops.SepConv(channels, channels, 5, stride, 2, affine=False)),
                    ("dilconv3x3", ops.DilConv(channels, channels, 3, stride, 2, 2, affine=False)),
                    ("dilconv5x5", ops.DilConv(channels, channels, 5, stride, 4, 2, affine=False))
                ]), key=choice_keys[-1]))
        self.drop_path = ops.DropPath()
        self.input_switch = mutables.InputChoice(choose_from=choice_keys, n_chosen=2, key="{}_switch".format(node_id))

    def forward(self, prev_nodes):
        assert len(self.ops) == len(prev_nodes)
        out = [op(node) for op, node in zip(self.ops, prev_nodes)]
        out = [self.drop_path(o) if o is not None else None for o in out]
        return self.input_switch(out)


class DartsCell(nn.Module):
    """
    Builtin Darts Cell structure. MUST manually call `init()` to setup.

    Attributes
    ---
    n_nodes: int
        the number of nodes contained in this cell
    """

    def __init__(self, n_nodes):
        super().__init__()
        self.reduction = False
        self.n_nodes = n_nodes
        self.reduction_p = False

        self.channels_p = 0
        self.channels_pp = 0
        self.channels = 0

        self.preproc0 = None
        self.preproc1 = None
        self.mutable_ops = None

    def init(self):
        # If previous cell is reduction cell, current input size does not match with
        # output size of cell[k-2]. So the output[k-2] should be reduced by preprocessing.
        if self.reduction_p:
            self.preproc0 = ops.FactorizedReduce(self.channels_pp, self.channels, affine=False)
        else:
            self.preproc0 = ops.StdConv(self.channels_pp, self.channels, 1, 1, 0, affine=False)
        self.preproc1 = ops.StdConv(self.channels_p, self.channels, 1, 1, 0, affine=False)

        # generate dag
        self.mutable_ops = nn.ModuleList()
        for depth in range(2, self.n_nodes + 2):
            self.mutable_ops.append(Node("{}_n{}".format("reduce" if self.reduction else "normal", depth),
                                         depth, self.channels, 2 if self.reduction else 0))

    def forward(self, s0, s1):
        # s0, s1 are the outputs of previous previous cell and previous cell, respectively.
        tensors = [self.preproc0(s0), self.preproc1(s1)]
        for node in self.mutable_ops:
            cur_tensor = node(tensors)
            tensors.append(cur_tensor)

        output = torch.cat(tensors[2:], dim=1)
        return output
