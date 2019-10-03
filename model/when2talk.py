#!/usr/bin/python3
# Author: GMFTBY
# Time: 2019.9.29

'''
When to talk, control the talk timing
'''


import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
from torch_geometric.nn import GCNConv, TopKPooling
from torch_geometric.data import Data, DataLoader    # create the graph batch dynamically
import numpy as np
import random
import math
from .layers import *
import ipdb


class Utterance_encoder_w2t(nn.Module):

    def __init__(self, input_size, embedding_size, 
                 hidden_size, dropout=0.5, n_layer=1, pretrained=False):
        super(Utterance_encoder_w2t, self).__init__()

        self.embedding_size = embedding_size
        self.hidden_size = hidden_size
        self.input_size = input_size
        self.n_layer = n_layer

        self.embed = nn.Embedding(input_size, self.embedding_size)
        self.gru = nn.GRU(self.embedding_size, self.hidden_size, num_layers=n_layer, 
                          dropout=dropout, bidirectional=True)
        self.hidden_proj = nn.Linear(n_layer * 2 * self.hidden_size, hidden_size)
        self.bn = nn.BatchNorm1d(num_features=hidden_size)

        self.init_weight()

    def init_weight(self):
        init.xavier_normal_(self.hidden_proj.weight)
        init.orthogonal_(self.gru.weight_hh_l0)
        init.orthogonal_(self.gru.weight_ih_l0)
        self.gru.bias_ih_l0.data.fill_(0.0)
        self.gru.bias_hh_l0.data.fill_(0.0)

    def forward(self, inpt, lengths, hidden=None):
        embedded = self.embed(inpt)
        if not hidden:
            hidden = torch.randn(self.n_layer * 2, len(lengths), self.hidden_size)
            if torch.cuda.is_available():
                hidden = hidden.cuda()

        embedded = nn.utils.rnn.pack_padded_sequence(embedded, lengths, enforce_sorted=False)
        _, hidden = self.gru(embedded, hidden)
        hidden = hidden.permute(1, 0, 2)
        hidden = hidden.reshape(hidden.size(0), -1)
        hidden = self.bn(self.hidden_proj(hidden))
        hidden = torch.tanh(hidden)

        return hidden    # [batch, hidden]

        
class GCNContext(nn.Module):

    '''
    GCN Context encoder

    It should be noticed that PyG merges all the subgraph in the batch into a big graph
    which is a sparse block diagonal adjacency matrices.
    Refer: Mini-batches in https://pytorch-geometric.readthedocs.io/en/latest/notes/introduction.html

    Our implementation is the three layers GCN with the position embedding
    '''

    def __init__(self, inpt_size, hidden_size, output_size, posemb_size, bn=False, dropout=0.5):
        # inpt_size: utter_hidden_size + user_embed_size
        super(GCNContext, self).__init__()
        self.conv1 = GCNConv(inpt_size + posemb_size, hidden_size)
        # self.pool1 = TopKPooling(hidden_size, ratio=0.8)
        self.conv2 = GCNConv(hidden_size, hidden_size)
        # self.pool2 = TopKPooling(hidden_size, ratio=0.8)
        self.conv3 = GCNConv(hidden_size, hidden_size)
        # self.pool3 = TopKPooling(hidden_size, ratio=0.8)

        # BN
        self.bn = bn
        if self.bn:
            self.bn1 = nn.BatchNorm1d(num_features=hidden_size)
            self.bn2 = nn.BatchNorm1d(num_features=hidden_size)
            self.bn3 = nn.BatchNorm1d(num_features=hidden_size)

        self.linear = nn.Linear(hidden_size, output_size)
        self.drop = nn.Dropout(p=dropout)
        self.posemb = nn.Embedding(100, posemb_size)    # 100 is far bigger than the max turn lengths
        
    def create_batch(self, gbatch, utter_hidden):
        '''create one graph batch
        :param: gbatch [batch_size, ([2, edge_num], [edge_num])]
        :param: utter_hidden [turn_len(node), batch, hidden_size]'''
        utter_hidden = utter_hidden.permute(1, 0, 2)    # [batch, node, hidden_size]
        batch_size = len(utter_hidden)
        data_list, weights = [], []
        for idx, example in enumerate(gbatch):
            edge_index, edge_w = example
            edge_index = torch.tensor(edge_index, dtype=torch.long)
            edge_w = torch.tensor(edge_w, dtype=torch.float)
            data_list.append(Data(x=utter_hidden[idx], edge_index=edge_index))
            weights.append(edge_w)
        # this special loader only have one batch
        loader = DataLoader(data_list, batch_size=batch_size)
        batch = list(loader)
        assert len(batch) == 1
        batch = batch[0]    # one big graph (mini-batch in PyG)
        weights = torch.cat(weights)

        return batch, weights

    def forward(self, gbatch, utter_hidden):
        # utter_hidden: [turn_len, batch, hidden_size + user_embed_size]
        batch, weights = self.create_batch(gbatch, utter_hidden)
        x, edge_index, batch = batch.x, batch.edge_index, batch.batch
        
        # cat pos_embed: [node, posemb_size]
        batch_size = torch.max(batch).item() + 1
        turn_size = utter_hidden.size(0)

        pos = []
        for i in range(batch_size):
            pos.append(torch.arange(turn_size, dtype=torch.long))
        pos = torch.cat(pos)

        # load to GPU
        if torch.cuda.is_available():
            x = x.cuda()
            edge_index = edge_index.cuda()
            batch = batch.cuda()
            weights = weights.cuda()
            pos = pos.cuda()    # [node]
        
        pos = self.posemb(pos)    # [node, pos_emb]
        x = torch.cat([x, pos], dim=1)    # [node, pos_emb + hidden_size + user_embed_size]

        if self.bn:
            x1 = F.relu(self.bn1(self.conv1(x, edge_index, edge_weight=weights)))
            x2 = F.relu(self.bn2(self.conv2(x1, edge_index, edge_weight=weights)))
            x3 = F.relu(self.bn3(self.conv3(x2, edge_index, edge_weight=weights)))
        else:
            x1 = F.relu(self.conv1(x, edge_index, edge_weight=weights))
            x2 = F.relu(self.conv1(x1, edge_index, edge_weight=weights))
            x3 = F.relu(self.conv1(x2, edge_index, edge_weight=weights))
            
        # residual, [nodes, hidden_size]
        x = x1 + x2 + x3
        x = self.drop(x)
        x = torch.tanh(self.linear(x))    # [nodes, hidden_size]

        # [nodes/turn_len, output_size]
        # take apart to get the mini-batch
        x = torch.stack(x.chunk(batch_size, dim=0))    # [batch, turn, output_size]
        return x

    
class Decoder_w2t(nn.Module):
    
    def __init__(self, output_size, embed_size, hidden_size, user_embed_size=10,
                 pretrained=None):
        super(Decoder_w2t, self).__init__()
        self.output_size = output_size
        self.hidden_size = hidden_size
        self.embed_size = embed_size
        self.embed = nn.Embedding(self.output_size, self.embed_size)
        self.gru = nn.GRU(self.embed_size + self.hidden_size, self.hidden_size)
        self.out = nn.Linear(hidden_size, output_size)

        self.attn = Attention(hidden_size)
        self.init_weight()

    def init_weight(self):
        init.orthogonal_(self.gru.weight_hh_l0)
        init.orthogonal_(self.gru.weight_ih_l0)
        
    def forward(self, inpt, last_hidden, gcncontext):
        # inpt: [batch_size], last_hidden: [1, batch, hidden_size]
        # gcncontext: [turn_len, batch, hidden_size], user_de: [batch, 11]
        embedded = self.embed(inpt).unsqueeze(0)    # [1, batch_size, embed_size]
        last_hidden = last_hidden.squeeze(0)    # [batch, hidden]

        # attention on the gcncontext
        attn_weights = self.attn(last_hidden, gcncontext)
        context = attn_weights.bmm(gcncontext.transpose(0, 1))
        context = context.transpose(0, 1)    # [1, batch, hidden]

        rnn_inpt = torch.cat([embedded, context], 2)    # [1, batch, embed_size + hidden]

        output, hidden = self.gru(rnn_inpt, last_hidden.unsqueeze(0).contiguous())
        output = output.squeeze(0)      # [batch, hidden_size]
        # context = context.squeeze(0)    # [batch, hidden]
        # output = torch.cat([output, context, user_de], 1)    # [batch, hidden * 2 + 1 + user_embed]
        output = self.out(output)   # [batch, output_size]
        output = F.log_softmax(output, dim=1)

        # [batch, output_size], [1, batch, hidden_size]
        return output, hidden
    
    
class When2Talk(nn.Module):
    
    '''
    When2Talk model
    '''
    
    def __init__(self, input_size, output_size, embed_size, utter_hidden_size, 
                 context_hidden_size, decoder_hidden_size, position_embed_size, 
                 user_embed_size=10, teach_force=0.5, pad=0, sos=0, dropout=0.5, utter_n_layer=1, bn=False):
        super(When2Talk, self).__init__()
        self.teach_force = teach_force
        self.output_size = output_size
        self.pad, self.sos = pad, sos
        self.utter_encoder = Utterance_encoder_w2t(input_size, embed_size, utter_hidden_size, 
                                                   dropout=dropout, n_layer=utter_n_layer) 
        self.gcncontext = GCNContext(utter_hidden_size+user_embed_size, context_hidden_size, 
                                     context_hidden_size, position_embed_size, bn=bn, dropout=dropout)
        self.decoder = Decoder_w2t(output_size, embed_size, decoder_hidden_size, 
                                   user_embed_size=user_embed_size) 
        
        # user embedding, 10 
        self.user_embed = nn.Embedding(2, 10)

        # decision module
        self.decision_1 = nn.Linear(context_hidden_size + user_embed_size, int(context_hidden_size / 2))
        self.decision_2 = nn.Linear(int(context_hidden_size / 2), 1)
        self.decision_drop = nn.Dropout(p=dropout)

        # hidden project
        self.hidden_proj = nn.Linear(context_hidden_size + user_embed_size, 
                                     decoder_hidden_size)
        self.hidden_drop = nn.Dropout(p=dropout)
        
    def forward(self, src, tgt, gbatch, subatch, tubatch, lengths):
        '''
        :param: src, [turns, lengths, bastch]
        :param: tgt, [lengths, batch]
        :param: gbatch, [batch, ([2, num_edges], [num_edges])]
        :param: subatch, [turn, batch]
        :param: tubatch, [batch]
        :param: lengths, [turns, batch]
        '''
        turn_size, batch_size, maxlen = len(src), tgt.size(1), tgt.size(0)
        outputs = torch.zeros(maxlen, batch_size, self.output_size)
        if torch.cuda.is_available():
            outputs = outputs.cuda()

        subatch = self.user_embed(subatch)    # [turn, batch, 10]
        tubatch = self.user_embed(tubatch)    # [batch, 10]

        # utterance encoding
        turns = []
        for i in range(turn_size):
            hidden = self.utter_encoder(src[i], lengths[i])
            turns.append(hidden)
        turns = torch.stack(turns)    # [turn_len, batch, utter_hidden]

        # GCN Context encoder
        # context_output: [batch, turn, hidden_size]
        # combine the subatch and turns
        x = torch.cat([turns, subatch], 2)    # [turn, batch, utter_hidden + user_embed_size]
        context_output = self.gcncontext(gbatch, x)
        context_output = context_output.permute(1, 0, 2)    # [turn, batch, hidden]
        hidden = context_output[-1]    # [batch, hidden]

        # decision, use the last sentence embedding as the input
        decision_inpt = torch.cat([hidden, tubatch], 1)     # [batch, hidden+10] 
        de = self.decision_drop(torch.tanh(self.decision_1(decision_inpt)))
        de = torch.sigmoid(self.decision_2(de)).squeeze(1)     # [batch]

        # ========== decoding with the tgt_user & decision information ==========
        # user_de = torch.cat([tubatch, de.unsqueeze(1)], 1)    # [batch, 1 + user_embed_size]

        # ========== hidden project ==========
        hidden = torch.cat([hidden, tubatch], 1)    # [batch, hidden+user_embed]
        hidden = self.hidden_drop(torch.tanh(self.hidden_proj(hidden)))  # [batch, hidden]

        # decoding step
        hidden = hidden.unsqueeze(0)     # [1, batch, hidden_size]
        output  = tgt[0, :]

        for i in range(1, maxlen):
            output, hidden = self.decoder(output, hidden, context_output)
            outputs[i] = output
            is_teacher = random.random() < self.teach_force
            top1 = output.data.max(1)[1]
            if is_teacher:
                output = tgt[i].clone().detach()
            else:
                output = top1

        # de: [batch], outputs: [maxlen, batch, output_size]
        return de, outputs
    
    def predict(self, src, gbatch, subatch, tubatch, maxlen, lengths):
        # similar with the forward function
        # src: [turn, maxlen, batch_size], lengths: [turn, batch_size]
        # subatch: [turn_len, batch], tubatch: [batch]
        # output: [maxlen, batch_size]
        turn_size, batch_size = len(src), src[0].size(1)
        outputs = torch.zeros(maxlen, batch_size)
        if torch.cuda.is_available():
            outputs = outputs.cuda()

        subatch = self.user_embed(subatch)    # [turn, batch, 10]
        tubatch = self.user_embed(tubatch)    # [batch, 10]

        # utterance encoding
        turns = []
        for i in range(turn_size):
            hidden = self.utter_encoder(src[i], lengths[i])
            turns.append(hidden)
        turns = torch.stack(turns)     # [turn, batch, hidden]

        # GCN Context encoding
        x = torch.cat([turns, subatch], 2)    # [turn, batch, hidden + user_embed_size]
        context_output = self.gcncontext(gbatch, x)    # [batch, turn, hidden]
        context_output = context_output.permute(1, 0, 2)    # [turn, batch, hidden]
        hidden = context_output[-1]     # [batch, hidden]

        # decision
        decision_inpt = torch.cat([hidden, tubatch], 1)     # [batch, hidden+user_embed_size]
        de = self.decision_drop(torch.tanh(self.decision_1(decision_inpt))) 
        de = torch.sigmoid(self.decision_2(de)).squeeze(1)     # [batch]

        # ========== decoding with tgt_user & decision information ==========
        # user_de = torch.cat([tubatch, de.unsqueeze(1)], 1)     # [batch, 1 + embed_size]

        # ========== hidden project ==========
        hidden = torch.cat([hidden, tubatch], 1)    # [batch, hidden+user_embed]
        hidden = self.hidden_drop(torch.tanh(self.hidden_proj(hidden)))  # [batch, hidden]

        hidden = hidden.unsqueeze(0)     # [1, batch, hidden]
        output = torch.zeros(batch_size, dtype=torch.long).fill_(self.sos)
        if torch.cuda.is_available():
            output = output.cuda()
        
        for i in range(1, maxlen):
            output, hidden = self.decoder(output, hidden, context_output)
            output = output.max(1)[1]
            outputs[i] = output

        # de: [batch], outputs: [maxlen, batch]
        return de, outputs


if __name__ == "__main__":
    pass
