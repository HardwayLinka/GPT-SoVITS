# modified from https://github.com/feng-yufei/shared_debugging_code/blob/main/model/t2s_model.py
import torch
from tqdm import tqdm

from AR.models.utils import make_pad_mask
from AR.models.utils import topk_sampling,sample,logits_to_probs,multinomial_sample_one_no_sync
from AR.modules.embedding import SinePositionalEmbedding
from AR.modules.embedding import TokenEmbedding
from AR.modules.transformer import LayerNorm
from AR.modules.transformer import TransformerEncoder
from AR.modules.transformer import TransformerEncoderLayer
from torch import nn
from torch.nn import functional as F
from torchmetrics.classification import MulticlassAccuracy

default_config = {
    "embedding_dim": 512,
    "hidden_dim": 512,
    "num_head": 8,
    "num_layers": 12,
    "num_codebook": 8,
    "p_dropout": 0.0,
    "vocab_size": 1024 + 1,
    "phoneme_vocab_size": 512,
    "EOS": 1024
}


class Text2SemanticDecoder(nn.Module):
    def __init__(self, config, norm_first=False, top_k=3):
        super(Text2SemanticDecoder, self).__init__()
        self.model_dim = config['model']["hidden_dim"]
        self.embedding_dim = config['model']["embedding_dim"]
        self.num_head = config['model']["head"]
        self.num_layers = config['model']["n_layer"]
        self.norm_first = norm_first
        self.vocab_size = config['model']["vocab_size"]
        self.phoneme_vocab_size = config['model']["phoneme_vocab_size"]
        self.p_dropout = config['model']["dropout"]
        self.EOS = config['model']["EOS"]
        self.norm_first = norm_first
        assert self.EOS == self.vocab_size - 1
        # should be same as num of kmeans bin
        # assert self.EOS == 1024
        self.bert_proj = nn.Linear(1024, self.embedding_dim)
        self.ar_text_embedding = TokenEmbedding(
            self.embedding_dim, self.phoneme_vocab_size, self.p_dropout)
        self.ar_text_position = SinePositionalEmbedding(
            self.embedding_dim, dropout=0.1, scale=False, alpha=True)
        self.ar_audio_embedding = TokenEmbedding(
            self.embedding_dim, self.vocab_size, self.p_dropout)
        self.ar_audio_position = SinePositionalEmbedding(
            self.embedding_dim, dropout=0.1, scale=False, alpha=True)

        self.h = TransformerEncoder(
            TransformerEncoderLayer(
                d_model=self.model_dim,
                nhead=self.num_head,
                dim_feedforward=self.model_dim * 4,
                dropout=0.1,
                batch_first=True,
                norm_first=norm_first, ),
            num_layers=self.num_layers,
            norm=LayerNorm(self.model_dim) if norm_first else None, )

        self.ar_predict_layer = nn.Linear(
            self.model_dim, self.vocab_size, bias=False)
        self.loss_fct = nn.CrossEntropyLoss(reduction='sum')

        self.ar_accuracy_metric = MulticlassAccuracy(
            self.vocab_size,
            top_k=top_k,
            average="micro",
            multidim_average="global",
            ignore_index=self.EOS, )

    def forward(self, x, x_lens, y, y_lens, bert_feature):
        '''
        x: phoneme_ids
        y: semantic_ids
        '''
        x = self.ar_text_embedding(x)
        x = x + self.bert_proj(bert_feature.transpose(1,2))
        x = self.ar_text_position(x)
        x_mask = make_pad_mask(x_lens)

        y_mask = make_pad_mask(y_lens)
        y_mask_int = y_mask.type(torch.int64)
        codes = y.type(torch.int64) * (1 - y_mask_int)

        # Training
        # AR Decoder
        y, targets = self.pad_y_eos(codes, y_mask_int, eos_id=self.EOS)
        x_len = x_lens.max()
        y_len = y_lens.max()
        y_emb = self.ar_audio_embedding(y)
        y_pos = self.ar_audio_position(y_emb)

        xy_padding_mask = torch.concat([x_mask, y_mask], dim=1)
        ar_xy_padding_mask = xy_padding_mask

        x_attn_mask = F.pad(
            torch.zeros((x_len, x_len), dtype=torch.bool, device=x.device),
            (0, y_len),
            value=True, )
        y_attn_mask = F.pad(
            torch.triu(
                torch.ones(y_len, y_len, dtype=torch.bool, device=x.device),
                diagonal=1, ),
            (x_len, 0),
            value=False, )
        xy_attn_mask = torch.concat([x_attn_mask, y_attn_mask], dim=0)
        bsz, src_len = x.shape[0], x_len + y_len
        _xy_padding_mask = (ar_xy_padding_mask.view(bsz, 1, 1, src_len)
                            .expand(-1, self.num_head, -1, -1)
                            .reshape(bsz * self.num_head, 1, src_len))
        xy_attn_mask = xy_attn_mask.logical_or(_xy_padding_mask)
        new_attn_mask = torch.zeros_like(xy_attn_mask, dtype=x.dtype)
        new_attn_mask.masked_fill_(xy_attn_mask, float("-inf"))
        xy_attn_mask = new_attn_mask
        # x 和完整的 y 一次性输入模型
        xy_pos = torch.concat([x, y_pos], dim=1)
        xy_dec, _ = self.h(
            (xy_pos, None),
            mask=xy_attn_mask, )
        logits = self.ar_predict_layer(xy_dec[:, x_len:]).permute(0, 2, 1)
        # loss
        # from feiteng: 每次 duration 越多, 梯度更新也应该更多, 所以用 sum
        loss = F.cross_entropy(logits, targets, reduction='sum')
        acc = self.ar_accuracy_metric(logits.detach(), targets).item()
        return loss, acc

    # 需要看下这个函数和 forward 的区别以及没有 semantic 的时候 prompts 输入什么
    def infer(self,
              x,
              x_lens,
              prompts,
              bert_feature,
              top_k: int=-100,
              early_stop_num: int=-1,
              temperature: float=1.0):

        x = self.ar_text_embedding(x)
        x = x + self.bert_proj(bert_feature.transpose(1,2))
        x = self.ar_text_position(x)

        # AR Decoder
        y = prompts
        prefix_len = y.shape[1]
        x_len = x.shape[1]
        x_attn_mask = torch.zeros((x_len, x_len), dtype=torch.bool)
        stop = False
        for _ in tqdm(range(1500)):
            y_emb = self.ar_audio_embedding(y)
            y_pos = self.ar_audio_position(y_emb)
            # x 和逐渐增长的 y 一起输入给模型
            xy_pos = torch.concat([x, y_pos], dim=1)
            y_len = y.shape[1]
            x_attn_mask_pad = F.pad(
                x_attn_mask,
                (0, y_len),
                value=True, )
            y_attn_mask = F.pad(
                torch.triu(
                    torch.ones(y_len, y_len, dtype=torch.bool), diagonal=1),
                (x_len, 0),
                value=False, )
            xy_attn_mask = torch.concat(
                [x_attn_mask_pad, y_attn_mask], dim=0).to(y.device)

            xy_dec, _ = self.h(
                (xy_pos, None),
                mask=xy_attn_mask, )
            logits = self.ar_predict_layer(xy_dec[:, -1])
            samples = topk_sampling(
                logits, top_k=top_k, top_p=1.0, temperature=temperature)

            if early_stop_num != -1 and (y.shape[1] - prefix_len
                                         ) > early_stop_num:
                print("use early stop num:", early_stop_num)
                stop = True

            if torch.argmax(
                    logits, dim=-1)[0] == self.EOS or samples[0, 0] == self.EOS:
                # print(torch.argmax(logits, dim=-1)[0] == self.EOS, samples[0, 0] == self.EOS)
                stop = True
            if stop:
                if prompts.shape[1] == y.shape[1]:
                    y = torch.concat([y, torch.zeros_like(samples)], dim=1)
                    print('bad zero prediction')
                print(f"T2S Decoding EOS [{prefix_len} -> {y.shape[1]}]")
                break
            # 本次生成的 semantic_ids 和之前的 y 构成新的 y
            # print(samples.shape)#[1,1]#第一个1是bs
            # import os
            # os._exit(2333)
            y = torch.concat([y, samples], dim=1)
        return y

    def pad_y_eos(self, y, y_mask_int, eos_id):
        targets = F.pad(
            y, (0, 1), value=0) + eos_id * F.pad(
                y_mask_int, (0, 1), value=1)
        # 错位
        return targets[:, :-1], targets[:, 1:]

    def infer_panel(self,
              x,#####全部文本token
              x_lens,
              prompts,####参考音频token
              bert_feature,
              top_k: int=-100,
              early_stop_num: int=-1,
              temperature: float=1.0):

        x = self.ar_text_embedding(x)
        x = x + self.bert_proj(bert_feature.transpose(1,2))
        x = self.ar_text_position(x)

        # AR Decoder
        y = prompts
        prefix_len = y.shape[1]
        x_len = x.shape[1]
        x_attn_mask = torch.zeros((x_len, x_len), dtype=torch.bool)
        stop = False
        # print(1111111,self.num_layers)
        cache={
            "all_stage":self.num_layers,
            "k":[None]*self.num_layers,###根据配置自己手写
            "v":[None]*self.num_layers,
            # "xy_pos":None,##y_pos位置编码每次都不一样的没法缓存，每次都要重新拼xy_pos.主要还是写法原因，其实是可以历史统一一样的，但也没啥计算量就不管了
            "y_emb":None,##只需要对最新的samples求emb，再拼历史的就行
            # "logits":None,###原版就已经只对结尾求再拼接了，不用管
            # "xy_dec":None,###不需要，本来只需要最后一个做logits
            "first_infer":1,
            "stage":0
        }
        for idx in tqdm(range(1500)):
            if(cache["first_infer"]==1):
                y_emb = self.ar_audio_embedding(y)
            else:
                y_emb = torch.cat([cache["y_emb"],self.ar_audio_embedding(y[:,-1:])],1)
            cache["y_emb"]=y_emb
            y_pos = self.ar_audio_position(y_emb)
            # x 和逐渐增长的 y 一起输入给模型
            if(cache["first_infer"]==1):
                xy_pos = torch.concat([x, y_pos], dim=1)
            else:
                xy_pos=y_pos[:,-1:]
            y_len = y_pos.shape[1]
            ###以下3个不做缓存
            if (cache["first_infer"] == 1):
                x_attn_mask_pad = F.pad(
                        x_attn_mask,
                        (0, y_len),###xx的纯0扩展到xx纯0+xy纯1，(x,x+y)
                        value=True, )
                y_attn_mask = F.pad(###yy的右上1扩展到左边xy的0,(y,x+y)
                    torch.triu(
                        torch.ones(y_len, y_len, dtype=torch.bool), diagonal=1),
                    (x_len, 0),
                    value=False, )
                xy_attn_mask = torch.concat(
                    [x_attn_mask_pad, y_attn_mask], dim=0).to(y.device)
            else:
                ###最右边一列（是错的）
                # xy_attn_mask=torch.ones((1, x_len+y_len), dtype=torch.bool,device=xy_pos.device)
                # xy_attn_mask[:,-1]=False
                ###最下面一行（是对的）
                xy_attn_mask = torch.zeros((1, x_len + y_len), dtype=torch.bool, device=xy_pos.device)
            # pdb.set_trace()
            ###缓存重头戏
            # print(1111,xy_pos.shape,xy_attn_mask.shape,x_len,y_len)
            xy_dec, _ = self.h(
                (xy_pos, None),
                mask=xy_attn_mask,cache=cache )
            logits = self.ar_predict_layer(xy_dec[:, -1])##不用改，如果用了cache的默认就是只有一帧，取最后一帧一样的
            # samples = topk_sampling(logits, top_k=top_k, top_p=1.0, temperature=temperature)
            samples = sample(logits[0], y, top_k=top_k, top_p=1.0, repetition_penalty=1.35)[0].unsqueeze(0)
            if early_stop_num != -1 and (y.shape[1] - prefix_len
                                         ) > early_stop_num:
                print("use early stop num:", early_stop_num)
                stop = True

            if torch.argmax(
                    logits, dim=-1)[0] == self.EOS or samples[0, 0] == self.EOS:
                # print(torch.argmax(logits, dim=-1)[0] == self.EOS, samples[0, 0] == self.EOS)
                stop = True
            if stop:
                if prompts.shape[1] == y.shape[1]:
                    y = torch.concat([y, torch.zeros_like(samples)], dim=1)
                    print('bad zero prediction')
                print(f"T2S Decoding EOS [{prefix_len} -> {y.shape[1]}]")
                break
            # 本次生成的 semantic_ids 和之前的 y 构成新的 y
            # print(samples.shape)#[1,1]#第一个1是bs
            y = torch.concat([y, samples], dim=1)
            cache["first_infer"]=0
        return y,idx
