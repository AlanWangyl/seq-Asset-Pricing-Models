import os
import pandas as pd
import numpy as np
import collections
from .modelBase import modelBase
from utils import CHARAS_LIST

from io import open
import unicodedata
import string
import re
import random

import torch
import torch.nn as nn
from torch.autograd import Variable
from torch import optim
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader, TensorDataset

use_cuda = torch.cuda.is_available()

MAX_EPOCH = 200


class seq2seq_base(nn.Module, modelBase):
    def __init__(self, name, omit_char=[], device='cuda'):
        nn.Module.__init__(self)
        modelBase.__init__(self, name)
        self.beta_seq = None
        self.factor_seq = None
        self.optimizer = None
        self.criterion = None
        self.omit_char = omit_char

        self.factor_seq_pred = []

        self.device = device

        self.datashare_chara = pd.read_pickle('./data/datashare_re.pkl').astype(np.float64)
        self.p_charas = pd.read_pickle('./data/p_charas.pkl').astype(np.float64).reset_index()
        self.portfolio_ret = pd.read_pickle('./data/portfolio_ret.pkl').astype(np.float64)
        self.mon_ret = pd.read_pickle('./data/month_ret.pkl').astype(np.float64)

        self.train_dataloader = None
        self.valid_dataloader = None
        self.test_dataloader = None

    def debug(self, month):
        beta_seq_input = self.p_charas.loc[self.p_charas['DATE'] == month][CHARAS_LIST]
        # beta_seq_input = self.datashare_chara.loc[self.datashare_chara['DATE'] == month].set_index('permno')[charas]
        print(beta_seq_input)

    def _get_item(self, month):
        if month not in self.p_charas['DATE'].values:
            # find the closest month in p_charas to month
            month = self.p_charas['DATE'].values[np.argmin(np.abs(self.p_charas['DATE'].values - month))]

        beta_seq_input = self.p_charas.loc[self.p_charas['DATE'] == month][CHARAS_LIST]  # (94, 94)
        labels = self.portfolio_ret.loc[self.portfolio_ret['DATE'] == month][CHARAS_LIST].T.values  # (94, 1)
        beta_seq_input['ret-rf'] = labels
        align_df = beta_seq_input.copy(deep=False).dropna()

        factor_seq_input = self.portfolio_ret.loc[self.portfolio_ret['DATE'] == month][CHARAS_LIST]

        # exit(0) if there is any nan in align_df
        if align_df.isnull().values.any():
            assert False, f'There is nan in align_df of : {month}'
        # return stock index (L), beta_seq_input (94*94=P*N), factor_seq_input (94*1=P*1), labels (94, = N,)
        return align_df.index, align_df.values[:, :-1].T, factor_seq_input.T.values, align_df.values[:, -1].T

    def dataloader(self, period):
        mon_list = pd.read_pickle('data/mon_list.pkl')
        mon_list = mon_list.loc[(mon_list >= period[0]) & (mon_list <= period[1])]
        beta_seq_input_set = []
        factor_seq_input_set = []
        label_set = []
        for mon in mon_list:
            _, _beta_input, _factor_input, label = self._get_item(mon)
            beta_seq_input_set.append(_beta_input)
            factor_seq_input_set.append(_factor_input)
            label_set.append(label)

        beta_seq_input_set = torch.tensor(beta_seq_input_set, dtype=torch.float32).to(self.device)
        factor_seq_input_set = torch.tensor(factor_seq_input_set, dtype=torch.float32).to(self.device)
        label_set = torch.tensor(label_set, dtype=torch.float32).to(self.device)

        dataset = TensorDataset(beta_seq_input_set, factor_seq_input_set, label_set)
        return DataLoader(dataset, batch_size=1, shuffle=True)

    def forward(self, char, pfret):
        processed_char = self.beta_seq(char)

        encoder_hidden = self.encoder_factor_seq.initHidden()
        input_length = pfret.size(0)
        encoder_outputs = torch.zeros(input_length, self.hidden_size, device=self.device)

        for ei in range(input_length):
            encoder_output, encoder_hidden = self.encoder_factor_seq(pfret[ei:ei+1].long(), encoder_hidden)
            encoder_outputs[ei] = encoder_output[0, 0]

        # Factor Seq: Decoding
        decoder_input = torch.tensor([[0]], device=self.device)  # Replace 0 with whatever start token you use
        decoder_hidden = encoder_hidden
        decoded_sequence = torch.zeros(input_length, 94, device=self.device)  # Assuming output size is 94

        for di in range(input_length):
            decoder_output, decoder_hidden = self.decoder_factor_seq(decoder_input, decoder_hidden)
            decoded_sequence[di] = decoder_output
            decoder_input = decoder_output.argmax(1)

        # Now, 'decoded_sequence' can be used as 'factor_seq'
        processed_pfret = decoded_sequence
        return torch.sum(processed_char * processed_pfret, dim=1)

    # train_one_epoch
    def __train_one_epoch(self):
        epoch_loss = 0.0
        for i, (beta_seq_input, factor_seq_input, labels) in enumerate(self.train_dataloader):
            self.optimizer.zero_grad()
            # beta_seq_input reshape: (1, 94, 94) -> (94, 94) (1*P*N => N*P)
            # factor_seq_input reshape: (1, 94, 1) -> (1, 94) (1*P*1 => 1*P)
            # labels reshape: (1, 94) -> (94, ) (1*N => N,)
            beta_seq_input = beta_seq_input.squeeze(0).T
            factor_seq_input = factor_seq_input.squeeze(0).T
            labels = labels.squeeze(0)
            output = self.forward(beta_seq_input, factor_seq_input)
            loss = self.criterion(output, labels)

            loss.backward()
            self.optimizer.step()
            epoch_loss += loss.item()

            if i % 100 == 0:
                # print(f'Batches: {i}, loss: {loss.item()}')
                pass

        return epoch_loss / len(self.train_dataloader)

    def __valid_one_epoch(self):
        epoch_loss = 0.0
        for i, (beta_seq_input, factor_seq_input, labels) in enumerate(self.valid_dataloader):
            # beta_seq_input reshape: (1, 94, 94) -> (94, 94) (1*P*N => N*P)
            # factor_seq_input reshape: (1, 94, 1) -> (1, 94) (1*P*1 => 1*P)
            # labels reshape: (1, 94) -> (94, ) (1*N => N,)
            beta_seq_input = beta_seq_input.squeeze(0).T
            factor_seq_input = factor_seq_input.squeeze(0).T
            labels = labels.squeeze(0)

            output = self.forward(beta_seq_input, factor_seq_input)
            loss = self.criterion(output, labels)
            epoch_loss += loss.item()

        return epoch_loss / len(self.valid_dataloader)

    def train_model(self):
        if 'saved_models' not in os.listdir('./'):
            os.mkdir('saved_models')

        self.train_dataloader = self.dataloader(self.train_period)
        self.valid_dataloader = self.dataloader(self.valid_period)
        self.test_dataloader = self.dataloader(self.test_period)

        min_error = np.Inf
        no_update_steps = 0
        valid_loss = []
        train_loss = []
        for i in range(MAX_EPOCH):
            # print(f'Epoch {i}')
            self.train()
            train_error = self.__train_one_epoch()
            train_loss.append(train_error)

            self.eval()
            # valid and early stop
            with torch.no_grad():
                valid_error = self.__valid_one_epoch()

            valid_loss.append(valid_error)
            if valid_error < min_error:
                min_error = valid_error
                no_update_steps = 0
                # save model
                torch.save(self.state_dict(), f'./saved_models/{self.name}.pt')
            else:
                no_update_steps += 1

            if no_update_steps > 2:  # early stop, if consecutive 3 epoches no improvement on validation set
                print(f'Early stop at epoch {i}')
                break
            # load from (best) saved model
            self.load_state_dict(torch.load(f'./saved_models/{self.name}.pt'))
        return train_loss, valid_loss

    def test_model(self):
        # beta, factor, label = self.test_dataset
        # i = np.random.randint(len(beta))
        # beta_seq_input = beta[i]
        # factor_seq_input = factor[i]
        # labels = label[i]
        output = None
        label = None
        for i, beta_seq_input, factor_seq_input, labels in enumerate(self.test_dataloader):
            # convert to tensor
            # beta_seq_input = torch.tensor(beta_seq_input, dtype=torch.float32).T.to(self.device)
            # factor_seq_input = torch.tensor(factor_seq_input, dtype=torch.float32).T.to(self.device)
            # labels = torch.tensor(labels, dtype=torch.float32).T.to(self.device)
            output = self.forward(beta_seq_input, factor_seq_input)
            break

        loss = self.criterion(output, labels)
        print(f'Test loss: {loss.item()}')
        print(f'Predicted: {output}')
        print(f'Ground truth: {labels}')
        return output, labels

    def calBeta(self, month, skip_char=[]):
        _, beta_seq_input, _, _ = self._get_item(month)  # beta input: 94*94 = P*N

        # if some variables need be omitted
        if len(skip_char):
            beta_seq_input = pd.DataFrame(beta_seq_input.T, columns=CHARAS_LIST)  # N*P
            beta_seq_input[skip_char] = beta_seq_input[skip_char] * 0.0
            beta_seq_input = beta_seq_input.values.T  # P*N

        beta_seq_input = torch.tensor(beta_seq_input, dtype=torch.float32).T.to(self.device)  # N*P
        return self.beta_seq(beta_seq_input)  # N*K

    def calFactor(self, month, skip_char=[]):
        _, _, factor_seq_input, _ = self._get_item(month)  # factor input: P*1(94*1)

        # if some variables need be omitted
        if len(skip_char):
            factor_seq_input = pd.DataFrame(factor_seq_input.T, columns=CHARAS_LIST)  # 1*P
            factor_seq_input[skip_char] = factor_seq_input[skip_char] * 0.0
            factor_seq_input = factor_seq_input.values.T  # P*1

        factor_seq_input = torch.tensor(factor_seq_input, dtype=torch.float32).T.to(self.device)  # 1*P
        factor_pred = self.factor_seq(factor_seq_input).T  # K*1

        self.factor_seq_pred.append(factor_pred)

        return factor_pred  # K*1

    def inference(self, month):
        if len(self.omit_char) == 0:
            assert month >= self.test_period[0], f"Month error, {month} is not in test period {self.test_period}"

            mon_factor, mon_beta = self.calFactor(month), self.calBeta(month)

            assert mon_beta.shape[1] == mon_factor.shape[
                0], f"Dimension mismatch between mon_factor: {mon_factor.shape} and mon_beta: {mon_beta.shape}"

            # R_{N*1} = Beta_{N*K} @ F_{K*1}
            return mon_beta @ mon_factor
        else:
            ret_R = []
            for char in self.omit_char:
                mon_factor, mon_beta = self.calFactor(month, [char]), self.calBeta(month, [char])
                ret_R.append((mon_beta @ mon_factor).cpu().detach().numpy())  # N*1

            mon_factor, mon_beta = self.calFactor(month), self.calBeta(month)
            ret_R.append((mon_beta @ mon_factor).cpu().detach().numpy())  # also add complete result

            return np.array(ret_R).squeeze(2).T  # N*m

    def cal_delayed_Factor(self, month):
        # calculate the last day of the previous month
        if self.refit_cnt == 0:
            avg_f_pred = self.factor_seq_pred[0]  # input of the first predict take hat{f}_t
            # print(avg_f_pred.shape)
        else:
            avg_f_pred = torch.mean(torch.stack(self.factor_seq_pred[:self.refit_cnt]), dim=0)

        return avg_f_pred

    def reset_weight(self):
        for layer in self.beta_seq:  # reset beta_nn parameters
            if hasattr(layer, 'reset_parameters'):
                layer.reset_parameters()

        for layer in self.factor_seq:  # reset factor_nn parameters
            if hasattr(layer, 'reset_parameters'):
                layer.reset_parameters()

        self.optimizer.state = collections.defaultdict(dict)  # reset optimizer state

    def release_gpu(self):
        if self.train_dataloader is not None:
            del self.train_dataloader
        if self.valid_dataloader is not None:
            del self.valid_dataloader
        if self.test_dataloader is not None:
            del self.test_dataloader
        torch.cuda.empty_cache()


class encoder(nn.Module):
    def __init__(self, input_size, hidden_size=128,n_layers=1):
        super(encoder, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.n_layers = n_layers

        self.gru = nn.GRU(hidden_size, hidden_size,n_layers)

    def forward(self, x):
        output,hidden = self.gru(x)
        return output, hidden

class decoder(nn.Module):
    def __init__(self, output_size, hidden_size = 128, n_layers = 1):
        super(decoder, self).__init__()
        self.output_size = output_size
        self.hidden_size = hidden_size
        self.n_layers = n_layers

        self.gru = nn.GRU(hidden_size, hidden_size, n_layers)
        self.out = nn.Linear(hidden_size, output_size)

    def forward(self, x, hidden):
        x = x.unsqueeze(0)
        output, hidden = self.gru(x, hidden)
        fac_pred = self.out(output)

        return fac_pred, hidden


# Combine Encoder and Decoder to create a seq2seq model to predict returns
class seq_model(nn.Module):
    def __init__(self, encoder, decoder, device):
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.device = device

    def forward(self, x, y, teacher_forcing_ratio=0.5):
        x = x.permute(1, 0, 2)  # dataloader is [batch, seq,dim]
        y = y.permute(1, 0, 2)  # output back
        """
        x = [input_seq_len, batch_size, feature_size]
        y = [target_seq_len, batch_size, feature_size]
        """
        batch_size = x.shape[1]
        target_len = y.shape[0]

        # tensor to store decoder outputs of each time step
        outputs = torch.zeros(y.shape).to(self.device)

        hidden = self.encoder(x)
        decoder_input = x[-1, :, :]  # first input to decoder is last of x

        for i in range(target_len):
            output, hidden = self.decoder(decoder_input, hidden)
            # place predictions in a tensor holding predictions for each time step
            outputs[i] = torch.squeeze(output, 0)

            teacher_forcing = random.random() < teacher_forcing_ratio
            # output is the same shape as decorder input-->[batch_size, feature_size]
            # so we use output directly as input or use true lable depending on teacher_forcing flag
            decoder_input = y[i] if teacher_forcing else torch.squeeze(output, 0)

            final_fac_output = outputs.permute(1, 0, 2)

        return final_fac_output




class seq2seq0(seq2seq_base):
    def __init__(self, hidden_size, lr=0.001, omit_char=[], device='cuda'):
        seq2seq_base.__init__(self, name=f'seq2seq0_{hidden_size}', omit_char=omit_char, device=device)
        self.hidden_size = hidden_size
        # P -> K
        self.beta_seq = nn.Sequential(
            # output layer
            nn.Linear(94, hidden_size)
        )
        self.factor_seq = final_output(# 此处想带入decoder产生的最后结果final_fac_output)

        self.optimizer = torch.optim.Adam(self.parameters(), lr=lr)
        self.criterion = nn.MSELoss().to(device)


class seq2seq1(seq2seq_base):
    def __init__(self, hidden_size, dropout=0.5, lr=0.001, omit_char=[], device='cuda'):
        seq2seq_base.__init__(self, name=f'seq2seq1_{hidden_size}', omit_char=omit_char, device=device)
        self.hidden_size = hidden_size
        self.dropout = dropout
        # P -> 32 -> K
        self.beta_seq = nn.Sequential(
            # hidden layer 1
            nn.Linear(94, 32),
            nn.BatchNorm1d(32),
            nn.ReLU(),
            nn.Dropout(self.dropout),
            # output layer
            nn.Linear(32, hidden_size)
        )
        self.factor_seq =

        self.optimizer = torch.optim.Adam(self.parameters(), lr=lr)
        self.criterion = nn.MSELoss().to(device)




class seq2seq2(seq2seq_base):
    def __init__(self, hidden_size, dropout=0.5, lr=0.001, omit_char=[], device='cuda'):
        seq2seq_base.__init__(self, name=f'seq2seq2_{hidden_size}', omit_char=omit_char, device=device)
        self.hidden_size = hidden_size
        self.dropout = dropout
        # P -> 32 -> 16 -> K
        self.beta_nn = nn.Sequential(
            # hidden layer 1
            nn.Linear(94, 32),
            nn.BatchNorm1d(32),
            nn.ReLU(),
            nn.Dropout(self.dropout),
            # hidden layer 2
            nn.Linear(32, 16),
            nn.BatchNorm1d(16),
            nn.ReLU(),
            nn.Dropout(self.dropout),
            # output layer
            nn.Linear(16, hidden_size)
        )
        self.factor_seq =

        self.optimizer = torch.optim.Adam(self.parameters(), lr=lr)
        self.criterion = nn.MSELoss().to(device)


class seq2seq3(seq2seq_base):
    def __init__(self, hidden_size, dropout=0.5, lr=0.001, omit_char=[], device='cuda'):
        seq2seq_base.__init__(self, name=f'seq2seq3_{hidden_size}', omit_char=omit_char, device=device)
        self.hidden_size = hidden_size
        self.dropout = dropout
        # P -> 32 -> 16 -> 8 -> K
        self.beta_seq = nn.Sequential(
            # hidden layer 1
            nn.Linear(94, 32),
            nn.BatchNorm1d(32),
            nn.ReLU(),
            nn.Dropout(self.dropout),
            # hidden layer 2
            nn.Linear(32, 16),
            nn.BatchNorm1d(16),
            nn.ReLU(),
            nn.Dropout(self.dropout),
            # hidden layer 3
            nn.Linear(16, 8),
            nn.BatchNorm1d(8),
            nn.ReLU(),
            nn.Dropout(self.dropout),
            # output layer
            nn.Linear(8, hidden_size)
        )
        self.factor_seq =

        self.optimizer = torch.optim.Adam(self.parameters(), lr=lr, weight_decay=0.01)
        self.criterion = nn.MSELoss().to(device)
