import numpy as np
import scanpy as sc
import muon as mu
import torch
import scipy
from pathlib import Path
from cfgen.data.utils import normalize_expression, compute_size_factor_lognorm

class RNAseqLoader:
    """Class for RNAseq data loader."""
    def __init__(
        self,
        data,
        layer_key: str,
        covariate_keys=None,
        subsample_frac=1,
        normalization_type="proportions", 
        is_binarized=False):
        """
        Initialize the RNAseqLoader.

        Args:
            data (str or anndata object): AnnData object to load. If this is a str, we assume it is a path. Otherwise assume this is an AnnData/MuData object
            layer_key (str): Layer key.
            covariate_keys (list, optional): List of covariate names. Defaults to None.
            subsample_frac (float, optional): Fraction of the dataset to use. Defaults to 1.
            normalization_type (str, optional): Must be in (proportions, log_gexp, log_gexp_scaled).
            is_binarized (bool): If the multimodal data is binarized.
        """
        # Initialize encoder type
        self.normalization_type = normalization_type  

        self.is_binarized = is_binarized

        self.covariate_keys = covariate_keys

        if type(data) == str or isinstance(data, Path):
            adata_mu = mu.read(str(data))
        else:
            adata_mu = data
    
        if hasattr(adata_mu, "mod"):
            self.modality_list = list(adata_mu.mod.keys())  # "rna" and "atac"
            adata = {}
            for mod in self.modality_list:
                adata[mod] = adata_mu.mod[mod]
            del adata_mu
        else:
            self.modality_list = ["rna"]
            adata = {}
            adata["rna"] = adata_mu
            del adata_mu
            
        # Transform genes to tensors
        for mod in self.modality_list:
            if layer_key not in adata[mod].layers:
                adata[mod].layers[layer_key] = adata[mod].X.copy()
        
        # Transform X into a tensor
        self.X = {}
        for mod in self.modality_list:
            mod_data = adata[mod].layers[layer_key]
            self.X[mod] = torch.Tensor(mod_data.todense() if scipy.sparse.issparse(mod_data) else mod_data)

        # Subsample if required
        if subsample_frac < 1:
            np.random.seed(42)
            n_to_keep = int(subsample_frac*len(self.X["rna"]))
            indices = np.random.choice(range(len(self.X["rna"])), n_to_keep, replace=False)
            for mod in self.modality_list:
                self.X[mod] = self.X[mod][indices]
                adata[mod] = adata[mod][indices]  
                    
        # Covariate to index
        self.id2cov = {}  # cov_name: dict_cov_2_id 
        self.Y_cov = {}   # cov: cov_ids
        adata_obs = adata["rna"].obs
        for cov_name in covariate_keys:
            cov = np.array(adata_obs[cov_name])
            unique_cov = np.unique(cov)
            zip_cov_cat = dict(zip(unique_cov, np.arange(len(unique_cov))))  
            self.id2cov[cov_name] = zip_cov_cat
            self.Y_cov[cov_name] = torch.tensor([zip_cov_cat[c] for c in cov])
        
        # Compute mean, standard deviation, maximum and minimum size factor - dictionary only if non-binarized multimodal 
        if not self.is_binarized:
            self.log_size_factor_mu, self.log_size_factor_sd, self.max_size_factor, self.min_size_factor = {},{},{},{}
            for mod in self.modality_list:
                # Compute size factor for both RNA and Poisson ATAC
                self.log_size_factor_mu[mod], self.log_size_factor_sd[mod] = compute_size_factor_lognorm(adata[mod], layer_key, self.id2cov)
                log_size_factors = torch.log(self.X[mod].sum(1))
                self.max_size_factor[mod], self.min_size_factor[mod] = log_size_factors.max(), log_size_factors.min()
        else:
            self.log_size_factor_mu, self.log_size_factor_sd = compute_size_factor_lognorm(adata["rna"], layer_key, self.id2cov)
            log_size_factors = torch.log(self.X["rna"].sum(1))
            self.max_size_factor, self.min_size_factor = log_size_factors.max(), log_size_factors.min()
                
        del adata
    
    def __getitem__(self, i):
        """
        Get item from the dataset.

        Args:
            i (int): Index.

        Returns:
            dict: Dictionary containing X (gene expression) and y (covariates).
        """
        # Covariate
        y = {cov: self.Y_cov[cov][i] for cov in self.Y_cov}
        # Return sampled cells
        X = {}
        X_norm = {}
        for mod in self.modality_list:
            X[mod] = self.X[mod][i]
            if mod == "rna":
                X_norm[mod] = normalize_expression(X[mod], X[mod].sum(), self.normalization_type)
            elif mod == "atac" and (not self.is_binarized): # Only log-normalization if ATAC not binarized
                X_norm[mod] = normalize_expression(X[mod], X[mod].sum(), self.normalization_type)
            else:
                X_norm[mod] = X[mod]
        return dict(X=X, X_norm=X_norm, y=y)

    def __len__(self):
        """
        Get the length of the dataset.

        Returns:
            int: Length of the dataset.
        """
        return len(self.X["rna"])
