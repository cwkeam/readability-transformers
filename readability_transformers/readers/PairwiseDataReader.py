import os
import pickle
import random
from math import fabs, erf, sqrt, log
import pandas as pd
from torch.utils.data import DataLoader, Dataset
from loguru import logger
from sentence_transformers import InputExample

from readability_transformers.readers import DataReader

CACHE_DIR = os.path.expanduser("~/.cache/readability-transformers/data")


class NormalDist:
    def __init__(self, mu=0.0, sigma=1.0):
        self._mu = float(mu)
        self._sigma = float(sigma)

    def cdf(self, x):
        return 0.5 * (1.0 + erf((x - self._mu) / (self._sigma * sqrt(2.0))))

    def overlap(self, other):
        """REFERNCE: https://www.rasch.org/rmt/rmt101r.htm, http://dx.doi.org/10.1080/03610928908830127
        N1 = NormalDist(2.4, 1.6)
        N2 = NormalDist(3.2, 2.0)
        N1.overlap(N2) = 0.8035050657330205
        """
        X, Y = self, other
        if (Y._sigma, Y._mu) < (X._sigma, X._mu):   # commutativity
            X, Y = Y, X
        X_var, Y_var = X.variance, Y.variance

        dv = Y_var - X_var
        dm = fabs(Y._mu - X._mu)
        if not dv:
            return 1.0 - erf(dm / (2.0 * X._sigma * sqrt(2.0)))
        a = X._mu * Y_var - Y._mu * X_var
        b = X._sigma * Y._sigma * sqrt(dm**2.0 + dv * log(Y_var / X_var))
        x1 = (a + b) / dv
        x2 = (a - b) / dv
        return 1.0 - (fabs(Y.cdf(x1) - X.cdf(x1)) + fabs(Y.cdf(x2) - X.cdf(x2)))
    @property
    def variance(self):
        "Square of the standard deviation."
        return self._sigma ** 2.0

class PairwiseDataReader(DataReader):
    def __init__(self, df: pd.DataFrame, cache_name: str = None, sample_k: int=None, width_scale: int=None):
        """Creates a general purpose pairwise datareader. Initialization will process the raw dataset
        to produce the pairwise dataset, which will be retrievable through get_dataset() or get_dataloader().

        Args:
            df (pd.DataFrame): DF object with columns [excerpt, target, standard_error]
            cache_name (str): If cache_name is set, the reader will pickle the dataset for immediate retrieval next time it's called with the same name. 
            sample_k (int): Pairing expands the dataset by n^2. This might be too much for the model to handle. it may be 
                preferable to only use k number of pairs per each text. i.e. instead of N*N rows, have N*sample_k rows.
                sample_k < len(df) for obvious reasons. if sample_k=None, then just processes it N x N.
            
        """
        self.df = self._remove_zeros(df)
        self.cache_name = cache_name
        self.sample_k = sample_k
        self.width_scale = width_scale

        if sample_k and sample_k >= len(df):
            raise Exception("sample_k must be less than len(df).")
        """
        Load/Generate Pairwise Dataset
        """  
        if self.cache_name:
            cache_path = os.path.join(CACHE_DIR, f"paired_{cache_name}_sample_k_{sample_k}.pkl")
            pre_loaded = os.path.isfile(cache_path)
            if pre_loaded:
                try:
                    f = open(cache_path, "rb")
                    self.paired_dataset = pickle.load(f)
                    f.close()
                except: 
                    logger.error(f"Failed to load pre-cached PAIRED dataset '{cache_name}'. Deleting current dumps and building from scratch...")
                    os.remove(cache_path)
                    pre_loaded = False
            
            if not pre_loaded:
                logger.info(f"Cached PAIRED dataset not found with name '{cache_name}'.\nBuilding from scratch...")
                self.paired_dataset = self._build_dataset(self.df)
                with open(cache_path, "wb") as f:
                    pickle.dump(self.paired_dataset, f)
        else:
            logger.info(f"No PAIRED cache supplied.\nBuilding PAIRED dataset from scratch...")
            self.paired_dataset = self._build_dataset(self.df)

    def get_dataset(self):
        return self.paired_dataset

    def get_dataloader(self, batch_size: int, shuffle: bool = True, drop_last=False):
        dataloader = DataLoader(
            self.paired_dataset,
            batch_size=batch_size,
            shuffle=shuffle,
            drop_last=drop_last
        )
        return dataloader
        

    def _build_dataset(self, df):
        first_row = df
        second_row = df
        if self.sample_k:
            second_row = df.sample(n=self.sample_k)
        
        paired = []
        for idx1, row1 in first_row.iterrows():
            for idx2, row2 in second_row.iterrows():
                if idx1 != idx2:
                    passage1 = row1["excerpt"]
                    passage2 = row2["excerpt"]
                    m1 = row1["target"]
                    m2 = row2["target"]
                    s1 = row1["standard_error"]
                    s2 = row2["standard_error"]

                    assert 0.0 not in [m1, m2, s1, s2]

                    if self.width_scale is not None:
                        s1 = self.width_scale * s1
                        s2 = self.width_scale * s2

                    sim_score = self._overlap_integral(m1, m2, s1, s2)
                    assert sim_score == self._overlap_integral(m2, m1, s2, s1) # commutativity
                    one = InputExample(texts=[passage1, passage2], label=sim_score)
                    paired.append(one)
        return paired      

    def _overlap_integral(self,m1, m2, s1, s2):
        overlap_area = NormalDist(mu=m1, sigma=s1).overlap(NormalDist(mu=m2, sigma=s2))
        return overlap_area  

    def _remove_zeros(self, df):
        filtered = []
        for idx, row in df.iterrows():
            m = row["target"]
            s = row["standard_error"]
            if 0.0 in [m, s]:
                continue
            else:
                filtered.append(row)
        return pd.DataFrame(filtered)
    
    def __len__(self):
        return len(self.paired_dataset)
