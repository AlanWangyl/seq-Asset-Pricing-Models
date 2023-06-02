import sys
sys.path.append('../')

from utils import charas
from .modelBase import modelBase

import pandas as pd
import statsmodels.api as sm
from dateutil.relativedelta import relativedelta


class FF(modelBase):
    def __init__(self, K, portfolio=True):
        super(FF, self).__init__(f'FF_{K}')
        self.K = K
        self.portfolio = portfolio
        self.train_period[0] = 19630731 # ff5 data from FF website is only available from 196307
        self.__prepare_FFf()
        
    
    def __prepare_FFf(self):
        ff5 = pd.read_csv('data/ff5.csv', index_col=0)
        UMD = pd.read_csv('data/UMD.csv', index_col=0)
        UMD.columns = ['UMD']
        FFf = pd.concat([ff5, UMD.loc[196307:]], axis=1)
        self.fname = ['Mkt-RF', 'SMB', 'HML', 'CMA', 'RMW', 'UMD']
        self.FFf = FFf[self.fname]
        self.portfolio_ret = pd.read_pickle('data/portfolio_ret.pkl')
        self.portfolio_ret['DATE'] = self.portfolio_ret['DATE'].apply(lambda x: x//100)
        
    
    def train_model(self):
        self.beta_matrix = []
        X = self.FFf[self.fname[:self.K]].loc[self.train_period[0]//100:self.train_period[1]//100]
        for col in charas:
            y = self.portfolio_ret.set_index('DATE')[col].loc[self.train_period[0]//100:self.train_period[1]//100]
            model = sm.OLS(y.values, X.values).fit()
            self.beta_matrix.append(model.params)
        self.beta_matrix = pd.DataFrame(self.beta_matrix, columns=self.fname[:self.K], index=charas)
    
        
    def calBeta(self, month):
        return self.beta_matrix # N * K
        
            
    def calFactor(self, month):
        return self.FFf[self.fname[:self.K]].loc[month//100] # K * 1
        
        
    def cal_delayed_Factor(self, month):
        last_mon = int(str(pd.to_datetime(str(month)) - relativedelta(months=1)).split(' ')[0].replace('-', '')[:-2])
        return self.FFf[self.fname[:self.K]].loc[self.valid_period[0]//100:last_mon].mean()
        