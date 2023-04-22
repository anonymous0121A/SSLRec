import torch as t
from torch import nn
import torch.nn.functional as F
from config.configurator import configs
from models.loss_utils import cal_bpr_loss, cal_infonce_loss, reg_params
from models.base_model import BaseModel
from models.model_utils import SpAdjEdgeDrop

init = nn.init.xavier_uniform_
uniformInit = nn.init.uniform
model_config = configs['model']

class HCCF(BaseModel):
	def __init__(self, data_handler):
		super(HCCF, self).__init__(data_handler)

		self.adj = data_handler.torch_adj

		self.layer_num = model_config['layer_num']
		self.reg_weight = model_config['reg_weight']
		self.cl_weight = model_config['cl_weight']
		self.hyper_num = model_config['hyper_num']
		self.mult = model_config['mult']
		self.keep_rate = model_config['keep_rate']
		self.temperature = model_config['temperature']

		self.user_embeds = nn.Parameter(init(t.empty(self.user_num, self.embedding_size)))
		self.item_embeds = nn.Parameter(init(t.empty(self.item_num, self.embedding_size)))
		self.hgnn_layer = HGNNLayer()
		self.user_hyper_embeds = nn.Parameter(init(t.empty(self.embedding_size, self.hyper_num)))
		self.item_hyper_embeds = nn.Parameter(init(t.empty(self.embedding_size, self.hyper_num)))

		self.edge_drop = SpAdjEdgeDrop()
	
	def _gcn_layer(self, adj, embeds):
		return t.spmm(adj, embeds)
	
	def forward(self, adj, keep_rate):
		embeds = t.concat([self.user_embeds, self.item_embeds], dim=0)
		embeds_list = [embeds]
		gcn_embeds_list = []
		hyper_embeds_list = []
		uu_hyper = self.user_embeds * self.mult
		ii_hyper = self.item_embeds * self.mult

		for i in range(self.layer_num):
			tem_embeds = self._gcn_layer(self.edge_drop(adj, keep_rate), embeds_list[-1])
			hyper_user_embeds = self.hgnn_layer(F.dropout(uu_hyper, p=1-keep_rate), embeds_list[-1][:self.user_num])
			hyper_item_embeds = self.hgnn_layer(F.dropout(ii_hyper, p=1-keep_rate), embeds_list[-1][self.user_num:])
			gcn_embeds_list.append(tem_embeds)
			hyper_embeds_list.append(t.concat([hyper_user_embeds, hyper_item_embeds], dim=0))
			embeds_list.append(tem_embeds + hyper_embeds_list[-1])
		embeds = sum(embeds_list)
		return embeds, gcn_embeds_list, hyper_embeds_list
	
	def cal_loss(self, batch_data):
		ancs, poss, negs = batch_data
		embeds, gcn_embeds_list, hyper_embeds_list = self.forward(self.adj, self.keep_rate)
		user_embeds, item_embeds = embeds[:self.user_num], embeds[self.user_num:]

		anc_embeds = user_embeds[ancs]
		pos_embeds = item_embeds[poss]
		neg_embeds = item_embeds[negs]
		bpr_loss = cal_bpr_loss(anc_embeds, pos_embeds, neg_embeds) / anc_embeds.shape[0]

		cl_loss = 0
		for i in range(len(gcn_embeds_list)):
			embeds1 = gcn_embeds_list[i].detach()
			embeds2 = hyper_embeds_list[i]
			pck_user_embeds1 = embeds1[:self.user_num][ancs]
			pck_user_embeds2 = embeds2[:self.user_num][ancs]
			pck_item_embeds1 = embeds1[self.user_num:][poss]
			pck_item_embeds2 = embeds2[self.user_num:][poss]
			cl_loss += cal_infonce_loss(pck_user_embeds1, pck_user_embeds2, embeds2[:self.user_num], self.temperature) / pck_user_embeds1.shape[0]
			cl_loss += cal_infonce_loss(pck_item_embeds1, pck_item_embeds2, embeds2[self.user_num:], self.temperature) / pck_item_embeds1.shape[0]
		
		reg_loss = reg_params(self) * self.reg_weight
		cl_loss *= self.cl_weight

		loss = bpr_loss + reg_loss + cl_loss
		losses = {'bpr_loss': bpr_loss, 'reg_loss': reg_loss, 'cl_loss': cl_loss}
		return loss, losses
	
	def full_predict(self, batch_data):
		embeds, _, _ = self.forward(self.adj, 1.0)
		user_embeds, item_embeds = embeds[:self.user_num], embeds[self.user_num:]
		pck_users, train_mask = batch_data
		pck_users = pck_users.long()
		pck_user_embeds = user_embeds[pck_users]
		full_preds = pck_user_embeds @ item_embeds.T
		full_preds = self._mask_predict(full_preds, train_mask)
		return full_preds

class HGNNLayer(nn.Module):
	def __init__(self):
		super(HGNNLayer, self).__init__()
		self.act = nn.LeakyReLU(negative_slope=model_config['leaky'])
	
	def forward(self, adj, embeds):
		hids = self.act(adj.T @ embeds)
		embeds = self.act(adj @ hids)
		return embeds